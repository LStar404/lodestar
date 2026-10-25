/** 块列表（规范 7.2）：按 order 渲染，I9 确定性排序。 */

import BlockCard from "./BlockCard";
import type { NoteBlock } from "../api/types";
import { sortBlocks } from "../api/types";

export default function BlockList({ blocks, selectedId, onSelect, renderEditor }: {
  blocks: NoteBlock[];
  selectedId: string | null;
  /** 返回 false = 用户在草稿保护确认里取消，调用方不得再执行打开放大层等副作用 */
  onSelect: (id: string) => boolean;
  renderEditor?: (block: NoteBlock) => React.ReactNode;
}) {
  const ordered = sortBlocks(blocks.filter((b) => !b.deleted_at));
  return (
    <div className="block-list">
      {ordered.length === 0 && <div className="empty">本章还没有笔记块，点击“生成本章”开始。</div>}
      {ordered.map((b) => (
        <div key={b.id} id={`block-${b.id}`}>
          <BlockCard block={b} selected={selectedId === b.id} onSelect={onSelect} />
          {selectedId === b.id && renderEditor?.(b)}
        </div>
      ))}
    </div>
  );
}
