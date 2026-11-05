/** 前端预览渲染（规范 7.4 MUST）：
 *  - markdown-it 关闭原始 HTML（html:false）+ KaTeX 默认安全配置；
 *  - 图占位符 ![{fig_id}] 在 inline token 阶段识别为专用 figure token（math 规则先注册，
 *    代码/数学区天然不被改写），渲染为 API URL；
 *  - 数学 $…$ / $$…$$ 走 KaTeX renderer，不对已渲染 HTML 做正则后处理。
 */

import MarkdownIt from "markdown-it";
import katex from "katex";
import "katex/dist/katex.min.css";   // KaTeX 字体/排版样式：缺了公式显示为乱排的 HTML

export interface RenderEnv {
  docId: string;
  captions?: Record<string, string>;
  figureUrls?: Record<string, string>;
}

const FIG_RE = /^!\[(fig_\d{8})\]/;
const MATH_INLINE_RE = /^\$([^$\n]+?)\$/;

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderMath(tex: string, displayMode: boolean): string {
  try {
    return katex.renderToString(tex, {
      displayMode,
      throwOnError: false,
      // 默认安全配置：不信任 \html 等可执行扩展
      trust: false,
      strict: "warn",
    });
  } catch {
    return "<code>" + escapeHtml(tex) + "</code>";
  }
}

/** 数学规则：块级 $$…$$ 优先于行内 $…$。 */
function mathPlugin(md: MarkdownIt): void {
  md.block.ruler.before(
    "fence",
    "math_block",
    (state, startLine, endLine, silent) => {
      const startPos = state.bMarks[startLine] + state.tShift[startLine];
      const maxPos = state.eMarks[startLine];
      const first = state.src.slice(startPos, maxPos).trim();
      if (!first.startsWith("$$")) return false;
      if (silent) return true;

      let content = first.slice(2);
      let lineNo = startLine;
      if (!(content.endsWith("$$") && content.length >= 2)) {
        const buf = [content];
        lineNo = startLine + 1;
        let closed = false;
        while (lineNo < endLine) {
          const s = state.bMarks[lineNo] + state.tShift[lineNo];
          const e = state.eMarks[lineNo];
          const text = state.src.slice(s, e);
          if (text.trim().endsWith("$$")) {
            buf.push(text.trim().slice(0, -2));
            closed = true;
            break;
          }
          buf.push(text);
          lineNo += 1;
        }
        if (!closed) return false;
        content = buf.join("\n");
      } else {
        content = content.slice(0, -2);
      }
      const token = state.push("math_block", "math", 0);
      token.content = content;
      token.map = [startLine, lineNo + 1];
      state.line = lineNo + 1;
      return true;
    },
    { alt: ["paragraph", "reference", "blockquote", "list"] }
  );

  md.inline.ruler.after("escape", "math_inline", (state, silent) => {
    const start = state.pos;
    if (state.src.charCodeAt(start) !== 0x24 /* $ */) return false;
    const match = MATH_INLINE_RE.exec(state.src.slice(start));
    if (!match) return false;
    if (!silent) {
      const token = state.push("math_inline", "math", 0);
      token.content = match[1];
    }
    state.pos += match[0].length;
    return true;
  });

  md.renderer.rules.math_inline = (tokens, idx) => renderMath(tokens[idx].content, false);
  md.renderer.rules.math_block = (tokens, idx) =>
    "<p>" + renderMath(tokens[idx].content, true) + "</p>";
}

/** 图占位符专用 token：仅在 inline 阶段、非数学/代码区命中。 */
function figurePlugin(md: MarkdownIt): void {
  md.inline.ruler.before("image", "figure", (state, silent) => {
    const match = FIG_RE.exec(state.src.slice(state.pos));
    if (!match) return false;
    if (!silent) {
      const token = state.push("figure", "img", 0);
      token.meta = { figId: match[1] };
      token.content = match[1];
    }
    state.pos += match[0].length;
    return true;
  });

  md.renderer.rules.figure = (tokens, idx, _options, env) => {
    const { figId } = tokens[idx].meta as { figId: string };
    const renderEnv = (env ?? {}) as RenderEnv;
    const caption = renderEnv.captions?.[figId] ?? figId;
    const docId = renderEnv.docId ?? "";
    const src = renderEnv.figureUrls?.[figId] ?? "/api/v1/docs/" + docId + "/figures/" + figId;
    return (
      '<img src="' +
      escapeHtml(src) +
      '" alt="' +
      escapeHtml(caption) +
      '" onerror="this.style.display=' + "'none'" + '">'
    );
  };
}

