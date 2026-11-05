import Icon from "../components/Icon";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement } from "react";
import { api } from "../api/client";
import HighlightLayer, { type HighlightRect } from "../components/HighlightLayer";
import { pdfRectToScreen, screenRectToPdf, type Rect } from "./coord";
import { useStore } from "../state/store";

let pdfjsModule: Promise<typeof import("pdfjs-dist")> | null = null;
function loadPdfJs() {
  return pdfjsModule ??= import("pdfjs-dist").then((pdfjs) => {
    pdfjs.GlobalWorkerOptions.workerSrc = new URL(
      "pdfjs-dist/build/pdf.worker.mjs", import.meta.url,
    ).toString();
    return pdfjs;
  }).catch((error) => { pdfjsModule = null; throw error; });
}

function token(): string {
  const meta = document.querySelector('meta[name="local-token"]');
  return meta ? meta.getAttribute("content") || "" : "";
}

const RELATION_COLORS: Record<string, string> = {
  defines: "rgba(59,130,246,0.28)",
  states: "rgba(16,185,129,0.28)",
  proves: "rgba(168,85,247,0.28)",
  motivates: "rgba(245,158,11,0.28)",
  illustrates: "rgba(236,72,153,0.28)",
  summarizes: "rgba(14,165,233,0.28)",
};

