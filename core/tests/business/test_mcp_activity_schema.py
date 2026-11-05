import asyncio

from eventmem.core import Engine
from eventmem.core.mcp import create_mcp


def test_activity_focus_list_has_element_schema_and_keeps_supported_references(tmp_path):
    server = create_mcp(Engine(tmp_path))
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == 'read_continuity_context')
    focus = tool.inputSchema['properties']['activity_focus']
    array = next(branch for branch in focus['anyOf'] if branch.get('type') == 'array')
    assert array['items'], 'Gemini rejects an empty array element schema before generation'
    assert {branch['type'] for branch in array['items']['anyOf']} == {'object', 'string'}
    fn = server._tool_manager.get_tool('read_continuity_context')
    references = ['activity:synthetic', {'kind': 'exploration', 'id': 'synthetic'}]
    args = fn.fn_metadata.arg_model.model_validate({'scope': {}, 'query': 'synthetic', 'activity_focus': references})
    assert args.activity_focus == references
