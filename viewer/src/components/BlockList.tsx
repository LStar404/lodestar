/** 块列表（规范 7.2）：按 order 渲染，I9 确定性排序。 */

import Icon from "./Icon";
import { useMemo } from "react";
import BlockCard from "./BlockCard";
import type { NoteBlock } from "../api/types";
import { sortBlocks } from "../api/types";

export default function BlockList({ blocks, selectedId, onSelect, renderEditor, loading = false, empty }: {
  blocks: NoteBlock[];
  selectedId: string | null;
  /** 返回 false = 用户在草稿保护确认里取消，调用方不得再执行打开放大层等副作用 */
  onSelect: (id: string) => boolean;
  renderEditor?: (block: NoteBlock) => React.ReactNode;
  loading?: boolean;
  empty?: React.ReactNode;
}) {
  const ordered = useMemo(() => sortBlocks(blocks.filter((b) => !b.deleted_at)), [blocks]);
  return (
    <div className="block-list" aria-busy={loading}>
      {loading && <div className="notes-skeleton" role="status" aria-label="正在加载笔记">{[1, 2, 3].map((n) => <div className="skeleton-card" key={n}><i /><i /><i /><i /></div>)}</div>}
      {!loading && ordered.length === 0 && (empty ?? <div className="workspace-empty">
        <span className="empty-symbol"><Icon name="notes" size={28} /></span>
        <strong>知识，从这里开始积累</strong>
        <p>导入教材并选择章节，点击「生成本章」，<br />把阅读内容整理成自己的笔记。</p>
        <span className="empty-step">01 导入教材 <span>→</span> 02 生成笔记 <span>→</span> 03 复习整理</span>
      </div>)}
      {ordered.map((b) => (
        <div key={b.id} id={`block-${b.id}`}>
          <BlockCard block={b} selected={selectedId === b.id} onSelect={onSelect} />
          {selectedId === b.id && renderEditor?.(b)}
        </div>
      ))}
    </div>
  );
}
