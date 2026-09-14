import {useCallback, useEffect, useState} from "react";
import {api, type Scope} from "./api";
import {Graph} from "./Graph";

const labels: Record<string,string> = {event:"事件", thread:"事情脉络", entity:"人物与实体", finding:"发现", work:"作品", artifact:"文件版本", exploration:"探索", share:"分享", association:"联想"};
const shares: Record<string,string> = {unshared:"未分享", partial:"部分分享", shared:"已分享", unconfirmed:"待核对"};
const relations: Record<string,string> = {participates:"参与",part_of:"属于",continues:"延续",responds_to:"回应",produces:"产生",delivers:"交付",shares:"分享",corrects:"更正",resolves:"解决",supports:"支持",refutes:"反驳",causes:"因果",association:"联想",follows:"先后",about:"关于",related:"关联"};
const bases: Record<string,string> = {observed:"工具观察", explicit:"用户陈述", documented:"资料记载", inferred:"待核验解读", internal_thought:"Kin 的联想"};

export function EventGraphPanel({data, scope, onSource}: {data:any; scope:Scope; onSource:(id:string)=>void}) {
  const [view,setView]=useState(data), [selected,setSelected]=useState<any>(null);
  const [query,setQuery]=useState(""), [layer,setLayer]=useState(""), [kind,setKind]=useState("");
  const [since,setSince]=useState(""), [until,setUntil]=useState(""), [focus,setFocus]=useState<string|undefined>();
  const [revisionAction,setRevisionAction]=useState("correct"),[reason,setReason]=useState(""),[evidence,setEvidence]=useState(""),[correction,setCorrection]=useState(""),[target,setTarget]=useState(""),[previousCommand,setPreviousCommand]=useState("");
  const [error,setError]=useState(""), [busy,setBusy]=useState(false);
  useEffect(()=>{setView(data);setSelected(null);setFocus(undefined);},[data]);
  const load=useCallback(async (extra:Record<string,any>={}, append=false)=>{
    setBusy(true);setError("");
    try {
      const next=await api.call("read_graph",{query:{...scope,query,...(layer?{layer}:{}),...(kind?{kind}:{}),
        ...(since?{since:new Date(since).toISOString()}:{}),...(until?{until:new Date(until).toISOString()}:{}),limit:150,...extra}});
      setView((previous:any)=>append ? {...next,nodes:[...new Map([...previous.nodes,...next.nodes].map((n:any)=>[n.id,n])).values()].slice(-300),
        edges:[...new Map([...previous.edges,...next.edges].map((n:any)=>[n.id,n])).values()]} : next);
    } catch(e) {setError(String(e));} finally {setBusy(false);}
  },[scope,query,layer,kind,since,until]);
  const choose=useCallback(async (id:string)=>{
    const local=[...(view?.nodes??[]),...(view?.edges??[])].find(n=>n.id===id);
    setSelected(local??{id}); setError("");
    try {setSelected(await api.call("read_graph_object",{path:{identifier:id},query:scope}));}
    catch {if(id.startsWith("mem_"))onSource(id);else setError("这条关系的详细资料暂时无法读取。");}
  },[scope,view,onSource]);
  const coverage=(entry:any)=>entry?.share_coverage;
  const revise=async()=>{
    setBusy(true);setError("");
    try {
      const request:any={id:selected.id,expected_revision:selected.revision,command_id:crypto.randomUUID(),action:revisionAction,reason,evidence_ids:evidence.split(/[\s,，]+/).filter(Boolean)};
      if(revisionAction==="correct")request.changes=JSON.parse(correction||"{}");
      if(revisionAction==="merge"){
        const other:any=await api.call("read_graph_object",{path:{identifier:target},query:scope});
        request.target_id=other.id;request.target_revision=other.revision;
      }
      if(["undo","split"].includes(revisionAction))request.previous_command_id=previousCommand;
      const receipt:any=await api.call("revise_graph",{body:{scope,request}});
      setPreviousCommand(receipt.command_id);
      await load({...focus?{focus,hops:2}:{}});
      await choose(selected.id);
    } catch(e){setError(String(e));} finally{setBusy(false);}
  };
  return <section className="event-graph-panel">
    <form className="graph-filter" onSubmit={e=>{e.preventDefault();setFocus(undefined);void load();}}>
      <label>人物、项目或旧事<input value={query} onChange={e=>setQuery(e.target.value)} placeholder="找一件事…"/></label>
      <label>关系层<select value={layer} onChange={e=>setLayer(e.target.value)}><option value="">事实与联想</option><option value="evidence">有来源的关联</option><option value="association">主观联想</option></select></label>
      <label>对象<select value={kind} onChange={e=>setKind(e.target.value)}><option value="">全部</option>{Object.entries(labels).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label>
      <label>从<input aria-label="图谱开始时间" type="datetime-local" value={since} onChange={e=>setSince(e.target.value)}/></label>
      <label>到<input aria-label="图谱结束时间" type="datetime-local" value={until} onChange={e=>setUntil(e.target.value)}/></label>
      <button className="primary" disabled={busy}>查找</button>
    </form>
    <p className="graph-legend">● 事件　■ 实体　◆ 发现　<span>实线：有来源的关联</span>　<span>虚线：主观联想</span>　发送回执与已读分别记录。</p>
    {error&&<p role="alert">{error}</p>}
    <Graph data={view} onSelect={id=>void choose(id)}/>
    <div className="graph-reading">
      <section aria-label="事件时间线"><h3>经过与后续</h3>
        <ol className="graph-timeline">{[...(view?.nodes??[])].sort((a,b)=>(a.occurred_at??"").localeCompare(b.occurred_at??"")).map((n:any)=><li key={n.id}>
          <button className={selected?.id===n.id?"selected":"text-button"} onClick={()=>void choose(n.id)}>
            <time>{n.occurred_at?new Date(n.occurred_at).toLocaleString():""}</time><strong>{n.title}</strong><small>{labels[n.kind]??n.kind}{coverage(n)?` · ${shares[coverage(n).state]}`:""}{n.needs_review?" · 待复核":""}</small>
          </button></li>)}</ol>
        {view?.cursor!==null&&view?.cursor!==undefined&&<button disabled={busy} onClick={()=>void load({cursor:view.cursor,...(focus?{focus,hops:2}:{})},true)}>继续读取</button>}
      </section>
      <section className="graph-detail" aria-label="图谱详情">
        {selected?<>
          <h3>{selected.title??relations[selected.predicate]??"关系详情"}</h3>
          <p>{bases[selected.basis]??selected.basis}{selected.needs_review?" · 来源需要复核":""}</p>
          {selected.record?.bubbles&&<section aria-label="实际发送正文"><h4>实际发送正文</h4>{Object.values(selected.record.bubbles).map((b:any)=><div key={b.id}><p className="graph-prose">{b.text}</p><small>{b.at} · {b.state} · {b.message_id??"回执待核对"}</small></div>)}</section>}
          {selected.text&&<p className="graph-prose">{selected.text}</p>}
          {selected.reason&&<p>{selected.reason}</p>}
          {selected.role&&<p>参与角色：{selected.role}</p>}
          {selected.created_by&&<p>制作者：{selected.created_by}</p>}
          {selected.subject&&<div className="button-row"><button onClick={()=>void choose(selected.subject)}>起点</button><span>→ {relations[selected.predicate]??selected.predicate} →</span><button onClick={()=>void choose(selected.object)}>终点</button></div>}
          {!selected.subject&&<button disabled={busy} onClick={()=>{setFocus(selected.id);void load({focus:selected.id,hops:2},true);}}>展开前因后续</button>}
          {coverage(selected)&&<section><h4>{shares[coverage(selected).state]}{coverage(selected).total!==undefined?` · ${coverage(selected).shared}/${coverage(selected).total}`:""}</h4>
            {(coverage(selected).units??[coverage(selected)]).map((unit:any)=><div key={unit.id} className="graph-coverage"><button className="text-button" onClick={()=>void choose(unit.id)}>{shares[unit.state]} · {view?.nodes.find((n:any)=>n.id===unit.id)?.title??unit.id}</button>
              {(unit.deliveries??[]).map((d:any)=><p key={d.bubble_id}><time>{new Date(d.at).toLocaleString()}</time> · {d.channel} · {d.state==="accepted"?"服务器已接收":"回执待核对"}<br/><button className="text-button" onClick={()=>void choose(d.share_id)}>查看对应消息</button>{d.message_id&&<small>{d.message_id}</small>}</p>)}</div>)}
          </section>}
          <h4>来源</h4>{(selected.evidence??[]).map((e:any)=><button className="text-button" key={`${e.source_id}:${e.record_id}`} onClick={()=>onSource(e.record_id)}>{e.namespace} · {e.occurred_at} · r{e.revision}</button>)}
          <h4>相关关系</h4>{(view?.edges??[]).filter((e:any)=>[e.subject,e.object].includes(selected.id)).map((e:any)=><button className="text-button" key={e.id} onClick={()=>void choose(e.id)}>{relations[e.predicate]??e.predicate} · {e.reason||e.role}</button>)}
          <details><summary>更正关联</summary><form className="graph-revision" onSubmit={e=>{e.preventDefault();void revise();}}>
            <label>修改方式<select value={revisionAction} onChange={e=>setRevisionAction(e.target.value)}><option value="correct">更正</option><option value="retract">撤销关联</option><option value="restore">恢复</option><option value="merge">合并同一对象</option><option value="split">拆分上次合并</option><option value="undo">撤销一次修改</option></select></label>
            <label>依据<input required value={reason} onChange={e=>setReason(e.target.value)} placeholder="这次更正的原因"/></label>
            <label>原始证据编号<input required value={evidence} onChange={e=>setEvidence(e.target.value)} placeholder="多个编号用空格分隔"/></label>
            {revisionAction==="correct"&&<label>更正字段<textarea value={correction} onChange={e=>setCorrection(e.target.value)} placeholder={'{"title":"名称","reason":"关联依据"}'}/></label>}
            {revisionAction==="merge"&&<label>合并到<input required value={target} onChange={e=>setTarget(e.target.value)}/></label>}
            {["undo","split"].includes(revisionAction)&&<label>原修改编号<input required value={previousCommand} onChange={e=>setPreviousCommand(e.target.value)}/></label>}
            <button disabled={busy} className="primary">保存更正</button>{previousCommand&&<small>修改编号：{previousCommand}</small>}
          </form></details>
          <details><summary>修订历史 · {selected.history?.length??1} 版</summary>{(selected.history??[]).map((v:any)=><p key={v.revision}>r{v.revision} · {v.updated_at}<br/>{v.revision_reason??v.reason??v.title} · {v.state}</p>)}</details>
        </>:<p>点开事件或连线，查看它的经过与关联依据。</p>}
      </section>
    </div>
  </section>;
}
