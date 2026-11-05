/** 分享：把分享载荷变成「能直接发出去」的几种形态（全部离线可用）。
 *
 *  - 复制 Markdown：贴进 Notion / Obsidian / Typora 会渲染公式
 *  - 下载 .md：归档或再加工
 *  - 下载 .html：单文件自包含（KaTeX 样式与字体字节内嵌、插图 base64），
 *    对方双击就能看，断网也不掉公式
 *  - 复制纯文本：贴到聊天软件里最省事（公式退化成 TeX 源码）
 */

import katexCssRaw from "katex/dist/katex.min.css?raw";

import { renderMarkdown } from "./markdown";

export interface ShareSource {
  page: number | null;
  quote: string;
}

export interface ShareBlock {
  id: string;
  type?: string;
  title?: string;
  content: string;
  status?: string;
  sources: ShareSource[];
}

export interface SharePayload {
  doc_id: string;
  title: string;
  filename: string;
  markdown: string;
  blocks: ShareBlock[];
  figures: Record<string, string>;
  generated_at?: string | null;
  presentation?: { name: string; note_type: string; cover: { style: string; color: string; subtitle: string; image: string } } | null;
}

const FONT_RE = /url\(fonts\/([A-Za-z0-9_.-]+\.woff2)\)/g;

function base64FromBytes(bytes: Uint8Array): string {
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

/** 把 KaTeX 字体取成 data URL 塞进样式表：分享页不带任何外部依赖。 */
export async function inlineKatexCss(): Promise<string> {
  const names = new Set<string>();
  for (const m of katexCssRaw.matchAll(FONT_RE)) names.add(m[1]);
  const map = new Map<string, string>();
  await Promise.all(
    [...names].map(async (name) => {
      try {
        const r = await fetch("/share-fonts/" + name);
        if (!r.ok) return;
        const buf = new Uint8Array(await r.arrayBuffer());
        map.set(name, "data:font/woff2;base64," + base64FromBytes(buf));
      } catch {
        /* 缺失字体使用浏览器回退，不把本机相对地址写进分享页面。 */
      }
    }),
  );
  // Keep one embedded source per face. KaTeX's .woff/.ttf fallbacks otherwise
  // retain local URLs and trigger blocked network requests in isolated readers.
  return katexCssRaw.replace(/@font-face\s*\{[^}]*\}/g, rule => {
    const name = rule.match(/url\(fonts\/([A-Za-z0-9_.-]+\.woff2)\)/)?.[1];
    if (!name) return rule;
    const embedded = map.get(name);
    if (!embedded) return "";
    return rule.replace(/src:[^;}]*/, 'src:url(' + embedded + ') format("woff2")');
  });
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function sourceLine(block: ShareBlock): string {
  const pages = [...new Set(block.sources.map((s) => s.page).filter((p) => p != null))];
  if (!pages.length) return "";
  return '<p class="src">出处 ' + pages.map((p) => "p." + p).join("、") + "</p>";
}

