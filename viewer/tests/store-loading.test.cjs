const test = require('node:test');
const assert = require('node:assert/strict');
const makeLoader = require('./load-ts.cjs');
const note = (id, revision=1) => ({id,revision,doc_id:'book',chapter_id:'ch01',sources:[],tags:[],title:id,content:'',type:'definition',status:'ai_generated',deleted_at:null});
function setup(api) {
  const load=makeLoader({'../api/client':{api:{getAnchors:async()=>({items:[]}),...api}}});
  const store=load('../src/state/store.ts').useStore;
  store.setState({docId:'book',chapterId:'ch01'});
  return store;
}
test('failed pagination preserves the complete previous list and exposes retry state', async()=>{
  let call=0;
  const old=note('old');
  const s=setup({listBlocks:async()=>{if(++call===2)throw Error('offline');return {items:[note('new')],next_cursor:'next',data_revision:1};}});
  s.setState({blocks:[old]});
  await s.getState().loadBlocks();
  assert.equal(s.getState().blocks[0],old);
  assert.match(s.getState().blocksError,/offline/);
  assert.equal(s.getState().blocksLoading,false);
});
test('unchanged revisions keep card identity and updated revisions replace it',async()=>{
  const a=note('a'), b=note('b');
  const s=setup({listBlocks:async()=>({items:[{...a},note('b',2)],data_revision:3,next_cursor:null})});
  s.setState({blocks:[a,b]});
  await s.getState().loadBlocks();
  assert.equal(s.getState().blocks[0],a);
  assert.notEqual(s.getState().blocks[1],b);
  assert.equal(s.getState().blocks[1].revision,2);
});
test('late failure from an old chapter cannot overwrite current loading/result state', async()=>{
  let reject;
  const s=setup({listBlocks:async(doc,ch)=>ch==='ch01'?new Promise((_,r)=>{reject=r;}):{items:[note('current')],data_revision:1,next_cursor:null}});
  const old=s.getState().loadBlocks();
  await s.getState().selectChapter('ch02');
  reject(Error('old failure'));
  await old;
  assert.equal(s.getState().blocks[0].id,'current');
  assert.equal(s.getState().blocksError,null);
  assert.equal(s.getState().blocksLoading,false);
});
test('clearing document selection invalidates a pending manifest request',async()=>{
  let resolve;
  const s=setup({getManifest:()=>new Promise(r=>{resolve=r;})});
  const pending=s.getState().selectDoc('other');
  await s.getState().selectDoc('');
  resolve({chapters:[],data_revision:1});
  await pending;
  assert.equal(s.getState().docId,null);
  assert.deepEqual(s.getState().blocks,[]);
});
test('source navigation returns focus reading to the visible PDF, including repeated jumps',()=>{
  const s=setup({});
  const anchor={anchor_id:'a',doc_id:'book',page:1};
  for(let i=0;i<2;i++) {
    s.getState().setReadingMode('focus');
    s.getState().setHighlight(anchor);
    assert.equal(s.getState().readingMode,'split');
  }
});
test('refreshing unchanged notes retries previously failed source loads',async()=>{
  let calls=0;
  const block={...note('b'),sources:[{anchor_id:'a'}]};
  const s=setup({
    listBlocks:async()=>({items:[block],data_revision:1,next_cursor:null}),
    getAnchors:async()=>{if(++calls===1)throw Error('offline');return {items:[{anchor_id:'a',doc_id:'book',page:1}]};},
  });
  await s.getState().loadBlocks();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(s.getState().anchors.a,undefined);
  await s.getState().loadBlocks();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls,2);
  assert.equal(s.getState().anchors.a.page,1);
});

test('saving updates only its card and leaves newer local edits marked dirty',()=>{
  const s=setup({});
  const a=note('a'), b=note('b');
  const saved={...note('a',2),content:'saved text'};
  const anchors={source:{anchor_id:'source',doc_id:'book',page:3}};
  s.setState({blocks:[a,b],anchors,selectedBlockId:'a',view:'source',dirtyDraft:'dirty'});
  s.getState().applySavedBlock(saved);
  assert.equal(s.getState().blocks[0],saved);
  assert.equal(s.getState().blocks[1],b);
  assert.equal(s.getState().anchors,anchors);
  assert.equal(s.getState().dirtyDraft,'dirty');
  assert.equal(s.getState().selectedBlockId,'a');
});

test('old, duplicate and absent-card save responses are ignored without broadcasting',()=>{
  const s=setup({});
  const current=note('a',3);
  s.setState({blocks:[current],blocksLoading:true});
  let notifications=0;
  const unsubscribe=s.subscribe(()=>notifications++);
  s.getState().applySavedBlock(note('a',2));
  s.getState().applySavedBlock(note('a',3));
  s.getState().applySavedBlock(note('missing',4));
  assert.equal(s.getState().blocks[0],current);
  assert.equal(s.getState().blocksLoading,true);
  assert.equal(notifications,0);
  unsubscribe();
});

test('late saves cannot update the same block id after changing chapter or document',()=>{
  for (const changed of [{chapterId:'ch02'},{docId:'other'}]) {
    const s=setup({});
    const current=note('a');
    s.setState({...changed,blocks:[current],dirtyDraft:'dirty'});
    s.getState().applySavedBlock(note('a',2));
    assert.equal(s.getState().blocks[0],current);
    assert.equal(s.getState().dirtyDraft,'dirty');
  }
});

test('a chapter response started before saving cannot restore stale content',async()=>{
  let resolve;
  const s=setup({listBlocks:()=>new Promise(r=>{resolve=r;})});
  const old=note('a');
  s.setState({blocks:[old]});
  const pending=s.getState().loadBlocks();
  assert.equal(s.getState().blocksLoading,true);
  const saved={...note('a',2),content:'saved text'};
  s.getState().applySavedBlock(saved);
  assert.equal(s.getState().blocksLoading,false);
  resolve({items:[old],next_cursor:null,data_revision:1});
  await pending;
  assert.equal(s.getState().blocks[0],saved);
  assert.equal(s.getState().blocksError,null);
});

test('a late chapter error after saving cannot introduce a misleading load failure',async()=>{
  let reject;
  const s=setup({listBlocks:()=>new Promise((_,r)=>{reject=r;})});
  s.setState({blocks:[note('a')]});
  const pending=s.getState().loadBlocks();
  const saved=note('a',2);
  s.getState().applySavedBlock(saved);
  reject(Error('old failure'));
  await pending;
  assert.equal(s.getState().blocks[0],saved);
  assert.equal(s.getState().blocksLoading,false);
  assert.equal(s.getState().blocksError,null);
});

test('typing publishes only clean-to-dirty transitions, including empty edited content',()=>{
  const s=setup({});
  let notifications=0;
  const unsubscribe=s.subscribe(()=>notifications++);
  s.getState().setDraft(null);
  assert.equal(notifications,0);
  for (const text of ['a','ab','abc','']) s.getState().setDraft(text);
  assert.equal(s.getState().dirtyDraft,'dirty');
  assert.equal(notifications,1);
  s.getState().setDraft(null);
  s.getState().setDraft(null);
  assert.equal(s.getState().dirtyDraft,null);
  assert.equal(notifications,2);
  unsubscribe();
});
