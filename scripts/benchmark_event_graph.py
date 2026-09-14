"""Synthetic scale probe. All source material is invented; never opens a private DB."""
import argparse
import json
import tempfile
import time
from pathlib import Path

from eventmem.core import Engine
from eventmem.core.db import dumps
from eventmem.core.models import Scope, SourceInput
from eventmem.core.retrieval import tokens
from kin_mind.context import Contexts
from kin_mind.memory import MemoryContinuity
from kin_mind.state import Mind


def run(root):
    engine=Engine(root);mind=Mind(engine,Scope(persona="synthetic-scale"));memory=MemoryContinuity(mind)
    source=engine.receive(SourceInput(namespace="synthetic-benchmark",key="fixture",text="Invented benchmark records and outcomes. No real events.",scope=mind.scope,authority="explicit",extract=False))["id"]
    mind.initialize(agent_version="synthetic-scale-v1",evidence_ids=[source])
    memory.configure({"records":True,"context":True,"graph":True,"sharing":True,"graph_recall":True})
    started=time.monotonic()
    with engine.db.connect(write=True) as conn:
        proof=memory.graph.proof(conn,[source])
        for i in range(50000):
            memory.graph._put(conn,{"id":f"scale-event-{i:05}","kind":"event","title":f"Synthetic project {i} event","text":f"Synthetic outcome {i}. The planned migration is not finished until validation.","basis":"explicit","evidence":proof,"source_ids":[source],"occurred_at":"2026-08-01T01:00:00.000000+00:00"})
        for i in range(1000):
            work=memory.graph._put(conn,{"id":f"scale-work-{i}","kind":"work","title":f"Work {i}","text":"Synthetic creator and version record","created_by":"synthetic-agent","basis":"observed","evidence":proof,"source_ids":[source]})
            unit=memory.sharing.units(conn,work["id"],[f"Work {i} has a synthetic result."],[source],owner_kind="work")[0]
            for j in range(10):
                data={"unit_id":unit["id"],"version":1,"bubble_id":f"scale-{i}-{j}","share_id":f"share-scale-{i}-{j}","state":"accepted","at":mind.clock(),"message_id":f"synthetic-receipt-{i}-{j}","needs_review":False,"visibility":"unverified"}
                memory.sharing._save_coverage(conn,data)
    populated=round(time.monotonic()-started,3)
    results={"events":50000,"works":1000,"shares":10000,"populate_seconds":populated}
    started=time.monotonic();page=memory.graph.read(limit=150);results["graph_page_ms"]=round(1000*(time.monotonic()-started),2);assert len(page["nodes"])<=150
    started=time.monotonic();old=memory.graph.read(query="49999",limit=150);results["old_event_recall_ms"]=round(1000*(time.monotonic()-started),2);assert any(n["id"]=="scale-event-49999" for n in old["nodes"])
    with engine.db.connect() as conn:
        finding=conn.execute("SELECT id FROM mind_graph_nodes WHERE kind='finding' LIMIT 1").fetchone()[0]
        coverage=memory.sharing.coverage(conn,finding);assert coverage["state"]=="shared" and len(coverage["deliveries"])==5 and coverage["delivery_count"]==10
    context=Contexts(mind)
    started=time.monotonic();reply=context.build(query="Work 0 synthetic result",purpose="chat",budget=800,allow_model=False)
    results.update(chat_background_ms=round(1000*(time.monotonic()-started),2),chat_tokens=tokens(dumps(reply)),background_tokens=reply["tokens"],graph_page_nodes=len(page["nodes"]),model_calls=0)
    assert reply["tokens"]<=800
    return results


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--output");args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="kin-graph-scale-") as root:
        report=run(Path(root))
    text=json.dumps(report,indent=2)
    if args.output:Path(args.output).write_text(text+"\n")
    print(text)
