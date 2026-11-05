"""Core's own tools in KIN's DSH conversations.

The DSH plugin mounts these through DSH's MCP tool bridge, inside the KIN preset only, and runs each
call in the resident worker. They are the MCP server's tools (`eventmem.core.mcp.create_mcp`, which
registers `mcp.register_mind_tools` too) as declared there, with one difference: the scope is this
store's, fixed here, so it leaves the schema and the model neither pays for nor fills it. Answers are
shown as `model_view` shows every tool's answer.

Every schema rides on every request of hers. She gets each tool that is something she can do while
talking. Left out: what the plugin gives her otherwise (`kin_recall` for recall, `kin_reply` for a reply
choice bound to its turn, her state with every turn), what her reviews already do on the background lane
(appraising each message), what needs the host's delivery ledger or contact policy (reply references,
reminders and contact tasks: the contact module's), and operator or destructive actions.
"""
from __future__ import annotations

import asyncio
import copy
import json

from mcp.server.fastmcp.exceptions import ToolError

CHAT_TOOLS = (
    # Plans, wishes, concerns, and the habits the owner asks of her.
    "read_autonomous_plans", "manage_autonomous_plan", "manage_desire", "manage_concern", "update_conversation_habits",
    # Her state with its revisions and ids, the traits she is growing into, and what she believes of herself.
    "read_affective_state", "read_trait_ledger", "read_self_knowledge", "record_self_claim", "predict_self_behavior",
    "assess_self_prediction",
    # Her memory past recall: events and people, originals, old records, topics, methods, what she shared and made.
    "read_graph", "read_event_thread", "read_memory", "read_archived_record", "browse_topics", "read_procedure_memory",
    "read_share_history", "read_work_history",
    # Keeping memory right: marking what to keep, what not to bring up again, a memory set right where it is
    # kept (`correct_memory`, which her page's 记错了 uses too), and the graph's own merges and splits.
    "mark_memorable", "set_memory_quiet", "correct_memory", "revise_graph",
    # Her inner world: notes to herself, dreams, diary and sealed letters.
    "read_private_notes", "read_dreams", "read_diary", "read_letters",
)


def _server(engine):
    from eventmem.core.mcp import create_mcp
    return create_mcp(engine)


def _unscoped(schema):
    if "scope" not in schema.get("properties", {}):
        return schema
    schema = copy.deepcopy(schema)
    schema["properties"].pop("scope")
    schema["required"] = [name for name in schema.get("required", []) if name != "scope"]
    defs = schema.get("$defs", {})
    if "#/$defs/Scope" not in json.dumps(schema["properties"]) and defs.pop("Scope", None) is not None and not defs:
        del schema["$defs"]
    return schema


def tools(engine):
    """The chat tools as MCP declares them: name, description and input schema."""
    return [{"name": tool.name, "description": tool.description, "inputSchema": _unscoped(tool.parameters)}
            for tool in _server(engine)._tool_manager.list_tools() if tool.name in CHAT_TOOLS]


def call(engine, scope, name, arguments):
    """One of the MCP server's tools, called in this store's scope, answered as the MCP server answers a
    call: its result as text, or what was wrong with the call (`isError`), so she can make it again. The
    host may call any of them; the model is offered CHAT_TOOLS."""
    tool = _server(engine)._tool_manager.get_tool(name)
    if tool is None:
        raise ValueError("Unknown mind tool")
    scoped = {"scope": scope} if "scope" in tool.parameters.get("properties", {}) else {}
    try:
        result = asyncio.run(tool.run({**(arguments or {}), **scoped}))
    except ToolError as error:
        return {"content": [{"type": "text", "text": str(error)}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]}
