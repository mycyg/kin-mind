"""Trusted operational recipient; never a second memory scope or affect state."""
from .companions import XIAOGUANG
from contextlib import contextmanager
from contextvars import ContextVar

OPERATIONAL_TARGET = ContextVar('kin_operational_target', default=None)

@contextmanager
def target_scope(mind, person=None):
    """Select operational ownership while keeping the same shared memory store.

    The conversation is context, not a delivery address. Actual delivery still
    resolves the recipient through the host's bound directory.
    """
    person = person or current()
    if person not in {'person:xiaoguang', 'person:yiyi', 'group:family'}:
        raise ValueError('Unknown operational recipient')
    if person == current():
        yield
        return
    # A plan transaction changes ownership only. Constructing CompanionView
    # here would initialize its tables through a second database connection
    # while this transaction already holds the write lock.
    token = OPERATIONAL_TARGET.set(person)
    try:
        yield
    finally:
        OPERATIONAL_TARGET.reset(token)

def current():
    if OPERATIONAL_TARGET.get() is not None:
        return OPERATIONAL_TARGET.get()
    from .companion_view import ACTIVE_VIEW
    view = ACTIVE_VIEW.get()
    return view.person if view else XIAOGUANG

def belongs(value):
    return value.get('recipient_person_id', XIAOGUANG) == current()

def key(parts):
    # Legacy identifiers remain unchanged; another recipient cannot collide with them.
    return parts if current() == XIAOGUANG else [*parts, current()]

def predicate(column='data'):
    # Only fixed internal identity constants reach SQL, never model/user input.
    person = current()
    if person not in {'person:xiaoguang', 'person:yiyi', 'group:family'}:
        raise ValueError('Unknown operational recipient')
    return "COALESCE(json_extract(" + column + ",'$.recipient_person_id'),'person:xiaoguang')='" + person + "'"

# Only scheduling counters/times live here; evidence, memory and affect keep base scope.
CURSOR_SCHEMA = '''CREATE TABLE IF NOT EXISTS mind_recipient_cursors(
 scope TEXT NOT NULL,recipient TEXT NOT NULL,kind TEXT NOT NULL,seq INTEGER NOT NULL DEFAULT 0,
 next_review TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,minutes INTEGER NOT NULL DEFAULT 60,
 PRIMARY KEY(scope,recipient,kind))'''

def cursor(conn, scope, kind):
    import json
    if current() == XIAOGUANG:
        row = conn.execute('SELECT * FROM '+kind+' WHERE scope=?', (scope,)).fetchone()
        return dict(row) if row else None
    row = conn.execute('SELECT * FROM mind_recipient_cursors WHERE scope=? AND recipient=? AND kind=?',
                       (scope,current(),kind)).fetchone()
    value = dict(row) if row else {'seq':0,'next_review':'1970-01-01T00:00:00+00:00','revision':0,'minutes':60}
    return {**value,'data':json.dumps({'minutes':value['minutes']})}

def save_cursor(conn, scope, kind, *, next_at, seq=0, data=None):
    from eventmem.core.db import dumps
    if current() == XIAOGUANG:
        if kind == 'mind_action_schedule':
            conn.execute('INSERT INTO mind_action_schedule VALUES(?,?,1,?) ON CONFLICT(scope) DO UPDATE SET next_review=excluded.next_review,revision=revision+1,data=excluded.data', (scope,next_at,dumps(data or {})))
        else:
            conn.execute('INSERT INTO mind_semantic_cursor(scope,seq,next_review,revision,data) VALUES(?,?,?,1,?) ON CONFLICT(scope) DO UPDATE SET seq=MAX(seq,excluded.seq),next_review=excluded.next_review,revision=revision+1,data=excluded.data', (scope,seq,next_at,dumps(data or {})))
    else:
        conn.execute('INSERT INTO mind_recipient_cursors VALUES(?,?,?,?,?,1,?) ON CONFLICT(scope,recipient,kind) DO UPDATE SET seq=MAX(seq,excluded.seq),next_review=excluded.next_review,revision=revision+1,minutes=excluded.minutes', (scope,current(),kind,seq,next_at,(data or {}).get('minutes',60)))


def preferences(state):
    """Legacy X preferences stay in place; others keep sourced relationship settings."""
    if current() == XIAOGUANG:
        return state
    return state.setdefault('recipient_preferences', {}).setdefault(current(), {})

def policy_key(scope):
    return scope if current() == XIAOGUANG else scope + ':' + current()
