"""Clear an operator-identified diagnostic reason from current state only.

Dry-run reads SQLite directly without initializing or migrating the store. Applying requires
that saved plan, a sourced command, and the exact revision. Mind's normal transaction records
a new history revision and idempotent receipt; old events and numeric state remain untouched.
No appraisal is queued and no model or delivery is invoked.
"""
import argparse
import json
import sqlite3
from pathlib import Path


def plan_state(state, event_id, expected_reason, scope):
    if not event_id or not isinstance(expected_reason, str) or not expected_reason.strip():
        raise ValueError('Name the exact event and nonempty diagnostic reason')
    paths = []
    assessment = state.get('last_assessment') or {}
    if assessment.get('event_id') == event_id and assessment.get('reason') == expected_reason:
        paths.append(['last_assessment', 'reason'])
    for key, value in sorted(state.get('dimensions', {}).items()):
        if value.get('event_id') == event_id and value.get('reason') == expected_reason:
            paths.append(['dimensions', key, 'reason'])
    return {'scope': scope, 'expected_revision': state['revision'], 'event_id': event_id,
            'expected_reason': expected_reason, 'replacement_reason': '', 'paths': paths}


def apply_plan(mind, plan, command_id, source):
    from eventmem.core.db import Conflict
    if not command_id or not source or plan.get('replacement_reason') != '':
        raise ValueError('A sourced command may only clear the reason')
    if plan.get('scope') != mind.scope.model_dump():
        raise ValueError('Plan belongs to another scope')
    if not plan.get('paths'):
        return {'state': 'unchanged', 'changed': 0}
    payload = {'command_id': command_id, 'agent_version': 'diagnostic-reason-recovery-v1',
               'expected_revision': plan['expected_revision'], 'source': source, 'repair': plan}
    def change(conn, state, event_id):
        expected = plan_state(state, plan['event_id'], plan['expected_reason'], mind.scope.model_dump())
        if expected != plan:
            raise Conflict('Diagnostic reason repair plan changed')
        for path in plan['paths']:
            value = state
            for key in path[:-1]:
                value = value[key]
            value[path[-1]] = ''
        return {'state': 'cleared', 'changed': len(plan['paths']), 'repaired_event_id': plan['event_id'],
                'paths': plan['paths'], 'before': plan['expected_reason'], 'after': ''}
    return mind._mutate(payload, 'diagnostic-reason-recovery', change)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--event-id')
    parser.add_argument('--reason-file', type=Path)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--command-id')
    parser.add_argument('--source')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.apply:
        from eventmem.core import Engine
        from eventmem.core.models import Scope
        from kin_mind.state import Mind
        plan = json.loads(args.plan.read_text())
        # Validate scope before initialization can touch this store.
        scope = Scope.model_validate(config['scope'])
        if plan['scope'] != scope.model_dump() or not args.command_id or not args.source:
            parser.error('Applying requires the matching scope, command-id and source')
        result = apply_plan(Mind(Engine(Path(config['root'])), scope), plan, args.command_id, args.source)
        print(json.dumps({k: v for k, v in result.items() if k not in {'before', 'after'}}, ensure_ascii=False))
    else:
        if not args.event_id or not args.reason_file:
            parser.error('Dry-run requires event-id and reason-file')
        from eventmem.core.models import Scope
        scope = Scope.model_validate(config['scope'])
        uri = (Path(config['root']) / 'memory.sqlite3').resolve().as_uri() + '?mode=ro'
        with sqlite3.connect(uri, uri=True) as conn:
            conn.execute('PRAGMA query_only=ON')
            row = conn.execute('SELECT data FROM mind_state WHERE scope=?', (scope.key(),)).fetchone()
            if not row:
                raise ValueError('Mind scope does not exist')
            plan = plan_state(json.loads(row[0]), args.event_id, args.reason_file.read_text().rstrip('\n'), scope.model_dump())
        with args.plan.open('x') as output:
            output.write(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
        args.plan.chmod(0o600)
        print(json.dumps({'state': 'dry-run', 'expected_revision': plan['expected_revision'], 'changed': len(plan['paths']), 'paths': plan['paths']}))


if __name__ == '__main__':
    main()