function createRenderer(): MarkdownIt {
  const md = new MarkdownIt({ html: false, linkify: true, breaks: false });
  mathPlugin(md);    // 先注册数学：figure 规则天然跳过数学/代码区
  figurePlugin(md);
  return md;
}

const renderer = createRenderer();

// 代码区哨兵：\u0001 在笔记正文里永不合法（后端 sanitize_control_chars 会清掉），
// 用它临时顶替代码片段，避免把代码里的 \(..\) 当公式改写。
const MASK = "\u0001";

function lineStartAt(src: string, pos: number): number {
  const i = src.lastIndexOf("\n", pos - 1);
  return i < 0 ? 0 : i + 1;
}
function lineEndAt(src: string, pos: number): number {
  const i = src.indexOf("\n", pos);
  return i < 0 ? src.length : i;
}

/** 遮住代码区：围栏代码块（``` 与 ~~~）与行内代码（`…`）。
 *  代码里的 \(x\) 是字面文本，必须原样保留——后端 normalize_math_text 只遮围栏，
 *  前端预览多遮一层行内代码（行内代码常被用来展示公式写法）。 */
function maskCode(src: string, masked: string[]): string {
  const keep = (s: string): string => {
    masked.push(s);
    return MASK + (masked.length - 1) + MASK;
  };
  const lines = src.split("\n");
  const out: string[] = [];
  let fence: { ch: string; len: number } | null = null;
  let buf: string[] = [];
  for (const line of lines) {
    if (fence) {
      buf.push(line);
      const close = /^[ \t]*(`{3,}|~{3,})[ \t]*$/.exec(line);
      if (close && close[1][0] === fence.ch && close[1].length >= fence.len) {
        out.push(keep(buf.join("\n")));
        buf = [];
        fence = null;
      }
      continue;
    }
    const open = /^[ \t]*(`{3,}|~{3,})(.*)$/.exec(line);
    if (open) {
      fence = { ch: open[1][0], len: open[1].length };
      buf = [line];
      continue;
    }
    out.push(line);
  }
  if (fence) out.push(keep(buf.join("\n")));   // 未闭合围栏：按代码吃到文末
  // 行内代码必须在围栏遮蔽之后处理：围栏内容已是哨兵，这里的反引号规则碰不到它
  return out.join("\n").replace(/(`+)([^`\n]*?)\1/g, (m) => keep(m));
}

/** 数学定界符典则化：\(…\) → $…$；\[…\] 独占一行 → $$…$$，行中 → $…$。
 *
 *  与后端 src/core/text.py normalize_math_text 的差异（有意为之）：
 *  - 后端一律把 \[..\] 典则化成 $$..$$；但 markdown-it 的 display 公式只认
 *    「行首 $$ … 行尾 $$」，行中的 $$ 会渲染出多余裸 $，同一行两个 \[..\] 还会
 *    把中间文字吞进公式（实测：前文 \[x+1\] 后文）。故前端只在「独占一行」时转 $$，
 *    行中改渲染为行内 $…$（信息不丢、不出裸 $）。
 *  - 后端只遮围栏代码，前端连行内代码一起遮（代码里的 \(..\) 必须原样保留）。
 *  - 行内 body（\(..\) 与行中的 \[..\]）换行并成空格，对齐后端行内规则；
 *    独占一行 \[..\] 的 body 换行保留（多行 display 合法）。
 *  其余对齐后端：已有 $..$ 区间内不转换（存量脏嵌套 $\(\zeta\)_1$），转换出的 $
 *  不得与相邻 $ 粘成假 $$（开闭邻接守卫）；只接受成对分隔符（\(…\] 不转）。
 *  inlineOnly=true 供标题等行内渲染：此时 \[..\] 一律按行内 $…$ 处理（行内渲染器
 *  没有块级公式规则，$$ 会显示成裸文本）。 */