/** 单文件 HTML：正文按现有渲染器（markdown-it + KaTeX）出 HTML，图片替换成内嵌数据。 */
export async function buildShareHtml(payload: SharePayload): Promise<string> {
  const katexCss = await inlineKatexCss();
  const parts: string[] = [];
  for (const b of payload.blocks) {
    const html = renderMarkdown(b.content || "", payload.doc_id, {}, payload.figures);
    parts.push(
      '<section class="blk">' +
        (b.title ? "<h3>" + escapeHtml(b.title) + "</h3>" : "") +
        '<div class="md">' + html + "</div>" +
        sourceLine(b) +
        "</section>",
    );
  }
  const when = payload.generated_at ? " · 生成于 " + payload.generated_at : "";
  const css = katexCss;
  const cover = payload.presentation?.cover;
  const color = /^#[a-fA-F0-9]{6}$/.test(cover?.color || "") ? cover!.color : "#6265ad";
  const image = /^data:image\/(png|jpeg);base64,[A-Za-z0-9+/=]+$/.test(cover?.image || "") ? cover!.image : "";
  const coverHtml = cover && cover.style !== "none" ? '<header style="border-radius:16px;overflow:hidden;padding:24px;color:white;background:' +
    (cover.style === "gradient" ? 'linear-gradient(125deg,' + color + ',#172033)' : color) + '">' +
    (cover.style === "image" && image ? '<img alt="笔记封面" style="width:100%;max-height:300px;object-fit:cover" src="' + image + '">' : '') +
    '<p>' + escapeHtml(payload.presentation?.note_type || "") + '</p><h2>' + escapeHtml(payload.title) + '</h2><p>' + escapeHtml(cover.subtitle) + '</p></header>' : "";
  return [
    "<!doctype html>",
    '<html lang="zh"><head><meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width,initial-scale=1">',
    "<title>" + escapeHtml(payload.title) + "</title>",
    "<style>" + css + "</style>",
    "<style>",
    ":root{color-scheme:light dark}",
    'body{max-width:820px;margin:32px auto;padding:0 20px;font:16px/1.75 -apple-system,"Segoe UI","Noto Sans SC",sans-serif;color:#1f2328;background:#fff}',
    "h1{font-size:26px;margin:0 0 4px}",
    ".meta{color:#6b7280;font-size:13px;margin-bottom:28px}",
    ".blk{border-top:1px solid #e5e7eb;padding:18px 0}",
    ".blk:first-of-type{border-top:0}",
    ".blk h3{font-size:18px;margin:0 0 8px}",
    ".md img{max-width:100%}",
    ".md table{border-collapse:collapse}.md td,.md th{border:1px solid #d1d5db;padding:4px 8px}",
    ".src{color:#6b7280;font-size:13px;margin:8px 0 0}",
    ".md blockquote{margin:8px 0;padding:4px 12px;border-left:3px solid #d1d5db;color:#4b5563}",
    ".katex-display{overflow-x:auto;overflow-y:hidden}",
    "@media (prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}.blk{border-color:#30363d}.src{color:#9ca3af}}",
    "@media print{body{max-width:none}.blk{break-inside:avoid}}",
    "</style></head><body>",
    coverHtml,
    "<h1>" + escapeHtml(payload.title) + "</h1>",
    '<div class="meta">note-gen 分享' + escapeHtml(when) + "</div>",
    parts.join("\n"),
    "</body></html>",
  ].join("\n");
}

/** 纯文本版：公式保留 TeX 源码，适合直接粘进聊天窗口。 */
export function buildPlainText(payload: SharePayload): string {
  const lines: string[] = [payload.title, "（note-gen 分享）", ""];
  for (const b of payload.blocks) {
    if (b.title) lines.push("—— " + b.title + " ——");
    lines.push((b.content || "").trim());
    const pages = [...new Set(b.sources.map((s) => s.page).filter((p) => p != null))];
    if (pages.length) lines.push("（出处 " + pages.map((p) => "p." + p).join("、") + "）");
    lines.push("");
  }
  return lines.join("\n").trimEnd() + "\n";
}


/** .ngshare 分享包：对方装了 note-gen 就能直接导入阅读（纯 JSON，图片内嵌）。 */
export interface NgshareBlock {
  type: string;
  title: string;
  content: string;
  sources: ShareSource[];
}

export interface NgsharePayload {
  format: "note-gen-share";
  version: 1;
  created_at: string;
  title: string;
  chapter_title: string;
  blocks: NgshareBlock[];
  figures: Record<string, string>;
}

export function buildNgshare(payload: SharePayload, chapterTitle: string): NgsharePayload {
  return {
    format: "note-gen-share",
    version: 1,
    created_at: new Date().toISOString(),
    title: payload.title,
    chapter_title: chapterTitle,
    blocks: payload.blocks.map((b) => ({
      type: b.type ?? "",
      title: b.title ?? "",
      content: b.content ?? "",
      sources: (b.sources ?? []).map((s) => ({ page: s.page, quote: s.quote })),
    })),
    figures: payload.figures,
  };
}

/** Resolve figure tokens directly; package keys must never become regex source. */
export function renderSharedBlock(content: string, figures: Record<string, string>): string {
  return renderMarkdown(content || "", "", {}, figures ?? {});
}

export function downloadText(filename: string, content: string, mime: string): void {
  const blob = new Blob([content], { type: mime + ";charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  ta.remove();
}
