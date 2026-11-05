/** API 客户端：所有 /api 请求自动携带 X-Local-Token（生产取自 HTML meta）。 */

import type { Anchor, DocSummary, NoteBlock } from "./types";

export type { Anchor, DocSummary, NoteBlock, SourceRef, SelectionHit } from "./types";

function token(): string {
  const meta = document.querySelector('meta[name="local-token"]');
  return meta ? meta.getAttribute("content") || "" : "";
}

async function request<T>(method: string, url: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const headers: Record<string, string> = { "X-Local-Token": token() };
  let payload: BodyInit | undefined;
  if (body instanceof FormData) {
    payload = body;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const resp = await fetch(url, { method, headers, body: payload, signal });
  if (!resp.ok) {
    let body: any = {};
    try {
      body = await resp.json();
    } catch {
      body = { error: String(resp.status) };
    }
    // 兼容两种错误形状：{error:"CODE",message,detail} 与 {error:{code,message,details}}
    const nested = body && typeof body.error === "object" && body.error !== null;
    const code = nested ? body.error.code : body.error;
    const message = nested ? body.error.message : body.message;
    const detail = nested ? body.error.details : body.detail;
    const fallback = resp.status === 409 ? "数据已被修改，请刷新后重试" : "请求失败 " + resp.status;
    const err = new Error(message || code || fallback);
    (err as any).code = code;
    (err as any).detail = detail;
    (err as any).status = resp.status;
    throw err;
  }
  return (await resp.json()) as T;
}

export const api = {
  get: <T>(url: string, signal?: AbortSignal) => request<T>("GET", url, undefined, signal),
  post: <T>(url: string, body?: unknown) => request<T>("POST", url, body),
  patch: <T>(url: string, body?: unknown) => request<T>("PATCH", url, body),
  put: <T>(url: string, body?: unknown) => request<T>("PUT", url, body),
  del: <T>(url: string, body?: unknown) => request<T>("DELETE", url, body),

  listDocs: () => api.get<{ docs: DocSummary[] }>("/api/v1/docs"),
  blockTypes: () => api.get<{ profile: string; types: any[]; presets: string[]; custom: any[] }>(
    "/api/v1/block-types"),
  listAllBlocks: (docId: string) =>
    api.get<{ items: NoteBlock[] }>("/api/v1/docs/" + docId + "/blocks"),
  blockAiEdit: (docId: string, blockId: string, instruction: string) =>
    api.post<{ block_id: string; proposal: string; original: string }>(
      "/api/v1/docs/" + docId + "/blocks/" + blockId + "/ai-edit", { instruction }),
  blockApplyEdit: (docId: string, blockId: string, body: any) =>
    api.post<{ block: NoteBlock }>(
      "/api/v1/docs/" + docId + "/blocks/" + blockId + "/apply-edit", body),
  blockAddLink: (docId: string, blockId: string, targetId: string, baseRevision?: number) =>
    api.post<{ block: NoteBlock }>(
      "/api/v1/docs/" + docId + "/blocks/" + blockId + "/links",
      { target_id: targetId, base_revision: baseRevision }),
  blockRemoveLink: (docId: string, blockId: string, targetId: string, baseRevision?: number) =>
    api.del<{ block: NoteBlock }>(
      "/api/v1/docs/" + docId + "/blocks/" + blockId + "/links/" + targetId
      + (baseRevision != null ? "?base_revision=" + baseRevision : "")),
  qbankList: (docId: string) =>
    api.get<{ count: number; entries: any[] }>("/api/v1/docs/" + docId + "/qbank"),
  qbankUpload: (docId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api.post<{ added: number; count: number; file: string }>(
      "/api/v1/docs/" + docId + "/qbank", form);
  },
  quiz: (docId: string, body: any) => api.post<any>("/api/v1/docs/" + docId + "/quiz", body),
  openPath: (path: string) => api.post<{ ok: boolean; path: string }>(
    "/api/v1/system/open-path", { path }),
  getManifest: (docId: string) => api.get<any>("/api/v1/docs/" + docId),
  deleteDoc: (docId: string) =>
    api.del<any>("/api/v1/docs/" + docId, { confirmed: true }),
  renameDoc: (docId: string, title: string) =>
    api.post<{ doc_id: string; title: string }>("/api/v1/docs/" + docId + "/rename", { title }),
  appendPdf: (docId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api.post<{ task_id: string; doc_id: string }>(
      "/api/v1/docs/" + docId + "/append", form);
  },
  rebuildDoc: (docId: string) => api.post<{ task_id: string; doc_id: string }>(
    "/api/v1/docs/" + docId + "/rebuild", {}),
  refreshFigures: (docId: string) => api.post<{ figures: number; patched_blocks: number }>(
    "/api/v1/docs/" + docId + "/refresh-figures", {}),
  pdfUrl: (docId: string) => "/api/v1/docs/" + docId + "/pdf",
  figureUrl: (docId: string, figureId: string) => "/api/v1/docs/" + docId + "/figures/" + figureId,
  listBlocks: (docId: string, chapterId: string, includeDeleted = false,
               limit = 200, cursor?: string | null) =>
    api.get<{ items: NoteBlock[]; next_cursor: string | null; data_revision: number }>(
      "/api/v1/docs/" + docId + "/chapters/" + chapterId + "/blocks?include_deleted="
      + (includeDeleted ? 1 : 0) + "&limit=" + limit
      + (cursor ? "&cursor=" + encodeURIComponent(cursor) : "")
    ),
  blockCounts: (docId: string) =>
    api.get<{ total: number; by_status: Record<string, number>; data_revision: number }>("/api/v1/docs/" + docId + "/block-counts"),
  reviewQueue: (docId: string) =>
    api.get<{ items: NoteBlock[] }>("/api/v1/docs/" + docId + "/blocks?status=needs_review"),
  getBlock: (blockId: string) =>
    api.get<{ block: NoteBlock; history: any[] }>("/api/v1/blocks/" + blockId),
  createBlock: (docId: string, chapterId: string, body: any) =>
    api.post<NoteBlock>("/api/v1/docs/" + docId + "/chapters/" + chapterId + "/blocks", body),
  updateBlock: (blockId: string, body: any) => api.patch<NoteBlock>("/api/v1/blocks/" + blockId, body),
  deleteBlock: (blockId: string, body: any) => api.del<NoteBlock>("/api/v1/blocks/" + blockId, body),
  restoreBlock: (blockId: string, body: any) => api.post<NoteBlock>("/api/v1/blocks/" + blockId + "/restore", body),
  rollbackBlock: (blockId: string, body: any) => api.post<NoteBlock>("/api/v1/blocks/" + blockId + "/rollback", body),
  allowRegeneration: (blockId: string, body: any) =>
    api.post<NoteBlock>("/api/v1/blocks/" + blockId + "/allow-regeneration", body),
  reorder: (docId: string, chapterId: string, body: any) =>
    api.post<{ items: NoteBlock[] }>("/api/v1/docs/" + docId + "/chapters/" + chapterId + "/reorder", body),
  getAnchor: (anchorId: string) => api.get<Anchor>("/api/v1/anchors/" + anchorId),
  getAnchors: (docId: string, ids: string[]) =>
    api.post<{ items: Anchor[]; missing: string[]; data_revision: number }>(
      "/api/v1/docs/" + docId + "/anchors/batch", { ids }),
  blockTitles: (docId: string) =>
    api.get<{ items: { id: string; title: string }[] }>("/api/v1/docs/" + docId + "/block-titles"),
  pageBlocks: (docId: string, page: number) =>
    api.get<any[]>("/api/v1/docs/" + docId + "/pages/" + page + "/blocks"),
  match: (docId: string, body: any) => api.post<{ hits: any[] }>("/api/v1/docs/" + docId + "/match", body),
  search: (docId: string, q: string, signal?: AbortSignal) =>
    api.get<{ anchors: any[]; blocks: any[] }>(
      "/api/v1/search?doc_id=" + docId + "&q=" + encodeURIComponent(q), signal
    ),
  agentEdit: (url: string, body: any) => api.post<any>(url, body),
  generate: (docId: string, body: any) => api.post<{ task_id: string }>("/api/v1/docs/" + docId + "/generate", body),
  generateReports: (docId: string) => api.get<{ reports: any[] }>("/api/v1/docs/" + docId + "/generate-reports"),
  shareDoc: (docId: string, chapterId?: string | null) =>
    api.get<any>("/api/v1/docs/" + docId + "/share"
      + (chapterId ? "?chapter_id=" + encodeURIComponent(chapterId) : "")),
  listShares: () => api.get<{ items: any[] }>("/api/v1/shares"),
  getShare: (shareId: string) => api.get<any>("/api/v1/shares/" + encodeURIComponent(shareId)),
  deleteShare: (shareId: string) => api.del<any>("/api/v1/shares/" + encodeURIComponent(shareId)),
  uploadShare: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api.post<{ share: any }>("/api/v1/shares", form);
  },
  exportDoc: (body: any) => api.post<{ task_id: string }>("/api/v1/export", body),
  getTask: (taskId: string) => api.get<any>("/api/v1/tasks/" + taskId),
  cancelTask: (taskId: string) => api.post<any>("/api/v1/tasks/" + taskId + "/cancel"),
  listTasks: (docId?: string | null, limit = 50) =>
    api.get<{ items: any[] }>(
      "/api/v1/tasks?limit=" + limit + (docId ? "&doc_id=" + encodeURIComponent(docId) : "")),
  deleteTask: (taskId: string) => api.del<any>("/api/v1/tasks/" + taskId),
  resumeTask: (taskId: string) => api.post<any>("/api/v1/tasks/" + taskId + "/resume", {}),
  models: () => api.get<any>("/api/v1/models"),
  testModel: (body: any) => api.post<any>("/api/v1/models/test", body),
  upsertProvider: (id: string, body: any) => api.put<any>("/api/v1/models/" + id, body),
  deleteProvider: (id: string) => api.del<any>("/api/v1/models/" + id),
  updateRoutes: (body: any) => api.put<any>("/api/v1/model-routes", body),
  usage: () => api.get<any>("/api/v1/usage"),
  setupStatus: () => api.get<any>("/api/v1/setup/status"),
  settings: () => api.get<any>("/api/v1/settings"),
  saveSettings: (body: any) => api.put<any>("/api/v1/settings", body),
};
