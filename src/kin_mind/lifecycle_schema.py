"""Additive storage for derived event views; original evidence is unchanged.

`mind_isolation_archive` keeps the derived text a read policy change invalidates. A derived
view is rebuilt, never corrected in place, and the version it replaces is kept here under the
rules version that retired it, so nothing is lost when a summary is regenerated.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_graph_record_refs(
 scope TEXT NOT NULL,record_id TEXT NOT NULL,node_id TEXT NOT NULL,
 PRIMARY KEY(scope,record_id,node_id));
CREATE INDEX IF NOT EXISTS mind_graph_ref_node ON mind_graph_record_refs(scope,node_id);
CREATE TABLE IF NOT EXISTS mind_event_digests(
 scope TEXT NOT NULL,event_id TEXT NOT NULL,state TEXT NOT NULL,generation INTEGER NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0,input_hash TEXT NOT NULL DEFAULT '',
 dirty_at TEXT NOT NULL,due_at REAL NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,event_id));
CREATE INDEX IF NOT EXISTS mind_digest_due ON mind_event_digests(state,due_at);
CREATE TABLE IF NOT EXISTS mind_event_dependencies(
 scope TEXT NOT NULL,event_id TEXT NOT NULL,record_id TEXT NOT NULL,revision INTEGER NOT NULL,
 PRIMARY KEY(scope,event_id,record_id));
CREATE INDEX IF NOT EXISTS mind_digest_source ON mind_event_dependencies(scope,record_id);
CREATE TABLE IF NOT EXISTS mind_event_routes(
 scope TEXT NOT NULL,id TEXT NOT NULL,digest TEXT NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,id));
CREATE TABLE IF NOT EXISTS mind_event_usage(
 scope TEXT NOT NULL,identifier TEXT NOT NULL,usage_id TEXT NOT NULL,origin TEXT NOT NULL,
 at TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,identifier,usage_id,origin));
CREATE INDEX IF NOT EXISTS mind_usage_latest ON mind_event_usage(scope,identifier,at);
CREATE TABLE IF NOT EXISTS mind_memory_temperature(
 scope TEXT NOT NULL,identifier TEXT NOT NULL,tier TEXT NOT NULL,
 protected INTEGER NOT NULL,updated_at TEXT NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,identifier));
CREATE TABLE IF NOT EXISTS mind_lifecycle_runs(
 scope TEXT NOT NULL,kind TEXT NOT NULL,slot TEXT NOT NULL,input_hash TEXT NOT NULL,
 state TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(scope,kind,slot));
CREATE TABLE IF NOT EXISTS mind_lifecycle_backfill(
 scope TEXT PRIMARY KEY,cursor TEXT NOT NULL,state TEXT NOT NULL,
 processed INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mind_foreground_leases(
 scope TEXT NOT NULL,session TEXT NOT NULL,expires_at REAL NOT NULL,
 PRIMARY KEY(scope,session));
CREATE TABLE IF NOT EXISTS mind_isolation_archive(
 scope TEXT NOT NULL,kind TEXT NOT NULL,identifier TEXT NOT NULL,revision INTEGER NOT NULL,
 rules_version TEXT NOT NULL,archived_at TEXT NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(scope,kind,identifier,revision));
"""


def initialize(conn):
    conn.executescript(SCHEMA)
    if "priority" not in {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}:
        # Multiple hosts can initialize the same additive schema concurrently.
        import sqlite3
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 100")
        except sqlite3.OperationalError:
            if "priority" not in {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}:
                raise
    conn.execute("CREATE INDEX IF NOT EXISTS job_priority ON jobs(state,priority,available)")


def initialize_graph_refs(conn):
    if conn.execute("SELECT 1 FROM meta WHERE key='kin_graph_refs_v1'").fetchone():
        return
    conn.execute("INSERT OR IGNORE INTO mind_graph_record_refs SELECT n.scope,j.value,n.id "
                 "FROM mind_graph_nodes n,json_each(n.data,'$.record_ids') j WHERE typeof(j.value)='text'")
    conn.execute("INSERT OR IGNORE INTO mind_graph_record_refs SELECT n.scope,json_extract(j.value,'$.record_id'),n.id "
                 "FROM mind_graph_nodes n,json_each(n.data,'$.evidence') j WHERE json_extract(j.value,'$.record_id') IS NOT NULL")
    conn.execute("INSERT OR IGNORE INTO meta VALUES('kin_graph_refs_v1',1)")
