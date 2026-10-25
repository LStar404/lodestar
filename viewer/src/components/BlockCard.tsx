import { memo, useEffect, useMemo } from "react";
import { api } from "../api/client";
import { RELATION_CN, STATUS_CN, type NoteBlock } from "../api/types";
import { previewHtml } from "../lib/markdown";
import MathText from "./MathText";
import { useStore } from "../state/store";

export { STATUS_CN };

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

function BlockCard({ block, selected, onSelect }: {
  block: NoteBlock;
  selected: boolean;
  /** 返回 false = 用户在草稿保护确认里取消（未保存草稿不丢），后续副作用必须中止 */
  onSelect: (id: string) => boolean;
}) {
  const fetchAnchors = useStore((s) => s.fetchAnchors);
  const docId = useStore((s) => s.docId);
  // 订阅粒度：只取本卡来源的页码字符串（原始值比较），避免任意锚点更新导致全部卡片重渲染
  const sourceKey = useStore((s) =>
    block.sources.map((src) => (s.anchors[src.anchor_id]?.page ?? "?")).join("|"));
  const pages = sourceKey.split("|");
  const setHighlight = useStore((s) => s.setHighlight);
  const openZoom = useStore((s) => s.openZoom);
  const typeInfo = useStore((s) => s.typeInfo);
  const openAiEdit = useStore((s) => s.openAiEdit);
  const openLinks = useStore((s) => s.openLinks);
  const setPdfPage = useStore((s) => s.setPdfPage);
  const setToast = useStore((s) => s.setToast);

  useEffect(() => {
    void fetchAnchors(block.sources.map((s) => s.anchor_id));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [block.sources]);
  const preview = useMemo(
    () => previewHtml(block.content || "", docId ?? ""),
    [block.content, docId]
  );

  const gotoAnchor = async (anchorId: string) => {
    try {
      // 点击时按需读取（useStore.getState 不产生订阅），避免为取页码而重渲染全部卡片
      const cached = useStore.getState().anchors[anchorId];
      const anchor = cached ?? (await api.getAnchor(anchorId));
      setHighlight(anchor);           // 高亮 + 2 秒脉冲 + 滚动居中（6.2）
      setPdfPage(anchor.page);
    } catch (e: any) {
      setToast("锚点跳转失败：" + e.message);
    }
  };

  return (
    <div
      className={`block-card ${selected ? "selected" : ""}`}
      onClick={() => {
        // 单击卡片 = 选中 + 展开阅读（⤢ 按钮保留，点击来源徽章等子控件不触发）；
        // onSelect 走草稿守卫：用户取消时不得再打开放大层（Review #6）
        if (!onSelect(block.id)) return;
        openZoom(block.id);
      }}
    >
      <div className="block-head">
        <span className="block-type" title={typeInfo[block.type]?.name ?? block.type}>
          {typeInfo[block.type]?.icon ?? TYPE_ICON[block.type] ?? "📝"}
        </span>
        <MathText className="block-title" text={block.title} docId={docId ?? ""} />
        <span className={`badge status-${block.status}`}>{STATUS_CN[block.status] ?? block.status}</span>
        {block.generation_protected && <span className="badge protected">已保护</span>}
        {block.basis === "synthesis" && <span className="badge synthesis">AI 归纳</span>}
        <button
          className="block-mini-btn"
          title="AI 编辑此块"
          onClick={(e) => {
            e.stopPropagation();
            openAiEdit(block.id);
          }}
        >
          ✨AI 改
        </button>
        <button
          className="block-mini-btn"
          title="关联其他知识点"
          onClick={(e) => {
            e.stopPropagation();
            openLinks(block.id);
          }}
        >
          🔗{(block.links ?? []).length > 0 ? String(block.links!.length) : "关联"}
        </button>
        <button
          className="block-zoom-btn"
          title="放大阅读"
          onClick={(e) => {
            e.stopPropagation();
            if (!onSelect(block.id)) return;
            openZoom(block.id);
          }}
        >
          ⤢
        </button>
      </div>
      {/* 卡片正文走与「预览」同一渲染器（markdown + KaTeX）；
          必须 memo：每次父级重渲染都重跑 markdown+KaTeX 是列表卡顿主因 */}
      <div
        className="block-preview"
        dangerouslySetInnerHTML={{ __html: preview }}
      />
      <div className="block-sources">
        {block.sources.map((s, i) => (
          <button
            key={i}
            className="source-badge"
            title={s.quote}
            onClick={(e) => {
              e.stopPropagation();
              void gotoAnchor(s.anchor_id);
            }}
          >
            {"📖 p." + (pages[i] ?? "?") + " · " + (RELATION_CN[s.relation] ?? s.relation)}
          </button>
        ))}
      </div>
    </div>
  );
}

export default memo(BlockCard);
