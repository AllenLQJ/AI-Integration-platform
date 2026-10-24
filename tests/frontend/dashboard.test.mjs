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
