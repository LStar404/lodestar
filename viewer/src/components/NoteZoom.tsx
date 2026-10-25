import { useEffect } from "react";
import { api } from "../api/client";
import { RELATION_CN, STATUS_CN, type NoteBlock } from "../api/types";
import { renderMarkdown } from "../lib/markdown";
import MathText from "./MathText";
import { useStore } from "../state/store";

const TYPE_ICON: Record<string, string> = {
  definition: "📌",
  theorem: "📘",
  motivation: "💡",
  figure: "🖼",
  link: "🔗",
  contribution: "🎯",
  method: "🧭",
  experiment: "📊",
};

/** 笔记放大浮层：小卡片点击后全屏放大阅读（Esc/遮罩/关闭按钮退出）。 */
export default function NoteZoom() {
  const zoomId = useStore((s) => s.zoomBlockId);
  const closeZoom = useStore((s) => s.closeZoom);
  const blocks = useStore((s) => s.blocks);
  const docId = useStore((s) => s.docId);
  const anchors = useStore((s) => s.anchors);
  const fetchAnchors = useStore((s) => s.fetchAnchors);
  const setHighlight = useStore((s) => s.setHighlight);
  const setPdfPage = useStore((s) => s.setPdfPage);
  const setToast = useStore((s) => s.setToast);
  const typeInfo = useStore((s) => s.typeInfo);

  const block: NoteBlock | undefined = blocks.find((b) => b.id === zoomId);

  useEffect(() => {
    if (!zoomId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeZoom();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoomId, closeZoom]);

  useEffect(() => {
    if (block) void fetchAnchors(block.sources.map((s) => s.anchor_id));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [block?.id]);

  if (!block) return null;

  const gotoAnchor = async (anchorId: string) => {
    try {
      const cached = useStore.getState().anchors[anchorId];
      const anchor = cached ?? (await api.getAnchor(anchorId));
      setHighlight(anchor);
      setPdfPage(anchor.page);
      closeZoom();
    } catch (e: any) {
      setToast("锚点跳转失败：" + e.message);
    }
  };

  return (
    <div className="note-zoom-mask" onClick={closeZoom}>
      <div className="note-zoom" onClick={(e) => e.stopPropagation()}>
        <div className="note-zoom-head">
          <span className="note-zoom-icon" title={typeInfo[block.type]?.name ?? block.type}>
            {typeInfo[block.type]?.icon ?? TYPE_ICON[block.type] ?? "📝"}
          </span>
          <h2><MathText text={block.title} docId={docId ?? ""} /></h2>
          <span className={`badge status-${block.status}`}>{STATUS_CN[block.status] ?? block.status}</span>
          {block.generation_protected && <span className="badge protected">已保护</span>}
          {block.basis === "synthesis" && <span className="badge synthesis">AI 归纳</span>}
          <button
            className="note-zoom-edit"
            title="AI 编辑此块"
            onClick={() => {
              useStore.getState().openAiEdit(block.id);
              closeZoom();
            }}
          >
            ✨AI 改
          </button>
          <button
            className="note-zoom-edit"
            title="关联其他知识点"
            onClick={() => {
              useStore.getState().openLinks(block.id);
              closeZoom();
            }}
          >
            🔗 关联
          </button>
          <button
            className="note-zoom-edit"
            title="编辑此卡（切到源码视图）"
            onClick={() => {
              // 切块统一走 store 的草稿守卫（Review #6）：不静默丢弃未保存草稿；
              // 用户取消时既不换块也不切视图
              const st = useStore.getState();
              if (!st.requestSelectBlock(block.id)) return;
              st.setView("source");
              closeZoom();
            }}
          >
            ✎ 编辑
          </button>
          <button className="note-zoom-close" onClick={closeZoom} title="关闭（Esc）">✕</button>
        </div>
        {/* 放大视图渲染全文：不得复用卡片预览（previewHtml 截断 320 字，曾吞掉乘法公理） */}
        <div
          className="note-zoom-body"
          dangerouslySetInnerHTML={{ __html: renderMarkdown(block.content || "", docId ?? "") }}
        />
        {block.sources.length > 0 && (
          <div className="note-zoom-sources">
            <span className="note-zoom-sources-label">来源</span>
            {block.sources.map((s, i) => (
              <button
                key={i}
                className="source-badge"
                title={s.quote}
                onClick={() => void gotoAnchor(s.anchor_id)}
              >
                {"📖 p." + ((anchors[s.anchor_id] as any)?.page ?? "?") + " · " +
                  (RELATION_CN[s.relation] ?? s.relation)}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}