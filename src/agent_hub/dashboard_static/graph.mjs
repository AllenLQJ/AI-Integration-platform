// Layout follows only explicit edges. Kahn traversal bounds cycles without inventing links.
export function layoutGraph(nodes, edges) {
  const unique = [...new Map(nodes.map(n=>[n.id,n])).values()];
  const ids = new Set(unique.map(n=>n.id));
  const links = edges.filter(e=>ids.has(e.source)&&ids.has(e.target));
  const incoming = new Map(unique.map(n=>[n.id,0])), children = new Map(unique.map(n=>[n.id,[]]));
  for (const edge of links) { incoming.set(edge.target,incoming.get(edge.target)+1); children.get(edge.source).push(edge.target); }
  const columns = new Map(), queue = unique.filter(n=>!incoming.get(n.id)).map(n=>n.id);
  for (const id of queue) columns.set(id,0);
  for (let i=0;i<queue.length;i++) {
    const id = queue[i];
    for (const target of children.get(id)) {
      columns.set(target,Math.max(columns.get(target)||0,columns.get(id)+1));
      incoming.set(target,incoming.get(target)-1);
      if (!incoming.get(target)) queue.push(target);
    }
  }
  const visited = new Set(queue); let nextColumn = Math.max(-1,...columns.values())+1;
  for (const item of unique) if (!visited.has(item.id)) columns.set(item.id,nextColumn++);
  const rows = new Map();
  const positioned = unique.map(item=>{
    const column = columns.get(item.id), row = rows.get(column)||0; rows.set(column,row+1);
    return {...item,x:24+column*286,y:26+row*160};
  });
  return {nodes:positioned,edges:links,width:Math.max(280,...positioned.map(n=>n.x+232)),height:Math.max(180,...positioned.map(n=>n.y+148))};
}
const edgeNames = {continuation:'续接',switch_executor:'切换执行',review:'审查'};
export function createGraph(viewport, stage, onSelect, label) {
  let key='', selected=null, buttons=new Map(), scale=1, layout={width:280,height:180};
  const el = (tag,cls,value)=>{const n=document.createElement(tag); n.className=cls; if(value!=null)n.textContent=value; return n;};
  function resize() { stage.style.width=`${layout.width*scale}px`; stage.style.height=`${layout.height*scale}px`; if(stage.firstElementChild)stage.firstElementChild.style.transform=`scale(${scale})`; }
  function select(id) {selected=id;for(const [key,button] of buttons){button.classList.toggle('selected',key===id);button.setAttribute('aria-pressed',String(key===id));}}
  function clear() {key='';buttons=new Map();stage.replaceChildren();viewport.scrollLeft=0;viewport.scrollTop=0;scale=1;}
  function render(nodes,edges,id) {
    const next=JSON.stringify([nodes,edges]);
    if(next===key){select(id);return;}
    key=next; const focus=document.activeElement?.dataset?.graphTask;
    const scrollLeft=viewport.scrollLeft, scrollTop=viewport.scrollTop;
    layout=layoutGraph(nodes,edges); buttons=new Map();
    const layer=el('div','graph-layer'); layer.style.width=`${layout.width}px`; layer.style.height=`${layout.height}px`;
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.setAttribute('width',layout.width);svg.setAttribute('height',layout.height);svg.setAttribute('aria-hidden','true');
    const positions=new Map(layout.nodes.map(n=>[n.id,n]));
    for(const edge of layout.edges){
      const a=positions.get(edge.source),b=positions.get(edge.target),x1=a.x+208,y1=a.y+58,x2=b.x,y2=b.y+58;
      const path=document.createElementNS('http://www.w3.org/2000/svg','path');
      const bend=Math.max(30,Math.abs(x2-x1)/2);
      path.setAttribute('d',`M ${x1} ${y1} C ${x1+bend} ${y1}, ${x2-bend} ${y2}, ${x2} ${y2}`);
      path.setAttribute('class',`graph-edge ${edge.kind==='review'?'review':''}`);svg.append(path);
      const arrow=document.createElementNS('http://www.w3.org/2000/svg','path');arrow.setAttribute('d',`M ${x2-5} ${y2-3} L ${x2} ${y2} L ${x2-5} ${y2+3}`);arrow.setAttribute('class','graph-edge');svg.append(arrow);
      const caption=document.createElementNS('http://www.w3.org/2000/svg','text');caption.setAttribute('x',(x1+x2)/2);caption.setAttribute('y',(y1+y2)/2-8);caption.setAttribute('text-anchor','middle');caption.textContent=edgeNames[edge.kind]||edge.kind;svg.append(caption);
    }
    layer.append(svg);
    for(const task of layout.nodes){
      const button=el('button','graph-node');button.dataset.graphTask=task.id;button.style.left=`${task.x}px`;button.style.top=`${task.y}px`;
      button.setAttribute('aria-label',`${task.agent||'未知执行器'} · ${label(task.state)} · ${task.title||'执行任务'} · ${task.id}`);
      button.setAttribute('title',task.title||task.id);
      button.append(el('strong','',task.agent||'未知执行器'),el('span','graph-node-state',label(task.state)),el('span','graph-node-title',task.title||'执行任务'),el('small','',task.id));
      button.addEventListener('click',()=>onSelect(task.id));buttons.set(task.id,button);layer.append(button);
    }
    stage.replaceChildren(layer);resize();select(id);
    if(focus&&buttons.has(focus))buttons.get(focus).focus({preventScroll:true});
    viewport.scrollLeft=scrollLeft;viewport.scrollTop=scrollTop;
  }
  return {render,select,clear,zoom(delta){scale=Math.min(1.5,Math.max(.6,Math.round((scale+delta)*10)/10));resize();return scale;},reset(){scale=1;resize();viewport.scrollLeft=0;viewport.scrollTop=0;return scale;}};
}
