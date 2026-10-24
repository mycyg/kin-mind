"""Actual MCP process, no model or network, one persistent tool session."""
import io
import json
import os
import sys
from kin_mind.native_execution import native_exploration_exchange


def test_native_exchange_uses_real_mcp_and_retains_tool_state(tmp_path,monkeypatch):
    server=tmp_path/'server.py'
    server.write_text('''from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent
s=FastMCP('synthetic')
counter=0
@s.tool()
def increment() -> dict:
 global counter
 counter+=1
 return {'value':counter}
s.run(transport='stdio')
''')
    frames=[{'native_tool':{'id':str(i),'server':'synthetic','name':'increment','arguments':{}}} for i in [1,2]]
    frames+=[{'native_tool':{'id':'3','server':'synthetic','name':'unadvertised','arguments':{}}},
             {'id':'test-exec','state':'complete','result':{},'receipt':{'native_session_id':'same-main'}}]
    incoming=io.StringIO(''.join(json.dumps(f)+'\n' for f in frames));out=io.StringIO()
    monkeypatch.setattr(sys,'stdin',incoming);monkeypatch.setattr(sys,'stdout',out)
    answer=native_exploration_exchange({'id':'test-exec','system':'Synthetic','profile':{'model':'synthetic'}},
        {'synthetic':{'command':sys.executable,'args':[str(server)],'env':dict(os.environ)}})
    assert answer['receipt']['native_session_id']=='same-main'
    observed=[json.loads(line) for line in out.getvalue().splitlines()]
    assert observed[0]['native_review']['execution_tools'][0]['name']=='increment'
    assert [(f['native_tool_result']['result'].get('structuredContent') or json.loads(f['native_tool_result']['result']['content'][0]['text']))['value'] for f in observed[1:3]]==[1,2]
    assert observed[3]['native_tool_result']['result']['isError'] is True
