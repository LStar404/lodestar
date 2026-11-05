import BookCover from "./BookCover";
import Icon from "./Icon";
import { useDeferredValue, useEffect, useMemo, useState } from "react";
import BlockList from "./BlockList";
import BlockEditor from "./BlockEditor";
import AgentCommandBar from "./AgentCommandBar";
import { sortBlocks } from "../api/types";
import { useStore } from "../state/store";
import { filterNotes } from "../lib/note-filter";

export default function NotesPane() {
  const blocks = useStore((s) => s.blocks);
  const manifest = useStore((s) => s.manifest);
  const docId = useStore((s) => s.docId);
  const chapterId = useStore((s) => s.chapterId);
  const loading = useStore((s) => s.blocksLoading);
  const error = useStore((s) => s.blocksError);
  const loadBlocks = useStore((s) => s.loadBlocks);
  const typeInfo = useStore((s) => s.typeInfo);
  const [query, setQuery] = useState("");
  const [type, setType] = useState("");
  const [reviewOnly, setReviewOnly] = useState(false);
  const deferredQuery = useDeferredValue(query);
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const revealBlock = useStore((s) => s.revealBlock);
  const selectedBlockId = useStore((s) => s.selectedBlockId);
  // 切块统一走受保护入口（Review #6）：源码/预览视图有未保存草稿时先确认再切，
  // 否则编辑器卸载即丢草稿；返回 false 表示用户取消
  const requestSelectBlock = useStore((s) => s.requestSelectBlock);
  const hasDraft = useStore((s) => s.dirtyDraft !== null);
  const setDraft = useStore((s) => s.setDraft);
  const chapter = manifest?.chapters?.find((c: any) => c.chapter_id === chapterId);
  const active = useMemo(() => sortBlocks(blocks.filter((b) => !b.deleted_at)), [blocks]);
  const filtered = useMemo(() => filterNotes(active, deferredQuery, type, reviewOnly), [active, deferredQuery, type, reviewOnly]);
  const pendingCount = active.filter((b) => b.status === "needs_review").length;
  const types = [...new Set(active.map((b) => b.type))];
  // 筛选不卸载正在编辑的卡片，避免搜索时丢掉尚未保存的本地文本。
  const editing = (view !== "card" || revealBlock?.id === selectedBlockId) ? active.find((b) => b.id === selectedBlockId) : undefined;
  const pinned = editing && !filtered.includes(editing);
  const displayed = pinned ? [editing, ...filtered] : filtered;
  useEffect(() => { setQuery(""); setType(""); setReviewOnly(false); }, [docId, chapterId]);

  useEffect(() => {
    if (!revealBlock) return;
    const frame = requestAnimationFrame(() => document.getElementById("block-" + revealBlock.id)
      ?.scrollIntoView({ behavior: "auto", block: "center" }));
    return () => cancelAnimationFrame(frame);
  }, [revealBlock]);

  // 视图切换（7.3）：只有「离开编辑器」才丢草稿——card 视图不挂载 BlockEditor，
  // 而 source ↔ preview 是同一个编辑器实例（只换渲染分支），草稿仍保留，不打断用户。
  // 顺序也重要：先选块、再切视图，草稿确认被取消时视图不得已经切走（不得有副作用）。
  const switchView = (v: "card" | "source" | "preview") => {
    if (v === view) return;
    if (v === "card" && hasDraft) {
      const ok = window.confirm("当前块有未保存草稿，切换视图将丢弃草稿，继续吗？");
      if (!ok) return;
      setDraft(null);
    }
    if (v !== "card" && !selectedBlockId) {
      const first = sortBlocks(blocks.filter((b) => !b.deleted_at))[0];
      // 自动选中第一块同样可能弹草稿确认：取消时直接返回，视图保持原样
      if (first && !requestSelectBlock(first.id)) return;
    }
    setView(v);
  };

  return (
    <section className="notes-pane">
      <BookCover />
      {!docId && <div className="pane-heading">
        <div className="pane-label"><Icon name="notes" /><strong>章节笔记</strong><span className="pane-count">{blocks.filter((b) => !b.deleted_at).length}</span></div>
        <span className="pane-caption">梳理 · 理解 · 内化</span>
      </div>}
      {docId && <div className="chapter-overview">
        <span className="chapter-eyebrow">CHAPTER NOTES <span>{chapterId?.replace("ch", "")}</span></span>
        <h2>{chapter?.title || "选择一个章节"}</h2>
        <div className="chapter-stats"><span><b>{active.length}</b> 条笔记</span><span><b>{types.length}</b> 类知识点</span><span className={pendingCount ? "has-review" : ""}>{pendingCount ? `${pendingCount} 条待审核` : "每一条笔记，都有迹可循"}</span></div>
      </div>}
      <div className="notes-toolbar">
        <span className="section-caption">阅读视图</span>
        <div className="view-switch" aria-label="笔记视图">
        <button aria-pressed={view === "card"} onClick={() => switchView("card")} className={view === "card" ? "active" : ""}>卡片</button>
        <button aria-pressed={view === "source"} onClick={() => switchView("source")} className={view === "source" ? "active" : ""}>编辑</button>
        <button aria-pressed={view === "preview"} onClick={() => switchView("preview")} className={view === "preview" ? "active" : ""}>预览</button>
        </div>
      </div>
      {docId && <div className="note-discovery">
        <div className="notes-search"><Icon name="search" size={16} />
          <input aria-label="搜索本章笔记" placeholder="搜索本章标题、正文或标签" value={query} onChange={(e) => setQuery(e.target.value)} />
          {query && <button aria-label="清空章内搜索" onClick={() => setQuery("")}>×</button>}
        </div>
        <div className="note-filters">
          <div className="filter-tabs">
            <button className={!reviewOnly ? "active" : ""} aria-pressed={!reviewOnly} onClick={() => setReviewOnly(false)}>全部 <span>{active.length}</span></button>
            <button className={reviewOnly ? "active" : ""} aria-pressed={reviewOnly} onClick={() => setReviewOnly(true)}>待审核 <span>{pendingCount}</span></button>
          </div>
          <select aria-label="筛选笔记类型" value={type} onChange={(e) => setType(e.target.value)}>
            <option value="">全部类型</option>
            {types.map((t) => <option key={t} value={t}>{typeInfo[t]?.name || t}</option>)}
          </select>
        </div>
        <div className="notes-result-hint" aria-live="polite"><span>{loading ? "正在更新笔记…" : `显示 ${filtered.length} 条笔记`}</span><span>{pinned ? (view === "card" ? "已定位的笔记已保留" : "正在编辑的笔记已保留") : view === "card" ? "点击卡片阅读，点击编辑修改" : "选择笔记，直接编辑"}</span></div>
      </div>}
      {error && <div className="notes-load-error" role="alert"><span>{error}</span><button disabled={loading} onClick={() => void loadBlocks()}>重新加载</button></div>}
      <BlockList
        blocks={displayed}
        loading={loading && active.length === 0}
        empty={error ? <div className="empty">重新加载后继续阅读</div> : active.length > 0 ? <div className="workspace-empty no-results"><Icon name="search" size={28} /><strong>没有找到匹配的笔记</strong><p>试试其他关键词，或清除筛选条件。</p><button onClick={() => { setQuery(""); setType(""); setReviewOnly(false); }}>查看全部笔记</button></div> : undefined}
        selectedId={selectedBlockId}
        onSelect={requestSelectBlock}
        renderEditor={(b) =>
          view === "card" ? null : (
            // 保存后由编辑器同步版本，保持实例以保留光标和保存期间的新输入。
            <BlockEditor key={docId + ":" + b.id} block={b} mode={view} />
          )
        }
      />
      <AgentCommandBar />
    </section>
  );
}
