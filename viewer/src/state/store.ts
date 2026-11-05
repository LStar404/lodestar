/** 全局状态（规范 7.3）：zustand 单一 store。 */

import { create } from "zustand";
import { api, type DocSummary, type NoteBlock, type Anchor } from "../api/client";

import { readChapter } from "../lib/chapter-loader";
import { AnchorLoader } from "../lib/anchor-loader";

const anchorLoader = new AnchorLoader(
  async (key, ids) => (await api.getAnchors(key.split(":")[0], ids)).items,
  (rows) => {
    const st = useStore.getState();
    const current = rows.filter((a) => a.doc_id === st.docId);
    if (current.length) useStore.setState({
      anchors: { ...st.anchors, ...Object.fromEntries(current.map((a) => [a.anchor_id, a])) },
    });
  },
);
let anchorScope = "";
function resetAnchors(docId: string, revision: number | undefined, force = false): boolean {
  const key = docId + ":" + (revision ?? "");
  if (!force && anchorScope === key) return false;
  anchorScope = key;
  anchorLoader.reset(key, force);
  return true;
}
let toastTimer: ReturnType<typeof setTimeout> | undefined;

// 请求代次（Review #7）：切章/换书的旧请求晚于新请求返回时不得覆盖当前列表。
// 每次 loadBlocks/selectDoc 自增；响应写回前必须发现代次仍是最新且目标未变。
let loadBlocksSeq = 0;
let selectDocSeq = 0;
let navigationSeq = 0;
let reviewSeq = 0;
let reviewCountSeq = 0;
let draftVersion = 0;

export interface AgentState {
  plan: any | null;
  planId: string | null;
  running: boolean;
  diff: any | null;
  error: string | null;
}

interface Store {
  docs: DocSummary[];
  docId: string | null;
  manifest: any | null;
  chapterId: string | null;
  blocks: NoteBlock[];
  blocksLoading: boolean;
  blocksError: string | null;
  selectedBlockId: string | null;
  zoomBlockId: string | null;
  pdfPage: number;
  selectionHits: any[];
  highlightAnchor: Anchor | null;
  agent: AgentState;
  view: "card" | "source" | "preview";
  readingMode: "split" | "focus";
  setReadingMode: (mode: "split" | "focus") => void;
  dirtyDraft: string | null;
  settingsOpen: boolean;
  setupOpen: boolean;
  onboardOpen: boolean;
  openOnboard: (open: boolean) => void;
  reviewOpen: boolean;
  reviewItems: NoteBlock[];
  reviewCount: number;
  reviewLoading: boolean;
  reviewError: string | null;
  revealBlock: { id: string; seq: number } | null;
  navigateBlock: (blockId: string, expectedDocId?: string, isActive?: () => boolean) => Promise<boolean>;
  // 块类型注册表（学科模板 + 用户自定义）：卡片图标/名称与生成同一事实源
  typeInfo: Record<string, { name: string; icon: string }>;
  // 单块 AI 编辑 / 知识点关联
  aiEditBlockId: string | null;
  linkBlockId: string | null;
  openAiEdit: (id: string) => void;
  closeAiEdit: () => void;
  openLinks: (id: string) => void;
  closeLinks: () => void;
  toast: string | null;
  anchors: Record<string, Anchor>;

  fetchAnchors: (ids: string[]) => Promise<void>;
  loadDocs: () => Promise<void>;
  selectDoc: (docId: string) => Promise<void>;
  selectChapter: (chapterId: string) => Promise<void>;
  loadBlocks: () => Promise<void>;
  /** 合并单块保存响应，不重载整章，也不改动编辑器自己的草稿状态。 */
  applySavedBlock: (block: NoteBlock) => void;
  selectBlock: (blockId: string | null) => void;
  /** 受保护的切块动作（草稿守卫）：返回 false 表示用户在确认框里取消，选中块未变。 */
  requestSelectBlock: (blockId: string | null) => boolean;
  openZoom: (blockId: string) => void;
  closeZoom: () => void;
  setPdfPage: (page: number) => void;
  setSelectionHits: (hits: any[]) => void;
  setHighlight: (anchor: Anchor | null) => void;
  setView: (view: "card" | "source" | "preview") => void;
  setDraft: (draft: string | null) => void;
  getDraftVersion: () => number;
  /** BlockEditor 卸载清理：该块仍是选中块且仍有脏草稿时清掉，避免「草稿已丢但标记还在」
   *  的僵尸标记导致之后每次切块都假确认。hasLocalEdits=false 时不动。 */
  clearStaleDraftOnUnmount: (blockId: string, hasLocalEdits: boolean) => void;
  setAgent: (patch: Partial<AgentState>) => void;
  setToast: (msg: string | null) => void;
  openSettings: (open: boolean) => void;
  openSetup: (open: boolean) => void;
  openReview: (open: boolean) => Promise<void>;
  refreshReviewCount: () => Promise<void>;
}

