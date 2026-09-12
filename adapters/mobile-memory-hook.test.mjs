import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';

test('host event receipts separate original owner input from internal turns and unknown markers',()=>{
  const result=execFileSync('python3',['-c',String.raw`
import importlib.util,json,tempfile
from pathlib import Path
spec=importlib.util.spec_from_file_location('mobile_hook','adapters/mobile-memory-hook.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as root:
 p=Path(root);marker='a'*32
 raw={'hook_event_name':'UserPromptSubmit','session_id':'synthetic','turn_id':'one','prompt':'Injected status and memory <kin-host-event>'+marker+'</kin-host-event>'}
 assert m.filtered_event(raw,p) is None
 record={'sessionId':'synthetic','kind':'owner','text':'The actual owner message'}
 (p/(marker+'.json')).write_text(json.dumps(record))
 assert m.filtered_event(raw,p)['prompt']=='The actual owner message'
 assert m.filtered_event({'hook_event_name':'Stop','session_id':'synthetic','turn_id':'one'},p)
 assert m.filtered_event({'hook_event_name':'Stop','session_id':'synthetic','turn_id':'other'},p) is None
 record['kind']='internal';(p/(marker+'.json')).write_text(json.dumps(record))
 assert m.filtered_event(raw,p) is None
 assert m.filtered_event({'hook_event_name':'Stop','session_id':'synthetic','turn_id':'one'},p) is None
 assert m.filtered_event({'hook_event_name':'PostToolUse','session_id':'synthetic','turn_id':'one'},p) is None
 record['sessionId']='other';(p/(marker+'.json')).write_text(json.dumps(record))
 assert m.filtered_event(raw,p) is None
 print('ok')
`],{encoding:'utf8'});
  assert.equal(result.trim(),'ok');
});
