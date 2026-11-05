import { api, type NoteBlock } from "../api/client";

/** Only publish complete chapters; a changing revision restarts pagination once. */
export async function readChapter(docId: string, chapterId: string, isCurrent = () => true) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const items: NoteBlock[] = [];
    let revision: number | undefined;
    let cursor: string | null = null;
    try {
      for (let page = 0; page < 25; page++) {
        const result = await api.listBlocks(docId, chapterId, false, 200, cursor);
        if (!isCurrent()) return null;
        if (revision !== undefined && result.data_revision !== revision) {
          throw { code: "CURSOR_STALE", message: "笔记正在更新，请重试" };
        }
        revision = result.data_revision;
        items.push(...result.items);
        cursor = result.next_cursor;
        if (!cursor) return { items, revision };
      }
      throw Error("本章笔记数量过多，未能完整读取");
    } catch (error: any) {
      if (!isCurrent()) return null;
      if (error?.code === "CURSOR_STALE" && attempt === 0) continue;
      throw error;
    }
  }
  return null;
}
