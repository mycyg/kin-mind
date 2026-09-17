"""Additive autonomy storage. Decisions never stand in for execution receipts."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_semantic_cache(
 id TEXT PRIMARY KEY,generation INTEGER NOT NULL,expires_at REAL NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mind_plans(
 id TEXT PRIMARY KEY,scope TEXT NOT NULL,revision INTEGER NOT NULL,status TEXT NOT NULL,
 next_review TEXT,updated_at TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mind_plan_due ON mind_plans(scope,status,next_review);
CREATE TABLE IF NOT EXISTS mind_plan_history(
 id TEXT NOT NULL,revision INTEGER NOT NULL,command_id TEXT NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(id,revision));
CREATE TABLE IF NOT EXISTS mind_plan_runs(
 id TEXT PRIMARY KEY,scope TEXT NOT NULL,plan_id TEXT NOT NULL,step_id TEXT NOT NULL,
 actor TEXT NOT NULL,state TEXT NOT NULL,lease_until REAL NOT NULL,owner TEXT NOT NULL,
 fence INTEGER NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mind_plan_executor ON mind_plan_runs(scope,actor,state);
CREATE TABLE IF NOT EXISTS mind_model_leases(
 id TEXT PRIMARY KEY,lane TEXT NOT NULL,expires_at REAL NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mind_model_lane ON mind_model_leases(lane,expires_at);
CREATE TABLE IF NOT EXISTS mind_procedures(
 id TEXT PRIMARY KEY,scope TEXT NOT NULL,revision INTEGER NOT NULL,status TEXT NOT NULL,
 updated_at TEXT NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mind_procedure_history(
 id TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(id,revision));
CREATE TABLE IF NOT EXISTS mind_procedure_trials(
 scope TEXT NOT NULL,id TEXT NOT NULL,procedure_id TEXT NOT NULL,revision INTEGER NOT NULL,
 case_id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,id));
CREATE TABLE IF NOT EXISTS mind_reinforcement(
 scope TEXT NOT NULL,identifier TEXT NOT NULL,use_key TEXT NOT NULL,at TEXT NOT NULL,
 origin TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,identifier,use_key));
CREATE INDEX IF NOT EXISTS mind_reinforcement_time ON mind_reinforcement(scope,identifier,at);
CREATE TABLE IF NOT EXISTS mind_strength_observations(
 scope TEXT NOT NULL,version TEXT NOT NULL,day TEXT NOT NULL,observed_at TEXT NOT NULL,
 data TEXT NOT NULL,PRIMARY KEY(scope,version,day));
CREATE TABLE IF NOT EXISTS mind_plan_reviews(
 plan_id TEXT NOT NULL,command_id TEXT NOT NULL,scope TEXT NOT NULL,kind TEXT NOT NULL,
 plan_revision INTEGER NOT NULL,at TEXT NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(plan_id,command_id));
CREATE INDEX IF NOT EXISTS mind_plan_review_time ON mind_plan_reviews(scope,plan_id,at);
CREATE TABLE IF NOT EXISTS mind_plan_wakeups(
 scope TEXT NOT NULL,plan_id TEXT NOT NULL,key_digest TEXT NOT NULL,reason TEXT NOT NULL,
 at TEXT NOT NULL,event_id TEXT NOT NULL,PRIMARY KEY(scope,plan_id,key_digest));
"""


def settings(conn, scope):
    import json
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mind_memory_config'").fetchone():
        return {}
    row = conn.execute("SELECT data FROM mind_memory_config WHERE scope=?", (scope,)).fetchone()
    return json.loads(row[0]) if row else {}


def enabled(conn, scope, flag="semantic_actions"):
    return settings(conn, scope).get(flag, False) is True


def optimized(conn, scope, flag):
    """Default-on optimization; only an explicit false restores the previous behavior."""
    return settings(conn, scope).get(flag) is not False
