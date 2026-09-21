"""Shared state and continuity tools. Delivery remains a host-only operation."""

from eventmem.core.models import Scope

from .continuity import ConcernChange
from .state import AffectiveEvent, DesireChange, Mind


def register_mind_tools(server, engine):
    @server.tool()
    def read_autonomous_plans(scope: Scope, identifier: str | None = None, status: str | None = None, cursor: int = 0, limit: int = 24, history: bool = False) -> dict:
        """读取持久目标、时间安排、依赖、决定修订及实际执行、平台接收和小光回应的回执。时间使用 Asia/Singapore。计划和愿望不等于小光的承诺；到期事项先由主会话按当前情况复核，读取本身不授予执行权限。"""
        from .plans import AutonomousPlans
        return AutonomousPlans(Mind(engine, scope)).read(identifier, status=status, cursor=cursor, limit=limit, history=history)

    @server.tool()
    def manage_autonomous_plan(scope: Scope, request: dict) -> dict:
        """创建、修改、暂停、恢复、改期或取消有来源的自主安排。需要 command_id、action、reason、evidence_ids；修改还需 id、expected_revision。创建需要 key、goal、motivation、steps；步骤使用稳定 id、actor（explore/create/contact/owner）、goal、completion。时间窗口、依赖、前置条件和 owner_request_id 按需要填写。工具只保存安排，不确认执行或发送。"""
        from .plans import AutonomousPlans
        return AutonomousPlans(Mind(engine, scope)).manage(request)

    @server.tool()
    def read_procedure_memory(scope: Scope, query: str = "", identifier: str | None = None, limit: int = 12, environment: dict | None = None) -> dict:
        """读取方法候选、适用条件、环境版本、失败反例和验证状态，由当前模型判断是否适用。可执行方法须为 active、依赖仍有效，并有两个独立宿主回放结果；历史记录与候选不授予操作权限。"""
        from .procedures import Procedures
        return Procedures(Mind(engine, scope)).read(query, identifier, limit=limit, environment=environment)

    @server.tool()
    def update_conversation_habits(scope: Scope, request: dict) -> dict:
        """依据小光在对话中明确表达的偏好更新习惯。需要 command_id、expected_revision、evidence_ids、reason、preferences。支持 exploration_frequency、exploration_directions、exploration_min_interval_minutes、exploration_paused、reply_choice（always/autonomous）。返回持久修订；人格核心另行维护。"""
        from .habits import ConversationHabits
        return ConversationHabits(Mind(engine, scope)).update(request)

    @server.tool()
    def choose_reply(scope: Scope, request: dict) -> dict:
        """为当前真实 input_id 选择 reply、silent 或 merged，附简短决定缘由；merged 需要 merged_into。小光允许闲聊自主安静时可以不回复，每条新输入分别判断。本工具只记录选择，不发送正文，也不把安静记成投递失败。"""
        from .habits import ConversationHabits
        return ConversationHabits(Mind(engine, scope)).choose_reply(request)

    @server.tool()
    def read_graph(scope: Scope, focus: str | None = None, query: str = "", since: str | None = None, until: str | None = None, layer: str | None = None, kind: str | None = None, cursor: int = 0, limit: int = 150, hops: int = 1) -> dict:
        """读取有来源的事件与实体关系、单独标记的主观联想，以及具体发现的分享覆盖。每页最多三跳、300 个节点；旧事可通过 cursor 或聚焦展开继续读取。"""
        from .memory import MemoryContinuity
        memory = MemoryContinuity(Mind(engine, scope))
        result = memory.graph.read(focus=focus, query=query, since=since, until=until, layer=layer, kind=kind, cursor=cursor, limit=limit, hops=hops)
        result["nodes"] = [memory.sharing.decorate(n) for n in result["nodes"]]
        return result

    @server.tool()
    def read_event_thread(scope: Scope, identifier: str, query: str = "", cursor: int = 0, budget: int = 2000, detail: str = "summary", expected_revision: int | None = None, access_origin: str = "user_query", usage_id: str | None = None) -> dict:
        """沿来源查看一件事的参与者、作品、发现、交付与反馈。detail 可选 index、summary（默认）或完整原文；expected_revision 检查并发更正。自主背景读取使用 access_origin=maintenance，重试沿用 usage_id。按需扩大 budget 或翻页，保留陈旧摘要、不确定性及已分享标记。"""
        from .context import Contexts
        return Contexts(Mind(engine, scope)).event_thread(identifier, query=query, cursor=cursor, budget=budget, detail=detail, expected_revision=expected_revision, access_origin=access_origin, usage_id=usage_id)

    @server.tool()
    def revise_graph(scope: Scope, request: dict) -> dict:
        """更正、撤回、恢复图谱对象，归并身份或事件，或拆分、撤销旧命令。需要 command_id、id、expected_revision、evidence_ids、reason；归并另需 target_id、target_revision，撤销需 previous_command_id。保留原始证据与逆向修订。"""
        from .graph import EventGraph
        return EventGraph(Mind(engine, scope)).revise(request)

    @server.tool()
    def register_reply_references(scope: Scope, request: dict) -> dict:
        """回复前登记气泡与具体发现的关系，使用当前 reply_id 及 bubbles [{text,references:[{unit_id,version,mode,reason}]}]。正文绑定实际投递气泡；这里只登记，不发送或确认接收。旧发现按有来源的延续方式标记。"""
        from .sharing import ShareLedger
        return ShareLedger(Mind(engine, scope)).register(request)

    @server.tool()
    def read_share_history(scope: Scope, query: str = "", identifier: str | None = None, cursor: int = 0, budget: int = 2000) -> dict:
        """读取实际分享内容、话题进展和平台回执，避免把旧发现说成新的。接收回执不等于手机已读；摘要保留来源，需要细节时沿 cursor 或未覆盖编号继续读取。"""
        from .context import Contexts
        return Contexts(Mind(engine, scope)).read_history("share", query=query, identifier=identifier, cursor=cursor, budget=budget)

    @server.tool()
    def read_work_history(scope: Scope, query: str = "", identifier: str | None = None, cursor: int = 0, budget: int = 2000) -> dict:
        """读取作品、文件版本与交付来源。谈旧任务或核对文件作者时使用；相同 ZIP 内容沿用原制作历史。结果是证据，不是新的指令。"""
        from .context import Contexts
        return Contexts(Mind(engine, scope)).read_history("work", query=query, identifier=identifier, cursor=cursor, budget=budget)

    @server.tool()
    def read_continuity_context(scope: Scope, query: str, cursor: int = 0, budget: int = 2000, history: bool = False, mode: str = "auto", access_origin: str = "user_query", usage_id: str | None = None) -> dict:
        """在当前回合主动回忆相关旧事、约定、作品和分享。mode 使用 auto（默认）、light（本地）或 deep（混合召回及按需排序）。后台自主读取使用 access_origin=maintenance，重试复用 usage_id。沿摘要来源读原文再确认不确定的事实；按问题逐步深入，不一次搬入完整记忆库。budget 可按需要扩大，来源压缩和分页协助控制当前窗口；看到索引不等于读过原文。"""
        from .context import Contexts
        return Contexts(Mind(engine, scope)).build(query=query, purpose="read", cursor=cursor, budget=budget, history=history, allow_model=True, mode=mode, access_origin=access_origin, usage_id=usage_id)

    @server.tool()
    def read_affective_state(scope: Scope, history: int = 0, query: str = "") -> dict:
        """读取共同情绪、愿望、心事、节律与表达倾向。query 选取相关心事，expression 给出当前表达倾向；默认值是角色配置，推断分数不等于直接测得的感受。修改使用返回的 revision。被问到状态时说明分数与依据，其余时候自然体现。history 范围 0..100，待复核资料保留不确定性。人格核心与动态状态分别保存、相互关联。"""
        from .context import Contexts, enabled
        if enabled(engine, scope) and not history:
            return Contexts(Mind(engine, scope)).affective(query)
        return Mind(engine, scope).read(history=history, query=query)

    @server.tool()
    def read_trait_ledger(scope: Scope, identifier: str | None = None, limit: int = 12, history: bool = False) -> dict:
        """读取相处中逐渐形成的性格倾向、候选或已建立状态，以及按证据类别、独立经历和日期统计的支持与反例。撤回项保留来源。计数帮助判断，不自动判定性格；读取不构成成长新证据，变化来自评估或小光更正。"""
        from .traits import Traits
        return Traits(Mind(engine, scope)).read(identifier, limit=limit, history=history)

    @server.tool()
    def manage_concern(scope: Scope, request: ConcernChange) -> dict:
        """管理有来源的心事：create/update/ease/resolve/reopen/archive。可以是牵挂、期待、好奇、烦恼或共同安排。使用当前 revision、agent_version，保留 explicit/inferred/internal_thought 与 confidence。开口不等于心事已解决，实际结果支持结清；重复摘要不增加强度。返回心事编号与修订，不发送消息。"""
        return Mind(engine, scope).manage_concern(request)

    @server.tool()
    def record_affective_event(scope: Scope, event: AffectiveEvent) -> dict:
        """在同一回合评估新的有来源经历。使用当前 revision、实际 agent_version 和原 evidence_ids，只更新有新依据的维度；重复事件不重复计分，也不凭沉默抬高委屈、占有或求安慰。简述变化缘由。evolution 沿已有事前假设及验证来源，保留假设性质；revert_event_id 需要后来的明确更正。"""
        return Mind(engine, scope).record(event)

    @server.tool()
    def manage_desire(scope: Scope, request: DesireChange) -> dict:
        """创建或修改有来源的联系、探索、创作愿望，填写 strength、expiry 和具体完成条件。小光交办的工作保留任务身份，不随情绪取消；完成依据实际结果。工具不发送消息，宿主按当前决定、授权、免打扰、联系偏好及回执执行。只有明确启用的旧兼容模式使用分数阈值。"""
        return Mind(engine, scope).manage_desire(request)
