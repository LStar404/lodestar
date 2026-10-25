import { useEffect, useRef, useState } from "react";
import { api, type NoteBlock } from "../api/client";
import { renderMarkdown } from "../lib/markdown";
import { useStore } from "../state/store";

export default function BlockEditor({ block, mode }: { block: NoteBlock; mode: "source" | "preview" }) {
  const docId = useStore((s) => s.docId);
  const loadBlocks = useStore((s) => s.loadBlocks);
  const setToast = useStore((s) => s.setToast);
  const setDraft = useStore((s) => s.setDraft);
  const clearStaleDraftOnUnmount = useStore((s) => s.clearStaleDraftOnUnmount);
  const [title, setTitle] = useState(block.title);
  const [content, setContent] = useState(block.content);
  const [error, setError] = useState<string | null>(null);
  // 卸载清理用快照：组件内最新编辑值 vs 最近一次 props（服务端版本）。
  // 在 render 期同步更新，保证 cleanup 读得到卸载前的最后一帧。
  const latest = useRef({ title, content, blockTitle: block.title, blockContent: block.content });
  latest.current = { title, content, blockTitle: block.title, blockContent: block.content };

  // 卸载清理（草稿守卫 backstop）：编辑器因 key 随 revision、blocks 刷新、切书等
  // 「非用户确认」的原因卸载时，组件内草稿已随实例消失；若这里确实还有未保存修改，
  // 把 store 里的脏标记也清掉，避免僵尸 dirtyDraft 让之后每次切块都假确认。
  // 正常保存/还原/受保护的切块在卸载前已清掉 draft，这里自然 no-op；
  // React StrictMode 首次双挂载时 local==props，也不会误清。
  useEffect(() => {
    return () => {
      const l = latest.current;
      clearStaleDraftOnUnmount(
        block.id,
        l.title !== l.blockTitle || l.content !== l.blockContent,
      );
    };
    // 只按块 id 重建：切换块时旧实例卸载才需要清理
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [block.id]);

  const save = async () => {
    try {
      await api.updateBlock(block.id, {
        title,
        content,
        expected_revision: block.revision,
        message: "人工编辑",
        author: "human",
      });
      setError(null);
      setDraft(null);
      setToast("已保存（revision " + (block.revision + 1) + "）");
      await loadBlocks();
    } catch (e: any) {
      if (e.code === "REVISION_CONFLICT") {
        setError("数据已被修改（当前 revision " + e.detail?.current_revision + "），请刷新后重试");
      } else {
        setError(e.message);
      }
    }
  };

  useEffect(() => {
    const onSave = () => void save();
    window.addEventListener("note-gen:save", onSave);
    return () => window.removeEventListener("note-gen:save", onSave);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [title, content, block]);

  if (mode === "preview") {
    return (
      <div
        className="block-preview-view"
        dangerouslySetInnerHTML={{
          __html: renderMarkdown(content, docId ?? "", {}),
        }}
      />
    );
  }

  return (
    <div className="block-editor">
      <input
        value={title}
        onChange={(e) => {
          setTitle(e.target.value);
          setDraft(e.target.value);
        }}
      />
      <textarea
        value={content}
        rows={10}
        onChange={(e) => {
          setContent(e.target.value);
          setDraft(e.target.value);
        }}
      />
      {error && <div className="error">{error}</div>}
      <div className="editor-actions">
        <button onClick={() => void save()}>保存（Ctrl+S）</button>
        <button
          onClick={() => {
            setTitle(block.title);
            setContent(block.content);
            setError(null);
            setDraft(null);   // 还原后不再是草稿态：否则切换视图误报「未保存草稿」
          }}
        >
          还原
        </button>
      </div>
    </div>
  );
}
