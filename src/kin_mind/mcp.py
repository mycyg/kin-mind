"""Three model-facing tools. Delivery receipts remain a host-only operation."""

from eventmem.core.models import Scope

from .state import AffectiveEvent, DesireChange, Mind


def register_mind_tools(server, engine):
    @server.tool()
    def read_affective_state(scope: Scope, history: int = 0) -> dict:
        """Read shared affect, desires and personality provenance before replying. Defaults are role configuration; inferred scores are not measured feelings. Use the returned revision for changes. Show scores only when requested. Review-marked state must not guide behavior. history is bounded 0..100."""
        return Mind(engine, scope).read(history=history)

    @server.tool()
    def record_affective_event(scope: Scope, event: AffectiveEvent) -> dict:
        """Appraise new source-backed evidence in this same agent turn. Use current revision, actual agent_version and original evidence IDs. Supply only dimensions with new evidence; do not rescore a repeated event or raise grievance/possessiveness/reassurance from silence. Explain the source-linked state change briefly. An evolution proposal uses existing prospective self-knowledge records and preserves its hypothesis status; revert_event_id requires a later explicit correction."""
        return Mind(engine, scope).record(event)

    @server.tool()
    def manage_desire(scope: Scope, request: DesireChange) -> dict:
        """Create or change a source-backed contact, exploration or creative desire. Include strength, expiry and a concrete completion condition. User-assigned work remains a task, not a cancellable mood-dependent desire. Completion is based on actual results. These tools never send messages; the owner-bound host checks threshold, quiet hours, fresh context and delivery receipts."""
        return Mind(engine, scope).manage_desire(request)
