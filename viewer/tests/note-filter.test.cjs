const test=require('node:test');
const assert=require('node:assert/strict');
const {filterNotes}=require('./load-ts.cjs')()('../src/lib/note-filter.ts');
test('search combines Unicode-normalized title/content/tags with type and review filters',()=>{
  const a={id:'a',title:'ＡＢＣ 定理',content:'可逆矩阵',tags:['期末'],type:'theorem',status:'needs_review'};
  const b={...a,id:'b',status:'ai_generated'};
  const removed={...a,id:'c',deleted_at:'2026-11-04'};
  assert.deepEqual(filterNotes([a,b,removed],'abc 期末','theorem',true),[a]);
  assert.deepEqual(filterNotes([a,b,removed],'可逆','',false),[a,b]);
  assert.deepEqual(filterNotes([a],'','definition',false),[]);
});
