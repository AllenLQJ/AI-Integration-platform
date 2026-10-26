import test from 'node:test';
import assert from 'node:assert/strict';
import { createAPI, consumeTicket, APIError } from '../../src/agent_hub/dashboard_static/api.mjs';
import { RequestGate, mergeEvents, DashboardState } from '../../src/agent_hub/dashboard_static/state.mjs';

test('ticket removed before exchange and never persisted', async () => {
  const calls = [];
  const location = { hash: '#ticket=a%2Bb', pathname: '/dashboard/', search: '?view=tasks' };
  const history = { replaceState: (...args) => calls.push(['clear', ...args]) };
  await consumeTicket({ session: async ticket => calls.push(['exchange', ticket]) }, location, history);
  assert.deepEqual(calls, [['clear', null, '', '/dashboard/?view=tasks'], ['exchange', 'a+b']]);
});
test('API encodes filters and uses only same-origin cookie credentials', async () => {
  let captured;
  const api = createAPI(async (url, init) => { captured = { url, init }; return new Response(JSON.stringify({items:[],next_offset:null})); });
  await api.tasks({q:'a & b',agent:'custom/one',limit:30,offset:0});
  assert.equal(new URL(captured.url, 'http://localhost').searchParams.get('q'), 'a & b');
  assert.equal(captured.init.credentials, 'same-origin');
  assert.equal(captured.init.headers.Authorization, undefined);
});
test('API preserves unauthorized and rejects malformed JSON and task shapes', async () => {
  await assert.rejects(createAPI(async () => new Response('', {status:401})).agents(), e => e instanceof APIError && e.status === 401);
  await assert.rejects(createAPI(async () => new Response('not json')).agents(), /响应/);
  await assert.rejects(createAPI(async () => new Response('{}')).tasks({}), /响应/);
});

test('API revalidates an in-memory response and drops it after authentication failure', async () => {
  const sent = [];
  let calls = 0;
  const api = createAPI(async (_url, init) => {
    sent.push(init.headers['If-None-Match']);
    calls++;
    if (calls === 2) return new Response(null, {status:304});
    if (calls === 3) return new Response('', {status:401});
    return new Response(JSON.stringify({items:[{id:'a'}],next_offset:null}), {headers:{ETag:'"revision-1"'}});
  });
  const first = await api.chains({});
  first.items[0].id = 'local mutation';
  assert.equal((await api.chains({})).items[0].id, 'a');
  await assert.rejects(api.chains({}), error => error.status === 401);
  await api.chains({});
  assert.deepEqual(sent, [undefined,'"revision-1"','"revision-1"',undefined]);
});
test('switching selection aborts previous fetch and rejects its late response', () => {
  const gate = new RequestGate(); const old = gate.next(); const current = gate.next();
  assert.equal(old.signal.aborted, true); assert.equal(gate.current(old), false); assert.equal(gate.current(current), true);
});
test('search invalidates old pagination immediately', () => {
  const state = new DashboardState(); const old = state.listGate.next();
  state.changeFilters({q:'new'});
  assert.equal(state.listGate.current(old), false); assert.equal(state.offset, 0); assert.deepEqual(state.items, []);
});
test('event cursor cannot move to another task from a late poll', () => {
  const state = new DashboardState(); const old = state.select('a'); const current = state.select('b');
  assert.equal(state.commitEvents(old, {events:[{seq:50}],next_seq:50}), false);
  assert.equal(state.cursor, 0);
  assert.equal(state.commitEvents(current, {events:[{seq:2,kind:'message'}],next_seq:2}), true);
  assert.equal(state.cursor, 2);
  assert.equal(state.commitEvents(current, {events:[{seq:1},{seq:2}],next_seq:1}), true);
  assert.equal(state.cursor, 2); assert.deepEqual(state.events.map(x=>x.seq), [1,2]);
});
test('incremental events are ordered and deduplicated by sequence', () => {
  assert.deepEqual(mergeEvents([{seq:2,kind:'old'}], [{seq:3},{seq:2,kind:'new'}]).map(x=>x.seq), [2,3]);
});

