"""Paid service calls counted per scope, local day, service and purpose.

Budgets admit a call here before it is made; the usage ledger reads the same rows.
Only numbers are kept: no request, result or source.
"""
import sqlite3
from datetime import datetime

SCHEMA = ("CREATE TABLE IF NOT EXISTS mind_paid_calls(scope TEXT NOT NULL,day TEXT NOT NULL,service TEXT NOT NULL,"
          "purpose TEXT NOT NULL,calls INTEGER NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,"
          "PRIMARY KEY(scope,day,service,purpose))")


class PaidCalls:
    def __init__(self, database, scope, purpose, *, clock=lambda: datetime.now().astimezone()):
        self.database, self.scope, self.purpose, self.clock = str(database), scope, purpose, clock

    def _connect(self):
        conn = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(SCHEMA)
        return conn

    def admit(self, service, services, limit):
        """Count one call of `service` unless today's calls of `services` for this purpose reached `limit`."""
        day, conn = self.clock().date().isoformat(), self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            used = conn.execute("SELECT COALESCE(SUM(calls),0) FROM mind_paid_calls WHERE scope=? AND day=? AND purpose=? AND service IN (%s)"
                                % ",".join("?" * len(services)), (self.scope, day, self.purpose, *services)).fetchone()[0]
            if limit is not None and used >= limit:
                conn.execute("ROLLBACK")
                return False
            conn.execute("INSERT INTO mind_paid_calls VALUES(?,?,?,?,1,0) ON CONFLICT(scope,day,service,purpose) DO UPDATE SET calls=calls+1",
                         (self.scope, day, service, self.purpose))
            conn.execute("COMMIT")
            return True
        finally:
            conn.close()

    def failed(self, service):
        conn = self._connect()
        try:
            conn.execute("INSERT INTO mind_paid_calls VALUES(?,?,?,?,0,1) ON CONFLICT(scope,day,service,purpose) DO UPDATE SET failures=failures+1",
                         (self.scope, self.clock().date().isoformat(), service, self.purpose))
        finally:
            conn.close()
