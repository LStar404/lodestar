import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useStore } from "../state/store";

/** AI 建议的请求身份：建议只属于「这次请求 + 这块 + 这一版」，
 *  与「之后弹窗里显示的是哪块」无关（Review #2：旧响应曾写进另一块）。 */
interface ProposalMeta {
  docId: string;
  blockId: string;
  /** 生成建议时块的 revision：apply-edit 的 base_revision（后端按 CAS 校验） */
  baseRevision: number | undefined;
  /** 请求代次：发起/关闭/切块都会自增，旧响应序号对不上即丢弃 */
  seq: number;
}

const PRESETS: [string, string][] = [
  ["更精简", "精简内容，保留核心结论与关键条件，删除冗余"],
  ["更详细", "补充细节与解释让内容更易懂，但不得编造笔记之外的事实"],
  ["更准确", "修正不准确的表述与记号，保持原意不变"],
  ["更通俗", "用更直白的语言重写，适合初学者理解"],
];

/** 单块 AI 编辑 + 知识点关联（每块独立入口，便于逐块修改与互链）。 */
export default function BlockActionModals() {
  const aiId = useStore((s) => s.aiEditBlockId);
  const linkId = useStore((s) => s.linkBlockId);
  const closeAiEdit = useStore((s) => s.closeAiEdit);
  const closeLinks = useStore((s) => s.closeLinks);
  const blocks = useStore((s) => s.blocks);
  const docId = useStore((s) => s.docId);
  const setToast = useStore((s) => s.setToast);
  const typeInfo = useStore((s) => s.typeInfo);
  const loadBlocks = useStore((s) => s.loadBlocks);
  const openZoom = useStore((s) => s.openZoom);

  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  const [proposal, setProposal] = useState("");
  const [original, setOriginal] = useState("");
  // 建议绑定到请求身份；apply 只认这份身份，绝不取「当前弹窗」的目标块
  const [proposalMeta, setProposalMeta] = useState<ProposalMeta | null>(null);
  const aiReqSeq = useRef(0);
  const [search, setSearch] = useState("");
  // 跨章关联必须用全书块列表：store.blocks 只有当前章（曾导致跨章链不上、chip 显示原始 id）
  const [allBlocks, setAllBlocks] = useState<any[]>([]);

  const activeId = aiId ?? linkId;
  const block = blocks.find((b) => b.id === activeId);
  const scopeBlocks = allBlocks.length ? allBlocks : blocks;

  useEffect(() => {
    // 换块/关闭弹窗：在途 AI 请求全部作废（代次自增），上一块的建议也清掉——
    // 否则 A 的响应会在 B 的弹窗里变成「建议」（Review #2 的复现路径）
    aiReqSeq.current += 1;
    setBusy(false);
    setInstruction("");
    setProposal("");
    setOriginal("");
    setProposalMeta(null);
    setSearch("");
    if (linkId && docId) {
      void api.listAllBlocks(docId).then((r) => setAllBlocks(r.items ?? [])).catch(() => setAllBlocks([]));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  const runAi = async () => {
    if (!docId || !aiId) return;
    if (!instruction.trim()) {
      setToast("先写修改指令，或点一个快捷指令");
      return;
    }
    // 请求身份快照：ai-edit 不返回 revision，所以基线取发起时刻的块 revision
    const seq = ++aiReqSeq.current;
    const reqDocId = docId;
    const reqBlockId = aiId;
    const baseRevision = block?.revision;
    // 新一轮请求开始先作废旧建议：否则第二次请求失败后旧建议还留在屏幕上，
    // 点「应用」会被身份校验拒绝（可点却打不动）；在途旧响应也会被 seq 挡住
    setProposal("");
    setOriginal("");
    setProposalMeta(null);
    setBusy(true);
    try {
      const r = await api.blockAiEdit(reqDocId, reqBlockId, instruction.trim());
      // 关闭弹窗/切块/重新发起都会让序号过期；身份也不是发起时的那块就直接丢。
      // 两个校验都过才收：旧建议绝不跨块、跨文档、跨版本落进来。
      if (seq !== aiReqSeq.current) return;
      const st = useStore.getState();
      if (st.docId !== reqDocId || st.aiEditBlockId !== reqBlockId) return;
      setProposal(r.proposal);
      setOriginal(r.original);
      setProposalMeta({ docId: reqDocId, blockId: reqBlockId, baseRevision, seq });
    } catch (e: any) {
      if (seq === aiReqSeq.current) setToast("AI 编辑失败：" + e.message);
    } finally {
      if (seq === aiReqSeq.current) setBusy(false);
    }
  };

  // await 之后的身份校验：换块/关闭弹窗/发起新请求都会让这次请求失效——
  // 失效后不得再关别人的弹窗、清别人的 busy、写别人的 toast（Review #2 同源）
  const isCurrentIdentity = (meta: ProposalMeta): boolean => {
    const st = useStore.getState();
    return meta.seq === aiReqSeq.current
      && st.aiEditBlockId === meta.blockId
      && st.docId === meta.docId;
  };

  const applyEdit = async () => {
    if (!docId || !aiId || !proposal.trim()) return;
    const meta = proposalMeta;
    // 只应用和当前弹窗身份一致的建议：换块/换文档/代次过期一律拒绝并提示重新生成，
    // 绝不用 B 的 revision 去写 A 的建议（Review #2 的错误写入路径）
    if (!meta || meta.seq !== aiReqSeq.current || meta.blockId !== aiId || meta.docId !== docId) {
      aiReqSeq.current += 1;
      setProposal("");
      setOriginal("");
      setProposalMeta(null);
      setToast("建议已失效，请重新生成");
      await loadBlocks();   // 刷新到最新内容，避免继续基于旧版本生成
      return;
    }
    setBusy(true);
    try {
      await api.blockApplyEdit(meta.docId, meta.blockId, {
        content: proposal,
        base_revision: meta.baseRevision,   // 生成建议时那一版的乐观锁基线
      });
      // await 之后身份可能已变（切块/关弹窗/发起新请求）：不得再操作当前弹窗
      if (!isCurrentIdentity(meta)) return;
      await loadBlocks();
      if (!isCurrentIdentity(meta)) return;
      setToast("已应用修改（原内容保存在历史里，可回溯）");
      setBusy(false);
      closeAiEdit();
    } catch (e: any) {
      if (!isCurrentIdentity(meta)) return;
      if (e.code === "REVISION_CONFLICT" || e.status === 409) {
        // 409 = 建议基于的版本已被改掉；同一建议重试必然继续失败，清算建议并刷新
        setProposal("");
        setOriginal("");
        setProposalMeta(null);
        setToast("数据已被修改（当前 revision 已更新），请重新生成建议");
        await loadBlocks();
      } else {
        setToast("应用失败：" + e.message);
      }
    } finally {
      // 身份已变时不碰 busy（可能已属于新请求的「生成中…」状态）
      if (isCurrentIdentity(meta)) setBusy(false);
    }
  };

  const addLink = async (targetId: string) => {
    if (!docId || !linkId) return;
    try {
      await api.blockAddLink(docId, linkId, targetId, block?.revision);
      await loadBlocks();
      setToast("已关联知识点");
    } catch (e: any) {
      setToast("关联失败：" + e.message);
    }
  };

  const removeLink = async (targetId: string) => {
    if (!docId || !linkId) return;
    try {
      await api.blockRemoveLink(docId, linkId, targetId, block?.revision);
      await loadBlocks();
    } catch (e: any) {
      setToast("取消关联失败：" + e.message);
    }
  };

  const others = scopeBlocks.filter(
    (b) => b.id !== linkId && !b.deleted_at
      && ((b.title ?? "") + (b.content ?? "")).toLowerCase().includes(search.toLowerCase()),
  );

  return (
    <>
      {aiId && block && (
        <div className="modal-mask" onClick={closeAiEdit}>
          <div className="modal wide" onClick={(e) => e.stopPropagation()}>
            <h2>AI 编辑 · {block.title}</h2>
            <div className="muted">修改建议只在你点「应用」后才写入（原内容进历史可回溯）。</div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", margin: "8px 0" }}>
              {PRESETS.map(([label, hint]) => (
                <button key={label} onClick={() => setInstruction(hint)}>{label}</button>
              ))}
            </div>
            <textarea
              rows={3}
              placeholder="修改指令，例如：把结论提前并补一句适用条件"
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              style={{ width: "100%" }}
            />
            <div className="export-actions">
              <button disabled={busy} onClick={() => void runAi()}>
                {busy ? "生成中…" : "生成修改建议"}
              </button>
            </div>
            {proposal && (
              <>
                <h3>修改建议（可再手改）</h3>
                <textarea
                  rows={10}
                  value={proposal}
                  onChange={(e) => setProposal(e.target.value)}
                  style={{ width: "100%" }}
                />
                {original && (
                  <details style={{ margin: "6px 0" }}>
                    <summary className="muted">对比原文</summary>
                    <pre className="export-path">{original}</pre>
                  </details>
                )}
                <div className="export-actions">
                  <button onClick={() => {
                    // 放弃建议同时解绑请求身份并作废在途响应（seq++）：
                    // 否则晚到的旧响应会把建议重新贴回屏幕
                    aiReqSeq.current += 1;
                    setProposal("");
                    setOriginal("");
                    setProposalMeta(null);
                  }}>放弃建议</button>
                  <button className="primary" disabled={busy} onClick={() => void applyEdit()}>
                    应用修改
                  </button>
                </div>
              </>
            )}
            {!proposal && (
              <div className="export-actions">
                <button onClick={closeAiEdit}>关闭</button>
              </div>
            )}
          </div>
        </div>
      )}
      {linkId && block && (
        <div className="modal-mask" onClick={closeLinks}>
          <div className="modal wide" onClick={(e) => e.stopPropagation()}>
            <h2>关联知识点 · {block.title}</h2>
            <div className="muted">把相关知识点互链起来（跨章可链），点关联即加入本块的引用。</div>
            {(block.links ?? []).length > 0 && (
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", margin: "8px 0" }}>
                {(block.links ?? []).map((ln) => {
                  const t = scopeBlocks.find((b) => b.id === ln);
                  return (
                    <span key={ln} className="badge">
                      {t ? t.title : ln}
                      <button style={{ marginLeft: 4 }} onClick={() => void removeLink(ln)}>✕</button>
                    </span>
                  );
                })}
              </div>
            )}
            <input
              placeholder="搜索其他知识点…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ width: "100%", margin: "6px 0" }}
            />
            <div className="review-list" style={{ maxHeight: "40vh", overflowY: "auto" }}>
              {others.slice(0, 60).map((b) => (
                <div key={b.id} className="review-row">
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <b>{typeInfo[b.type]?.icon ?? "📝"} {b.title}</b>
                    <div className="muted" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {(b.content ?? "").slice(0, 90)}
                    </div>
                  </div>
                  {(block.links ?? []).includes(b.id) ? (
                    <button onClick={() => void removeLink(b.id)}>已关联 · 取消</button>
                  ) : (
                    <button onClick={() => void addLink(b.id)}>+ 关联</button>
                  )}
                </div>
              ))}
              {!others.length && <div className="muted">没有匹配的块</div>}
            </div>
            <div className="export-actions">
              <button onClick={() => { openZoom(linkId); closeLinks(); }}>查看本块</button>
              <button className="primary" onClick={closeLinks}>完成</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
