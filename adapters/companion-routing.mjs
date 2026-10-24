/** Trusted identities and frozen delivery routes. No model or network calls. */
import fs from 'node:fs';
import {createHash} from 'node:crypto';
const copy=x=>structuredClone(x);
const token=(s,prefix)=>typeof s==='string'&&new RegExp('^'+prefix+'[A-Za-z0-9_-]+$').test(s);
export const XIAOGUANG='person:xiaoguang',YIYI='person:yiyi',FAMILY='group:family';
export const conversationKey=(app,chat)=>'feishu:'+app+':'+chat;
export const messageKey=(app,id)=>'inj_feishu_'+createHash('sha256').update(app+':'+id).digest('hex').slice(0,32);
export function companionSeed() {return {schema:1,enabled:false,revision:1,people:{
  [XIAOGUANG]:{name:'小光',aliases:['小光','Ica'],sharing:'ordinary-shared',binding:null,proactive:false},
  [YIYI]:{name:'杨一一',address:'一一',aliases:['杨一一','一一','老大'],sharing:'pending',binding:null,proactive:false}},
  groups:{[FAMILY]:{name:'家庭群',members:[XIAOGUANG,YIYI],binding:null,mention_only:false}},
  relationshipFacts:[{speaker_person_id:XIAOGUANG,basis:'explicit',source:'Sentinel_ab62d0e51c4c81919c9520784e28070c',
    text:'杨一一通常称呼一一，是小光的真人伴侣、家里管事的人；小光说自己见她很怂。KIN可以向一一撒娇、吐槽小光，也可以向小光吐槽一一。'},{"speaker_person_id": "person:xiaoguang", "basis": "explicit", "source": "Sentinel_9940528b58808191bee1249020058e22", "kin_birthday": "2026-09-10", "met_xiaoguang": "2026-09-10", "met_yiyi": "2026-10-22", "calendar_timezone": "Asia/Shanghai", "text": "KIN生日与认识小光的日期是2026-09-10；认识一一的设定日期是2026-10-22（UTC+8日历日期）。一一的直接互动以接入后的真实消息为准；此前提及一一的记录保留小光转述或其它实际来源，不表示已经与一一直接相处。"}]};}
export function validateDirectory(d) {
  if(d?.schema!==1||typeof d.enabled!=='boolean'||!Number.isSafeInteger(d.revision)||d.revision<1)throw Error('companion-directory-invalid');
  if(!d.people||Object.keys(d.people).sort().join()!==[XIAOGUANG,YIYI].sort().join())throw Error("companion-people-required");
  const seen=new Set(),chats=new Set();
  for(const [id,p] of Object.entries(d.people??{})) {
    if(![XIAOGUANG,YIYI].includes(id)||!p.name||!['pending','private','ordinary-shared'].includes(p.sharing))throw Error('companion-person-invalid');
    if(!p.binding)continue;
    const b=p.binding,key=b.app_id+':'+b.open_id;
    if(!token(b.app_id,'cli_')||!token(b.open_id,'ou_')||!token(b.chat_id,'oc_')||!Number.isSafeInteger(b.revision)||b.revision<1||!b.confirmation_source_id||seen.has(key)||chats.has(b.app_id+':'+b.chat_id))throw Error('companion-binding-invalid');
    seen.add(key);chats.add(b.app_id+':'+b.chat_id);
  }
  for(const [id,g] of Object.entries(d.groups??{})) {
    if(id!==FAMILY||!g.name||!Array.isArray(g.members)||!g.members.length||new Set(g.members).size!==g.members.length||g.members.some(p=>![XIAOGUANG,YIYI].includes(p)))throw Error('companion-group-invalid');
    if(!g.binding)continue;
    const b=g.binding;
    if(!token(b.app_id,'cli_')||!token(b.chat_id,'oc_')||!token(b.bot_open_id,'ou_')||!Number.isSafeInteger(b.revision)||b.revision<1||!b.confirmation_source_id||chats.has(b.app_id+':'+b.chat_id)||g.members.some(p=>d.people[p].binding?.app_id!==b.app_id))throw Error('companion-group-binding-invalid');
    chats.add(b.app_id+':'+b.chat_id);
  }
  return d;
}
export class CompanionDirectory {
  constructor(read){this.read=typeof read==='string'?()=>JSON.parse(fs.readFileSync(read,'utf8')):read;}
  snapshot(){return copy(validateDirectory(this.read()));}
  resolve(alias){const d=this.snapshot(),matches=Object.entries(d.people).filter(([id,p])=>id===alias||p.name===alias||p.address===alias||(p.aliases??[]).includes(alias));if(matches.length!==1)throw Error('companion-recipient-ambiguous');return matches[0][0];}
  targets(){const d=this.snapshot();return {...d.people,...d.groups};}
  target(id){return this.targets()[id];}
  route(person){const d=this.snapshot(),p=d.people[person]??d.groups?.[person],b=p?.binding;if(!d.enabled||!b)throw Error('companion-recipient-pending');return {schema:1,channel:'feishu',person_id:person,app_id:b.app_id,open_id:b.open_id??null,...(person===FAMILY?{recipient_kind:'group'}:{}),chat_id:b.chat_id,conversation_key:conversationKey(b.app_id,b.chat_id),binding_revision:b.revision};}
  validate(route){const expected=this.route(route?.person_id);if(Object.keys(expected).some(k=>expected[k]!==route?.[k]))throw Error('companion-route-stale');return expected;}
  receive({app_id,sender_open_id,sender_type,chat_id,chat_type,message_id,sender_tenant_key=null,event_tenant_key=null,mentions=[]}) {
    if(sender_type!=='user'||!['p2p','group'].includes(chat_type)||!token(message_id,'om_'))throw Error('companion-inbound-invalid');
    const d=this.snapshot(),found=Object.entries(d.people).filter(([,p])=>p.binding?.app_id===app_id&&p.binding?.open_id===sender_open_id);
    if(!d.enabled||found.length!==1)throw Error('companion-sender-unbound');
    const [person,p]=found[0];
    let route=this.route(person),reply_requested=true;
    if(chat_type==='group'){
      const g=d.groups?.[FAMILY];
      if(!g?.binding||g.binding.app_id!==app_id||g.binding.chat_id!==chat_id||!g.members.includes(person))throw Error('companion-group-unbound');
      reply_requested=mentions.some(m=>(m.id?.open_id??m.open_id)===g.binding.bot_open_id);
      if(!reply_requested&&g.mention_only!==false)throw Error('companion-group-not-mentioned');
      route=this.route(FAMILY);
    }
    if(route.chat_id!==chat_id||p.binding.sender_tenant_key&&p.binding.sender_tenant_key!==sender_tenant_key)throw Error('companion-chat-mismatch');
    // Cross-organization senders need not share the app/event tenant.
    return {speaker_person_id:person,conversation_key:route.conversation_key,source_message_id:message_id,
      app_id,sender_tenant_key,event_tenant_key,chat_type,mentions,reply_requested,route,sharing:p.sharing,...(chat_type==='group'?{audience_person_ids:d.groups[FAMILY].members}: {})};
  }
}
/** Sequential per window, concurrent across windows. A failed turn never poisons its queue. */
export class ConversationLanes {
  constructor(){this.tails=new Map();}
  run(key,work){const prior=this.tails.get(key)??Promise.resolve();const next=prior.catch(()=>{}).then(work);this.tails.set(key,next);return next.finally(()=>{if(this.tails.get(key)===next)this.tails.delete(key);});}
  busy(key){return this.tails.has(key);}
  async drain(){await Promise.allSettled([...this.tails.values()]);}
}
