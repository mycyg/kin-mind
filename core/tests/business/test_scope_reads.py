"""Which scope a read is for (E3-03).

The scopes that hold records are listed a page at a time, by seeks on an index instead of a
DISTINCT over the whole table, and the cursor goes on where the page stopped. A read that names no
scope reads the deployment's own scope (the Kin service passes the one mind-config names), not an
empty default one; a read that names part of a scope keeps the model defaults for the rest."""
from fastapi.testclient import TestClient

from eventmem.core import Engine
from eventmem.core.api import create_app
from eventmem.core.models import Scope, SourceInput

HEADERS = {"Authorization": "Bearer synthetic-scopes"}
HOME = Scope(project="personal", persona="Kin")


def receive(engine, scope, key, text):
    engine.receive(SourceInput(namespace="scopes", key=key, scope=scope, authority="explicit",
                               kind="knowledge", title=key, text=text))


def serve(engine, **options):
    return TestClient(create_app(engine=engine, token="synthetic-scopes", workers=False, mcp_enabled=False, **options))


def test_scopes_are_listed_a_page_at_a_time_by_index_seeks(tmp_path):
    engine = Engine(tmp_path / "db")
    scopes = [Scope(project=f"project-{i:02d}") for i in range(7)]
    for i, scope in enumerate(scopes):
        receive(engine, scope, f"k{i}", f"synthetic record {i}")
        receive(engine, scope, f"k{i}-more", f"another synthetic record {i}")
    client = serve(engine)
    seen, cursor, pages = [], "", 0
    while True:
        page = client.get("/v1/scopes", headers=HEADERS, params={"limit": 3, "cursor": cursor}).json()
        pages += 1
        assert len(page["items"]) <= 3
        seen.extend(page["items"])
        if not page["cursor"]:
            break
        cursor = page["cursor"]
    assert pages == 3 and seen == sorted((s.model_dump() for s in scopes), key=lambda s: Scope(**s).key())
    with engine.db.connect() as conn:
        plan = " ".join(row[3] for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT scope FROM records WHERE scope>? AND deleted=0 ORDER BY scope LIMIT 1", ("",)))
    assert plan.startswith("SEARCH records USING COVERING INDEX")


def test_a_read_that_names_no_scope_reads_the_deployments_own(tmp_path):
    engine = Engine(tmp_path / "db")
    receive(engine, HOME, "kin", "a record of the deployment's own scope")
    receive(engine, Scope(), "plain", "a record of the model's default scope")
    receive(engine, Scope(project="/work/repo"), "hook", "a record of a project scope")
    served = serve(engine, default_scope=HOME)

    def titles(params=None):
        answer = served.get("/v1/memories", headers=HEADERS, params=params or {})
        assert answer.status_code == 200, answer.text[:300]
        return [item["title"] for item in answer.json()["items"]]

    assert titles() == ["kin"]
    assert served.get("/v1/health", headers=HEADERS).json()["default_scope"] == HOME.model_dump()
    assert served.get("/v1/scopes", headers=HEADERS).json()["default_scope"] == HOME.model_dump()
    # Part of a scope keeps the model defaults for the rest, as before; all of one is that one.
    assert titles({"project": "/work/repo"}) == ["hook"]
    assert titles(Scope().model_dump()) == ["plain"]
    # A service that was given no scope of its own reads the model default, as before.
    assert [i["title"] for i in serve(engine).get("/v1/memories", headers=HEADERS).json()["items"]] == ["plain"]