export const useStore = create<Store>((set, get) => ({
  docs: [],
  docId: null,
  manifest: null,
  chapterId: null,
  blocks: [],
  blocksLoading: false,
  blocksError: null,
  selectedBlockId: null,
  zoomBlockId: null,
  pdfPage: 1,
  selectionHits: [],
  highlightAnchor: null,
  agent: { plan: null, planId: null, running: false, diff: null, error: null },
  view: "card",
  readingMode: "split",
  setReadingMode: (readingMode) => set({ readingMode }),
  dirtyDraft: null,
  settingsOpen: false,
  setupOpen: false,
  onboardOpen: false,
  reviewOpen: false,
  reviewItems: [],
  reviewCount: 0,
  reviewLoading: false,
  reviewError: null,
  revealBlock: null,
  typeInfo: {},
  aiEditBlockId: null,
  linkBlockId: null,
  openAiEdit: (id) => set({ aiEditBlockId: id }),
  closeAiEdit: () => set({ aiEditBlockId: null }),
  openLinks: (id) => set({ linkBlockId: id }),
  closeLinks: () => set({ linkBlockId: null }),
  toast: null,
  anchors: {},

  fetchAnchors: async (ids) => {
    if (get().docId) await anchorLoader.fetch(ids);
  },
  loadDocs: async () => {
    const { docs } = await api.listDocs();
    set({ docs });
  },
  selectDoc: async (docId) => {
    // 换书竞态（Review #7 同源）：A/B 两次换书时 manifest 可能乱序返回，
    // 后点的书必须赢；旧 manifest 丢弃，避免把顶栏切回上一本。
    const seq = ++selectDocSeq;
    ++navigationSeq;
    if (!docId) {
      ++loadBlocksSeq;
      ++reviewSeq;
      ++reviewCountSeq;
      resetAnchors("", undefined);
      set({ docId: null, manifest: null, chapterId: null, blocks: [], anchors: {},
            selectedBlockId: null, zoomBlockId: null, dirtyDraft: null,
            reviewOpen: false, reviewItems: [], reviewCount: 0, reviewLoading: false, reviewError: null, revealBlock: null,
            blocksLoading: false, blocksError: null, selectionHits: [], highlightAnchor: null });
      return;
    }
    const manifest = await api.getManifest(docId);
    if (seq !== selectDocSeq) return;                 // 失败会抛出 → 不清草稿
    const chapterId = manifest.chapters?.[0]?.chapter_id ?? null;
    // 成功切换：选中块/编辑器随 reset 卸载，草稿标记一并清掉（失败路径不执行到这里）
    ++loadBlocksSeq;
    ++reviewSeq;
    ++reviewCountSeq;
    resetAnchors(docId, manifest.data_revision, true);
    set({ docId, manifest, chapterId, blocks: [], anchors: {}, selectedBlockId: null,
          blocksLoading: false, blocksError: null, selectionHits: [], highlightAnchor: null,
          reviewOpen: false, reviewItems: [], reviewCount: 0, reviewLoading: false, reviewError: null, revealBlock: null,
          zoomBlockId: null, pdfPage: 1, dirtyDraft: null });
    if (chapterId) await get().loadBlocks();
  },
  selectChapter: async (chapterId) => {
    ++navigationSeq;
    // 成功切换章节：选中块被清空、编辑器卸载，草稿标记同时清掉（不存在失败分支）
    set({ chapterId, blocks: [], selectedBlockId: null, zoomBlockId: null, dirtyDraft: null });
    await get().loadBlocks();
  },
  loadBlocks: async () => {
    // 请求代次（Review #7）：开始时快照目标文档/章节并自增代次；响应（含翻页中途）
    // 回来时若已切章/换书或已有更新的请求，直接丢弃——旧章列表不得覆盖当前章。
    const seq = ++loadBlocksSeq;
    const { docId, chapterId } = get();
    if (!docId || !chapterId) { set({ blocksLoading: false, blocksError: null }); return; }
    const isCurrent = () =>
      seq === loadBlocksSeq && get().docId === docId && get().chapterId === chapterId;
    set({ blocksLoading: true, blocksError: null });
    const failed = (message: string) => {
      if (isCurrent()) set({ blocksLoading: false, blocksError: message });
    };
    let loaded;
    try {
      loaded = await readChapter(docId, chapterId, isCurrent);
    } catch (error: any) {
      failed("笔记加载失败：" + (error?.message || "请检查连接后重试"));
      return;
    }
    if (!loaded || !isCurrent()) return;
    const { items, revision: loadedRevision } = loaded;

    // 草稿保护 backstop（Review #6 残余 / P2-D1）：所有内容刷新都走 loadBlocks
    // （生成任务完成、AI 应用/关联、刷新图资源…）。若选中的编辑块 revision 将变或从
    // 列表消失，BlockEditor 会因 key=id:revision 重挂载并丢未保存草稿——先确认：
    // 取消 → 不写回（编辑器保持挂载、dirtyDraft 保留）；确认 → 写回并清草稿标记。
    // revision 未变 / 无草稿 / card 视图（无编辑器）不得弹确认。
    const st = get();
    if (st.view !== "card" && st.dirtyDraft !== null && st.selectedBlockId) {
      const prev = st.blocks.find((b) => b.id === st.selectedBlockId);
      if (prev) {
        const next = items.find((b) => b.id === st.selectedBlockId);
        if (!next || next.revision !== prev.revision) {
          const ok =
            typeof window !== "undefined" && typeof window.confirm === "function"
              ? window.confirm("刷新会丢弃当前块未保存的草稿，继续吗？")
              : true;
          if (!ok) { set({ blocksLoading: false }); return; }
          set({ dirtyDraft: null });
        }
      }
    }
    const previous = new Map(get().blocks.map((b) => [b.id, b]));
    // 未变化的卡片保留引用，定时刷新不重复解析 Markdown 和公式。
    const stable = items.map((b) => {
      const old = previous.get(b.id);
      return old?.revision === b.revision ? old : b;
    });
    const anchorsChanged = resetAnchors(docId, loadedRevision);
    set({ blocks: stable, blocksLoading: false, blocksError: null,
          ...(anchorsChanged ? { anchors: {} } : {}) });
    // 已缓存来源不会再请求；曾失败的来源可随下一次刷新重试。
    void get().fetchAnchors(stable.flatMap((b) => b.sources.map((a) => a.anchor_id)));
  },
  navigateBlock: async (blockId, expectedDocId, isActive = () => true) => {
    const origin = get();
    const docId = origin.docId;
    if (!docId || (expectedDocId && expectedDocId !== docId)) return false;
    const seq = ++navigationSeq;
    const isCurrent = () => isActive() && seq === navigationSeq && get().docId === docId
      && get().chapterId === origin.chapterId && get().selectedBlockId === origin.selectedBlockId;
    try {
      const { block } = await api.getBlock(blockId);
      if (!isCurrent()) return false;
      if (block.doc_id !== docId || block.deleted_at) throw Error("这条笔记已移除，请刷新后重试");
      const changeChapter = block.chapter_id !== origin.chapterId;
      const loaded = changeChapter ? await readChapter(docId, block.chapter_id, isCurrent) : null;
      if (!isCurrent() || (changeChapter && !loaded)) return false;
      const rows = loaded?.items ?? get().blocks;
      if (!rows.some((b) => b.id === blockId && !b.deleted_at)) throw Error("笔记列表已变化，请刷新后重试");
      // 目标已完整读到才允许丢弃草稿；读取失败或用户取消时，原章和编辑器保持不变。
      if (!get().requestSelectBlock(blockId)) return false;
      if (loaded) {
        ++loadBlocksSeq;
        const changed = resetAnchors(docId, loaded.revision);
        set({ chapterId: block.chapter_id, blocks: loaded.items,
          blocksLoading: false, blocksError: null, zoomBlockId: null,
          ...(changed ? { anchors: {} } : {}) });
        void get().fetchAnchors(loaded.items.flatMap((b) => b.sources.map((a) => a.anchor_id)));
      }
      set({ revealBlock: { id: blockId, seq } });
      return true;
    } catch (error: any) {
      if (isCurrent()) get().setToast("定位失败：" + error.message);
      return false;
    }
  },
  applySavedBlock: (block) => {
    const st = get();
    if (block.doc_id !== st.docId || block.chapter_id !== st.chapterId) return;
    const previous = st.blocks.find((b) => b.id === block.id);
    // 已离开该章、块已移除、或较新的响应已经生效时，晚到的保存不影响当前界面。
    if (!previous || previous.revision >= block.revision) return;
    // 保存前开始的整章读取可能仍带旧内容；让其失效，避免把保存结果覆盖回去。
    ++loadBlocksSeq;
    set({
      blocks: st.blocks.map((b) => b.id === block.id ? block : b),
      blocksLoading: false,
    });
  },
  requestSelectBlock: (blockId) => {
    ++navigationSeq;
    // 草稿守卫（Review #6）：换 selectedBlockId 会卸载源码/预览视图里的 BlockEditor，
    // 而草稿只存在它的组件 state 中（丢 = 不可恢复）。卡片单击、J/K、详情面板跳转、
    // 检索结果、审核队列等所有切块入口统一走这里；取消则选中块保持不变。
    // card 视图没有挂载编辑器，不存在可丢的草稿。
    const { view, dirtyDraft, selectedBlockId } = get();
    if (blockId !== selectedBlockId && view !== "card" && dirtyDraft !== null) {
      const ok =
        typeof window !== "undefined" && typeof window.confirm === "function"
          ? window.confirm("当前块有未保存草稿，切换将丢弃草稿，继续吗？")
          : true;
      if (!ok) return false;
      set({ dirtyDraft: null });
    }
    set({ selectedBlockId: blockId });
    return true;
  },
  // 兼容入口：保留旧调用方（返回 void，取消不可观测）。任何在切块之后还要滚动、
  // 请求锚点、切 PDF 页的入口都必须改用 requestSelectBlock 判断返回值——
  // 本轮已将审核队列定位、Toolbar 检索结果等全部改为 requestSelectBlock。
  selectBlock: (blockId) => {
    void get().requestSelectBlock(blockId);
  },
  openZoom: (blockId) => set({ zoomBlockId: blockId }),
  closeZoom: () => set({ zoomBlockId: null }),
  setPdfPage: (page) => set({ pdfPage: page }),
  setSelectionHits: (hits) => set({ selectionHits: hits }),
  setHighlight: (anchor) => set({ highlightAnchor: anchor, ...(anchor ? { readingMode: "split" } : {}) }),
  setView: (view) => set({ view }),
  getDraftVersion: () => draftVersion,
  setDraft: (draft) => {
    ++draftVersion;
    // 正文留在编辑器本地；全局只订阅“是否有修改”，连续输入不广播整棵界面。
    const dirtyDraft = draft === null ? null : "dirty";
    if (get().dirtyDraft !== dirtyDraft) set({ dirtyDraft });
  },
  clearStaleDraftOnUnmount: (blockId, hasLocalEdits) => {
    // BlockEditor 卸载清理（草稿守卫 backstop）：编辑器因刷新/重挂载/切书卸载后，
    // 组件内草稿已不存在，但 store 标记可能还留着（僵尸标记→之后每次切块假确认）。
    // 只有当「该块仍是选中块」且「确有未保存的本地修改」时才清，避免误清别的块的草稿。
    // 正常保存/还原/确认切换在卸载前已把 dirtyDraft 置空，这里自然 no-op。
    if (!hasLocalEdits) return;
    const { dirtyDraft, selectedBlockId } = get();
    if (dirtyDraft !== null && selectedBlockId === blockId) set({ dirtyDraft: null });
  },
  setAgent: (patch) => set({ agent: { ...get().agent, ...patch } }),
  setToast: (msg) => {
    clearTimeout(toastTimer);
    set({ toast: msg });
    if (msg) toastTimer = setTimeout(() => set({ toast: null }), 3000);
  },
  openSettings: (open) => set({ settingsOpen: open }),
  openSetup: (open) => set({ setupOpen: open }),
  openOnboard: (open) => set({ onboardOpen: open }),
  openReview: async (open) => {
    const seq = ++reviewSeq;
    if (!open) {
      set({ reviewOpen: false, reviewLoading: false });
      void get().refreshReviewCount();
      return;
    }
    const { docId } = get();
    if (!docId) return;
    ++reviewCountSeq;
    set({ reviewOpen: true, reviewLoading: true, reviewError: null });
    try {
      const { items } = await api.reviewQueue(docId);
      if (seq !== reviewSeq || get().docId !== docId) return;
      ++reviewCountSeq;
      set({ reviewItems: items, reviewCount: items.length, reviewLoading: false });
    } catch (error: any) {
      if (seq === reviewSeq && get().docId === docId) {
        set({ reviewLoading: false, reviewError: "审核列表加载失败：" + error.message });
      }
    }
  },
  refreshReviewCount: async () => {
    const seq = ++reviewCountSeq;
    const { docId } = get();
    if (!docId) return set({ reviewCount: 0 });
    try {
      const result = await api.blockCounts(docId);
      if (seq === reviewCountSeq && get().docId === docId) {
        set({ reviewCount: result.by_status.needs_review ?? 0 });
      }
    } catch {
      /* 角标刷新失败保留上次计数，避免干扰阅读。 */
    }
  },
}));
