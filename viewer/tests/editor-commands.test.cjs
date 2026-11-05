const test = require('node:test');
const assert = require('node:assert/strict');
const { markdownEdit, mergeSavedText, sameText } = require('./load-ts.cjs')()('../src/lib/editor-commands.ts');

function apply(text, start, end, command) {
  const edit = markdownEdit(text, start, end, command);
  assert.ok(edit.from >= 0 && edit.from <= edit.to && edit.to <= text.length, 'replacement must stay inside the original selection range or its line prefix');
  const value = text.slice(0, edit.from) + edit.replacement + text.slice(edit.to);
  assert.ok(edit.selectionStart >= 0 && edit.selectionStart <= edit.selectionEnd && edit.selectionEnd <= value.length, 'result selection must stay inside the new text');
  return { value, selection: value.slice(edit.selectionStart, edit.selectionEnd) };
}

for (const [command, formatted] of [
  ['bold', '**选中文字**'],
  ['italic', '*选中文字*'],
  ['math', '$选中文字$'],
  ['code', '`选中文字`'],
  ['link', '[选中文字](https://)'],
]) {
  test(`${command} formats the selected text without changing its neighbours`, () => {
    assert.deepEqual(apply('前文选中文字后文', 2, 6, command), {
      value: `前文${formatted}后文`, selection: '选中文字',
    });
  });
}

test('an empty selection inserts a placeholder at the cursor and selects only its editable body', () => {
  assert.deepEqual(apply('前后', 1, 1, 'math'), { value: '前$x^2$后', selection: 'x^2' });
  assert.deepEqual(apply('', 0, 0, 'link'), { value: '[链接文字](https://)', selection: '链接文字' });
});

test('formatting the first line preserves its unselected suffix and following paragraph', () => {
  assert.deepEqual(apply('第一行正文\n第二段', 0, 3, 'heading'), {
    value: '## 第一行正文\n第二段', selection: '第一行',
  });
});

test('a heading command at a cursor inside a line starts at the beginning of that line', () => {
  assert.equal(apply('第一段\n第二段正文', 7, 7, 'heading').value, '第一段\n## 第二段正文');
});

test('multiline selection prefixes each touched line and preserves neighbouring paragraphs', () => {
  const text = '保留\n第一段\n第二段\n末段';
  assert.equal(apply(text, text.indexOf('一段'), text.indexOf('段\n末段'), 'list').value,
    '保留\n- 第一段\n- 第二段\n末段');
});

test('a selection ending at the next line start does not format that next line', () => {
  const text = '第一段\n第二段\n保持原样';
  assert.deepEqual(apply(text, 0, text.indexOf('保持'), 'quote'), {
    value: '> 第一段\n> 第二段\n保持原样', selection: '第一段\n> 第二段',
  });
});

test('formatting an empty first line does not move text across the leading newline', () => {
  assert.deepEqual(apply('\n第二段', 0, 0, 'heading'), {
    value: '## \n第二段', selection: '',
  });
});

test('formatting at the end of a trailing newline creates a new list item', () => {
  assert.deepEqual(apply('第一段\n', 4, 4, 'list'), {
    value: '第一段\n- ', selection: '',
  });
});

test('saving adopts server-normalized fields when the user has made no further edits', () => {
  const sent = { title: ' 标题 ', content: '正文\r\n' };
  const saved = { title: '标题', content: '正文\n' };
  const merged = mergeSavedText({ ...sent }, sent, saved);
  assert.deepEqual(merged, saved);
  assert.equal(sameText(merged, saved), true);
});

test('editing the title while saving keeps the new title and adopts normalized saved content', () => {
  const sent = { title: '原标题', content: '正文\r\n' };
  const saved = { title: '原标题', content: '正文\n' };
  const current = { ...sent, title: '保存期间的新标题' };
  const merged = mergeSavedText(current, sent, saved);
  assert.deepEqual(merged, { title: current.title, content: saved.content });
  assert.equal(sameText(merged, saved), false);
});

test('editing the body while saving keeps all new text and adopts the normalized saved title', () => {
  const sent = { title: ' 标题 ', content: '旧正文' };
  const saved = { title: '标题', content: '旧正文' };
  const current = { ...sent, content: '旧正文\n保存期间继续输入的文字' };
  const merged = mergeSavedText(current, sent, saved);
  assert.deepEqual(merged, { title: saved.title, content: current.content });
  assert.equal(sameText(merged, saved), false);
});

test('editing both fields while saving keeps both local values, including clearing the body', () => {
  const sent = Object.freeze({ title: '原标题', content: '旧正文' });
  const saved = Object.freeze({ title: '原标题', content: '规范化正文' });
  const current = Object.freeze({ title: '新标题', content: '' });
  assert.deepEqual(mergeSavedText(current, sent, saved), current);
});

test('sameText notices edits to either field and returning both fields to baseline clears dirty state', () => {
  const base = { title: '标题', content: '正文' };
  assert.equal(sameText(base, { ...base, title: '新标题' }), false);
  assert.equal(sameText(base, { ...base, content: '新正文' }), false);
  assert.equal(sameText(base, { ...base }), true);
});
