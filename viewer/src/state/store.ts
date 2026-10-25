/** 全局状态（规范 7.3）：zustand 单一 store。 */

import { create } from "zustand";
import { api, type DocSummary, type NoteBlock, type Anchor } from "../api/client";

const anchorCache = new Map<string, Anchor>();
// 在途请求合并：几十张卡片同时挂载时，同一锚点会被重复拉取（无 pending 表时是请求风暴）
const anchorPending = new Map<string, Promise<void>>();

// 请求代次（Review #7）：切章/换书的旧请求晚于新请求返回时不得覆盖当前列表。
// 每次 loadBlocks/selectDoc 自增；响应写回前必须发现代次仍是最新且目标未变。
let loadBlocksSeq = 0;
let selectDocSeq = 0;

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
  selectedBlockId: string | null;
  zoomBlockId: string | null;
  pdfPage: number;
  selectionHits: any[];
  highlightAnchor: Anchor | null;
  agent: AgentState;
  view: "card" | "source" | "preview";
  dirtyDraft: string | null;
  settingsOpen: boolean;
  setupOpen: boolean;
  onboardOpen: boolean;
  openOnboard: (open: boolean) => void;
  reviewOpen: boolean;
  reviewItems: NoteBlock[];
  reviewCount: number;
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
  selectedBlockId: null,
  zoomBlockId: null,
  pdfPage: 1,
  selectionHits: [],
  highlightAnchor: null,
  agent: { plan: null, planId: null, running: false, diff: null, error: null },
  view: "card",
  dirtyDraft: null,
  settingsOpen: false,
  setupOpen: false,
  onboardOpen: false,
  reviewOpen: false,
  reviewItems: [],
  reviewCount: 0,
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
    const wanted = ids.filter((id) => id && !anchorCache.has(id));
    if (!wanted.length) return;
    await Promise.all(
      wanted.map((id) => {
        let p = anchorPending.get(id);
        if (!p) {
          p = api
            .getAnchor(id)
            .then((a) => {
              anchorCache.set(id, a);
            })
            .catch(() => {
              /* 锚点缺失时徽标显示占位 */
            })
            .finally(() => {
              anchorPending.delete(id);
            });
          anchorPending.set(id, p);
        }
        return p;
      })
    );
    set({ anchors: Object.fromEntries(anchorCache) });
  },
  loadDocs: async () => {
    const { docs } = await api.listDocs();
    set({ docs });
  },
  selectDoc: async (docId) => {
    // 换书竞态（Review #7 同源）：A/B 两次换书时 manifest 可能乱序返回，
    // 后点的书必须赢；旧 manifest 丢弃，避免把顶栏切回上一本。
    const seq = ++selectDocSeq;
    const manifest = await api.getManifest(docId);
    if (seq !== selectDocSeq) return;                 // 失败会抛出 → 不清草稿
    const chapterId = manifest.chapters?.[0]?.chapter_id ?? null;
    // 成功切换：选中块/编辑器随 reset 卸载，草稿标记一并清掉（失败路径不执行到这里）
    set({ docId, manifest, chapterId, blocks: [], selectedBlockId: null,
          zoomBlockId: null, pdfPage: 1, dirtyDraft: null });
    if (chapterId) await get().loadBlocks();
  },
  selectChapter: async (chapterId) => {
    // 成功切换章节：选中块被清空、编辑器卸载，草稿标记同时清掉（不存在失败分支）
    set({ chapterId, blocks: [], selectedBlockId: null, zoomBlockId: null, dirtyDraft: null });
    await get().loadBlocks();
  },
  loadBlocks: async () => {
    // 请求代次（Review #7）：开始时快照目标文档/章节并自增代次；响应（含翻页中途）
    // 回来时若已切章/换书或已有更新的请求，直接丢弃——旧章列表不得覆盖当前章。
    const seq = ++loadBlocksSeq;
    const { docId, chapterId } = get();
    if (!docId || !chapterId) return;
    const isCurrent = () =>
      seq === loadBlocksSeq && get().docId === docId && get().chapterId === chapterId;
    // 完整性（Review #7 修复引入的回归）：章块接口是游标分页（limit 默认 100、上限 200）。
    // 只有「完整走完分页」才允许写回：首页失败写 [] 会把当前章凭空清空；第二页失败写
    // 半份会永久缺块；旧请求完整、新请求半份时旧响应又被代次门丢弃 → 永久停在半份。
    // 故任何错误 / 分页上限耗尽都不写回，保留旧列表（不弹致命错误，静默保留）。
    // CURSOR_STALE（取页期间数据 revision 变了，后端要求「重新取首页」）→ 从首页重试一次。
    let items: NoteBlock[] | null = null;
    for (let attempt = 0; attempt < 2 && items === null; attempt++) {
      const all: NoteBlock[] = [];
      let cursor: string | null = null;
      let complete = false;
      try {
        for (let guard = 0; guard < 25; guard++) {   // 25 × 200 = 5000 块上限，防御死循环
          const r = await api.listBlocks(docId, chapterId, false, 200, cursor);
          if (!isCurrent()) return;                  // 翻页途中切章/换书：旧的半份数据不要
          all.push(...(r.items ?? []));
          if (!r.next_cursor) {
            complete = true;
            break;
          }
          cursor = r.next_cursor;
        }
      } catch (e: any) {
        if (!isCurrent()) return;
        if (e?.code === "CURSOR_STALE" && attempt === 0) continue;   // 重新取首页（仅一次）
        return;   // 其它错误 / 重试仍失败：保留旧列表，绝不写半份/空列表
      }
      if (complete) items = all;
      else return;   // 超过 25 页防御上限：视为不完整，不写回
    }
    if (!items || !isCurrent()) return;

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
          if (!ok) return;
          set({ dirtyDraft: null });
        }
      }
    }
    set({ blocks: items });
  },
  requestSelectBlock: (blockId) => {
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
  setHighlight: (anchor) => set({ highlightAnchor: anchor }),
  setView: (view) => set({ view }),
  setDraft: (draft) => set({ dirtyDraft: draft }),
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
    set({ toast: msg });
    if (msg) setTimeout(() => set({ toast: null }), 3000);
  },
  openSettings: (open) => set({ settingsOpen: open }),
  openSetup: (open) => set({ setupOpen: open }),
  openOnboard: (open) => set({ onboardOpen: open }),
  openReview: async (open) => {
    if (!open) {
      set({ reviewOpen: false });
      void get().refreshReviewCount();   // 审核完关窗即刷新角标
      return;
    }
    const { docId } = get();
    if (!docId) return;
    const { items } = await api.reviewQueue(docId);
    set({ reviewOpen: true, reviewItems: items, reviewCount: items.length });
  },
  refreshReviewCount: async () => {
    // 未审核角标（生成完/切书/关窗后调用）：失败不打扰主界面
    const { docId } = get();
    if (!docId) return set({ reviewCount: 0 });
    try {
      const { items } = await api.reviewQueue(docId);
      set({ reviewCount: items.length });
    } catch {
      /* 忽略角标刷新失败 */
    }
  },
}));
