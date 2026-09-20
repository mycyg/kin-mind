from kin_mind.reply_review import ReplyReviews

from test_event_graph import system

def test_text_repair_reads_owner_input_and_never_executes_or_sends(system):
    mind, memory, *_ = system
    memory.ingest({'id': 'input', 'kind': 'owner-message', 'at': mind.clock(), 'text': '再玩一次昨天的游戏嘛'})
    class Provider:
        def structured(self, name, schema, prompt, context):
            assert context['original_input'] == '再玩一次昨天的游戏嘛'
            assert context['unsent_draft'] == 'malformed output'
            assert context['already_sent'] == ['我来啦']
            return schema(bubbles=['好呀，接着玩～']), {'usage': {'output_tokens': 12}}
    result = ReplyReviews(memory.sharing).regenerate({'input_id': 'input', 'reason': 'format', 'draft': 'malformed output', 'sent': ['我来啦']}, Provider())
    assert result['bubbles'] == ['好呀，接着玩～']
    assert result['receipt']['usage']['output_tokens'] == 12
    with mind.engine.db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM mind_share_coverage').fetchone()[0] == 0
