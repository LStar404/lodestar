import { memo, useEffect, useMemo, useRef, useState } from "react";
import { api, type NoteBlock } from "../api/client";
import { renderMarkdown } from "../lib/markdown";
import { markdownEdit, mergeSavedText, sameText, type EditCommand, type EditorText } from "../lib/editor-commands";
import { useStore } from "../state/store";
import { readBrand, writeBrand } from "../lib/brand";
import "./editor.css";

const Preview = memo(function Preview({ content, docId }: { content: string; docId: string }) {
  const html = useMemo(() => renderMarkdown(content, docId), [content, docId]);
  return <div className="block-preview-view" dangerouslySetInnerHTML={{ __html: html }} />;
});
const commands: [EditCommand, string, string][] = [
  ["bold", "加粗", "Ctrl / ⌘ + B"], ["italic", "斜体", "Ctrl / ⌘ + I"],
  ["heading", "标题", "插入二级标题"], ["list", "列表", "插入无序列表"],
  ["quote", "引用", "插入引用"], ["math", "公式", "插入行内公式"],
  ["code", "代码", "插入行内代码"], ["link", "链接", "插入链接"],
];

export default function BlockEditor({ block, mode }: { block: NoteBlock; mode: "source" | "preview" }) {
  const docId = useStore((s) => s.docId);
  const setDraft = useStore((s) => s.setDraft);
  const applySavedBlock = useStore((s) => s.applySavedBlock);
  const [draft, setLocalDraft] = useState<EditorText>({ title: block.title, content: block.content });
  const current = useRef(draft);
  const baseline = useRef(block);
  const [saving, setSaving] = useState(false);
  const inFlight = useRef(false);
  const pending = useRef<EditorText | null>(null);
  const mounted = useRef(false);
  const generation = useRef(0);
  const [error, setError] = useState<string | null>(null);
  const [autoSave, setAutoSave] = useState(() => {
    try { return readBrand("auto-save") !== "false"; } catch { return true; }
  });
  const [showPreview, setShowPreview] = useState(false);
  const [previewContent, setPreviewContent] = useState(draft.content);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const composing = useRef(false);
  const [isComposing, setIsComposing] = useState(false);
  const hasChanges = (text: EditorText) => !sameText(text, baseline.current)
    || (pending.current !== null && !sameText(text, pending.current));
  const dirty = hasChanges(draft);

  const edit = (next: EditorText) => {
    current.current = next;
    setLocalDraft(next);
    setDraft(hasChanges(next) ? "dirty" : null);
  };
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      generation.current++;
      const st = useStore.getState();
      if (st.docId === docId) st.clearStaleDraftOnUnmount(block.id, hasChanges(current.current));
    };
  }, [block.id, docId]);

  // loadBlocks guards external changes. Own saves publish only after advancing this baseline.
  useEffect(() => {
    if (block.revision === baseline.current.revision) return;
    generation.current++;
    pending.current = null;
    baseline.current = block;
    edit({ title: block.title, content: block.content });
    setError(null);
  }, [block]);
  useEffect(() => {
    if (mode === "source") textarea.current?.focus({ preventScroll: true });
  }, [mode]);
  // Expensive Markdown / KaTeX work is debounced and memoized independently of typing.
  useEffect(() => {
    if (mode !== "preview" && !showPreview) return;
    const timer = window.setTimeout(() => setPreviewContent(draft.content), 250);
    return () => window.clearTimeout(timer);
  }, [draft.content, mode, showPreview]);

  const save = async () => {
    if (inFlight.current || composing.current || sameText(current.current, baseline.current)) return;
    inFlight.current = true;
    setSaving(true);
    setError(null);
    const sent = { ...current.current };
    pending.current = sent;
    const version = generation.current;
    try {
      const saved = await api.updateBlock(block.id, {
        ...sent, expected_revision: baseline.current.revision, message: "人工编辑", author: "human",
      });
      if (!mounted.current || version !== generation.current) {
        const st = useStore.getState();
        // A late save may refresh another card, but never replace a reopened dirty editor.
        if (st.selectedBlockId !== saved.id || st.dirtyDraft === null) applySavedBlock(saved);
        return;
      }
      const next = mergeSavedText(current.current, sent, saved);
      pending.current = null;
      baseline.current = saved;
      edit(next);
      applySavedBlock(saved);
    } catch (e: any) {
      if (!mounted.current || version !== generation.current) return;
      setError(e.code === "REVISION_CONFLICT" || e.status === 409
        ? "这条笔记已在别处修改，当前输入已保留。请复制草稿后重新加载，再合并修改。"
        : "保存失败，当前输入已保留：" + (e.message || "请检查连接后重试"));
    } finally {
      inFlight.current = false;
      pending.current = null;
      if (mounted.current && version === generation.current) setDraft(hasChanges(current.current) ? "dirty" : null);
      if (mounted.current) setSaving(false);
    }
  };
  const saveRef = useRef(save);
  saveRef.current = save;
  useEffect(() => {
    const onSave = () => void saveRef.current();
    window.addEventListener("lodestar:save", onSave);
    return () => window.removeEventListener("lodestar:save", onSave);
  }, []);
  useEffect(() => {
    if (!autoSave || !dirty || saving || error || isComposing) return;
    const timer = window.setTimeout(() => void saveRef.current(), 1800);
    return () => window.clearTimeout(timer);
  }, [draft, autoSave, dirty, saving, error, isComposing]);

  const format = (command: EditCommand) => {
    const field = textarea.current;
    if (!field || composing.current) return;
    const change = markdownEdit(field.value, field.selectionStart, field.selectionEnd, command);
    field.focus();
    field.setSelectionRange(change.from, change.to);
    // insertText keeps formatting in Chromium's native undo stack.
    const inserted = document.execCommand("insertText", false, change.replacement);
    if (!inserted) field.setRangeText(change.replacement, change.from, change.to, "end");
    edit({ ...current.current, content: field.value });
    field.setSelectionRange(change.selectionStart, change.selectionEnd);
  };
  const copyDraft = async () => {
    try {
      await navigator.clipboard.writeText(current.current.title + "\n\n" + current.current.content);
      useStore.getState().setToast("草稿已复制");
    } catch { setError("无法访问剪贴板，请在编辑框中全选并复制草稿。"); }
  };
  const compositionProps = {
    onCompositionStart: () => { composing.current = true; setIsComposing(true); },
    onCompositionEnd: () => { composing.current = false; setIsComposing(false); },
  };

  return <div className="block-editor" data-dirty={dirty}>
    <div className="editor-topbar">
      <strong>{mode === "preview" ? "笔记预览" : "编辑笔记"}</strong>
      <label className="editor-autosave"><input type="checkbox" checked={autoSave} onChange={(e) => {
        setAutoSave(e.target.checked);
        try { writeBrand("auto-save", String(e.target.checked)); } catch { /* optional preference */ }
      }} />自动保存</label>
      {mode === "source" && <button aria-pressed={showPreview} onClick={() => {
        setPreviewContent(current.current.content); setShowPreview(!showPreview);
      }}>{showPreview ? "收起预览" : "边写边预览"}</button>}
    </div>
    {mode === "source" && <>
      <label className="editor-field-label">笔记标题
        <input aria-label="笔记标题" value={draft.title} {...compositionProps}
          onChange={(e) => edit({ ...current.current, title: e.target.value })} />
      </label>
      <div className="editor-formatbar" role="group" aria-label="常用格式">
        {commands.map(([command, label, hint]) => <button key={command} title={hint}
          onMouseDown={(e) => e.preventDefault()} onClick={() => format(command)}>{label}</button>)}
      </div>
    </>}
    <div className={"editor-workspace" + (showPreview && mode === "source" ? " with-preview" : "")}>
      {mode === "source" && <textarea ref={textarea} aria-label="笔记正文" value={draft.content} rows={16}
        placeholder="写下你的理解…支持 Markdown 和 LaTeX 公式" spellCheck={false} {...compositionProps}
        onChange={(e) => edit({ ...current.current, content: e.target.value })}
        onKeyDown={(e) => {
          if (e.nativeEvent.isComposing || composing.current) return;
          if ((e.ctrlKey || e.metaKey) && ["b", "i"].includes(e.key.toLowerCase())) {
            e.preventDefault(); format(e.key.toLowerCase() === "b" ? "bold" : "italic");
          }
        }} />}
      {(mode === "preview" || showPreview) && <Preview content={mode === "preview" ? draft.content : previewContent} docId={docId ?? ""} />}
    </div>
    {error && <div className="error" role="alert">{error} <button onClick={() => void copyDraft()}>复制草稿</button></div>}
    <div className="editor-actions">
      <span className={"editor-save-status" + (error ? " has-error" : "")} role="status">
        {saving ? "正在保存…" : error ? "尚未保存" : dirty ? autoSave ? "等待自动保存…" : "有未保存修改" : "已保存"}
      </span>
      <button disabled={!dirty || saving || isComposing} onClick={() => void save()} title="Ctrl / ⌘ + S">
        {saving ? "保存中…" : error ? "重试保存" : "保存"}
      </button>
      <button disabled={!dirty || saving} onClick={() => {
        if (!window.confirm("放弃当前未保存修改，恢复到上次保存的内容？")) return;
        edit({ title: baseline.current.title, content: baseline.current.content }); setError(null);
      }}>撤销未保存修改</button>
    </div>
    <div className="editor-hint">Ctrl / ⌘ + S 保存 · Ctrl / ⌘ + Z 撤销 · 自动保存会在停止输入后执行</div>
  </div>;
}
