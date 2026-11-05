import { useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { useStore } from "../state/store";
import "./knowledge.css";

type Source = { anchor_id: string; page: number | null; quote: string; available: boolean };
type Note = { id: string; title: string; content: string; chapter_id: string; sources: Source[] };
type Group = { id: string; title: string; children: Note[] };
type Chapter = { id: string; title: string; page_start: number; page_end: number; note_count: number;
  failed_pages: number[]; children: Group[] };
type BookTree = { id: string; title: string; children: Chapter[]; coverage: {
  chapters_total: number; chapters_with_notes: number; notes_total: number; scope: string;
} };
type Clipping = { id: string; title: string; source: string; source_url: string; created_at: string;
  content?: string; duplicate?: boolean };
type Download = { filename: string; media_type: string; content: string };

function download(data: Download) {
  const url = URL.createObjectURL(new Blob([data.content], { type: data.media_type }));
  const anchor = document.createElement("a");
  anchor.href = url; anchor.download = data.filename;
  document.body.appendChild(anchor); anchor.click(); anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function Branch({ className, defaultOpen, summary, children }: {
  className: string; defaultOpen: boolean; summary: ReactNode; children: () => ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return <details className={className} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{summary}</summary>
    {open && children()}
  </details>;
}

/** Book → chapter → note type → knowledge point, sharing the original note IDs. */
export default function KnowledgePanel({ onClose }: { onClose: () => void }) {
  const docId = useStore((s) => s.docId);
  const [tab, setTab] = useState<"map" | "exchange">("map");
  const [tree, setTree] = useState<BookTree | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [query, setQuery] = useState("");
  const [expandAll, setExpandAll] = useState(false);
  const [expandRevision, setExpandRevision] = useState(0);
  const [sources, setSources] = useState<string[]>(["通用文本"]);
  const [source, setSource] = useState("通用文本");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [clippings, setClippings] = useState<Clipping[]>([]);
  const [clippingTotal, setClippingTotal] = useState(0);
  const [viewing, setViewing] = useState<Clipping | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    return () => previous?.focus();
  }, []);

  useEffect(() => {
    let current = true;
    setTree(null);
    setError("");
    if (!docId) { setLoading(false); return; }
    setLoading(true);
    api.get<BookTree>(`/api/v1/docs/${docId}/knowledge`).then((result) => {
      if (current) setTree(result);
    }).catch((e: Error) => { if (current) setError(e.message); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [docId, revision]);

  useEffect(() => {
    if (tab !== "exchange") return;
    let current = true;
    Promise.all([
      api.get<{ sources: string[] }>("/api/v1/integrations"),
      api.get<{ items: Clipping[]; total: number }>("/api/v1/integrations/clippings"),
    ]).then(([capabilities, inbox]) => {
      if (!current) return;
      setSources(capabilities.sources); setClippings(inbox.items); setClippingTotal(inbox.total);
    }).catch((e: Error) => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [tab]);

  async function run(action: () => Promise<void>) {
    setBusy(true); setError(""); setMessage("");
    try { await action(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  async function openNote(note: Note, sourceRef?: Source) {
    await run(async () => {
      const state = useStore.getState();
      if (state.docId !== docId) return;
      // Guard before selectChapter, which intentionally clears any editor draft.
      if (!state.requestSelectBlock(note.id)) return;
      if (state.chapterId !== note.chapter_id) await state.selectChapter(note.chapter_id);
      if (useStore.getState().docId !== docId) return;
      if (!useStore.getState().requestSelectBlock(note.id)) return;
      if (sourceRef) {
        const anchor = await api.getAnchor(sourceRef.anchor_id);
        if (useStore.getState().docId !== docId) return;
        useStore.getState().setHighlight(anchor);
        useStore.getState().setPdfPage(anchor.page);
        useStore.getState().setView("source");
      } else {
        useStore.getState().openZoom(note.id);
      }
      onClose();
    });
  }

  const needle = query.trim().toLocaleLowerCase();
  const filtered = (tree?.children ?? []).map((chapter) => {
    if (!needle || chapter.title.toLocaleLowerCase().includes(needle)) return chapter;
    const groups = chapter.children.map((group) => ({ ...group, children: group.children.filter((note) =>
      group.title.toLocaleLowerCase().includes(needle) || (note.title + " " + note.content).toLocaleLowerCase().includes(needle)) }))
      .filter((group) => group.children.length);
    return { ...chapter, children: groups };
  }).filter((chapter) => !needle || chapter.children.length || chapter.title.toLocaleLowerCase().includes(needle));

  return <div className="modal-mask" onClick={onClose}>
    <div className="modal knowledge-panel" role="dialog" aria-modal="true" aria-labelledby="knowledge-heading"
      ref={panelRef} onClick={(event) => event.stopPropagation()} onKeyDown={(event) => {
        if (event.key === "Escape") { event.stopPropagation(); onClose(); }
        if (event.key !== "Tab") return;
        const focusable = Array.from(panelRef.current?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input, textarea, select, summary, a[href]') ?? []).filter((item) => item.getClientRects().length);
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }}>
      <div className="knowledge-header">
        <div><small>KNOWLEDGE WORKSPACE</small><h2 id="knowledge-heading">全书导图与软件互通</h2></div>
        <button ref={closeRef} onClick={onClose} aria-label="关闭全书导图">关闭</button>
      </div>
      <div className="knowledge-tabs" role="tablist" aria-label="知识工作区">
        <button role="tab" aria-selected={tab === "map"} onClick={() => { setTab("map"); setError(""); }}>全书思维导图</button>
        <button role="tab" aria-selected={tab === "exchange"} onClick={() => { setTab("exchange"); setError(""); }}>软件互通 · 摘录箱</button>
      </div>
      {error && <p className="knowledge-error" role="alert">{error}</p>}
      {message && <p className="knowledge-message" role="status">{message}</p>}
      {tab === "map" ? <>
        {!docId && <p className="knowledge-empty">先选择一本已导入的书，即可查看章节与笔记组成的导图。</p>}
        {loading && <p role="status">正在整理章节与笔记…</p>}
        {tree && <>
          <div className="knowledge-overview">
            <h3>{tree.title}</h3>
            <p>{tree.coverage.chapters_with_notes} / {tree.coverage.chapters_total} 章已有笔记 · {tree.coverage.notes_total} 个知识点</p>
            <small>{tree.coverage.scope} 未导入的章节无法自动识别。</small>
          </div>
          <div className="knowledge-actions">
            <input aria-label="搜索导图知识点" placeholder="搜索章节、类型或知识点…" value={query} onChange={(e) => setQuery(e.target.value)} />
            <button onClick={() => { setExpandAll(!expandAll); setExpandRevision((n) => n + 1); }}>{expandAll ? "收起全部" : "展开全部"}</button>
            <button disabled={loading} onClick={() => setRevision((n) => n + 1)}>刷新导图</button>
          </div>
          <div className="knowledge-tree" key={`${expandRevision}:${needle ? "search" : "browse"}`}>
            <div className="knowledge-root">{tree.title}</div>
            <div className="knowledge-branches">
              {filtered.map((chapter) => <Branch className="knowledge-chapter" key={chapter.id} defaultOpen={expandAll || !!needle}
                summary={<><strong>{chapter.title}</strong><span>{chapter.note_count} 个知识点 · 第 {chapter.page_start}–{chapter.page_end} 页</span>
                  {!chapter.note_count && <em>尚无笔记</em>}{chapter.failed_pages.length > 0 && <em>部分原文未解析</em>}</>}>
                {() => <>
                {chapter.failed_pages.length > 0 && <p className="knowledge-warning">第 {chapter.failed_pages.join("、")} 页解析失败，导图可能不完整。</p>}
                {!chapter.note_count && <p className="knowledge-empty">本章尚未整理笔记，可返回正文选择此章生成。</p>}
                {chapter.children.map((group) => <Branch className="knowledge-group" key={group.id} defaultOpen={expandAll || !!needle}
                  summary={<>{group.title}<span>{group.children.length}</span></>}>
                  {() => <div className="knowledge-notes">{group.children.map((note) => <article className="knowledge-note" key={note.id}>
                    <button className="knowledge-note-title" disabled={busy} onClick={() => void openNote(note)}>{note.title} ↗</button>
                    <p>{note.content.length > 150 ? note.content.slice(0, 150) + "…" : note.content || "正文尚未填写"}</p>
                    <div className="knowledge-sources">{note.sources.length ? note.sources.map((ref, index) =>
                      <button key={ref.anchor_id + index} title={ref.quote} disabled={busy || !ref.available}
                        onClick={() => void openNote(note, ref)}>{ref.page ? `原文第 ${ref.page} 页` : "来源待定位"}</button>) : <small>无原文引用</small>}</div>
                  </article>)}</div>}
                </Branch>)}
                </>}
              </Branch>)}
              {!filtered.length && <p className="knowledge-empty">{needle ? "没有匹配的知识点。" : "文件尚无章节，请先完成导入。"}</p>}
            </div>
          </div>
        </>}
      </> : <div className="knowledge-exchange">
        <section>
          <h3>把笔记带到其他软件</h3>
          <p>下载通用文件，再从目标软件的导入菜单打开。格式是否受支持，以你使用的软件版本为准。</p>
          <div className="knowledge-export-grid">
            {([['markdown', 'Markdown 笔记', '.md · 章节、正文和来源'], ['freemind', 'FreeMind 导图', '.mm · 交换树状导图'], ['opml', 'OPML 大纲', '.opml · 交换层级大纲']] as const).map(([format, label, hint]) =>
              <button key={format} disabled={!docId || busy} onClick={() => void run(async () => {
                download(await api.get<Download>(`/api/v1/docs/${docId}/knowledge/export?format=${format}`));
                setMessage("已生成下载文件，可在目标软件中导入。");
              })}><strong>{label}</strong><small>{hint}</small></button>)}
          </div>
          {!docId && <small>请选择一本书后导出。</small>}
          <p className="knowledge-note-hint">目前提供文件交换与粘贴导入，不连接外部账号、不自动同步。图片资源不打包，公式保留为原始文本。</p>
        </section>
        <section>
          <h3>收集其他软件的摘录</h3>
          <p>从微信读书、Zotero、Notion、飞书、语雀、WPS 等复制文本，也可选择 .txt / .md 文件。摘录独立保存在本地，不改写书籍笔记。</p>
          <form onSubmit={(event) => { event.preventDefault(); void run(async () => {
            const saved = await api.post<Clipping>("/api/v1/integrations/clippings", {
              title, content, source, source_url: sourceUrl,
            });
            const inbox = await api.get<{ items: Clipping[]; total: number }>("/api/v1/integrations/clippings");
            setClippings(inbox.items); setClippingTotal(inbox.total); setViewing(saved);
            setContent(""); setTitle(""); setSourceUrl("");
            setMessage(saved.duplicate ? "摘录已存在，已定位原记录。" : "已保存到摘录箱。");
          }); }}>
            <div className="knowledge-form-row">
              <label>摘录标题<input required maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} /></label>
              <label>来源软件<select value={source} onChange={(e) => setSource(e.target.value)}>{sources.map((name) => <option key={name}>{name}</option>)}</select></label>
            </div>
            <label>来源链接（可选）<input type="url" maxLength={2048} value={sourceUrl} placeholder="https://…" onChange={(e) => setSourceUrl(e.target.value)} /></label>
            <label>摘录正文<textarea required rows={6} value={content} onChange={(e) => setContent(e.target.value)} placeholder="粘贴你要保留的摘录…" /></label>
            <div className="knowledge-actions">
              <label className="knowledge-file">或选择文本文件<input type="file" accept=".txt,.md,text/plain,text/markdown" onChange={(event) => {
                const file = event.target.files?.[0]; if (!file) return;
                void run(async () => {
                  if (!/\.(txt|md)$/i.test(file.name) || file.size > 512 * 1024) throw new Error("请选择不超过 512 KiB 的 .txt 或 .md 文件。");
                  const text = await file.text();
                  setContent(text); if (!title) setTitle(file.name.replace(/\.(txt|md)$/i, ""));
                  setMessage("文件已填入正文，检查后点击保存。");
                }); event.target.value = "";
              }} /></label>
              <button type="submit" disabled={busy || !title.trim() || !content.trim()}>保存摘录</button>
            </div>
          </form>
        </section>
        <section>
          <h3>摘录箱 <small>{clippingTotal} 条{clippingTotal > 100 ? " · 显示最近 100 条" : ""}</small></h3>
          <div className="knowledge-inbox">{clippings.map((item) => <button key={item.id} disabled={busy}
            onClick={() => void run(async () => setViewing(await api.get<Clipping>(`/api/v1/integrations/clippings/${item.id}`)))}>
            <strong>{item.title}</strong><small>{item.source} · {new Date(item.created_at).toLocaleDateString()}</small>
          </button>)}</div>
          {!clippings.length && <p className="knowledge-empty">保存第一条摘录后，会在这里长期保留。</p>}
          {viewing && <article className="knowledge-clipping"><h4>{viewing.title}</h4><small>{viewing.source}</small>
            {viewing.source_url && <p><a href={viewing.source_url} target="_blank" rel="noreferrer">打开来源链接 ↗</a></p>}
            <pre>{viewing.content}</pre><button onClick={() => download({ filename: viewing.title.replace(/[<>:"/\\|?*]/g, "_") + ".md",
              media_type: "text/markdown;charset=utf-8", content: `# ${viewing.title}\n\n${viewing.content ?? ""}\n\n来源：${viewing.source}\n${viewing.source_url}` })}>下载这条摘录</button>
          </article>}
        </section>
      </div>}
    </div>
  </div>;
}
