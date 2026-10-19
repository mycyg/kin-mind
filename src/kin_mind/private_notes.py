"""Per-reply role notes. Not sources, facts, shared diary entries, or appraisal jobs."""
from __future__ import annotations

import json
import time
from datetime import timedelta

from eventmem.core.db import Conflict, dumps
from .state import ensure_schema, timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_private_notes(
 scope TEXT NOT NULL,id TEXT NOT NULL,at TEXT NOT NULL,input_id TEXT,group_id TEXT NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,id));
CREATE INDEX IF NOT EXISTS mind_private_notes_time ON mind_private_notes(scope,at);
CREATE INDEX IF NOT EXISTS mind_private_notes_input ON mind_private_notes(scope,input_id);
CREATE INDEX IF NOT EXISTS mind_private_notes_group ON mind_private_notes(scope,group_id);
"""
PROMPT = ('这是你自己写的私密心情札记，属于当时的角色感受和猜测，不是小光的事实或新消息。'
          '只用于续接心情；不要主动复述。小光问当时怎么想时，可按时间或消息用 read_private_notes 读取并自然回答。'
          '小光的最新说明和纠正优先；旧猜测不自动代表现在。')
MAX_NOTES = 20000
MAX_TEXT = 4000


class PrivateNotes:
    def __init__(self, mind):
        self.mind, self.engine, self.scope = mind, mind.engine, mind.scope.key()

    def save(self, request):
        ensure_schema(self.engine, 'private-notes-v1', SCHEMA)
        identifier, group = request.get('id'), request.get('group_id')
        text = request.get('text')
        if not isinstance(identifier, str) or not identifier or not isinstance(group, str) or not group:
            raise ValueError('private-note-identity-required')
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError('private-note-text-invalid')
        at = timestamp(request.get('at') or self.mind.clock()).isoformat()
        input_id = request.get('input_id')
        data = {k: request[k] for k in ('turn_id', 'model', 'provider', 'session', 'reply_text', 'corrects') if k in request}
        if isinstance(data.get('reply_text'), str):
            data['reply_text'] = data['reply_text'][:2000]
        data.update(text=text.strip(), basis='role_note', visibility='kin_only', evidence_ids=[])
        with self.engine.db.connect(write=True) as conn:
            old = conn.execute('SELECT data FROM mind_private_notes WHERE scope=? AND id=?', (self.scope, identifier)).fetchone()
            if old:
                if json.loads(old['data']).get('text') != data['text']:
                    raise Conflict('Private note identity already used', code='private-note-conflict')
                return {'state': 'saved', 'id': identifier}
            if input_id:
                rows = conn.execute('SELECT id,deleted FROM sources WHERE scope=? AND source_key=?', (self.scope, input_id)).fetchall()
                if any(r['deleted'] for r in rows):
                    raise Conflict('Private note input was erased', code='private-note-input-erased')
                data['evidence_ids'] = [r['id'] for r in rows]
            conn.execute('INSERT INTO mind_private_notes VALUES(?,?,?,?,?,?)', (self.scope, identifier, at, input_id, group, dumps(data)))
            # Bound disk use as well as prompt use. No automatic model summaries or fact promotion.
            conn.execute('DELETE FROM mind_private_notes WHERE scope=? AND id IN '
                         '(SELECT id FROM mind_private_notes WHERE scope=? ORDER BY at DESC,id DESC LIMIT -1 OFFSET ?)',
                         (self.scope, self.scope, MAX_NOTES))
        return {'state': 'saved', 'id': identifier}

    def read(self, *, query='', identifier=None, input_id=None, group_id=None, since=None, until=None, cursor=0, limit=5, semantic=True):
        if not 1 <= limit <= 20 or cursor < 0:
            raise ValueError('private-note-page-invalid')
        where, args = ['scope=?'], [self.scope]
        for col, value in [('id', identifier), ('input_id', input_id), ('group_id', group_id)]:
            if value:
                where.append(col+'=?'); args.append(value)
        for op, value in [('>=', since), ('<=', until)]:
            if value:
                where.append('julianday(at)'+op+'julianday(?)'); args.append(timestamp(value).isoformat())
        with self.engine.db.connect() as conn:
            # Queries stay local. A bounded semantic rerank is optional and never a paid chat call.
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_private_notes'").fetchone()
            rows = conn.execute('SELECT * FROM mind_private_notes WHERE '+' AND '.join(where)+' ORDER BY at DESC,id DESC', args).fetchall() if exists else []
        entries = [{**json.loads(r['data']), 'id': r['id'], 'at': r['at'], 'input_id': r['input_id'], 'group_id': r['group_id']} for r in rows]
        state = 'not-needed'
        if query:
            from .memory import tokenize
            words = set(tokenize(query).split())
            def lexical(e):
                body = e['text']+' '+str(e.get('reply_text', ''))
                return len(words & set(tokenize(body).split())) + (4 if query in body else 0)
            ordered = sorted(entries, key=lexical, reverse=True)
            pool = ordered[:48]
            # Recent candidates complement lexical matches for paraphrases without overlapping words.
            ids = {e['id'] for e in pool}
            pool += [e for e in entries[:16] if e['id'] not in ids]
            if semantic and pool:
                from .recall_admission import embed, _cosine, Unavailable
                try:
                    vectors, _ = embed(self.engine, self.scope, [('query', query)]+[(e['id'], e['text']+' '+str(e.get('reply_text', ''))) for e in pool], time.monotonic()+3)
                    for e, vector in zip(pool, vectors[1:]): e['relevance'] = round(_cosine(vectors[0], vector), 4)
                    entries = sorted(pool, key=lambda e: e['relevance'], reverse=True)
                    state = 'local-semantic'
                except (Unavailable, ValueError, KeyError):
                    entries = [e for e in ordered if lexical(e)>0]
                    state = 'lexical-fallback'
            else:
                entries = [e for e in ordered if lexical(e)>0]
                state = 'lexical'
        selected = entries[cursor:cursor+limit]
        with self.engine.db.connect() as conn:
            has_shares = conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_memory_nodes'").fetchone()
            for entry in selected:
                # Preserve what was felt then, and expose nearby later context without
                # declaring that every later note is a correction or a confirmed fact.
                later = conn.execute('SELECT id,at,input_id,data FROM mind_private_notes WHERE scope=? AND at>? ORDER BY at,id LIMIT 2', (self.scope, entry['at'])).fetchall()
                entry['later_notes'] = [{'id': r['id'], 'at': r['at'], 'input_id': r['input_id'], 'text': json.loads(r['data'])['text']} for r in later]
                shares = conn.execute("SELECT data FROM mind_memory_nodes WHERE scope=? AND kind='share' AND json_extract(data,'$.delivery_id')=?", (self.scope, entry['group_id'])).fetchall() if has_shares else []
                entry['delivery'] = [{'state': d.get('state'), 'channel': d.get('channel'), 'bubbles': list(d.get('bubbles', {}).values())} for d in (json.loads(r['data']) for r in shares)]
                if not shares: entry['delivery_state'] = 'not-confirmed'
        return {'notes': selected, 'cursor': cursor+limit if len(entries)>cursor+limit else None,
                'recall': state, 'notice': PROMPT, 'retention': {'max_notes': MAX_NOTES}}

    def recent(self):
        return self.read(since=(timestamp(self.mind.clock())-timedelta(hours=6)).isoformat(), limit=2, semantic=False)


    def appraisal_context(self):
        """A small private input, not public evidence or an instruction source."""
        recent = self.recent()
        return {'notice': PROMPT + '可据此整理自己的主观偏好，不复制札记原句到公开内容或事实记忆；不改变用户要求。没有新认识就不更新。',
                'notes': [{k: note[k] for k in ('id', 'at', 'text', 'basis', 'visibility', 'evidence_ids')}
                          for note in recent['notes']]}