function canonicalizeMathDelimiters(src: string, inlineOnly = false): string {
  if (!src) return src;
  const masked: string[] = [];
  let text = maskCode(src.replace(/\r\n?/g, "\n"), masked);

  // 区间守卫（与后端 normalize_math_text 同规则）：已有 $..$ 段内的定界符不转换
  // （存量脏嵌套 $\(\zeta\)_1$）；邻接守卫由正则的 (?<!\$)/(?!\$) 承担。
  const spans: Array<[number, number]> = [];
  for (const m of text.matchAll(/(\$\$[\s\S]*?\$\$|\$[^$]*?\$)/g)) {
    spans.push([m.index ?? 0, (m.index ?? 0) + m[0].length]);
  }
  const inMathSpan = (start: number, end: number): boolean =>
    spans.some(([a, b]) => !(end <= a || start >= b));

  // 开闭配对表：合法公式是 `\(`…`\)` 与 `\[`…`\]`——开盘和收盘本就是不同字符，
  // 必须按表校验；只接受成对的分隔符。
  const PAIRS: Record<string, string> = { "(": ")", "[": "]" };
  text = text.replace(
    /(?<!\$)\\(\[|\()([\s\S]*?)\\(\]|\))(?!\$)/g,
    (full, open: string, body: string, close: string, offset: number) => {
      if (PAIRS[open] !== close) return full;
      if (inMathSpan(offset, offset + full.length)) return full;
      // 「独占一行」= 开标记所在行前面只有空白，且闭标记之后到行尾只有空白
      const standalone = !inlineOnly
        && text.slice(lineStartAt(text, offset), offset).trim() === ""
        && text.slice(offset + full.length, lineEndAt(text, offset + full.length)).trim() === "";
      if (open === "[" && standalone) return "$$" + body + "$$";
      // 行内渲染：跨行 body 并成空格，否则 markdown-it 的 $…$ 规则不跨行匹配，
      // $ 会配对错位、散文被吞进公式（同后端行内规则）
      return "$" + body.replace(/\r?\n/g, " ") + "$";
    }
  );

  // 还原代码区：逐 token 还原（行内代码原文不会包含围栏哨兵）
  for (let i = 0; i < masked.length; i++) {
    text = text.split(MASK + i + MASK).join(masked[i]);
  }
  return text;
}

export function renderMarkdown(content: string, docId: string, captions: Record<string, string> = {},
                               figureUrls?: Record<string, string>): string {
  return renderer.render(canonicalizeMathDelimiters(content || ""),
    { docId, captions, figureUrls } as RenderEnv);
}

/** 行内渲染（标题等单行文本）：$…$ 走 KaTeX，无块级包装。 */
export function renderInline(content: string, docId: string = ""): string {
  return renderer.renderInline(canonicalizeMathDelimiters(content || "", true),
    { docId, captions: {} } as RenderEnv);
}

/** 卡片预览截断：按行边界截，绝不切断公式——
 *  $…$ 成对保留；$$…$$ 块整体保留或整体丢弃（截断后计数为奇数就回退到开标记前）。 */
export function previewSource(content: string, maxChars = 320): string {
  const src = canonicalizeMathDelimiters(content || "");
  if (src.length <= maxChars) return src;
  let cut = src.lastIndexOf("\n", maxChars);
  if (cut < 40) cut = maxChars;
  let head = src.slice(0, cut);
  const dbl = (head.match(/\$\$/g) || []).length;
  if (dbl % 2 === 1) {
    head = head.slice(0, head.lastIndexOf("$$"));
  }
  const single = (head.replace(/\$\$/g, "").match(/\$/g) || []).length;
  if (single % 2 === 1) {
    head = head.slice(0, head.lastIndexOf("$"));
  }
  return head.trimEnd() + "\n…";
}

/** 卡片预览 HTML：与「预览」视图同一渲染器（markdown + KaTeX）。 */
export function previewHtml(content: string, docId: string): string {
  return renderMarkdown(previewSource(content), docId ?? "", {});
}

/** 图占位符重写（供导出对照逻辑复用）；代码/数学区不改写。 */
export function rewriteFigures(content: string, docId: string, captions: Record<string, string> = {}): string {
  return content.replace(/!\[(fig_\d{8})\]/g, (_m, figId: string) =>
    "![" + (captions[figId] ?? figId) + "](/api/v1/docs/" + docId + "/figures/" + figId + ")"
  );
}
