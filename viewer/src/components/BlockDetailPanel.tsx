import Icon from "./Icon";
import { useEffect, useRef, useState } from "react";
import { api, type NoteBlock } from "../api/client";
import { STATUS_CN, sortBlocks } from "../api/types";
import { useStore } from "../state/store";
import MathText from "./MathText";

/** 重排目标计算（Review #12）：后端 `reorder_block(block_id, after)` 的语义是
 *  「把 block 插到 after 之后」，after=null 表示插到最前；而它插入的基准是
 *  排除自身后的排序列表。因此：
 *   - 上移一格 = 插到「前驱的前驱」之后（下标 i-2）；i==1 时前驱没有前驱，
 *     after=null（移到开头）；i==0 已在最前，禁用（避免只涨 revision 的假操作）。
 *   - 下移一格 = 插到直接后继之后（下标 i+1），与后端语义一致；最后一块下移
 *     后端会因 after=自身 报 NotFound，直接禁用。
 *  可见块列表与 BlockList 的渲染顺序同源：sortBlocks + 排除软删块。 */
export function reorderTarget(
  ordered: NoteBlock[],
  blockId: string,
  direction: "up" | "down",
): { after: string | null; enabled: boolean } {
  const i = ordered.findIndex((b) => b.id === blockId);
  if (i < 0) return { after: null, enabled: false };
  if (direction === "up") {
    return { after: i >= 2 ? ordered[i - 2].id : null, enabled: i > 0 };
  }
  return {
    after: i + 1 < ordered.length ? ordered[i + 1].id : null,
    enabled: i < ordered.length - 1,
  };
}

