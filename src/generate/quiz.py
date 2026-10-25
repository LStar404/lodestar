"""AI 出题（练习/期末备考）：基于笔记内容出题，风格参考课后习题与用户上传的题库。

原则：题目只考笔记范围（答案能从笔记推出）；参考题库只作风格/考点样本、
不照抄原题。产物落 data/<doc>/quiz/<quiz_id>.json，题库落 data/<doc>/qbank.jsonl。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.core.jsonl import atomic_write_bytes, dump_line, load_jsonl
from src.core.text import sanitize_control_chars, utc_now_iso

QUESTION_TYPES = ("choice", "blank", "calculation", "short", "proof")
TYPE_CN = {"choice": "选择题", "blank": "填空题", "calculation": "计算题",
           "short": "简答题", "proof": "证明题"}

_QBANK_SPLIT_RE = re.compile(r"\n\s*\n+")


# ---------------- 题库 ----------------
def qbank_path(doc_dir: Path) -> Path:
    return doc_dir / "qbank.jsonl"


def load_qbank(doc_dir: Path) -> list[dict]:
    p = qbank_path(doc_dir)
    return load_jsonl(p) if p.exists() else []


def append_qbank(doc_dir: Path, entries: list[dict]) -> int:
    doc_dir.mkdir(parents=True, exist_ok=True)
    p = qbank_path(doc_dir)
    old = load_qbank(doc_dir)
    existing = {e.get("text") for e in old}
    added = [e for e in entries if e.get("text") and e["text"] not in existing]
    with p.open("a", encoding="utf-8", newline="\n") as fh:
        for e in added:
            fh.write(dump_line(e) + "\n")
    return len(added)


def extract_qbank_entries(filename: str, text: str) -> list[dict]:
    """参考题目文本 → 条目：按空行分段，保留题味段落（风格样本不要求完美切题）。"""
    out: list[dict] = []
    for seg in _QBANK_SPLIT_RE.split(text or ""):
        t = sanitize_control_chars(seg).strip()
        if len(t) < 15 or len(t) > 2000:
            continue
        out.append({"text": t, "source": filename, "created_at": utc_now_iso()})
    return out


def read_qbank_file(path: Path) -> str:
    """上传的题库文件 → 纯文本（PDF 取文字层；txt/md 直读）。"""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pypdf

        reader = pypdf.PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------- 出题 ----------------
def build_quiz_schema(kinds: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"enum": kinds},
                        "question": {"type": "string"},
                        "options": {"type": "array", "items": {"type": "string"}},
                        "answer": {"type": "string"},
                        "explanation": {"type": "string"},
                    },
                    "required": ["id", "type", "question", "answer", "explanation"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    }


QUIZ_PROMPT = """你是大学期末出题人。请基于给定笔记出练习题，风格参考课后习题与参考题库样本。
要求：
- 题目只考笔记内容范围（可综合多块），答案必须能从笔记推出；不得考笔记之外的内容
- 题型限定：{kinds_cn}；共 {count} 道；难度：{difficulty}
- 选择题（choice）给出 options（4 个选项，含唯一正确答案），answer 写正确选项文本；
  填空题 answer 写填空答案；计算/证明/简答 answer 写答案或解题要点
