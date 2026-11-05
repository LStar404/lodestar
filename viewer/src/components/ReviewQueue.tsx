import { useEffect, useRef, useState } from "react";
import { useStore } from "../state/store";
import { api, type NoteBlock } from "../api/client";
import MathText from "./MathText";
import Icon from "./Icon";

export default function ReviewQueue() {
  const open = useStore((s) => s.reviewOpen);
  const items = useStore((s) => s.reviewItems);
  const loading = useStore((s) => s.reviewLoading);
  const error = useStore((s) => s.reviewError);
  const manifest = useStore((s) => s.manifest);
  const docId = useStore((s) => s.docId);
  const setOpen = useStore((s) => s.openReview);
  const pending = useRef(new Set<string>());
  const [busy, setBusy] = useState<string[]>([]);
  const modal = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    modal.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => { if (previous?.isConnected) previous.focus(); };
  }, [open]);

  const run = async (block: NoteBlock, action: "locate" | "confirm") => {
    if (pending.current.has(block.id) || !docId) return;
    pending.current.add(block.id); setBusy([...pending.current]);
    const st = useStore.getState();
    const draft = st.dirtyDraft;
    const draftVersion = st.getDraftVersion();
    const discardsDraft = action === "confirm" && block.id === st.selectedBlockId && st.view !== "card" && draft !== null;
    try {
      if (action === "locate") {
        if (!await st.navigateBlock(block.id, docId)) return;
        void setOpen(false);
        const first = block.sources?.[0]?.anchor_id;
        if (first) {
          const { items: anchors } = await api.getAnchors(docId, [first]);
          const current = useStore.getState();
          if (current.docId !== docId || current.selectedBlockId !== block.id) return;
          if (anchors[0]) { current.setHighlight(anchors[0]); current.setPdfPage(anchors[0].page); }
        }
      } else {
        if (discardsDraft && !window.confirm("正在编辑的笔记有未保存草稿，确认后刷新将丢弃草稿，继续吗？")) return;
        await api.updateBlock(block.id, { status: "manually_edited", expected_revision: block.revision,
          message: "人工确认", author: "human" });
        const current = useStore.getState();
        if (current.docId !== docId) return;
        if (discardsDraft && current.selectedBlockId === block.id && current.dirtyDraft === draft && current.getDraftVersion() === draftVersion) current.setDraft(null);
        current.setToast("已确认，这条笔记已移出待审核列表");
        await current.loadBlocks();
        if (useStore.getState().docId !== docId) return;
        if (useStore.getState().reviewOpen) await setOpen(true);
        else await useStore.getState().refreshReviewCount();
      }
    } catch (e: any) {
      if (useStore.getState().docId === docId) st.setToast((action === "confirm" ? "确认失败：" : "原文暂时无法打开：") + e.message);
    } finally {
      pending.current.delete(block.id); setBusy([...pending.current]);
    }
  };
  if (!open) return null;
  return <div className="modal-mask" onClick={() => void setOpen(false)}>
    <div className="modal review-dialog" ref={modal} role="dialog" aria-modal="true" aria-labelledby="review-title"
      onClick={(e) => e.stopPropagation()} onKeyDown={(e) => {
        if (e.key === "Escape") { e.stopPropagation(); void setOpen(false); }
        if (e.key !== "Tab") return;
        const buttons = [...(modal.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])];
        const first = buttons[0], last = buttons[buttons.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
      }}>
      <div className="review-heading"><div><span className="chapter-eyebrow">REVIEW NOTES</span>
        <h2 id="review-title">让知识更可靠 <span className="pane-count">{items.length}</span></h2>
        <p>对照教材检查内容，确认后继续安心阅读。</p></div>
        <button className="review-close" aria-label="关闭审核列表" onClick={() => void setOpen(false)}>×</button></div>
      <div className="review-content" aria-busy={loading}>
        {loading && <div className="review-feedback" role="status">正在更新待审核笔记…</div>}
        {error && <div className="notes-load-error" role="alert"><span>{error}</span><button onClick={() => void setOpen(true)}>重试</button></div>}
        {!loading && !error && items.length === 0 && <div className="review-empty"><Icon name="book" size={32} /><strong>本书笔记已全部审核</strong><p>接下来，专注于理解和复习。</p></div>}
        {items.map((b, index) => <article key={b.id} className="review-card">
          <div className="review-card-meta"><span>{String(index + 1).padStart(2, "0")} · {manifest?.chapters?.find((c: any) => c.chapter_id === b.chapter_id)?.title || b.chapter_id}</span><span className="review-badge">待审核</span></div>
          <h3><MathText text={b.title} /></h3><p className="review-excerpt">{b.content?.slice(0, 180) || "内容尚未补全，可对照原文后继续生成。"}</p>
          <div className="review-card-footer"><span>{b.updated_at?.replace("T", " ").slice(0, 16)}</span>
            <div><button disabled={busy.includes(b.id)} onClick={() => void run(b, "locate")}><Icon name="book" size={14} />对照原文</button>
              <button className="review-confirm" disabled={busy.includes(b.id)} onClick={() => void run(b, "confirm")}>{busy.includes(b.id) ? "处理中…" : "确认已修"}</button></div></div>
        </article>)}
      </div>
      <div className="review-bottom"><span>因网络或余额不足而未完成的笔记，可在任务中继续生成。</span><button onClick={() => void setOpen(false)}>返回阅读</button></div>
    </div>
  </div>;
}
