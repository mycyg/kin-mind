"""Prepared context is not a delivery. Only a host-verified native item counts."""
import hashlib
import json

from eventmem.core.db import Conflict, Missing, digest, dumps
from eventmem.core.retrieval import tokens

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_context_deliveries(
 scope TEXT NOT NULL,session TEXT NOT NULL,epoch TEXT NOT NULL,id TEXT NOT NULL,
 state TEXT NOT NULL,data TEXT NOT NULL,at TEXT NOT NULL,
 PRIMARY KEY(scope,session,epoch,id));
CREATE INDEX IF NOT EXISTS mind_context_delivery_pending ON mind_context_deliveries(scope,session,state);
"""


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


class ContextDelivery:
    def __init__(self, contexts):
        self.ctx = contexts
        self.db = contexts.engine.db
        self.scope = contexts.mind.scope.key()
        with self.db.connect() as conn:
            conn.executescript(SCHEMA)

    def _policy(self):
        from eventmem.core.read_policy import ReadPolicy

        return ReadPolicy.load(self.ctx.engine, self.ctx.mind.scope, 'experience_recall')

    def _get(self, conn, session, epoch, identifier):
        row = conn.execute('SELECT data FROM mind_context_deliveries WHERE scope=? AND session=? AND epoch=? AND id=?',
                           (self.scope, session, epoch, identifier)).fetchone()
        if not row:
            raise Missing(identifier)
        return json.loads(row[0])

    def _put(self, conn, value):
        conn.execute('INSERT OR REPLACE INTO mind_context_deliveries VALUES(?,?,?,?,?,?,?)',
                     (self.scope, value['session'], value['epoch'], value['id'], value['state'], dumps(value), self.ctx.mind.clock()))

    def prepare(self, session, epoch, event_id, text, items, *, budget=4000, overhead=0, kind='background', manifest_id=None):
        if not session or not event_id or not text or not 0 <= overhead <= 500:
            raise ValueError('Context preparation needs identity and public text')
        identifier = 'context:' + digest([self.scope, session, epoch, event_id, text, kind])
        marker = 'kin-context:' + identifier
        body = marker + '\n' + text
        count = tokens(body) + overhead
        if count > budget:
            return {'state': 'incomplete', 'reason': 'context-envelope-budget', 'tokens': count}
        value = {'id': identifier, 'session': session, 'epoch': epoch, 'event_id': event_id,
                 'state': 'prepared', 'kind': kind, 'marker': marker, 'text': body, 'text_hash': text_hash(body),
                 'tokens': count, 'content_tokens': tokens(body), 'overhead': overhead, 'items': items,
                 'manifest_id': manifest_id, 'prepared_at': self.ctx.mind.clock()}
        with self.db.connect(write=True) as conn:
            if self.ctx.window(session, conn)['epoch'] != epoch:
                raise Conflict('Context window changed during preparation')
            try:
                return self.view(self._get(conn, session, epoch, identifier))
            except Missing:
                self._put(conn, value)
        return self.view(value)

    @staticmethod
    def view(value):
        return {k: v for k, v in value.items() if k != 'items'}

    def begin(self, session, epoch, id):
        with self.db.connect(write=True) as conn:
            value = self._get(conn, session, epoch, id)
            if value['state'] != 'prepared':
                return self.view(value)
            window, policy = self.ctx.window(session, conn), self._policy()
            # A prepared injection is text about to reach the window: it is checked against
            # the same read it was built for, so a stale one goes stale instead of arriving.
            if window['epoch'] != epoch or not all(self.ctx._current(i, policy) for i in value['items']):
                value['state'] = 'stale'
                self._put(conn, value)
                return self.view(value)
            held = conn.execute("SELECT data FROM mind_context_deliveries WHERE scope=? AND session=? AND epoch=? AND state IN ('sending','unconfirmed')", (self.scope, session, epoch)).fetchall()
            if held:
                return {'state': 'waiting', 'reason': 'previous-context-unconfirmed'}
            if window['used'] + value['tokens'] > 12000:
                return {'state': 'waiting', 'reason': 'automatic-background-budget'}
            value['state'] = 'sending'
            self._put(conn, value)
            return self.view(value)

    def uncertain(self, session, epoch, id):
        with self.db.connect(write=True) as conn:
            value = self._get(conn, session, epoch, id)
            if value['state'] == 'sending':
                value['state'] = 'unconfirmed'
                self._put(conn, value)
            return self.view(value)

    def pending(self, session):
        with self.db.connect() as conn:
            rows = conn.execute("SELECT data FROM mind_context_deliveries WHERE scope=? AND session=? AND state IN ('sending','unconfirmed') ORDER BY at LIMIT 16", (self.scope, session)).fetchall()
        return [self.view(json.loads(row[0])) for row in rows]

    def acknowledge(self, session, epoch, id, *, actual_session, marker, text_hash, verified=False, native_at=None):
        with self.db.connect(write=True) as conn:
            value = self._get(conn, session, epoch, id)
            if not verified or actual_session != session or marker != value['marker'] or text_hash != value['text_hash']:
                raise Conflict('A matching persisted native message is required')
            if value['state'] == 'accepted':
                return self.view(value)
            # Sources can change after a real injection. Count the bytes that did
            # arrive, but only current evidence participates in future de-dup.
            policy = self._policy()
            current = [i for i in value['items'] if self.ctx._current(i, policy)]
            window = self.ctx.window(session, conn)
            historical = window['epoch'] != epoch
            if not historical:
                if window['used'] + value['tokens'] > 12000:
                    raise Conflict('Native context receipt exceeds reserved budget')
                window['used'] += value['tokens']
                for item in current:
                    window['seen'][item['id']] = item['revision']
                window['receipts'][id] = {'state': 'recorded', 'id': id, 'tokens': value['tokens'], 'epoch': epoch}
                self.ctx._save_window(conn, session, window)
            value.update(state='accepted', accepted_at=self.ctx.mind.clock(), native_at=native_at,
                         historical=historical, needs_review=len(current) != len(value['items']),
                         evidence={'kind': 'native-message-hash', 'marker': marker, 'text_hash': text_hash})
            self._put(conn, value)
            # Access records and receipt are committed together. An index isn't
            # upgraded to original just because it was selected or summarized.
            for item in current:
                conn.execute('INSERT OR IGNORE INTO mind_memory_access VALUES(?,?,?,?,?,?)',
                             (self.scope, session, item['id'], item['revision'], item.get('depth', 'summary'), self.ctx.mind.clock()))
            return self.view(value)

    def metrics(self, session):
        with self.db.connect() as conn:
            rows = conn.execute('SELECT state,COUNT(*) AS n FROM mind_context_deliveries WHERE scope=? AND session=? GROUP BY state', (self.scope, session)).fetchall()
        return {'states': {r['state']: r['n'] for r in rows}, 'window': self.ctx.window(session)['used'],
                'model_requests': 0, 'receipt_basis': 'native-message-hash'}