export default function BlockDetailPanel() {
  const docId = useStore((s) => s.docId);
  const chapterId = useStore((s) => s.chapterId);
  const blocks = useStore((s) => s.blocks);
  const selectedBlockId = useStore((s) => s.selectedBlockId);
  const selectionHits = useStore((s) => s.selectionHits);
  const pdfPage = useStore((s) => s.pdfPage);
  const anchors = useStore((s) => s.anchors);
  const fetchAnchors = useStore((s) => s.fetchAnchors);
  const loadBlocks = useStore((s) => s.loadBlocks);
  const setPdfPage = useStore((s) => s.setPdfPage);
  const setHighlight = useStore((s) => s.setHighlight);
  const setToast = useStore((s) => s.setToast);
  const [detail, setDetail] = useState<{ block: NoteBlock; history: any[] } | null>(null);
  const [pageBlocks, setPageBlocks] = useState<any[]>([]);
  // 跨章链接要按标题显示：blocks 只有当前章，曾把其他章的链接显示成原始块 id
  const [titleById, setTitleById] = useState<Record<string, string>>({});
  // 可见块（与 BlockList 渲染同源）：重排目标按这个排序列表算，不能用 store.blocks 的原始顺序
  const ordered = sortBlocks(blocks.filter((b) => !b.deleted_at));
  const up = reorderTarget(ordered, detail?.block.id ?? "", "up");
  const down = reorderTarget(ordered, detail?.block.id ?? "", "down");

  // 切块统一走草稿守卫（Review #6）：换选中块会卸载源码/预览编辑器并丢草稿
  const goToBlock = (id: string) => { void useStore.getState().navigateBlock(id); };

  // 草稿守卫（Review #6 残余）：删除/回滚/重排/解锁会改选中块的 revision 或让它从列表
  // 消失，BlockEditor 按 key=id:revision 重挂载 → 未保存草稿丢失。先确认再发请求；
  // 取消时直接 return，不产生任何副作用。P3-7：确认后不先清标记——只有请求真的成功
  // （act 里）才清，失败时标记保留，之后切块仍会确认（不会因失败而悄悄丢草稿）。
  const guardDraft = (): boolean => {
    const st = useStore.getState();
    if (st.view === "card" || st.dirtyDraft === null) return true;
    return window.confirm("当前块有未保存草稿，继续操作将丢弃草稿，继续吗？");
  };

  // detail 拉取代次（可选加固）：快速切块时旧的 getBlock 可能晚到并覆盖新块的详情，
  // 导致随后对「上一块」发起回滚/重排。晚到响应一律丢弃。
  const detailSeq = useRef(0);

  useEffect(() => {
    const seq = ++detailSeq.current;
    if (!selectedBlockId) {
      setDetail(null);
      return;
    }
    api.getBlock(selectedBlockId).then((d: any) => {
      if (seq !== detailSeq.current) return;
      setDetail(d);
      // 来源页码要显示 p.N：本面板此前只预取「选区命中」的锚点，
      // 块自己的来源全渲染成「p.?」（卡片/放大层都有预取，唯独这里漏了）
      void fetchAnchors((d?.block?.sources ?? []).map((s: any) => s.anchor_id));
    }).catch(() => {
      if (seq === detailSeq.current) setDetail(null);
    });
    return () => {
      detailSeq.current += 1;   // 卸载/依赖变化：之前那次请求作废
    };
  }, [selectedBlockId, blocks, docId]);

  useEffect(() => {
    setPageBlocks([]);
    if (!docId || !pdfPage) return;
    let cancelled = false;
    api.pageBlocks(docId, pdfPage).then((rows) => {
      if (!cancelled) setPageBlocks(rows);
    }).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [docId, pdfPage, blocks]);

  useEffect(() => {
    void fetchAnchors(selectionHits.map((h: any) => h.anchor_id));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectionHits]);

  useEffect(() => {
    let cancelled = false;
    setTitleById({});
    if (docId) void api.blockTitles(docId).then(({ items }) => {
      if (!cancelled) setTitleById(Object.fromEntries(items.map((b) => [b.id, b.title])));
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [docId, blocks]);

  const act = async (fn: () => Promise<any>, msg: string) => {
    try {
      await fn();
      // P3-7：确认「丢弃草稿」是在请求前问的，但只有请求成功才真的清标记——
      // 失败时保留标记（之后切块仍会确认），否则确认上移失败后再切块会静默丢草稿。
      // 先清标记再 loadBlocks，避免刷新 backstop 因 revision 已变再弹一次确认。
      useStore.getState().setDraft(null);
      setToast(msg);
      await loadBlocks();
    } catch (e: any) {
      setToast("操作失败：" + e.message);
    }
  };

  const jump = async (anchorId: string) => {
    const anchor = anchors[anchorId] ?? (await api.getAnchor(anchorId));
    setHighlight(anchor);
    setPdfPage(anchor.page);
  };

  return (
    <aside className="detail-panel">
      <div className="pane-heading">
        <div className="pane-label"><Icon name="link" /><strong>阅读索引</strong></div>
        <span className="pane-count">P. {pdfPage}</span>
      </div>
      <h3>本页关联笔记 <span className="section-count">{pageBlocks.length}</span></h3>
      {pageBlocks.length === 0 && <div className="empty">本页没有关联块</div>}
      {pageBlocks.map((b) => (
        <button key={b.block_id} className="hit-block" onClick={() => goToBlock(b.block_id)}>
          <MathText text={b.title} />
        </button>
      ))}

      <h3>选区定位</h3>
      {selectionHits.length === 0 ? (
        <div className="empty">在 PDF 上选中文字可匹配关联块</div>
      ) : (
        selectionHits.map((h) => (
          <div key={h.anchor_id} className="hit-card" onClick={() => void jump(h.anchor_id)}>
            <div>p.{h.page} · {h.match === "geometry" ? "几何命中" : "文本命中"}</div>
            <div className="muted">{h.content?.slice(0, 60)}</div>
            <div>
              {(h.blocks ?? []).map((b: any) => (
                <button key={b.block_id} onClick={(e) => {
                  e.stopPropagation();
                  goToBlock(b.block_id);
                }}>
                  <MathText text={b.title} />
                </button>
              ))}
            </div>
          </div>
        ))
      )}

      <h3>笔记详情</h3>
      {!detail ? (
        <div className="empty">选中一条笔记，查看来源、关联与编辑记录。</div>
      ) : (
        <div className="detail-body">
          <div>
            <b><MathText text={detail.block.title} /></b> <span className="muted">{STATUS_CN[detail.block.status]}</span>
          </div>
          <div className="muted">
            revision {detail.block.revision} · {detail.block.type} · order {detail.block.order}
          </div>

          <h4>生成记录</h4>
          <div className="muted">
            {(detail.block.created_by === "ai"
              ? "AI 生成"
              : detail.block.created_by === "agent"
                ? "agent 创建"
                : "人工创建") +
              " · " +
              String(detail.block.created_at ?? "").replace("T", " ").slice(0, 19)}
            {detail.history.some((h: any) => h.message === "再生成替换") && " · 经历过再生成"}
            {detail.block.generation_protected && " · 已锁定，不再被自动覆盖"}
          </div>

          <h4>来源</h4>
          {detail.block.sources.length === 0 && <div className="muted">无</div>}
          {detail.block.sources.map((s, i) => (
            <div key={i} className="source-row">
              <button onClick={() => void jump(s.anchor_id)}>
                {"📖 p." + (anchors[s.anchor_id]?.page ?? "?") + " 跳转"}
              </button>
              <span className="muted">{s.quote}</span>
            </div>
          ))}

          <h4>链接</h4>
          {detail.block.links.length === 0 && <div className="muted">无</div>}
          {detail.block.links.map((l) =>
            l.startsWith("b_") ? (
              <button key={l} onClick={() => goToBlock(l)}>
                <MathText text={titleById[l] ?? blocks.find((b) => b.id === l)?.title ?? l} />
              </button>
            ) : (
              <div key={l} className="muted">{l}</div>
            )
          )}

          <h4>历史</h4>
          <div className="history">
            {detail.history.map((h) => (
              <div key={h.rev_id} className="history-row">
                <code>{h.rev_id}</code>
                <span className="muted">{h.author} · {h.message} · {h.created_at}</span>
                <button
                  onClick={() => {
                    if (!guardDraft()) return;
                    void act(
                      () => api.rollbackBlock(detail.block.id, {
                        rev_id: h.rev_id,
                        expected_revision: detail.block.revision,
                      }),
                      "已回滚到 " + h.rev_id
                    );
                  }}
                >
                  回滚
                </button>
              </div>
            ))}
          </div>

          <div className="detail-actions">
            {detail.block.deleted_at ? (
              <button onClick={() => {
                if (!guardDraft()) return;
                void act(() => api.restoreBlock(detail!.block.id, {
                  expected_revision: detail!.block.revision }), "已恢复");
              }}>恢复</button>
            ) : (
              <button onClick={() => {
                if (!guardDraft()) return;
                void act(() => api.deleteBlock(detail!.block.id, {
                  expected_revision: detail!.block.revision }), "已删除（软删除）");
              }}>删除</button>
            )}
            {detail.block.generation_protected && (
              <button onClick={() => {
                if (!guardDraft()) return;
                void act(() => api.allowRegeneration(detail!.block.id, {
                  expected_revision: detail!.block.revision, confirmed: true }), "已允许重新生成覆盖");
              }}>
                允许重新生成
              </button>
            )}
            <button
              disabled={!up.enabled}
              title={up.enabled ? "与上一块交换位置" : "已在最前"}
              onClick={() => {
                if (!up.enabled) return;
                if (!guardDraft()) return;
                void act(() => api.reorder(docId!, chapterId!, {
                  block_id: detail!.block.id, after: up.after,
                  expected_revision: detail!.block.revision }), "已上移");
              }}
            >
              上移
            </button>
            <button
              disabled={!down.enabled}
              title={down.enabled ? "与下一块交换位置" : "已在最后"}
              onClick={() => {
                if (!down.enabled) return;
                if (!guardDraft()) return;
                void act(() => api.reorder(docId!, chapterId!, {
                  block_id: detail!.block.id, after: down.after,
                  expected_revision: detail!.block.revision }), "已下移");
              }}
            >
              下移
            </button>
          </div>
        </div>
      )}
    </aside>
  );
}
