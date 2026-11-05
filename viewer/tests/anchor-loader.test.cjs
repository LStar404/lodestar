const test = require('node:test');
const assert = require('node:assert/strict');
const {AnchorLoader} = require('./load-ts.cjs')()('../src/lib/anchor-loader.ts');
const tick = () => new Promise(resolve => setImmediate(resolve));

test('500 simultaneous card lookups use three bounded batches and one publication per batch', async () => {
  const calls = [], publications = [];
  let active = 0, peak = 0;
  const loader = new AnchorLoader(async (key, ids) => {
    calls.push(ids); peak = Math.max(peak, ++active);
    await tick(); active--;
    return ids.map(anchor_id => ({anchor_id}));
  }, rows => publications.push(rows));
  loader.reset('book:1');
  await Promise.all(Array.from({length:500}, (_,i) => loader.fetch(['a'+i, 'a'+i])));
  assert.deepEqual(calls.map(x => x.length), [200,200,100]);
  assert.equal(peak,2);
  assert.equal(publications.length,3);
  await loader.fetch(['a1','a2']);
  assert.equal(calls.length,3);
});

test('switching document/revision discards late responses and refetches the same anchor', async () => {
  let finish;
  const seen = [];
  const loader = new AnchorLoader((key, ids) => key === 'book:1' ? new Promise(resolve => {finish=resolve;}) : Promise.resolve([{anchor_id:'a',page:2}]), rows=>seen.push(...rows));
  loader.reset('book:1');
  const old = loader.fetch(['a']);
  await tick();
  loader.reset('book:2');
  await loader.fetch(['a']);
  finish([{anchor_id:'a',page:1}]);
  await old; await tick();
  assert.deepEqual(seen, [{anchor_id:'a',page:2}]);
});

test('failed requests can retry while confirmed missing anchors are cached until revision changes', async () => {
  let calls=0;
  const loader = new AnchorLoader(async () => { if (++calls === 1) throw Error('offline'); return []; }, ()=>{});
  loader.reset('book:1');
  await loader.fetch(['missing']);
  await loader.fetch(['missing']);
  await loader.fetch(['missing']);
  assert.equal(calls,2);
  loader.reset('book:2');
  await loader.fetch(['missing']);
  assert.equal(calls,3);
});

test('reopening the same document can explicitly reset its published anchor cache', async () => {
  let calls=0;
  const loader = new AnchorLoader(async () => { calls++; return []; }, ()=>{});
  loader.reset('book:1');
  await loader.fetch(['a']);
  loader.reset('book:1',true);
  await loader.fetch(['a']);
  assert.equal(calls,2);
});