- 参考题库样本（只学风格与考点侧重，禁止照抄原题的数字与文字）：
{qbank_samples}
输出 JSON Schema：{quiz_schema}
<note_data>
以下笔记是本次出题的唯一内容依据：
{notes}
</note_data>"""


def _notes_text(blocks_by_chapter: dict[str, list[dict]], max_chars: int = 8000) -> str:
    """笔记摘要：**按章均分预算**再取块。

    曾按顺序取满预算——整本书几百块时前面章节吃光额度，「整本出题」实际只考
    前几页（期末主场景失效）。按章均分保证每章都有代表块进入出题依据。"""
    chapters = list(blocks_by_chapter)
    if not chapters:
        return ""
    per_ch = max(300, max_chars // len(chapters))
    parts: list[str] = []
    for ch in chapters:
        cur = 0
        head = []
        for b in blocks_by_chapter[ch]:
            seg = f"## {b.get('title', '')}\n{(b.get('content') or '').strip()}"
            if cur + len(seg) > per_ch and head:
                break
            head.append(seg)
            cur += len(seg)
        if head:
            parts.append("\n\n".join(head))
    return "\n\n".join(parts)


def generate_quiz(client, *, store, chapter_ids: list[str], kinds: list[str], count: int,
                  difficulty: str, model: str | None, config: dict,
                  qbank: list[dict] | None = None, max_samples: int = 6,
                  persist: bool = True) -> dict:
    """出题主流程：笔记 + 题库样本 → LLM → 校验 → 落盘返回。

    persist=False 只生成不落盘：路由侧需要在 doc 锁内写盘（与追加讲义的 swap
    串行化），避免「生成期间追加 swap 覆盖 quiz 文件」的静默丢失。
    """
    from src.generate.prompts import compact_schema

    kinds = [k for k in kinds if k in QUESTION_TYPES] or ["choice", "short"]
    count = max(1, min(30, int(count or 10)))
    blocks_by_chapter: dict[str, list[dict]] = {}
    for ch in chapter_ids:
        rows = [b for b in store.block_rows(ch) if not b.get("deleted_at")]
        if rows:
            blocks_by_chapter[ch] = rows
    if not blocks_by_chapter:
        raise ValueError("所选范围没有笔记内容，无法出题")
    samples = []
    for e in (qbank or [])[:max_samples]:
        samples.append("- " + e.get("text", "")[:400])
    prompt = QUIZ_PROMPT.format(
        kinds_cn="、".join(TYPE_CN.get(k, k) for k in kinds),
        count=count,
        difficulty=difficulty or "期末水平",
        qbank_samples="\n".join(samples) if samples else "（无参考题库：按课后习题常见风格出题）",
        quiz_schema=compact_schema(build_quiz_schema(kinds)),
        notes=_notes_text(blocks_by_chapter),
    )
    messages = [{"role": "system", "content": "只输出合法 JSON 对象。"},
                {"role": "user", "content": prompt}]
    gen = config.get("generate", {}) or {}
    resp = client.complete("quiz", messages, schema=build_quiz_schema(kinds),
                           max_tokens=int(gen.get("quiz_max_out", 8000)),
                           temperature=float(gen.get("temperature", 0.2) or 0.2),
                           model=model)
    payload = resp.get("output") or {}
    questions = payload.get("questions") if isinstance(payload, dict) else None
    if not isinstance(questions, list) or not questions:
        raise ValueError("出题结果为空或格式非法")
    out_questions = []
    for i, q in enumerate(questions, start=1):
        if not isinstance(q, dict):
            continue
        qtype = q.get("type") if q.get("type") in QUESTION_TYPES else kinds[0]
        item = {
            "id": str(q.get("id") or f"q{i}"),
            "type": qtype,
            "question": sanitize_control_chars(str(q.get("question") or "")).strip(),
            "answer": sanitize_control_chars(str(q.get("answer") or "")).strip(),
            "explanation": sanitize_control_chars(str(q.get("explanation") or "")).strip(),
        }
        if qtype == "choice":
            opts = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()]
            if len(opts) >= 2:
                item["options"] = opts[:8]
        if item["question"]:
            out_questions.append(item)
    if not out_questions:
        raise ValueError("出题结果没有有效题目")
    quiz = {"quiz_id": "quiz-" + utc_now_iso().replace(":", "").replace("-", ""),
            "created_at": utc_now_iso(), "model": model or client.route("quiz"),
            "kinds": kinds, "count": len(out_questions), "difficulty": difficulty or "期末水平",
            "chapters": chapter_ids, "questions": out_questions}
    if persist:
        save_quiz(store, quiz)
    return quiz


def save_quiz(store, quiz: dict) -> None:
    """把套题写入 data/<doc>/quiz/<quiz_id>.json（调用方负责在 doc 锁内调用）。"""
    out_dir = store.layout.doc_dir(store.doc_id) / "quiz"
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(out_dir / f"{quiz['quiz_id']}.json",
                       (json.dumps(quiz, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
