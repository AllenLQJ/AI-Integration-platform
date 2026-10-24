import test from 'node:test';
import assert from 'node:assert/strict';
import {layoutGraph} from '../../src/agent_hub/dashboard_static/graph.mjs';

test('branch layout preserves explicit edges and terminates for cycles and dangling edges', () => {
  const nodes = ['a','b','c','d'].map(id=>({id}));
  const edges = [{source:'a',target:'b',kind:'continuation'},{source:'a',target:'c',kind:'review'},{source:'c',target:'d',kind:'continuation'}];
  const layout = layoutGraph(nodes,edges);
  const positions = new Map(layout.nodes.map(n=>[n.id,n]));
  assert.ok(positions.get('b').x > positions.get('a').x);
  assert.ok(positions.get('c').x > positions.get('a').x);
  assert.notEqual(positions.get('b').y,positions.get('c').y);
  assert.equal(layout.edges.length,3);
  const cyclic = layoutGraph(nodes,[...edges,{source:'d',target:'a',kind:'review'},{source:'missing',target:'b',kind:'review'}]);
  assert.equal(cyclic.nodes.length,4); assert.equal(cyclic.edges.length,4);
  assert.ok(cyclic.nodes.every(n=>Number.isFinite(n.x)&&Number.isFinite(n.y)));
  assert.equal(new Set(cyclic.nodes.map(n=>`${n.x},${n.y}`)).size,4);
});