test('event windows bound count and bytes without losing the live cursor', () => {
  const state = new DashboardState(), token = state.select('busy');
  for (let page=0;page<10;page++) state.commitEvents(token, {
    events:Array.from({length:200},(_,i)=>({seq:page*200+i+1,kind:'progress',data:{text:'x'.repeat(2000)}})),
    next_seq:(page+1)*200,
  });
  assert.ok(state.events.length <= 1000);
  assert.ok(new TextEncoder().encode(JSON.stringify(state.events)).length <= 2*1024*1024);
  assert.equal(state.cursor,2000);
  assert.equal(state.events.at(-1).seq,2000);
  assert.equal(state.eventsTruncated,true);
  state.commitEvents(token,{events:[{seq:2001,data:{text:'x'.repeat(3*1024*1024)}}],next_seq:2001});
  assert.equal(state.events.at(-1).seq,2001);
  assert.ok(JSON.stringify(state.events).length <= 2*1024*1024);
});

test('history pages replace the event window and stay stable during live polls', () => {
  const state = new DashboardState(), token = state.select('a');
  state.commitEvents(token,{events:[{seq:400}],next_seq:400});
  state.replaceEvents(token,{events:[{seq:20},{seq:21}],next_seq:21,has_more:true},true);
  state.commitEvents(token,{events:[{seq:401}],next_seq:401});
  assert.deepEqual(state.events.map(e=>e.seq),[20,21]);
  assert.equal(state.historyMode,true);
  state.replaceEvents(token,{events:[{seq:500}],next_seq:500,has_more:true},false);
  state.commitEvents(token,{events:[{seq:501}],next_seq:501});
  assert.deepEqual(state.events.map(e=>e.seq),[500,501]);
  assert.equal(state.cursor,501);
  const next = state.select('b');
  assert.equal(state.replaceEvents(token,{events:[{seq:1}],next_seq:1},true),false);
  assert.equal(state.historyMode,false);
  assert.ok(state.detailGate.current(next));
});

test('older event API uses an exclusive cursor instead of mixing directions', async () => {
  const urls=[];
  const api=createAPI(async url=>{urls.push(url);return new Response(JSON.stringify({events:[],next_seq:0,has_more:false}));});
  await api.eventHistory('task/id',123);
  assert.equal(urls[0],'/dashboard/api/v1/tasks/task%2Fid/events?before_seq=123');
});

test('chain API accepts member id while retaining encoded URLs and validates graph shape', async () => {
  const urls = [];
  const api = createAPI(async url=>{ urls.push(url); return new Response(JSON.stringify(url.includes('?') ? {items:[],next_offset:null} : {chain:{id:'root'},nodes:[],edges:[],messages:[],warnings:[],truncated:false})); });
  await api.chains({q:'branch & review',offset:0});
  await api.chain('member/id');
  assert.equal(new URL(urls[0],'http://local').searchParams.get('q'),'branch & review');
  assert.ok(urls[1].endsWith('/chains/member%2Fid'));
  await assert.rejects(createAPI(async()=>new Response('{}')).chain('root'),/响应/);
});

test('chain and node selections have independent gates and reject stale responses', () => {
  const state = new DashboardState(); const chainA = state.selectChain('a');
  const nodeA = state.select('a1'); const nodeB = state.select('a2');
  assert.ok(state.chainGate.current(chainA)); assert.ok(!state.detailGate.current(nodeA));
  const chainB = state.selectChain('b');
  assert.ok(!state.chainGate.current(chainA)); assert.ok(!state.detailGate.current(nodeB));
  assert.ok(state.chainGate.current(chainB)); assert.equal(state.chainId,'b');
  assert.equal(state.chain,null); assert.equal(state.detail,null);
});
