"""Real SQLite/business hooks; no live models, channels or production database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json

import pytest

from eventmem.core import Engine
from eventmem.core.db import Conflict
from eventmem.core.models import Scope, SourceInput
from kin_mind import energy
from kin_mind.actions import ActionEvents
from kin_mind.appraisal import appraisal_context
from kin_mind.companion_view import CompanionView, companion_scope
from kin_mind.continuity import ContinuityConfig, RhythmProposal
from kin_mind.context import Contexts, affect_projection
from kin_mind.interaction_projection import interaction_projection
from kin_mind.memory import MemoryContinuity
from kin_mind.plans import AutonomousPlans
from kin_mind.rhythm import interaction_windows, rhythm_view, at_rest
from kin_mind.state import Mind, AffectiveEvent


@pytest.fixture
def env(tmp_path):
    clock = [datetime.now(timezone.utc) + timedelta(minutes=1)]
    mind = Mind(Engine(tmp_path), Scope(persona="synthetic-energy"), clock=lambda: clock[0].isoformat())
    def source(key, namespace="kin-owner-input", metadata=None, at=None):
        return mind.engine.receive(SourceInput(namespace=namespace, key=key, scope=mind.scope,
            text=key, authority="explicit", occurred_at=at or mind.clock(), extract=False,
            metadata={"role":"user", "host_event":"message", **(metadata or {})}))["id"]
    initial = source("initial")
    mind.initialize(agent_version="energy-test", evidence_ids=[initial])
    mind.configure_continuity(ContinuityConfig(command_id="rhythm",agent_version="energy-test",
        expected_revision=mind.read()["revision"], evidence_ids=[initial],features={"rhythm":True},reason="test"))
    memory = MemoryContinuity(mind)
    memory.configure({"records":True,"semantic":True,"operational_lanes":True,"semantic_actions":True,
                      "autonomous_plans":True,"creative_execution":True})
    return mind, memory, clock, source, initial


def test_recovery_caps_before_cost_read_is_pure_and_reboot_deduplicates(env):
    mind, _, clock, _, _ = env
    energy.configure(mind,{"night_penalty":0,"awake_fatigue_per_hour":0,"recovery_per_hour":6,"enabled":True,"initial":100})
    clock[0] += timedelta(hours=1)
    first=energy.activity(mind,{"kind":"reply","activity_id":"reply-group"})
    assert first["energy_context"]["value"] == 98.5
    restarted=Mind(mind.engine,mind.scope,clock=mind.clock)
    for _ in range(5):
        assert restarted.read()["energy_context"]["value"] == 98.5
        assert energy.activity(restarted,{"kind":"reply","activity_id":"reply-group"})["state"] == "duplicate"
    assert restarted.read()["energy_context"]["revision"] == first["energy_context"]["revision"]
    clock[0] -= timedelta(hours=2)
    assert restarted.read()["energy_context"]["value"] == 98.5
    clock[0] += timedelta(hours=10)
    assert restarted.read()["energy_context"]["value"] == 100


def test_config_change_does_not_reprice_past_and_disabled_time_does_not_recover(env):
    mind, _, clock, _, _=env
    energy.configure(mind,{"night_penalty":0,"awake_fatigue_per_hour":0,"recovery_per_hour":6,"enabled":True,"initial":20})
    clock[0]+=timedelta(hours=2)
    result=energy.configure(mind,{"recovery_per_hour":12})
    assert result["energy_context"]["value"]==32
    clock[0]+=timedelta(hours=1)
    assert mind.read()["energy_context"]["value"]==44
    energy.configure(mind,{"enabled":False})
    clock[0]+=timedelta(hours=8)
    assert energy.activity(mind,{"kind":"reply","activity_id":"disabled"})["state"]=="disabled"
    assert energy.configure(mind,{"night_penalty":0,"awake_fatigue_per_hour":0,"recovery_per_hour":6,"enabled":True})["energy_context"]["value"]==44
    before=mind.read()["energy_context"]
    energy.configure(mind,{"night_penalty":0,"awake_fatigue_per_hour":0,"recovery_per_hour":6,"enabled":True})
    assert mind.read()["energy_context"]==before


def test_shared_multichannel_concurrency_and_projection(env):
    mind, memory, _, _, _=env
    energy.configure(mind,{"night_penalty":0,"awake_fatigue_per_hour":0,"recovery_per_hour":6,"enabled":True})
    def reply(index):
        with companion_scope(CompanionView(mind.engine,mind.scope,"person:yiyi" if index%2 else "person:xiaoguang","chat")):
            return energy.activity(mind,{"kind":"reply","activity_id":"logical-"+str(index//2)})
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(reply,range(20)))
    assert sum(r["state"]=="applied" for r in results)==10
    view=mind.read()
    assert view["energy_context"]["value"]==60
    assert interaction_projection(view)["energy_context"]==view["energy_context"]
    assert appraisal_context({"state":view,"stimulus":"idle-review"})["state"]["energy_context"]==view["energy_context"]
    assert Contexts(mind).affective()["energy_context"]==view["energy_context"]
    assert affect_projection(view)["energy_context"]==view["energy_context"]
    memory.configure({"context":True})
    background=Contexts(mind).build("",purpose="chat",session="energy-main",event_id="turn-energy",allow_model=False)
    entries=[json.loads(line) for line in background["text"].splitlines()]
    affect=next(item for item in entries if item["id"]=="affect")
    assert json.loads(affect["text"])["energy_context"]==view["energy_context"]


def test_low_energy_keeps_task_attention_and_internal_reviews_cost_nothing(env):
    mind,memory,_,source,_=env
    energy.configure(mind,{"night_penalty":0,"awake_fatigue_per_hour":0,"recovery_per_hour":6,"enabled":True,"initial":0})
    permission=source("allow quiet")
    memory.habits.update({"command_id":"allow-quiet","expected_revision":0,"evidence_ids":[permission],
        "reason":"test","preferences":{"reply_choice":"autonomous"}})
    memory.ingest({"id":"urgent","kind":"owner-message","text":"Help now","at":mind.clock(),"attention_for":"urgent"})
    with pytest.raises(Conflict):memory.habits.choose_reply({"input_id":"urgent","action":"silent"})
    assert memory.habits.choose_reply({"input_id":"urgent","action":"reply"})["state"]=="decided"
    for _ in range(3):ActionEvents(mind).crossings()
    assert mind.read()["energy_context"]["value"]==0
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_energy_events").fetchone()[0]==0


def test_companion_windows_keep_people_and_conversations_separate(env):
    mind,_,clock,source,_=env
    for person,chat,minute in [("person:xiaoguang","x-private",1),("person:yiyi","y-private",2),("group:family","family",3),("person:xiaoguang","another-x-chat",4)]:
        source(chat,namespace="kin-companion-input",metadata={"speaker_person_id":"person:yiyi" if person=="group:family" else person,
            "recipient_person_id":person,"conversation_key":chat},at=(clock[0]+timedelta(minutes=minute)).isoformat())
    clock[0]+=timedelta(minutes=5)
    for person,chat,expected in [("person:xiaoguang","x-private",1),("person:yiyi","y-private",2),("group:family","family",3)]:
        with companion_scope(CompanionView(mind.engine,mind.scope,person,chat)):
            windows=mind.read()["rhythm"]["interactions"]
        assert windows["last_owner_at"]==(clock[0]-timedelta(minutes=5-expected)).isoformat()
        assert windows["recipient_person_id"]==person
        assert windows["conversation_key"]==chat


def test_real_split_phase_proposal_uses_phase_and_arrival_cannot_overwrite_it(env):
    mind,memory,clock,_,_=env
    mind.record(AffectiveEvent(command_id="rest",agent_version="energy-test",expected_revision=mind.read()["revision"],subjective=True,
        rhythm=RhythmProposal(phase="resting",base_phase="awake",alertness=8,target=0,half_life_minutes=60)))
    assert mind.read()["rhythm"]["phase"]=="resting"
    clock[0]+=timedelta(minutes=1)
    memory.ingest({"id":"chat","kind":"owner-message","text":"Hello","at":mind.clock()})
    view=mind.read()
    assert view["rhythm"]["phase"]==view["rhythm"]["base_phase"]=="resting"
    assert view["rhythm"]["attention_for"]=="owner_message"
    assert at_rest(view,mind.clock())
    # Existing rows are interpreted coherently without an unsolicited history rewrite.
    legacy={"phase":"resting","base_phase":"awake","at":mind.clock(),"alertness":8,"target":0,
            "half_life_minutes":60,"event_id":"legacy","config_version":"legacy"}
    assert rhythm_view(legacy,mind.clock(),{})["phase"]=="resting"


def test_explicit_long_review_is_not_preempted_by_alertness_half_life(env):
    mind,memory,clock,_,_=env
    memory.configure({"idle":True})
    mind.record(AffectiveEvent(command_id="rest",agent_version="energy-test",expected_revision=mind.read()["revision"],subjective=True,
        rhythm=RhythmProposal(phase="resting",alertness=20,target=5,half_life_minutes=60)))
    with mind.engine.db.connect(write=True) as conn:
        memory.commit_action(conn,[],"rest-plan",240,{})
    clock[0]+=timedelta(hours=1)
    ActionEvents(mind).crossings()
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_action_events WHERE kind='rhythm-review'").fetchone()[0]==0
    assert memory.due() is None
    clock[0]+=timedelta(hours=3)
    assert memory.due() is not None
    memory.configure({"idle":False})
    ActionEvents(mind).crossings()
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_action_events WHERE kind='rhythm-review'").fetchone()[0]==1


def test_plan_claim_is_not_spending_start_and_retries_share_one_effort(env):
    mind,_,_,_,initial=env
    energy.configure(mind,{"night_penalty":0,"awake_fatigue_per_hour":0,"recovery_per_hour":6,"enabled":True})
    plans=AutonomousPlans(mind)
    plan=plans.manage({"command_id":"plan","action":"create","key":"work","goal":"Make a clock","motivation":"interest",
        "reason":"test","evidence_ids":[initial],"steps":[{"id":"make","actor":"create","goal":"Draw clock","completion":"Saved svg"}]})
    def decide(plan,cmd):
        with mind.engine.db.connect(write=True) as conn:
            plans.decide(conn,{"plan_id":plan["id"],"step_id":"make","expected_revision":plan["revision"],"action":"execute",
                "reason":"test","evidence_ids":[initial]},cmd,{"provider":"deepseek","reasoning":"high","agent_version":"energy-test"},mind._evidence(conn,[initial]))
    decide(plan,"first")
    run=plans.claim(actor="create",owner="executor")["run"]
    assert mind.read()["energy_context"]["value"]==75
    assert plans.started(run["id"],"executor",1)["energy_context"]["value"]==69
    plans.started(run["id"],"executor",1)
    assert mind.read()["energy_context"]["value"]==69
    plans.settle(run["id"],"executor",1,state="failed",result={"reason":"temporary"})
    with mind.engine.db.connect() as conn:plan=plans.get(conn,plan["id"])
    decide(plan,"retry")
    retry=plans.claim(actor="create",owner="executor2")["run"]
    assert retry["id"]!=run["id"] and retry["energy_activity_id"]==run["energy_activity_id"]
    plans.started(retry["id"],"executor2",1)
    assert mind.read()["energy_context"]["value"]==69
    assert mind.read()["energy_context"]["active_execution_count"]==1
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("UPDATE mind_plan_runs SET lease_until=0 WHERE id=?",(retry["id"],))
    plans.recover(workers_stopped=True)
    assert mind.read()["energy_context"]["active_execution_count"]==0
    assert energy.activity(mind,{"kind":"work","activity_id":retry["energy_activity_id"],"execution_id":retry["id"],"execution_state":"started"})["energy_context"]["active_execution_count"]==0


def test_exploration_real_start_is_shared_once_across_failed_retry(env,tmp_path):
    from kin_mind.exploration import Explorations, EXPLORATION_RETRY_SECONDS
    from kin_mind.state import DesireChange
    mind,_,clock,_,initial=env
    energy.configure(mind,{"night_penalty":0,"awake_fatigue_per_hour":0,"recovery_per_hour":6,"enabled":True,"recovery_per_hour":0})
    mind.manage_desire(DesireChange(command_id="explore",agent_version="energy-test",expected_revision=mind.read()["revision"],
        evidence_ids=[initial],action="create",content="Study the clock",topic="clock",kind="explore",strength=80,
        expires_at=(clock[0]+timedelta(days=1)).isoformat(),completion="A sourced result",reason="test"))
    seen=[]
    def runner(executable,brief,directory,**kwargs):
        seen.append(brief["energy_context"]["value"])
        kwargs["on_started"]()
        kwargs["on_started"]()  # Repeated start callback is one logical activity.
        return {"state":"failed","reason":"native-run-incomplete","attempt":1,"partial":True}
    runner.reports_start=True
    assert Explorations(mind).run("unused",str(tmp_path/"runs"),"energy-test",runner=runner)["state"]=="failed"
    assert mind.read()["energy_context"]["value"]==71
    clock[0]+=timedelta(seconds=EXPLORATION_RETRY_SECONDS+60)
    assert Explorations(mind).run("unused",str(tmp_path/"runs"),"energy-test",runner=runner)["state"]=="failed"
    assert mind.read()["energy_context"]["value"]==71
    assert seen==[75,71]
    assert mind.read()["energy_context"]["active_execution_count"]==0
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_energy_events WHERE kind='explore'").fetchone()[0]==1


def test_host_dispatch_config_and_activity_share_same_projection(env,tmp_path):
    from kin_mind.host import dispatch, RESIDENT_ACTIONS
    mind,_,_,_,_=env
    config={"root":str(mind.engine.db.path.parent),"scope":mind.scope.model_dump(),"agent_version":"energy-test"}
    assert {"energy-configure","energy-activity","plan-started"}<=RESIDENT_ACTIONS
    assert dispatch(config,"energy-configure",{"enabled":True})["state"]=="configured"
    assert dispatch(config,"energy-activity",{"kind":"work","activity_id":"task:id:version"})["state"]=="applied"
    assert dispatch(config,"energy-activity",{"kind":"work","activity_id":"task:id:version"})["state"]=="duplicate"


def set_phase(mind,phase,key):
    return mind.record(AffectiveEvent(command_id=key,agent_version="energy-test",expected_revision=mind.read()["revision"],
        subjective=True,rhythm=RhythmProposal(phase=phase,alertness=20 if phase=="resting" else 70,target=20,half_life_minutes=60)))


def daytime(clock):
    # A future local noon, keeping synthetic evidence current while remaining deterministic.
    clock[0]=(clock[0]+timedelta(days=1)).replace(hour=4,minute=0,second=0,microsecond=0)


def test_circadian_daytime_idle_recovers_reserve_but_accumulates_awake_fatigue(env):
    mind,_,clock,_,_=env
    daytime(clock)
    energy.configure(mind,{"enabled":True})
    clock[0]+=timedelta(hours=4)
    v=mind.read()["energy_context"]
    assert (v["reserve"],v["fatigue"],v["night_penalty"],v["value"])==(79,8,0,71)
    assert v["mode"]=="awake" and v["timezone"]=="Asia/Singapore"
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_energy_events").fetchone()[0]==0


def test_midnight_crossing_and_late_work_never_turn_waiting_into_vigor(env):
    mind,_,clock,_,_=env
    daytime(clock)
    clock[0]+=timedelta(hours=10)  # Singapore 22:00
    energy.configure(mind,{"enabled":True})
    start=mind.read()["energy_context"]
    clock[0]+=timedelta(hours=4)  # next day 02:00
    before=mind.read()["energy_context"]
    assert (before["reserve"],before["fatigue"],before["night_penalty"],before["value"])==(79,8,20,51)
    assert before["local_time"][:10]!=start["local_time"][:10]
    assert energy.activity(mind,{"kind":"work","activity_id":"late-work"})["energy_context"]["value"]==45
    clock[0]+=timedelta(hours=2)
    assert mind.read()["energy_context"]["value"]==43
    assert energy.activity(mind,{"kind":"work","activity_id":"late-work"})["state"]=="duplicate"


def test_sleep_recovery_uses_real_phase_transition_and_does_not_reset_on_repeat(env):
    mind,_,clock,_,_=env
    daytime(clock)
    energy.configure(mind,{"enabled":True})
    clock[0]+=timedelta(hours=11) # 23:00, reserve86/fatigue22
    set_phase(mind,"resting","sleep")
    first=mind.read()["energy_context"]
    assert first["mode"]=="resting" and first["fatigue"]==22
    clock[0]+=timedelta(hours=1)
    set_phase(mind,"resting","still-sleeping")
    second=mind.read()["energy_context"]
    assert (second["reserve"],second["fatigue"])==(94,12)
    # Same mode is not a new energy clock or fatigue reset.
    assert second["revision"]==first["revision"]
    clock[0]+=timedelta(hours=7) # 07:00
    asleep=mind.read()["energy_context"]
    assert (asleep["reserve"],asleep["fatigue"],asleep["night_penalty"],asleep["value"])==(100,0,10,90)
    clock[0]+=timedelta(hours=1) # 08:00
    set_phase(mind,"awake","wake")
    assert mind.read()["energy_context"]["value"]==100
    clock[0]+=timedelta(hours=2)
    assert mind.read()["energy_context"]["value"]==96


@pytest.mark.parametrize("phase,expected_fatigue,expected_value",[("awake",40,60),("resting",0,100)])
def test_long_offline_and_reboot_follow_last_recorded_phase_not_guessed_sleep(env,phase,expected_fatigue,expected_value):
    mind,_,clock,_,_=env
    daytime(clock)
    set_phase(mind,phase,"before-enable")
    energy.configure(mind,{"enabled":True})
    assert mind.read()["energy_context"]["mode"]==phase # first enable while already resting
    clock[0]+=timedelta(days=30)
    before=mind.read()["energy_context"]
    assert (before["reserve"],before["fatigue"],before["value"])==(100,expected_fatigue,expected_value)
    restarted=Mind(mind.engine,mind.scope,clock=mind.clock)
    assert restarted.read()["energy_context"]==before
    for _ in range(3):
        assert restarted.read()["energy_context"]==before
    clock[0]-=timedelta(hours=1)
    assert restarted.read()["energy_context"]["value"]==expected_value


def test_timezone_and_night_curve_are_projection_only(env):
    mind,_,clock,_,_=env
    daytime(clock)
    clock[0]+=timedelta(hours=12) # Singapore midnight, UTC16:00
    energy.configure(mind,{"enabled":True,"recovery_per_hour":0,"awake_fatigue_per_hour":0})
    sg=mind.read()["energy_context"]
    assert (sg["reserve"],sg["night_penalty"],sg["value"])==(75,10,65)
    utc=energy.configure(mind,{"timezone":"UTC"})["energy_context"]
    assert (utc["reserve"],utc["fatigue"],utc["night_penalty"],utc["value"])==(75,0,0,75)
    energy.configure(mind,{"timezone":"Asia/Singapore"})
    clock[0]+=timedelta(hours=2)
    assert mind.read()["energy_context"]["night_penalty"]==20
    clock[0]+=timedelta(hours=4)
    assert mind.read()["energy_context"]["night_penalty"]==20
    clock[0]+=timedelta(hours=2)
    assert mind.read()["energy_context"]["value"]==75
    with pytest.raises(ValueError):energy.configure(mind,{"timezone":"Invalid/Zone"})


def test_dst_and_utc_elapsed_time_do_not_double_count_a_repeated_local_hour(env):
    mind,_,clock,_,_=env
    daytime(clock)
    # Next known future fall-back Sunday in New York. 05:30 and 06:30 UTC
    # are both 01:30 local, but elapsed fatigue is exactly one hour.
    clock[0]=datetime(2027,11,7,5,30,tzinfo=timezone.utc)
    energy.configure(mind,{"enabled":True,"timezone":"America/New_York"})
    first=mind.read()["energy_context"]
    clock[0]+=timedelta(hours=1)
    second=mind.read()["energy_context"]
    assert first["local_time"][11:16]==second["local_time"][11:16]=="01:30"
    assert second["fatigue"]==2 and second["reserve"]==76
    assert first["night_penalty"]==second["night_penalty"]==17.5


def execution_receipt(mind,key,state,activity="long-work"):
    return energy.activity(mind,{"kind":"work","activity_id":activity,"execution_id":key,"execution_state":state})


def test_actual_work_overrides_resting_until_all_executions_finish(env):
    mind,_,clock,_,_=env
    daytime(clock)
    clock[0]+=timedelta(hours=10)  # Singapore 22:00: resting plus actual overnight work.
    set_phase(mind,"resting","rest-before-work")
    energy.configure(mind,{"enabled":True})
    execution_receipt(mind,"run-1","started")
    execution_receipt(mind,"run-2","started",activity="other-work")
    clock[0]+=timedelta(hours=2)
    set_phase(mind,"resting","still-resting-but-working")
    v=mind.read()["energy_context"]
    assert (v["reserve"],v["fatigue"],v["mode"],v["rhythm_mode"],v["active_execution_count"])==(65,4,"awake","resting",2)
    execution_receipt(mind,"run-1","finished")
    assert mind.read()["energy_context"]["mode"]=="awake"
    clock[0]+=timedelta(hours=1)
    execution_receipt(mind,"run-2","finished",activity="other-work")
    ended=mind.read()["energy_context"]
    assert (ended["reserve"],ended["fatigue"],ended["mode"],ended["active_execution_count"])==(66,6,"resting",0)
    clock[0]+=timedelta(hours=1)
    assert (mind.read()["energy_context"]["reserve"],mind.read()["energy_context"]["fatigue"])==(74,0)
    assert mind.read()["energy_context"]["night_penalty"]==20
    assert mind.read()["energy_context"]["value"]==54


def test_terminal_before_delayed_start_reboot_and_retry_do_not_leave_stale_busy(env):
    mind,_,clock,_,_=env
    daytime(clock)
    set_phase(mind,"resting","rest")
    energy.configure(mind,{"enabled":True})
    terminal=execution_receipt(mind,"run-finished","finished")
    assert terminal["energy_context"]["reserve"]==69 # actual terminal proves one execution
    reboot=Mind(mind.engine,mind.scope,clock=mind.clock)
    late=execution_receipt(reboot,"run-finished","started")
    assert late["state"]=="duplicate" and late["energy_context"]["active_execution_count"]==0
    execution_receipt(reboot,"retry-new-run","started") # same logical cost; distinct execution
    assert reboot.read()["energy_context"]["reserve"]==69
    assert reboot.read()["energy_context"]["active_execution_count"]==1
    energy.configure(reboot,{"enabled":False})
    execution_receipt(reboot,"retry-new-run","finished")
    energy.configure(reboot,{"enabled":True})
    assert reboot.read()["energy_context"]["active_execution_count"]==0
    assert reboot.read()["energy_context"]["mode"]=="resting"
    execution_receipt(reboot,"retry-new-run","started")
    assert reboot.read()["energy_context"]["active_execution_count"]==0


def test_overlapping_retries_share_cost_but_keep_both_execution_lifetimes(env):
    mind,_,clock,_,_=env
    daytime(clock)
    set_phase(mind,"resting","rest")
    energy.configure(mind,{"enabled":True})
    execution_receipt(mind,"retry-one","started")
    execution_receipt(mind,"retry-two","started")
    assert (mind.read()["energy_context"]["reserve"],mind.read()["energy_context"]["active_execution_count"])==(69,2)
    execution_receipt(mind,"retry-one","finished")
    assert mind.read()["energy_context"]["active_execution_count"]==1
    assert mind.read()["energy_context"]["mode"]=="awake"
    execution_receipt(mind,"retry-two","finished")
    assert mind.read()["energy_context"]["mode"]=="resting"
    with mind.engine.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM mind_energy_events").fetchone()[0]==1


@pytest.mark.parametrize("terminal",["settle","reclaim"])
@pytest.mark.parametrize("receipt_first",[True,False])
def test_plan_fallback_start_receipt_closes_even_without_plan_started_marker(env,terminal,receipt_first):
    mind,_,clock,_,initial=env
    daytime(clock)
    set_phase(mind,"resting","rest-for-fallback")
    energy.configure(mind,{"enabled":True})
    plans=AutonomousPlans(mind)
    plan=plans.manage({"command_id":"fallback-plan","action":"create","key":"fallback-work","goal":"Make a clock",
        "motivation":"interest","reason":"test","evidence_ids":[initial],
        "steps":[{"id":"make","actor":"create","goal":"Draw clock","completion":"Saved svg"}]})
    with mind.engine.db.connect(write=True) as conn:
        plans.decide(conn,{"plan_id":plan["id"],"step_id":"make","expected_revision":plan["revision"],"action":"execute",
            "reason":"test","evidence_ids":[initial]},"fallback-first",{"provider":"deepseek","reasoning":"high","agent_version":"energy-test"},mind._evidence(conn,[initial]))
    run=plans.claim(actor="create",owner="executor")["run"]
    assert "execution_started_at" not in run
    if receipt_first:
        energy.activity(mind,{"kind":"work","activity_id":run["energy_activity_id"],"execution_id":run["id"],"execution_state":"started"})
    assert mind.read()["energy_context"]["active_execution_count"]==int(receipt_first)
    clock[0]+=timedelta(hours=1)
    if terminal=="settle":
        plans.settle(run["id"],"executor",1,state="failed",result={"reason":"temporary"})
    else:
        with mind.engine.db.connect(write=True) as conn:
            conn.execute("UPDATE mind_plan_runs SET lease_until=0 WHERE id=?",(run["id"],))
        plans.recover(workers_stopped=True)
    view=mind.read()["energy_context"]
    assert (view["active_execution_count"],view["mode"],view["reserve"],view["fatigue"])==((0,"resting",70,2) if receipt_first else (0,"resting",83,0))
    energy.activity(mind,{"kind":"work","activity_id":run["energy_activity_id"],"execution_id":run["id"],"execution_state":"started"})
    assert mind.read()["energy_context"]["active_execution_count"]==0
    assert mind.read()["energy_context"]["reserve"]==(70 if receipt_first else 77)


def test_dead_exploration_receipt_is_reclaimed_without_leaving_busy(env):
    from kin_mind.exploration import Explorations
    mind,_,clock,_,_=env
    daytime(clock)
    set_phase(mind,"resting","rest-for-recovery")
    energy.configure(mind,{"enabled":True})
    explorer=Explorations(mind)
    receipt={"energy_activity_id":"wish:recovery","execution_started_at":mind.clock(),
             "liveness":{"pid":999999,"started":None,"deadline":0}}
    with mind.engine.db.connect(write=True) as conn:
        conn.execute("INSERT INTO mind_explorations VALUES(?,?,?,?,?)",("explore-dead",mind.scope.key(),"running",mind.clock(),json.dumps(receipt)))
        energy.execution(conn,mind,"explore","wish:recovery","explore-dead","started")
    clock[0]+=timedelta(hours=1)
    assert explorer.reclaim_dead()==["explore-dead"]
    view=mind.read()["energy_context"]
    assert (view["mode"],view["active_execution_count"],view["reserve"],view["fatigue"])==("resting",0,72,2)
    assert explorer.reclaim_dead()==[]
    energy.activity(mind,{"kind":"explore","activity_id":"wish:recovery","execution_id":"explore-dead","execution_state":"started"})
    assert mind.read()["energy_context"]["active_execution_count"]==0
