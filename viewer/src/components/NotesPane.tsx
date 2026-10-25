import BlockList from "./BlockList";
import BlockEditor from "./BlockEditor";
import AgentCommandBar from "./AgentCommandBar";
import { sortBlocks } from "../api/types";
import { useStore } from "../state/store";

export default function NotesPane() {
  const blocks = useStore((s) => s.blocks);
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const selectedBlockId = useStore((s) => s.selectedBlockId);
  // 切块统一走受保护入口（Review #6）：源码/预览视图有未保存草稿时先确认再切，
  // 否则编辑器卸载即丢草稿；返回 false 表示用户取消
  const requestSelectBlock = useStore((s) => s.requestSelectBlock);
  const dirtyDraft = useStore((s) => s.dirtyDraft);
  const setDraft = useStore((s) => s.setDraft);

  // 视图切换（7.3）：只有「离开编辑器」才丢草稿——card 视图不挂载 BlockEditor，
  // 而 source ↔ preview 是同一个编辑器实例（只换渲染分支），草稿仍保留，不打断用户。
  // 顺序也重要：先选块、再切视图，草稿确认被取消时视图不得已经切走（不得有副作用）。
  const switchView = (v: "card" | "source" | "preview") => {
    if (v === view) return;
    if (v === "card" && dirtyDraft !== null) {
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
      <div className="notes-toolbar">
        <button onClick={() => switchView("card")} className={view === "card" ? "active" : ""}>卡片</button>
        <button onClick={() => switchView("source")} className={view === "source" ? "active" : ""}>源码</button>
        <button onClick={() => switchView("preview")} className={view === "preview" ? "active" : ""}>预览</button>
      </div>
      <BlockList
        blocks={blocks}
        selectedId={selectedBlockId}
        onSelect={requestSelectBlock}
        renderEditor={(b) =>
          view === "card" ? null : (
            // key 带 revision：生成/保存后数据更新要重挂载刷新内容，
            // 而视图切换（source↔preview）保持草稿不丢
            <BlockEditor key={b.id + ":" + b.revision} block={b} mode={view} />
          )
        }
      />
      <AgentCommandBar />
    </section>
  );
}
