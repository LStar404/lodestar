import type { NoteBlock } from "../api/types";

export function filterNotes(blocks: NoteBlock[], query: string, type: string, reviewOnly: boolean): NoteBlock[] {
  const terms = query.normalize("NFKC").trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return blocks.filter((b) => {
    if (b.deleted_at || (type && b.type !== type) || (reviewOnly && b.status !== "needs_review")) return false;
    if (!terms.length) return true;
    const text = [b.title, b.content, ...b.tags].join(" ").normalize("NFKC").toLocaleLowerCase();
    return terms.every((term) => text.includes(term));
  });
}
