"""Anki 闪卡导出（11.4 / E 类学习闭环）：definition/theorem/motivation → 卡片。

正面 = 概念/标题；背面 = content + 来源页码；优先生成 .apkg（genanki），
不可用时回退 CSV（Anki 可导入）。卡片与 deck ID 由 doc/章确定性派生，重复导入即更新。
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import os
import re
import tempfile
from pathlib import Path

from src.core.store import DocStore

from .render import figure_index, live_blocks, rewrite_citations

# 闪卡类型 = 注册表中除纯视觉/延伸类以外的全部类型（学科模板的定律/算法/诊断等
# 都是知识点，曾写死数学三类导致新模板的内容进不了牌组）
NO_CARD_TYPES = ("figure", "link")


def card_types() -> tuple[str, ...]:
    from src.core.block_types import REGISTRY, _CUSTOM

    return tuple(t for t in list(REGISTRY) + list(_CUSTOM) if t not in NO_CARD_TYPES)
MATH_RE = re.compile(r"(\$\$.*?\$\$|\$.*?\$)", re.DOTALL)


def _stable_id(*parts: str) -> int:
    """genanki 需要 31 位整数 ID；由内容确定性派生，重复导入即更新。"""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % (2 ** 31)


def _math_to_anki(text: str) -> str:
    r"""Anki/MathJax 使用 \( \) 与 \[ \]；保持内容本身不动（对拍友好）。"""
    def repl(m: re.Match) -> str:
        seg = m.group(0)
        if seg.startswith("$$"):
            return "\\[" + seg[2:-2] + "\\]"
        return "\\(" + seg[1:-1] + "\\)"
    return MATH_RE.sub(repl, text)


_MATH_SEG_RE = re.compile(r"\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]")


def _md_to_card_html(text: str) -> str:
    r"""卡片背面的轻量 Markdown → HTML：Anki 不渲染 md，字面 **加粗**/[链接](x) 会很难看。

    数学段 \(..\)/\[..\] 先遮蔽再做 md 变换——里面的 * 是乘号不是斜体；
    解遮时单独做 HTML 转义：数学里的 <（R<\infty）必须转义，否则被当标签吞掉。"""
    masked: list[str] = []

    def _keep(m: re.Match) -> str:
        masked.append(m.group(0))
        return chr(0) + str(len(masked) - 1) + chr(0)

    work = _MATH_SEG_RE.sub(_keep, text)
    work = html.escape(work)
    work = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", work)
    work = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", work)
    work = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", work)
    # 链接：http(s) 成超链接；note:// 等站内引用在 Anki 里点不开 → 留纯文本
    work = re.sub(
        r"\[([^\]\n]+)\]\(([^)\n]+)\)",
        lambda m: (f'<a href="{m.group(2)}">{m.group(1)}</a>'
                  if m.group(2).startswith(("http://", "https://")) else m.group(1)),
        work)
    work = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", work)
    work = work.replace("\n", "<br/>")
    for i, seg in enumerate(masked):
        work = work.replace(chr(0) + str(i) + chr(0), html.escape(seg))
    return work


def _back_html(block: dict, anchors_by_id: dict[str, dict]) -> str:
    content = _math_to_anki(block.get("content", ""))
    pages = []
    for src in block.get("sources", []):
        page = (anchors_by_id.get(src["anchor_id"]) or {}).get("page")
        if page and page not in pages:
            pages.append(page)
    # 必须 HTML 转义：数学里的 <（R<\infty）会被当标签吞掉，Anki 渲染残缺。
    # 实体在文本节点里被浏览器还原，MathJax 照常读到原始 <。
    content_html = _md_to_card_html(content)
    footer = ("来源页码：" + "、".join(f"p.{p}" for p in pages)) if pages else "（AI 归纳，无来源页码）"
    return f"<div>{content_html}</div><hr/><div style='color:#666;font-size:12px'>{footer}</div>"


def collect_cards(store: DocStore, chapter_id: str | None = None) -> list[dict]:
    anchors_by_id = {a["anchor_id"]: a for a in store.all_anchor_rows()}
    figure_paths, figure_captions = figure_index(store.doc_id, store)
    refs_by_num = {int(r["num"]): r for r in store.ref_rows()}
    chapters = [chapter_id] if chapter_id else [c["chapter_id"] for c in store.manifest().get("chapters", [])]
    cards: list[dict] = []
    for ch in chapters:
        for block in live_blocks(store.block_rows(ch)):
            if block.get("type") not in card_types():
                continue
            content = rewrite_citations(block.get("content", ""), refs_by_num, mode="md")
            content = rewrite_figures_plain(content, figure_paths, figure_captions)
            cards.append({
                "block_id": block["id"],
                "chapter_id": ch,
                "type": block["type"],
                "front": block.get("title", ""),
                "back": _back_html({**block, "content": content}, anchors_by_id),
                "back_text": block.get("content", ""),
                "tags": [block.get("type", "card")] + list(block.get("tags", [])),
                "pages": sorted({(anchors_by_id.get(s["anchor_id"]) or {}).get("page")
                                 for s in block.get("sources", [])} - {None}),
            })
    return cards


def rewrite_figures_plain(content: str, figure_paths: dict[str, str],
                          figure_captions: dict[str, str]) -> str:
    """卡片内图占位符降级为纯文本图注（Anki 资源另行打包，避免断链）。
    数学/代码区不改写（与 render.rewrite_figures 同规则）。"""
    import re as _re

    def repl(m: _re.Match) -> str:
        fig_id = m.group(1)
        return f"［图：{figure_captions.get(fig_id, figure_paths.get(fig_id, fig_id))}］"

    protected: list[str] = []

    def stash(m: _re.Match) -> str:
        protected.append(m.group(0))
        return chr(0) + str(len(protected) - 1) + chr(0)

    from .render import CODE_FENCE, MATH_REGION

    text = CODE_FENCE.sub(stash, content)
    text = MATH_REGION.sub(stash, text)
    text = _re.sub(r"!\[(fig_\d{8})\]", repl, text)
    for i, raw in enumerate(protected):
        text = text.replace(chr(0) + str(i) + chr(0), raw)
    return text


def export_anki(store: DocStore, chapter_id: str | None = None, *, out_dir: Path,
                deck_name: str | None = None, lang: str = "zh",
                encoding: str = "utf-8") -> Path:
    cards = collect_cards(store, chapter_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-{chapter_id}" if chapter_id else ""
    deck_name = deck_name or f"Lodestar · {store.manifest().get('title', store.doc_id)}{suffix}"

    try:
        import genanki
    except Exception:
        return _export_csv(store, cards, out_dir, suffix, encoding=encoding)

    model_id = _stable_id("note-gen-model", "v1")  # 稳定 id 保持不变，老用户不会多出一套模型
    deck_id = _stable_id("note-gen-deck", store.doc_id, chapter_id or "_book")
    model = genanki.Model(
        model_id,
        "Lodestar 笔记卡",
        fields=[{"name": "Front"}, {"name": "Back"}, {"name": "Source"}],
        templates=[{
            "name": "卡片",
            "qfmt": "<div class='q'>{{Front}}</div>",
            "afmt": "{{FrontSide}}<hr id='answer'><div class='a'>{{Back}}</div>",
        }],
        css=".q{font-size:20px;} .a{font-size:16px;}",
    )
    deck = genanki.Deck(deck_id, deck_name)
    for card in cards:
        note = genanki.Note(
            model=model,
            fields=[html.escape(card["front"]), card["back"],
                    "、".join(f"p.{p}" for p in card["pages"]) or "-"],
            tags=[t.replace(" ", "_") for t in card["tags"] if t],
            guid=genanki.guid_for(store.doc_id, card["block_id"]),
        )
        deck.add_note(note)
    # 先写临时文件再走导出守护：用户改过同名 .apkg 时落 .exported 副本，不静默覆盖
    from src.export.md_export import _safe_write_bytes

    fd, tmp = tempfile.mkstemp(prefix="ng-anki-", suffix=".apkg")
    os.close(fd)
    try:
        genanki.Package(deck).write_to_file(tmp)
        return _safe_write_bytes(out_dir, f"{store.doc_id}{suffix}.apkg", Path(tmp).read_bytes())
    finally:
        Path(tmp).unlink(missing_ok=True)


def _export_csv(store: DocStore, cards: list[dict], out_dir: Path, suffix: str,
                encoding: str = "utf-8") -> Path:
    """genanki 不可用时的回退：可导入 CSV（front,back,tags）。

    同样走导出守护（此前直接 open("w") 覆盖，手改过的 CSV 会被静默盖掉）。"""
    from src.export.md_export import _safe_write

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["front", "back", "tags", "source"])
    for card in cards:
        writer.writerow([card["front"], card["back_text"],
                         ";".join(card["tags"]),
                         "、".join(f"p.{p}" for p in card["pages"])])
    return _safe_write(out_dir, f"{store.doc_id}{suffix}-anki.csv", buf.getvalue(),
                       encoding=encoding)
