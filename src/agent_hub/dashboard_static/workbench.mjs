(function(){
"use strict";
const body=document.body,base="/work/"+encodeURIComponent(body.dataset.work);
const ACTIVE=new Set(["queued","running","waiting_for_user","cancelling"]);
const VERIFYING=new Set(["awaiting_verification","verifying","reviewing"]);
const REFRESH=new Set(["state","verification","verification_retry","session","artifact","review"]);
const attempts=new Map();
const snap={state:"connecting",detail:"",failures:0,last:null,timer:null,busy:false,dirty:false};
let current=body.dataset.current,reloading=false;
function one(selector,root){return (root||document).querySelector(selector)}
function put(selector,value,root){const node=one(selector,root);if(node&&node.textContent!==value)node.textContent=value}
function pretty(value){return JSON.stringify(value,null,2)}
function key(artifact){return artifact.artifact_id||pretty(artifact)}
function failed(state){return state==="down"||state==="unauthorized"}
function backoff(failures,status){return status===401?5000:Math.min(30000,1000*Math.pow(2,Math.min(failures-1,5)))}
function settled(a){return !ACTIVE.has(a.state)&&!VERIFYING.has(a.verification)}
function renderArtifacts(a){put(".artifacts",pretty(Array.from(a.artifacts.values())),a.panel)}
const KINDS={state:"状态",message:"消息",tool:"工具",progress:"进度",artifact:"产物",verification:"验收",review:"审核",session:"会话",activity:"操作",process:"进程"};
function summary(e){
  const kind=String(e.kind||"事件"),data=e.data&&typeof e.data==="object"?e.data:{};
  let detail="";
  for(const field of ["message","text","state","name","reason","status"]){const value=data[field];if(typeof value==="string"&&value){detail=value;break}}
  if(kind==="tool"&&typeof data.name==="string")detail=data.name+(typeof data.status==="string"?" · "+data.status:"");
  const chars=Array.from(Array.from(detail).map(function(c){return c.trim()?c:" "}).join("").split(" ").filter(Boolean).join(" "));
  detail=chars.length>120?chars.slice(0,117).join("")+"…":chars.join("");
  return "#"+e.seq+" "+(KINDS[kind]||kind)+(detail?"："+detail:"");
}
function eventNode(e){
  const node=document.createElement("pre");
  node.tabIndex=0;node.dataset.seq=String(e.seq);node.dataset.kind=String(e.kind||"");node.dataset.summary=summary(e);
  node.textContent=pretty(e);
  return node;
}
function earliest(a){
  let low=0;
  for(const node of one(".events",a.section).children){const seq=Number(node.dataset.seq)||0;if(seq&&(!low||seq<low))low=seq}
  return low;
}
async function older(a,button){
  if(a.loadingOlder)return;
  a.loadingOlder=true;button.disabled=true;
  const low=earliest(a);
  const result=low?await load(base+"/events?attempt_id="+encodeURIComponent(a.id)+"&before_seq="+low):{data:{events:[],has_more:false}};
  a.loadingOlder=false;button.disabled=false;
  if(result.error!==undefined){button.textContent="加载失败，点击重试";return}
  const box=one(".events",a.section),first=box.firstElementChild;
  for(const e of result.data.events)if(e.seq<low)box.insertBefore(eventNode(e),first);
  box.dataset.more=String(Boolean(result.data.has_more));
  button.textContent="加载更早事件";button.hidden=!result.data.has_more;
}
for(const section of document.querySelectorAll("[data-task]")){
  let panel=null;
  for(const candidate of document.querySelectorAll("[data-verification]"))if(candidate.dataset.verification===section.dataset.task)panel=candidate;
  const a={id:section.dataset.task,section:section,panel:panel,after:0,artifacts:new Map(),state:one(".exec-state",section).textContent,
           verification:one(".verification-state",panel).textContent,timer:null,busy:false,failure:null,failures:0};
  for(const node of one(".events",section).children)a.after=Math.max(a.after,Number(node.dataset.seq)||0);
  let initial=[];
  try{initial=JSON.parse(one(".artifacts",panel).textContent)}catch(error){initial=[]}
  for(const artifact of initial)a.artifacts.set(key(artifact),artifact);
  renderArtifacts(a);
  attempts.set(a.id,a);
  const button=one(".older",section);
  if(button)button.addEventListener("click",function(){older(a,button)});
}
function eventsChannel(){
  const channel={state:"ok",detail:"",failures:0};
  for(const a of attempts.values()){
    if(!a.failure)continue;
    channel.failures=Math.max(channel.failures,a.failures);
    if(a.failure.status===401){channel.state="unauthorized";channel.detail=a.failure.reason}
    else if(channel.state!=="unauthorized"){channel.state="down";channel.detail=a.failure.reason}
  }
  return channel;
}
function describe(name,channel,impact){
  return name+(channel.state==="unauthorized"?"未授权":"中断")+"（"+channel.detail+"，已失败 "+channel.failures+" 次）："+impact;
}
function showConnection(){
  const node=one("#connection"),events=eventsChannel(),time=snap.last?snap.last.toLocaleTimeString():"页面加载时";
  const state=snap.state==="unauthorized"||events.state==="unauthorized"?"unauthorized":
    failed(snap.state)||failed(events.state)?"down":snap.state==="connecting"?"connecting":"live";
  node.dataset.state=state;node.dataset.snapshot=snap.state;node.dataset.events=events.state;
  node.dataset.snapshotAt=snap.last?String(snap.last.getTime()):"";
  if(state==="connecting"){node.textContent="正在建立实时同步";return}
  if(state==="live"){node.textContent="实时同步中，最后状态快照 "+time;return}
  const parts=[];
  if(failed(snap.state))parts.push(describe("状态快照通道",snap,"顶部状态、执行状态与验收结论停留在 "+time+" 的快照，不代表最新进展"));
  if(failed(events.state))parts.push(describe("事件通道",events,"执行时间线与产物可能缺少最新事件"));
  node.textContent=(state==="unauthorized"?"未授权：":"连接中断：")+parts.join("；")+"。"+(state==="unauthorized"?
    "工作台会话已失效或无权访问此工作项，请由总控重新签发工作台会话（POST "+base+"/session）；会话恢复后本页会自动继续同步，也可重新签发后刷新页面。":
    "正在按退避间隔自动重连。");
}
async function load(url){
  let response;
  try{response=await fetch(url,{credentials:"same-origin",cache:"no-store"})}catch(error){return {error:0,reason:"网络错误"}}
  if(!response.ok)return {error:response.status,reason:"HTTP "+response.status};
  try{return {data:await response.json()}}catch(error){return {error:0,reason:"响应不完整"}}
}
// Hidden tabs schedule nothing; each skipped poll runs as soon as the tab is visible again.
const deferred=new Map();
function later(key,fn,delay){
  if(document.visibilityState==="hidden"){deferred.set(key,fn);return -1}
  return setTimeout(fn,delay);
}
document.addEventListener("visibilitychange",function(){
  if(document.visibilityState==="hidden"||reloading)return;
  const run=Array.from(deferred.values());deferred.clear();
  for(const fn of run)fn();
});
function schedule(a,delay){
  if(reloading||a.timer||a.busy)return;
  if(delay===undefined)delay=a.id===current||!settled(a)?0:30000;
  a.timer=later(a,function(){a.timer=null;poll(a)},delay);
}
// Live events append without bound on long sessions; keep the newest and let "older" reload the rest.
const MAX_EVENT_NODES=1000;
function trim(a){
  const box=one(".events",a.section);
  if(box.children.length<=MAX_EVENT_NODES)return;
  while(box.children.length>MAX_EVENT_NODES)box.firstElementChild.remove();
  box.dataset.more="true";
  const button=one(".older",a.section);
  if(button)button.hidden=false;
}
function setState(a,state){
  a.state=state;put(".exec-state",state,a.section);
  if(a.id===current)put("#current-state",state);
}
function eventsFailed(a,status,reason){
  a.failures+=1;a.failure={status:status,reason:reason};
  showConnection();
  schedule(a,backoff(a.failures,status));
}
async function poll(a){
  if(reloading||a.busy)return;
  a.busy=true;
  const result=await load(base+"/events?attempt_id="+encodeURIComponent(a.id)+"&after_seq="+a.after);
  a.busy=false;
  if(reloading)return;
  if(result.error!==undefined)return eventsFailed(a,result.error,result.reason);
  let refresh=false;
  try{
    for(const e of result.data.events){
      if(e.seq<=a.after)continue;
      a.after=e.seq;
      one(".events",a.section).append(eventNode(e));
      trim(a);
      if(e.kind==="state"&&e.data&&typeof e.data.state==="string")setState(a,e.data.state);
      if(e.kind==="artifact"&&e.data){a.artifacts.set(key(e.data),e.data);renderArtifacts(a)}
      if(REFRESH.has(e.kind))refresh=true;
    }
  }catch(error){return eventsFailed(a,0,"响应异常")}
  a.failure=null;a.failures=0;
  showConnection();
  if(refresh)requestSnapshot();
  schedule(a);
}
function apply(data){
  for(const s of data.attempts)if(!attempts.has(s.task_id)){reloading=true;location.reload();return}
  current=data.current_task_id;body.dataset.current=current;
  put("#work-status",data.work_status);put("#current-attempt",current);
  put("#pending-actions",data.pending_actions.length?data.pending_actions.join("；"):"无");
  for(const s of data.attempts){
    const a=attempts.get(s.task_id);
    a.verification=s.verification.state;
    setState(a,s.state);
    put(".verification-state",s.verification.state,a.panel);
    put(".verdict",pretty(s.verification),a.panel);
    a.artifacts=new Map();
    for(const artifact of s.artifacts)a.artifacts.set(key(artifact),artifact);
    renderArtifacts(a);
    a.section.dataset.current=a.panel.dataset.current=String(s.task_id===current);
  }
  const now=attempts.get(current);
  put("#current-state",now?now.state:"缺失");put("#verification-status",now?now.verification:"缺失");
}
function scheduleSnapshot(delay){
  if(reloading)return;
  if(snap.timer&&snap.timer!==-1)clearTimeout(snap.timer);
  snap.timer=later("snapshot",function(){snap.timer=null;snapshot()},delay);
}
function requestSnapshot(){
  if(failed(snap.state))return;
  if(snap.busy)snap.dirty=true;else scheduleSnapshot(0);
}
async function snapshot(){
  if(snap.busy){snap.dirty=true;return}
  snap.busy=true;snap.dirty=false;
  const result=await load(base+"/snapshot");
  snap.busy=false;
  if(reloading)return;
  let status=result.error,reason=result.reason;
  if(status===undefined){try{apply(result.data)}catch(error){status=0;reason="响应异常"}}
  if(reloading)return;
  if(status!==undefined){
    snap.state=status===401?"unauthorized":"down";snap.detail=reason;snap.failures+=1;
    showConnection();
    return scheduleSnapshot(backoff(snap.failures,status));
  }
  snap.state="ok";snap.detail="";snap.failures=0;snap.last=new Date();
  showConnection();
  if(snap.dirty)return scheduleSnapshot(0);
  let active=false;
  for(const a of attempts.values())if(!settled(a))active=true;
  scheduleSnapshot(active?3000:15000);
}
showConnection();
for(const a of attempts.values())schedule(a,0);
scheduleSnapshot(0);
})();