export default function PdfPane(): ReactElement {
  const docId = useStore((s) => s.docId);
  const pdfPage = useStore((s) => s.pdfPage);
  const setPdfPage = useStore((s) => s.setPdfPage);
  const highlight = useStore((s) => s.highlightAnchor);
  const selectionHits = useStore((s) => s.selectionHits);
  const setSelectionHits = useStore((s) => s.setSelectionHits);
  const setHighlight = useStore((s) => s.setHighlight);
  const setToast = useStore((s) => s.setToast);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const pageRef = useRef<HTMLDivElement>(null);
  const docRef = useRef<any>(null);
  const pdfjsRef = useRef<typeof import("pdfjs-dist") | null>(null);
  const viewportRef = useRef<any>(null);
  const [numPages, setNumPages] = useState(0);
  const [scale, setScale] = useState(1.2);
  // 拖拽平移：默认开启（拖动画面），关掉后恢复文字框选（选区匹配）；中键拖拽始终平移
  const [panMode, setPanMode] = useState(() => localStorage.getItem("note-gen-pdf-pan") !== "0");
  // 文档代次：加载完成 +1，驱动重绘效果（仅靠 docId 会在切换教材时漏触发——
  // 加载是异步的，等它完成时 pdfPage 已被重置为 1，依赖未变、画布残留旧教材）
  const [docGen, setDocGen] = useState(0);
  const [panning, setPanning] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ x: number; y: number; left: number; top: number } | null>(null);
  const [textItems, setTextItems] = useState<any[]>([]);
  const [pageLabels, setPageLabels] = useState<string[] | null>(null);
  const [renderKey, setRenderKey] = useState(0);
  const renderTaskRef = useRef<any>(null);

  useEffect(() => {
    if (!docId) return;
    let cancelled = false;
    // 切换教材/卸载时销毁上一个 PDF 文档，释放 worker 与解析缓存
    if (docRef.current) {
      try {
        docRef.current.destroy();
      } catch {
        /* 已销毁 */
      }
      docRef.current = null;
    }
    // 切换教材即刻清空旧画面/文本层/高亮：不得把上一本教材的页留在预览里
    setTextItems([]);
    setPageLabels(null);
    setHighlight(null);
    setSelectionHits([]);
    viewportRef.current = null;
    if (canvasRef.current) {
      canvasRef.current.width = 0;
      canvasRef.current.height = 0;
    }
    (async () => {
      const pdfjs = await loadPdfJs();
      if (cancelled) return;
      pdfjsRef.current = pdfjs;
      const task = pdfjs.getDocument({
        url: api.pdfUrl(docId),
        httpHeaders: { "X-Local-Token": token() },
        // CJK CID 字体解码依赖预定义 CMap（缺了整页中文空白）；
        // 非嵌入字体（CMR10 等）依赖标准字体数据（缺了根号等字形错位）
        cMapUrl: "/cmaps/",
        cMapPacked: true,
        standardFontDataUrl: "/standard_fonts/",
        useSystemFonts: true,
      });
      const doc = await task.promise;
      if (cancelled) {
        doc.destroy();
        return;
      }
      docRef.current = doc;
      setDocGen((g) => g + 1);   // 文档就绪：驱动重绘（pdfPage 未变时唯一的重绘触发源）
      setNumPages(doc.numPages);
      try {
        const labels = await doc.getPageLabels();
        if (!cancelled) setPageLabels(labels ?? null);
      } catch {
        if (!cancelled) setPageLabels(null);
      }
      if (!cancelled) setPdfPage(1);
    })().catch((e: Error) => { if (!cancelled) setToast("PDF 加载失败：" + e.message); });
    return () => {
      cancelled = true;
      // 卸载：取消在途渲染并销毁文档，避免泄漏与卸载后 setState
      if (renderTaskRef.current) {
        try {
          renderTaskRef.current.cancel();
        } catch {
          /* 已完成 */
        }
      }
      if (docRef.current) {
        try {
          docRef.current.destroy();
        } catch {
          /* 已销毁 */
        }
        docRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId]);

  useEffect(() => {
    const doc = docRef.current;
    if (!doc || !canvasRef.current) return;
    let cancelled = false;
    (async () => {
      const page = await doc.getPage(pdfPage);
      if (cancelled) return;
      // 取消上一次未完成的渲染，避免翻页/缩放并发互相打断
      if (renderTaskRef.current) {
        try {
          renderTaskRef.current.cancel();
        } catch {
          /* 已完成的渲染任务无需取消 */
        }
      }
      const viewport = page.getViewport({ scale });
      viewportRef.current = viewport;
      const canvas = canvasRef.current!;
      const ctx = canvas.getContext("2d")!;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.floor(viewport.width * dpr);
      canvas.height = Math.floor(viewport.height * dpr);
      canvas.style.width = viewport.width + "px";
      canvas.style.height = viewport.height + "px";
      const task = page.render({
        canvasContext: ctx,
        viewport,
        transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
      });
      renderTaskRef.current = task;
      try {
        await task.promise;
      } catch {
        return; // 渲染被取消
      }
      const content = await page.getTextContent();
      if (cancelled) return;
      setTextItems(content.items);
      setRenderKey((k) => k + 1);
    })().catch((e: Error) => { if (!cancelled) setToast("页面渲染失败：" + e.message); });
    return () => {
      cancelled = true;
    };
  }, [docId, pdfPage, scale, docGen]);

  const highlights: HighlightRect[] = useMemo(() => {
    const viewport = viewportRef.current;
    const out: HighlightRect[] = [];
    if (!viewport) return out;
    const push = (bbox: Rect, color: string, pulse: boolean, anchorId?: string) => {
      out.push({ ...pdfRectToScreen(bbox, viewport), color, pulse, anchorId });
    };
    if (highlight && highlight.page === pdfPage) {
      push(highlight.bbox, "rgba(59,130,246,0.35)", true, highlight.anchor_id);
    }
    for (const hit of selectionHits) {
      if (hit.page === pdfPage && (!highlight || hit.anchor_id !== highlight.anchor_id)) {
        push(hit.bbox, RELATION_COLORS[hit.match] ?? "rgba(245,158,11,0.3)", false, hit.anchor_id);
      }
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [highlight, selectionHits, pdfPage, scale, renderKey]);

  const scrollTarget = useMemo(
    // 高亮锚点被 push 在数组首位，曾取「最后一个」元素——有选区命中时会滚到错误矩形
    () => (highlight && highlight.page === pdfPage
      ? highlights.find((h) => h.anchorId === highlight.anchor_id) ?? null
      : null),
    [highlight, highlights, pdfPage]
  );

  const onMouseUp = () => {
    const viewport = viewportRef.current;
    const container = pageRef.current;
    if (!viewport || !container || !docId) return;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return;
    const containerRect = container.getBoundingClientRect();
    // 还原到 viewport CSS 尺度（容器被 CSS 缩放时按比例校正），再转 PDF 用户空间
    const cssScale = (container.clientWidth || viewport.width) / (viewport.width || 1);
    const selAny = sel as any;
    const rawRects: DOMRect[] =
      typeof selAny.getClientRects === "function"
        ? Array.from(selAny.getClientRects() as ArrayLike<DOMRect>)
        : Array.from(sel.getRangeAt(0).getClientRects());
    const rects: Rect[] = [];
    for (const raw of rawRects) {
      if (raw.width <= 0 || raw.height <= 0) continue;
      rects.push([
        (raw.left - containerRect.left) / cssScale,
        (raw.top - containerRect.top) / cssScale,
        (raw.right - containerRect.left) / cssScale,
        (raw.bottom - containerRect.top) / cssScale,
      ]);
    }
    if (!rects.length) return;
    const pdfRects = rects.map((r) => screenRectToPdf(r, viewport));
    const text = sel.toString();
    api
      .match(docId, { page: pdfPage, rects: pdfRects, text })
      .then((res) => setSelectionHits(res.hits))
      .catch((e: Error) => setToast("选区匹配失败：" + e.message));
  };

  const onPointerDown = (e: React.PointerEvent) => {
    const want = panMode || e.button === 1;
    const el = scrollRef.current;
    if (!want || !el) return;
    dragRef.current = { x: e.clientX, y: e.clientY, left: el.scrollLeft, top: el.scrollTop };
    setPanning(true);
    el.setPointerCapture(e.pointerId);
    e.preventDefault();          // 平移时抑制文字选择
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current;
    const el = scrollRef.current;
    if (!d || !el) return;
    el.scrollLeft = d.left - (e.clientX - d.x);
    el.scrollTop = d.top - (e.clientY - d.y);
  };
  const endPan = (e: React.PointerEvent) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    setPanning(false);
    scrollRef.current?.releasePointerCapture(e.pointerId);
  };

  const fitWidth = async () => {
    const doc = docRef.current;
    if (!doc || !scrollRef.current) return;
    try {
      const page = await doc.getPage(pdfPage);
      if (doc !== docRef.current || !scrollRef.current) return;
      const style = getComputedStyle(scrollRef.current);
      const width = scrollRef.current.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      setScale(Math.max(0.2, Math.min(3, width / page.getViewport({ scale: 1 }).width)));
    } catch { /* 文档切换过程中不应用旧尺寸 */ }
  };

  return (
    <div className="pdf-pane">
      <div className="pane-heading">
        <div className="pane-label"><Icon name="book" /><strong>教材原文</strong></div>
        <span className="pane-caption">边读边记，有据可循</span>
      </div>
      <div className="pdf-toolbar" style={{ display: docId ? undefined : "none" }}>
        <button title="上一页" aria-label="上一页" onClick={() => setPdfPage(Math.max(1, pdfPage - 1))} disabled={pdfPage <= 1}><Icon name="left" size={15} /></button>
        <span className="page-indicator">
          {pdfPage} / {numPages || "?"}
          {pageLabels && pageLabels[pdfPage - 1] ? "（印刷页 " + pageLabels[pdfPage - 1] + "）" : ""}
        </span>
        <button title="下一页" aria-label="下一页" onClick={() => setPdfPage(Math.min(numPages, pdfPage + 1))} disabled={pdfPage >= numPages}><Icon name="right" size={15} /></button>
        <button aria-label="缩小原文" onClick={() => setScale((s) => Math.max(0.5, s - 0.2))}>−</button>
        <span className="zoom-level">{Math.round(scale * 100)}%</span>
        <button aria-label="放大原文" onClick={() => setScale((s) => Math.min(3, s + 0.2))}>+</button>
        <button onClick={() => void fitWidth()} title="让原文适应当前栏宽">适宽</button>
        <button
          className={"pan-toggle " + (panMode ? "active" : "")}
          aria-pressed={panMode}
          title="拖拽平移开/关：开启时按住拖动画面；关闭后可框选文字做选区匹配（中键拖拽始终平移）"
          onClick={() =>
            setPanMode((v) => {
              localStorage.setItem("note-gen-pdf-pan", v ? "0" : "1");
              return !v;
            })
          }
        >
          {panMode ? "拖拽" : "选字"}
        </button>
      </div>
      <div
        className={"pdf-scroll" + (panMode ? " pan-mode" : "") + (panning ? " panning" : "")}
        ref={scrollRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endPan}
        onPointerCancel={endPan}
      >
        {!docId && <div className="workspace-empty">
          <span className="empty-symbol"><Icon name="book" size={30} /></span>
          <strong>开启一段专注的阅读</strong>
          <p>导入一份 PDF 或 PPTX 讲义，<br />让原文、笔记和知识关联在这里相遇。</p>
          <span className="empty-step">支持拖放文件到窗口</span>
        </div>}
        <div className="pdf-page" style={{ display: docId ? undefined : "none" }} ref={pageRef} onMouseUp={onMouseUp}>
          <canvas ref={canvasRef} />
          <div className="text-layer">
            {textItems.map((item, i) => {
              const viewport = viewportRef.current;
              if (!viewport || !item.str || !pdfjsRef.current) return null;
              const tx = pdfjsRef.current.Util.transform(viewport.transform, item.transform);
              const fontSize = Math.hypot(tx[2], tx[3]);
              return (
                <span
                  key={i}
                  style={{
                    left: tx[4] + "px",
                    top: tx[5] - fontSize + "px",
                    fontSize: fontSize + "px",
                    height: fontSize + "px",
                  }}
                >
                  {item.str}
                </span>
              );
            })}
          </div>
          <HighlightLayer rects={highlights} scrollTarget={scrollTarget} />
        </div>
      </div>
    </div>
  );
}
