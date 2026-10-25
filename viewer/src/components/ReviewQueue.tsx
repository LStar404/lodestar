import { useStore } from "../state/store";
import { api } from "../api/client";
import { STATUS_CN } from "./BlockCard";
import MathText from "./MathText";

async function locateBlock(block: any) {
  const st = useStore.getState();
  // 草稿守卫（Review #6 残余）：切块入口统一走受保护动作。返回 false = 用户在确认框里
  // 取消，此时选中块不变，且后面一切都不得执行——不滚动、不请求锚点、不写高亮、不切页。
  if (!st.requestSelectBlock(block.id)) return;
  document.getElementById("block-" + block.id)?.scrollIntoView({ behavior: "smooth", block: "center" });
  // 对照 sources 原文（7.4 待审核工作流）
  const first = block.sources?.[0]?.anchor_id;
  if (first) {
    try {
      const anchor = await api.getAnchor(first);
      // 锚点请求是异步的：等待期间用户可能又切到别的块。晚到的锚点只允许写回
      // 「当前选中块仍是本次目标」的情况，否则会把界面跳去一个已经离开的块。
      if (useStore.getState().selectedBlockId !== block.id) return;
      st.setHighlight(anchor);
      st.setPdfPage(anchor.page);
    } catch {
      /* 锚点缺失时仅定位块 */
    }
  }
}

export default function ReviewQueue() {
  const open = useStore((s) => s.reviewOpen);
  const items = useStore((s) => s.reviewItems);
  const setOpen = useStore((s) => s.openReview);
  const loadBlocks = useStore((s) => s.loadBlocks);
  const setToast = useStore((s) => s.setToast);
  if (!open) return null;

  const confirm = async (id: string, revision: number) => {
    // 草稿守卫（同源）：确认已修会写库并刷新列表；若正在编辑的就是这一块，
    // revision 变化会让 BlockEditor 按 key 重挂载、未保存草稿丢失——先确认。
    // P3-7：确认后不先清标记，只有写库+刷新成功才清（失败保留，之后切块仍会确认）。
    const st = useStore.getState();
    if (id === st.selectedBlockId && st.view !== "card" && st.dirtyDraft !== null) {
      const ok =
        typeof window !== "undefined" && typeof window.confirm === "function"
          ? window.confirm("正在编辑的块有未保存草稿，确认后刷新将丢弃草稿，继续吗？")
          : true;
      if (!ok) return;
    }
    try {
      await api.updateBlock(id, {
        status: "manually_edited",
        expected_revision: revision,
        message: "人工确认",
        author: "human",
      });
      // 成功才清草稿标记（先清再刷新，避免 loadBlocks 的草稿保护再弹一次确认）
      useStore.getState().setDraft(null);
      setToast("已确认");
      await loadBlocks();
      await setOpen(true);
    } catch (e: any) {
      setToast("确认失败：" + e.message);
    }
  };

  return (
    <div className="modal-mask" onClick={() => setOpen(false)}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>待审核（needs_review）</h2>
        {items.length === 0 && <div className="empty">没有待审核的块</div>}
        {items.map((b) => (
          <div key={b.id} className="review-row">
            <div>
              <b><MathText text={b.title} /></b> <span className="muted">{STATUS_CN[b.status]}</span>
            </div>
            <div className="muted">{(b.content ?? "").slice(0, 120)}</div>
            <div className="muted" style={{ fontSize: 11 }}>
              记录时间 {(b as any).updated_at
                ? String((b as any).updated_at).replace("T", " ").slice(0, 19)
                : "—"}
              （余额/网络类失败是当时的记录：充值或稍后点「继续生成」会自动补全本块）
            </div>
            <button onClick={() => void locateBlock(b)}>定位并对照原文</button>
            <button onClick={() => void confirm(b.id, b.revision)}>确认已修</button>
          </div>
        ))}
        <button onClick={() => setOpen(false)}>关闭</button>
      </div>
    </div>
  );
}
