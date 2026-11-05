const test = require('node:test');
const assert = require('node:assert/strict');
const makeLoader = require('./load-ts.cjs');
const note = (id, chapter='ch01') => ({id, chapter_id:chapter, doc_id:'book', revision:1, sources:[], tags:[], title:id, content:'', status:'needs_review', deleted_at:null});
const deferred = () => { let resolve, reject; const promise = new Promise((a,b)=>{resolve=a; reject=b}); return {promise,resolve,reject}; };
function setup(api={}) {
  const store=makeLoader({'../api/client':{api:{getAnchors:async()=>({items:[]}),blockCounts:async()=>({by_status:{}}),...api}}})('../src/state/store.ts').useStore;
  store.setState({docId:'book',chapterId:'ch01',blocks:[note('old')],selectedBlockId:'old'});
  return store;
}
test('cross-chapter navigation waits for every page before selecting and revealing target', async()=>{
  const second=deferred(); let calls=0;
  const s=setup({getBlock:async()=>({block:note('target','ch02')}), listBlocks:async()=>++calls===1
    ? {items:[note('first','ch02')],data_revision:2,next_cursor:'next'} : second.promise});
  const action=s.getState().navigateBlock('target');
  await new Promise(r=>setImmediate(r));
  assert.equal(s.getState().chapterId,'ch01'); assert.equal(s.getState().selectedBlockId,'old');
  second.resolve({items:[note('target','ch02')],data_revision:2,next_cursor:null});
  assert.equal(await action,true);
  assert.equal(s.getState().chapterId,'ch02'); assert.deepEqual(s.getState().blocks.map(b=>b.id),['first','target']);
  assert.equal(s.getState().selectedBlockId,'target'); assert.equal(s.getState().revealBlock.id,'target');
});
test('failed destination read leaves the current chapter, selection and unsaved draft intact', async()=>{
  const s=setup({getBlock:async()=>({block:note('target','ch02')}),listBlocks:async()=>{throw Error('offline')}});
  s.setState({view:'source',dirtyDraft:'unsaved'});
  assert.equal(await s.getState().navigateBlock('target'),false);
  assert.equal(s.getState().chapterId,'ch01'); assert.equal(s.getState().selectedBlockId,'old');
  assert.equal(s.getState().dirtyDraft,'unsaved'); assert.equal(s.getState().blocks[0].id,'old');
});
test('declining draft confirmation cancels every navigation side effect',async()=>{
  const previous=global.window; let prompts=0;
  global.window={confirm:()=>{prompts++;return false}};
  try {
    const s=setup({getBlock:async()=>({block:note('target','ch02')}),listBlocks:async()=>({items:[note('target','ch02')],data_revision:1,next_cursor:null})});
    s.setState({view:'source',dirtyDraft:'keep'});
    assert.equal(await s.getState().navigateBlock('target'),false);
    assert.equal(prompts,1); assert.equal(s.getState().dirtyDraft,'keep');
    assert.equal(s.getState().chapterId,'ch01'); assert.equal(s.getState().selectedBlockId,'old'); assert.equal(s.getState().revealBlock,null);
  } finally {global.window=previous}
});
test('direct selection invalidates an in-flight cross-chapter jump',async()=>{
  const pending=deferred(); const s=setup({getBlock:()=>pending.promise});
  const action=s.getState().navigateBlock('target'); s.getState().requestSelectBlock('newer');
  pending.resolve({block:note('target','ch02')});
  assert.equal(await action,false); assert.equal(s.getState().selectedBlockId,'newer'); assert.equal(s.getState().chapterId,'ch01');
});
test('stale search results from another document cannot initiate navigation',async()=>{
  let calls=0; const s=setup({getBlock:async()=>{calls++;return {block:note('target')}}});
  assert.equal(await s.getState().navigateBlock('target','other-book'),false); assert.equal(calls,0);
});
test('closing review during loading cannot reopen it when the response arrives',async()=>{
  const pending=deferred(); const s=setup({reviewQueue:()=>pending.promise});
  const action=s.getState().openReview(true); assert.equal(s.getState().reviewLoading,true);
  await s.getState().openReview(false); pending.resolve({items:[note('late')]}); await action;
  assert.equal(s.getState().reviewOpen,false); assert.equal(s.getState().reviewLoading,false); assert.deepEqual(s.getState().reviewItems,[]);
});
test('switching documents discards pending review items and counts, even when returning to same book',async()=>{
  const queue=deferred(), counts=deferred();
  const s=setup({reviewQueue:()=>queue.promise,blockCounts:()=>counts.promise,getManifest:async()=>({chapters:[],data_revision:1})});
  const a=s.getState().openReview(true), b=s.getState().refreshReviewCount();
  await s.getState().selectDoc('other'); await s.getState().selectDoc('book');
  queue.resolve({items:[note('stale')]}); counts.resolve({by_status:{needs_review:99}}); await Promise.all([a,b]);
  assert.deepEqual(s.getState().reviewItems,[]); assert.equal(s.getState().reviewCount,0); assert.equal(s.getState().reviewOpen,false);
});
test('review failures remain dismissible and allow a successful retry',async()=>{
  let calls=0;const s=setup({reviewQueue:async()=>{if(++calls===1)throw Error('offline');return {items:[note('ready')]}}});
  await s.getState().openReview(true);
  assert.equal(s.getState().reviewOpen,true); assert.match(s.getState().reviewError,/offline/); assert.equal(s.getState().reviewLoading,false);
  await s.getState().openReview(true); assert.equal(s.getState().reviewError,null); assert.equal(s.getState().reviewCount,1);
});
test('fresh review contents invalidate an older counter request',async()=>{
  const counts=deferred(); const s=setup({blockCounts:()=>counts.promise, reviewQueue:async()=>({items:[note('a')]})});
  const pending=s.getState().refreshReviewCount(); await s.getState().openReview(true);
  counts.resolve({by_status:{needs_review:77}});await pending; assert.equal(s.getState().reviewCount,1);
});
test('chapter reader restarts stale pagination without mixing versions',async()=>{
  let calls=0;
  const load=makeLoader({'../api/client':{api:{listBlocks:async()=>{
    calls++; if(calls===1)return {items:[note('old')],data_revision:1,next_cursor:'next'};
    if(calls===2)return {items:[note('mixed')],data_revision:2,next_cursor:null};
    return {items:[note('current')],data_revision:2,next_cursor:null};
  }}}});
  const result=await load('../src/lib/chapter-loader.ts').readChapter('book','ch01');
  assert.equal(calls,3);assert.deepEqual(result.items.map(b=>b.id),['current']); assert.equal(result.revision,2);
});
test('dismissed search cancels pending navigation without changing the selected note',async()=>{
  const pending=deferred();let active=true;
  const s=setup({getBlock:()=>pending.promise});
  const result=s.getState().navigateBlock('target','book',()=>active);
  active=false;pending.resolve({block:note('target','ch02')});
  assert.equal(await result,false);assert.equal(s.getState().selectedBlockId,'old');
});

test('review actions can detect new typing without broadcasting every keystroke',()=>{
  const s=setup();s.getState().setDraft('first');
  const before=s.getState().getDraftVersion();let events=0;
  const off=s.subscribe(()=>events++);
  s.getState().setDraft('second');
  assert.ok(s.getState().getDraftVersion()>before);
  assert.equal(events,0);off();
});
