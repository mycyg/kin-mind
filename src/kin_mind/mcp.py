"""Shared state and continuity tools. Delivery remains a host-only operation."""

from eventmem.core.models import Scope

from .continuity import ConcernChange
from .state import AffectiveEvent, DesireChange, Mind


def register_mind_tools(server, engine):
    @server.tool()
    def read_affective_state(scope: Scope, history: int = 0, query: str = "") -> dict:
        """Read shared affect, wishes, concerns, rhythm and expression before replying. query selects up to three relevant concerns; expression provides up to three current tendencies. Defaults are role configuration; inferred scores are not measured feelings. Use the returned revision for changes. Show scores and provenance when asked; history is bounded 0..100. Review-marked data requires clarification. The core persona stays separate from these dynamic results."""
        return Mind(engine, scope).read(history=history, query=query)

    @server.tool()
    def manage_concern(scope: Scope, request: ConcernChange) -> dict:
        """Manage a sourced concern: create/update/ease/resolve/reopen/archive. Concerns include care, anticipation, curiosity, distress and shared plans. Use current revision and agent_version; preserve explicit/inferred/internal_thought basis and confidence. A sent wish does not resolve its concern. New outcome evidence supports resolution; repeated source summaries cannot reinforce intensity. Returns a durable concern ID and revision. This tool never sends a message."""
        return Mind(engine, scope).manage_concern(request)

    @server.tool()
    def record_affective_event(scope: Scope, event: AffectiveEvent) -> dict:
        """Appraise new source-backed evidence in this same agent turn. Use current revision, actual agent_version and original evidence IDs. Supply only dimensions with new evidence; do not rescore a repeated event or raise grievance/possessiveness/reassurance from silence. Explain the source-linked state change briefly. An evolution proposal uses existing prospective self-knowledge records and preserves its hypothesis status; revert_event_id requires a later explicit correction."""
        return Mind(engine, scope).record(event)

    @server.tool()
    def manage_desire(scope: Scope, request: DesireChange) -> dict:
        """Create or change a source-backed contact, exploration or creative desire. Include strength, expiry and a concrete completion condition. User-assigned work remains a task, not a cancellable mood-dependent desire. Completion is based on actual results. These tools never send messages; the owner-bound host checks threshold, quiet hours, fresh context and delivery receipts."""
        return Mind(engine, scope).manage_desire(request)
