import hashlib
import json

import pytest

from eventmem.core.db import Missing
from kin_mind.reply_review import ReplyReviews

from test_event_graph import system

CORE = "【MY_PERSONA_LOAD】synthetic core voice rules【/MY_PERSONA_LOAD】"
VOICE = "Short, warm, playful sentences."


def install_persona(mind):
    policy = {"schema": 1, "scope": mind.scope.model_dump(), "version": "persona-test", "approved_source": "synthetic",
              "requires_owner_confirmation": True, "mutable_trait_keys": [],
              "core": CORE, "voice": VOICE, "maintenance": "Keep it."}
    for name in ("core", "voice", "maintenance"):
        policy[name + "_sha256"] = hashlib.sha256(policy[name].encode()).hexdigest()
    (mind.engine.db.root / "persona-policy.json").write_text(json.dumps(policy))


def test_text_repair_reads_owner_input_and_never_executes_or_sends(system):
    mind, memory, *_ = system
    install_persona(mind)
    memory.ingest({'id': 'input', 'kind': 'owner-message', 'at': mind.clock(), 'text': '再玩一次昨天的游戏嘛'})
    class Provider:
        def structured(self, name, schema, prompt, context):
            assert context['original_input'] == '再玩一次昨天的游戏嘛'
            assert context['unsent_draft'] == 'malformed output'
            assert context['already_sent'] == ['我来啦']
            # E3-24: the repair is written in Kin's confirmed voice.
            assert CORE in prompt and VOICE in prompt
            return schema(bubbles=['好呀，接着玩～']), {'usage': {'output_tokens': 12}}
    result = ReplyReviews(memory.sharing).regenerate({'input_id': 'input', 'reason': 'format', 'draft': 'malformed output', 'sent': ['我来啦']}, Provider())
    assert result['bubbles'] == ['好呀，接着玩～']
    assert result['receipt']['usage']['output_tokens'] == 12
    with mind.engine.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM mind_share_coverage').fetchone()[0] == 0


def test_no_repair_is_written_without_the_persona_canon(system):
    mind, memory, *_ = system
    memory.ingest({'id': 'input', 'kind': 'owner-message', 'at': mind.clock(), 'text': 'hi'})
    class Provider:
        def structured(self, *args, **kwargs):
            raise AssertionError('no model call without the canon')
    with pytest.raises(Missing):
        ReplyReviews(memory.sharing).regenerate({'input_id': 'input', 'reason': 'format', 'draft': 'x', 'sent': []}, Provider())
