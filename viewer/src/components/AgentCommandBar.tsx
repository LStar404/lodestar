import { useState } from "react";
import { api } from "../api/client";
import { useStore } from "../state/store";

/** Agent 编辑两步式（5.5）：plan → 模态确认 → exec → diff 提示条（可撤销）。 */
export default function AgentCommandBar() {
  const docId = useStore((s) => s.docId);
  const chapterId = useStore((s) => s.chapterId);
  const selectedBlockId = useStore((s) => s.selectedBlockId);
  const agent = useStore((s) => s.agent);
  const setAgent = useStore((s) => s.setAgent);
  const loadBlocks = useStore((s) => s.loadBlocks);
  const setToast = useStore((s) => s.setToast);
  const [instruction, setInstruction] = useState("");

  const url = selectedBlockId
    ? `/api/v1/blocks/${selectedBlockId}/agent-edit`
    : `/api/v1/docs/${docId}/chapters/${chapterId}/agent-edit`;

  // 草稿守卫（Review #6 残余）：有选中块时 agent 的编辑目标就是它。plan/exec 都基于
  // 服务端内容工作，exec 后 loadBlocks 还会让 revision 变化 → BlockEditor 按 key 重挂载，
  // 未保存草稿随之丢失。有草稿时先让用户保存/还原，不要静默覆盖。
  // view==="card" 时编辑器未挂载（与 requestSelectBlock/loadBlocks 的守卫条件一致），不拦。
  const blockedByDraft = (): boolean => {
    const st = useStore.getState();
    if (!selectedBlockId || st.view === "card" || st.dirtyDraft === null) return false;
    setToast("当前块有未保存草稿：请先保存（Ctrl+S）或还原，再让 agent 编辑");
    return true;
  };

  const plan = async () => {
    if (!instruction.trim()) return;
    if (blockedByDraft()) return;
    if (!selectedBlockId && !chapterId) {
      // 否则请求打到 chapters/undefined/agent-edit，报错令人困惑
      setToast("请先选择章节或块，再给 agent 下指令");
      return;
    }
    setAgent({ running: true, error: null, diff: null });
    try {
      const res = await api.agentEdit(url, { mode: "plan", instruction });
      setAgent({ plan: res.plan, planId: res.plan_id, running: false });
    } catch (e: any) {
      setAgent({ running: false, error: e.message });
    }
  };

  const exec = async () => {
    if (!agent.planId) return;
    if (blockedByDraft()) return;
    setAgent({ running: true, error: null });
    try {
      const res = await api.agentEdit(url, { mode: "exec", plan_id: agent.planId, confirmed: true });
      setAgent({ running: false, diff: res.diff, error: res.error?.error ?? null, plan: null, planId: null });
      setToast(res.partial ? "agent 部分执行完成" : "agent 编辑完成");
      await loadBlocks();
    } catch (e: any) {
      const hint =
        e.code === "BLOCK_LOCKED"
          ? "目标块为人工编辑块，agent 只读；请先在块详情中解锁后重试"
          : e.status === 409
            ? "数据已被修改，请刷新后重试"
            : e.message;
      setAgent({ running: false, error: hint });
    }
  };

  const undo = async () => {
    // 撤销会回滚块并 loadBlocks 刷新：若正在编辑的块被改到，编辑器重挂载会丢草稿
    if (blockedByDraft()) return;
    for (const item of agent.diff?.applied ?? []) {
      if (!item.block_id) continue;
      try {
        // 新建块没有「上一版本」可回滚：撤销 = 软删除（曾静默漏掉、却提示「已撤销」）
        if (item.tool === "create_block") {
          const { block } = await api.getBlock(item.block_id);
          await api.deleteBlock(item.block_id, { expected_revision: block.revision });
          continue;
        }
        const { block, history } = await api.getBlock(item.block_id);
        const current = Number(block.revision);
        const prev = history
          .map((h: any) => ({ ...h, n: Number(String(h.rev_id).split("@r").pop()) }))
          .filter((h: any) => Number.isFinite(h.n) && h.n < current)
          .sort((a: any, b: any) => b.n - a.n)[0];
        if (prev) {
          await api.rollbackBlock(item.block_id, { rev_id: prev.rev_id, expected_revision: block.revision });
        }
      } catch (e: any) {
        setToast("撤销失败：" + e.message);
      }
    }
    await loadBlocks();
    setAgent({ diff: null, plan: null, planId: null });
    setToast("已撤销本次 agent 编辑");
  };

  return (
    <>
      <div className="agent-bar">
        <div className="agent-caption"><b>AI 编辑助手</b><span>先生成计划，由你确认执行</span></div>
        <input
          aria-label="AI 编辑指令"
          placeholder="例如：把本章整理为复习提纲…"
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void plan()}
        />
        <button onClick={() => void plan()} disabled={agent.running}>生成计划</button>
        {agent.diff && (
          <div className="diff-bar">
            <span>已应用 {agent.diff.applied?.length ?? 0} 个操作</span>
            <button onClick={() => void undo()}>撤销本次编辑</button>
          </div>
        )}
        {agent.error && <div className="error">{agent.error}</div>}
      </div>

      {agent.plan && (
        <div className="modal-mask" onClick={() => setAgent({ plan: null, planId: null })}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>确认 agent 编辑计划</h2>
            <p><b>意图：</b>{agent.plan.intent}</p>
            <p><b>影响块：</b>{(agent.plan.affected_blocks ?? []).join(", ")}</p>
            <table className="usage-table">
              <thead>
                <tr><th>工具</th><th>参数</th><th>说明</th></tr>
              </thead>
              <tbody>
                {(agent.plan.proposed_changes ?? []).map((c: any, i: number) => (
                  <tr key={i}>
                    <td>{c.tool}</td>
                    <td><code>{JSON.stringify(c.args)}</code></td>
                    <td>{c.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted">默认不自动执行；确认后仅执行上表已确认的工具参数。</p>
            <button onClick={() => void exec()} disabled={agent.running}>确认执行</button>
            <button onClick={() => setAgent({ plan: null, planId: null })}>取消</button>
          </div>
        </div>
      )}
    </>
  );
}
