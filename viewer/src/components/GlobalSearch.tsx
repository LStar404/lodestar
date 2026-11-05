import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useStore } from "../state/store";
import Icon from "./Icon";

type Hit = { kind: "note" | "source"; id: string; title: string; detail: string; chapter?: string };

export default function GlobalSearch() {
  const docId = useStore((s) => s.docId);
  const manifest = useStore((s) => s.manifest);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [hits, setHits] = useState<Hit[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState(-1);
  const [retry, setRetry] = useState(0);
  const [locating, setLocating] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const request = useRef(0);
  const navigation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const [composing, setComposing] = useState(false);
  const dismiss = () => { setOpen(false); ++navigation.current; setLocating(false); };
  useEffect(() => {
    ++request.current; ++navigation.current;
    setQuery(""); setHits([]); setOpen(false); setError(null); setLocating(false);
  }, [docId]);
  useEffect(() => {
    const onOutside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) dismiss();
    };
    document.addEventListener("pointerdown", onOutside);
    return () => document.removeEventListener("pointerdown", onOutside);
  }, []);
  useEffect(() => {
    const seq = ++request.current;
    controller.current?.abort();
    setHits([]); setActive(-1); setError(null);
    const term = query.trim();
    if (!docId || !term || composing) { setLoading(false); return; }
    setLoading(true);
    const abort = new AbortController();
    controller.current = abort;
    const timer = window.setTimeout(async () => {
      try {
        const result = await api.search(docId, term, abort.signal);
        if (seq !== request.current || abort.signal.aborted) return;
        setHits([
          ...result.blocks.map((b) => ({ kind: "note" as const, id: b.block_id, title: b.title,
            detail: b.summary, chapter: b.chapter_id })),
          ...result.anchors.map((a) => ({ kind: "source" as const, id: a.anchor_id,
            title: `教材原文 · 第 ${a.page} 页`, detail: a.content })),
        ]);
      } catch (e: any) {
        if (seq === request.current && !abort.signal.aborted) setError(e.message || "暂时无法检索");
      } finally {
        if (seq === request.current && !abort.signal.aborted) setLoading(false);
      }
    }, 250);
    return () => { window.clearTimeout(timer); abort.abort(); };
  }, [docId, query, retry, composing]);
  useEffect(() => {
    if (active >= 0) document.getElementById(`global-hit-${active}`)?.scrollIntoView({ block: "nearest" });
  }, [active]);
  const locate = async (hit: Hit) => {
    if (!docId || locating) return;
    const seq = ++navigation.current;
    setLocating(true);
    try {
      if (hit.kind === "note") {
        if (!await useStore.getState().navigateBlock(hit.id, docId, () => seq === navigation.current)) return;
      } else {
        const { items } = await api.getAnchors(docId, [hit.id]);
        if (seq !== navigation.current || useStore.getState().docId !== docId) return;
        if (!items[0]) throw Error("原文位置已变化，请重新检索");
        useStore.getState().setHighlight(items[0]);
        useStore.getState().setPdfPage(items[0].page);
        document.querySelector(".col-source")?.scrollIntoView({ block: "nearest" });
      }
      if (seq === navigation.current) setOpen(false);
    } catch (e: any) {
      if (seq === navigation.current && useStore.getState().docId === docId) {
        useStore.getState().setToast("定位失败：" + e.message);
      }
    } finally {
      if (seq === navigation.current) setLocating(false);
    }
  };
  const popup = open && !!docId;
  return <div className="search-box global-search" ref={root} onBlur={(e) => {
    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) dismiss();
  }}>
    <Icon name="search" size={17} />
    <input id="global-search" ref={input} aria-label="全局检索" role="combobox"
      aria-expanded={popup} aria-controls={popup ? "global-search-results" : undefined}
      aria-autocomplete="list" aria-activedescendant={popup && active >= 0 ? `global-hit-${active}` : undefined}
      placeholder={docId ? "搜索整本教材与笔记…" : "选择教材后搜索…"} disabled={!docId}
      autoComplete="off" value={query} onFocus={() => setOpen(true)}
      onChange={(e) => { ++request.current; controller.current?.abort(); ++navigation.current;
        setLocating(false); setQuery(e.target.value); setOpen(true); setActive(-1); }}
      onCompositionStart={() => setComposing(true)} onCompositionEnd={() => setComposing(false)}
      onKeyDown={(e) => {
        if (e.nativeEvent.isComposing || composing) return;
        if (e.key === "Escape") { e.stopPropagation(); dismiss(); }
        else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault(); setOpen(true);
          if (hits.length) setActive((i) => i < 0 ? (e.key === "ArrowDown" ? 0 : hits.length - 1)
            : (i + (e.key === "ArrowDown" ? 1 : -1) + hits.length) % hits.length);
        } else if (e.key === "Enter") {
          e.preventDefault();
          if (popup && active >= 0 && hits[active]) void locate(hits[active]);
          else { setOpen(true); setRetry((n) => n + 1); }
        }
      }} />
    {query ? <button className="search-clear" aria-label="清空全局检索" onClick={() => {
      ++request.current; controller.current?.abort(); ++navigation.current; setLocating(false);
      setQuery(""); setHits([]); input.current?.focus();
    }}>×</button> : <kbd className="search-shortcut">/</kbd>}
    {popup && <div className="search-results search-popover">
      <div className="search-heading"><strong>全书检索</strong><span>{locating ? "正在定位…" : "笔记与教材原文"}</span></div>
      <div className="search-feedback" role="status">
        {loading ? "正在查找…" : error ? <><span>{error}</span><button onClick={() => setRetry((n) => n + 1)}>重试</button></>
          : !query.trim() ? "输入标题、正文或关键词，找到后直接跳转。"
          : !hits.length ? "没有找到相关内容，试试更短的关键词。" : `找到 ${hits.length} 条相关内容`}
      </div>
      <div id="global-search-results" role="listbox" aria-label="检索结果" aria-busy={loading || locating}>
        {hits.map((hit, i) => <div key={hit.kind + hit.id} id={`global-hit-${i}`} role="option"
          aria-selected={active === i} className={`search-hit ${active === i ? "active" : ""}`}
          onPointerDown={(e) => e.preventDefault()} onClick={() => void locate(hit)} onMouseEnter={() => setActive(i)}>
          <span className={`search-hit-icon ${hit.kind}`}><Icon name={hit.kind === "note" ? "notes" : "book"} size={16} /></span>
          <span className="search-hit-copy"><span className="search-hit-label">{hit.kind === "note"
            ? manifest?.chapters?.find((c: any) => c.chapter_id === hit.chapter)?.title || "章节笔记" : "教材原文"}</span>
            <strong>{hit.title}</strong><span className="search-excerpt">{hit.detail}</span></span>
          <Icon name="right" size={14} />
        </div>)}
      </div>
      <div className="search-footer"><span>↑ ↓ 选择 · Enter 打开</span><span>Esc 收起</span></div>
    </div>}
  </div>;
}
