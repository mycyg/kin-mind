"""A shared, sourced working set for recall and native-window recovery.

Selection is local. Semantic notes are supplied by the existing appraisal, not
by another per-message model request. Raw evidence and operation ledgers remain
canonical; this module only persists derived views.
"""
import json

from eventmem.core.db import Missing, digest, dumps

from .computer import redact
from .graph import query_terms
from .state import timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_continuity_manifests(
 id TEXT PRIMARY KEY,scope TEXT NOT NULL,conversation TEXT NOT NULL,generation INTEGER NOT NULL,
 complete INTEGER NOT NULL,data TEXT NOT NULL,at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mind_manifest_conversation ON mind_continuity_manifests(scope,conversation,generation,at);
"""
FIELDS = ('content', 'topic', 'target', 'status', 'kind', 'completion', 'reason', 'owner_request', 'concern_ids', 'expires_at')


class ContinuityManifest:
    def __init__(self, mind, contexts=None):
        if contexts is None:
            from .context import Contexts
            contexts = Contexts(mind)
        self.ctx, self.mind, self.memory = contexts, mind, contexts.memory
        with mind.engine.db.connect() as conn:
            conn.executescript(SCHEMA)

    def select(self, query='', *, tasks=(), pending=(), intent=None, limit=12, policy=None):
        """Bounded candidate expansion, with open matters independent of recency."""
        if policy is None:
            from eventmem.core.read_policy import ReadPolicy
            policy = ReadPolicy.load(self.mind.engine, self.mind.scope, 'experience_recall')
        items, warnings = [], []
        settings = self.memory.settings()
        terms = set(query_terms(query))
        task_ids = {t['id'] for t in tasks if t.get('id')}
        active_matters = set((intent or {}).get('concern_ids', [])) | {c for t in tasks for c in t.get('concern_ids', [])}
        focus_ids = list(dict.fromkeys([r.get('unit_id') for e in pending for r in e.get('references', []) if r.get('unit_id')]
                                     + [x for x in ((intent or {}).get('exploration_id'), (intent or {}).get('work_id')) if x]))
        with self.mind.engine.db.connect() as conn:
            state = self.mind._load(conn)
            entry_terms = {}
            def overlap(entry):
                # One tokenization per entry per select: the sort key and the priority
                # test below ask the same question of the same topic+content.
                if entry['id'] not in entry_terms:
                    entry_terms[entry['id']] = set(query_terms(entry.get('topic', '') + ' ' + entry.get('content', '')))
                return bool(terms & entry_terms[entry['id']])
            for kind in ('concerns', 'desires'):
                active = [d for d in state.get(kind, {}).values() if d.get('status') in {'active', 'easing', 'wanted', 'in_progress', 'waiting'}]
                active.sort(key=lambda d: (not overlap(d), -d.get('intensity', d.get('strength', 0)), d['id']))
                for entry in active[:3 if kind == 'concerns' else 2]:
                    if entry.get('expires_at') and timestamp(entry['expires_at']) <= timestamp(self.mind.clock()):
                        continue
                    if not self.mind._fresh(conn, entry.get('evidence', [])):
                        warnings.append(entry['id']); continue
                    value = {k: entry[k] for k in FIELDS if k in entry}
                    items.append({'id': entry['id'], 'revision': digest(entry), 'text': dumps(value),
                        'basis': entry.get('basis', 'inferred'), 'facts': {'kind': kind, 'status': entry['status']},
                        'dependencies': [{'id': r['record_id'], 'revision': r['revision']} for r in entry.get('evidence', [])],
                        'state_dependency': {'kind': kind, 'id': entry['id'], 'digest': digest(entry)}, 'priority': 0 if entry['id'] in active_matters else 1 if overlap(entry) else 3})
            for task_id in sorted(task_ids):
                for row in conn.execute("SELECT DISTINCT n.data FROM mind_memory_nodes n, json_each(n.data,'$.task_ids') t WHERE n.scope=? AND n.kind='work' AND t.value=? ORDER BY n.updated_at DESC LIMIT 3", (self.mind.scope.key(), task_id)):
                    node = json.loads(row[0])
                    if self.memory._fresh(conn, node):
                        items.append({**self.ctx.node_item(node, policy=policy), 'priority': -1})
                        focus_ids.append(node['id'])
        graph_nodes, graph_edges = {}, {}
        if settings['graph'] or settings['graph_recall']:
            views = []
            for focus in focus_ids[:6]:
                try:
                    views.append(self.memory.graph.read(focus=focus, limit=20, hops=2, policy=policy))
                except Missing:
                    warnings.append(focus)
            if query:
                views.append(self.memory.graph.read(query=query, limit=40, hops=2, policy=policy))
            for view in views:
                for node in view['nodes']:
                    if not node['needs_review']:
                        graph_nodes.setdefault(node['id'], node)
                graph_edges.update({e['id']: e for e in view['edges'] if not e['needs_review']})
            for node in graph_nodes.values():
                if node['kind'] not in {'finding', 'work', 'artifact', 'thread', 'episode', 'association'}:
                    continue
                if node['kind'] == 'association' and not settings['associations']:
                    continue
                # Graph query includes time-near candidates; time alone doesn't
                # promote them ahead of a matching work or explicit focus.
                match = bool(terms & set(query_terms(node.get('title', '') + ' ' + node.get('text', ''))))
                related = any(e['subject'] in focus_ids or e['object'] in focus_ids for e in graph_edges.values() if node['id'] in {e['subject'], e['object']})
                if not (match or related or node['id'] in focus_ids):
                    continue
                unit = self.ctx.graph_item(node, list(graph_edges.values()), compact=True, policy=policy)
                unit['priority'] = 0 if node['id'] in focus_ids else 1 if node['kind'] in {'work','finding','thread'} else 3
                items.append(unit)
        if query:
            for kind, count in (('work', 3), ('share', 5)):
                for node in self.memory.history(kind, query=query, limit=count)['items']:
                    if not node['needs_review']:
                        items.append({**self.ctx.node_item(node, policy=policy), 'priority': 1 if kind == 'work' else 2})
        # An observed file effect is available even before the journal drains.
        # This is a bounded operation record, never arbitrary tool output.
        for event in pending:
            if event.get('kind') not in {'artifact-created', 'artifact-observed', 'task-result'}:
                continue
            artifact = event.get('artifact', {})
            relevant = event.get('task_id') in task_ids or bool(terms & set(query_terms(artifact.get('name', '') + ' ' + event.get('text', ''))))
            if not relevant:
                continue
            facts = {k: event[k] for k in ('kind', 'at', 'task_id', 'tool_call_id', 'state', 'operation_status', 'account_basis') if k in event}
            if event['kind'] == 'task-result':
                facts['public_result'] = event.get('text', '')
            facts['artifact'] = {k: artifact[k] for k in ('name', 'sha256', 'members_sha256', 'bytes') if k in artifact}
            facts['created_by'] = event.get('actor', 'Kin') if event['kind'] == 'artifact-created' else 'unknown'
            facts['journal_state'] = 'pending-semantic-ingestion'
            item = {'id': 'journal:' + event['id'], 'revision': digest(redact(event)), 'text': dumps(facts),
                    'basis': 'public-output' if event['kind'] == 'task-result' else 'host-operation', 'facts': {'kind': event['kind']}, 'priority': -1,
                    'pending_dependency': {'id': event['id'], 'digest': digest(redact(event))}}
            if self.ctx._current(item, policy):
                items.append(item)
        # Raw accepted outbox receipts override stale semantic sharing views.
        # No guessed semantic match is treated as proof of coverage.
        for item in items:
            for event in pending:
                for ref in event.get('references', []):
                    if ref.get('unit_id') != item['id']:
                        continue
                    facts = item.setdefault('facts', {})
                    coverage = facts.get('share_coverage', {})
                    if ref.get('version') != coverage.get('version', ref.get('version')):
                        continue
                    if event.get('kind') == 'delivery' and event.get('state') == 'accepted' and event.get('message_id'):
                        facts['share_coverage'] = {**coverage, 'state': 'shared', 'last_shared_at': event['at'],
                            'last_message_id': event['message_id'], 'visibility': 'unverified', 'basis': 'pending-host-receipt'}
                        item['revision'] = digest([item['revision'], facts['share_coverage']])
                        item['priority'] = -2
            item.pop('cached_summary', None)
        unique = {}
        for item in sorted(items, key=lambda i: i.get('priority', 2)):
            unique.setdefault(item['id'], item)
        selected = list(unique.values())[:limit]
        return {'items': selected, 'index': [{'id': i['id'], 'revision': i['revision']} for i in list(unique.values())[limit:limit+40]],
                'critical_ids': [i['id'] for i in unique.values() if i.get('priority', 2) <= 0],
                'needs_review_ids': warnings, 'model_requests': 0}

    def watermarks(self, pending=()):
        with self.mind.engine.db.connect() as conn:
            durable = conn.execute('SELECT COALESCE(MAX(seq),0) FROM mind_runtime_events WHERE scope=?', (self.mind.scope.key(),)).fetchone()[0]
            row = conn.execute('SELECT seq FROM mind_semantic_cursor WHERE scope=?', (self.mind.scope.key(),)).fetchone()
        return {'durable_event_seq': durable, 'semantic_seq': row[0] if row else 0,
                'pending_journal_count': len(pending), 'pending_digest': digest(pending),
                'semantic_lag_is_not_missing_evidence': True}

    def store(self, checkpoint):
        with self.mind.engine.db.connect(write=True) as conn:
            conn.execute('INSERT OR IGNORE INTO mind_continuity_manifests VALUES(?,?,?,?,?,?,?)',
                (checkpoint['id'], self.mind.scope.key(), checkpoint['conversationId'], checkpoint['generation'],
                 int(checkpoint['complete']), dumps(checkpoint), self.mind.clock()))
        return checkpoint

    def read(self, identifier=None, *, conversation=None, cursor=0, limit=20):
        if not 0 <= cursor or not 1 <= limit <= 40:
            raise ValueError('Invalid manifest page')
        with self.mind.engine.db.connect() as conn:
            if identifier:
                row = conn.execute('SELECT data FROM mind_continuity_manifests WHERE scope=? AND id=?', (self.mind.scope.key(), identifier)).fetchone()
                if not row:
                    raise Missing(identifier)
                value = json.loads(row[0])
                deps = value.get('contextDependencies', [])
                # A stored manifest that leans on something recall may no longer see needs review.
                from eventmem.core.read_policy import ReadPolicy
                policy = ReadPolicy.load(self.mind.engine, self.mind.scope, 'experience_recall', conn=conn)
                return {'id': identifier, 'complete': value['complete'], 'needs_review': not all(self.ctx._current(i, policy) for i in deps),
                        'payload': value['payload'], 'coverage': value['coverage'], 'watermarks': value.get('watermarks'),
                        'sources': [{'id': i['id'], 'revision': i['revision']} for i in deps[cursor:cursor+limit]],
                        'cursor': cursor+limit if len(deps) > cursor+limit else None, 'instruction_authority': 'data'}
            rows = conn.execute('SELECT id,complete,at FROM mind_continuity_manifests WHERE scope=? AND (? IS NULL OR conversation=?) ORDER BY at DESC,id LIMIT ? OFFSET ?', (self.mind.scope.key(), conversation, conversation, limit+1, cursor)).fetchall()
        return {'items': [dict(r) for r in rows[:limit]], 'cursor': cursor+limit if len(rows)>limit else None}
