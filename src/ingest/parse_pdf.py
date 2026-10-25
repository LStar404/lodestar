"""阶段 1 Ingest：PDF → 按页结构化版面块（5.1）。

适配器必须明确源单位/原点/旋转与缩放，页 JSON 记录可逆 `coordinate_meta.source_to_pdf`；
bbox 一律换算到 4.3 契约（未旋转的原始 PDF 用户空间）。禁止猜 scale/rotation。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Callable

from src.core.coord import transform_rect, validate_bbox
from src.core.errors import InputError, PipelineError
from src.core.jsonl import atomic_write_bytes, canonical_json, sha256_file
from src.core.models import Manifest
from src.core.paths import Layout, rel_to_root
from src.core.text import doc_namespace, utc_now_iso

from ..core.text import repair_surrogates
from .chapter_detect import detect_chapters
from .figure_crop import crop_figure
from .formula_ocr import FormulaOCR
from .mathtype_decode import decode_mt2_text


def _decode_font_text(font_dict, text: str) -> str:
    """按绘制字体解码文本（MathType MT2 字形名错位恢复，4.3 附录）。

    pypdf 对 >BMP 字符产出 UTF-16 代理对，进 JSON 写盘必崩——一并修复。
    """
    base = ""
    if isinstance(font_dict, dict):
        base = str(font_dict.get("/BaseFont") or font_dict.get("BaseFont") or "")
    return repair_surrogates(decode_mt2_text(base.lstrip("/"), text))

PAGE_JSON_VERSION = "1"


def _pdf_page_meta(page) -> dict:
    """manifest.pages 的几何元数据直接取自 PDF（4.3）：view_box/user_unit/rotation。"""

    def box(b):
        return [float(b.left), float(b.bottom), float(b.right), float(b.top)]

    view = box(page.cropbox) if page.cropbox is not None else box(page.mediabox)
    return {
        "view_box": view,
        "user_unit": float(page.get("/UserUnit", 1.0) or 1.0),
        "rotation": int(page.rotation or 0) % 360,
    }


class ParserAdapter:
    """解析器适配器基类：产出页内版面块（源空间）与坐标换算矩阵。"""

    name = "base"
    version = "0"

    def parse_page(self, reader, page_index: int) -> dict:
        raise NotImplementedError


def _pypdf_extract_range(args: tuple) -> list[dict]:
    """进程池 worker：独立打开 PDF 抽取 [start, end) 页。

    pypdf 是纯 Python、GIL 绑定（实测抽取占整解析 86%：297 页 38.8s/45.2s），
    多线程无益，多进程按页区间分摊；parse_page 每页自包含，输出与串行逐字节一致。"""
    pdf_path, start, end = args
    import pypdf

    reader = pypdf.PdfReader(pdf_path)
    adapter = PypdfAdapter()
    return [adapter.parse_page(reader, i) for i in range(start, end)]


def _extract_pages_parallel(adapter: "ParserAdapter", pdf_path, reader, n_pages: int,
                            progress: Callable[[dict], None] | None = None) -> list[dict]:
    """非 bulk 适配器的逐页抽取：页数多时多进程分摊，小文档保持串行（起进程不值）。

    并行只是执行顺序变化，页面结果按页号归位输出；并行路径任何异常退回串行重算，
    不让并行性成为新的故障面。"""
    import os

    workers = max(1, min(8, (os.cpu_count() or 2) - 1))
    if not isinstance(adapter, PypdfAdapter) or n_pages < 24 or workers < 2:
        return [adapter.parse_page(reader, i) for i in range(n_pages)]
    from concurrent.futures import ProcessPoolExecutor, as_completed

    chunk = max(4, (n_pages + workers * 4 - 1) // (workers * 4))
    ranges = [(str(pdf_path), s, min(n_pages, s + chunk)) for s in range(0, n_pages, chunk)]
    out: dict[int, dict] = {}
    try:
        with ProcessPoolExecutor(max_workers=min(workers, len(ranges))) as pool:
            futures = {pool.submit(_pypdf_extract_range, r): r for r in ranges}
            for fut in as_completed(futures):
                start = futures[fut][1]
                for j, res in enumerate(fut.result()):
                    out[start + j] = res
                if progress:
                    progress({"stage": "ingest", "pages_done": len(out),
                              "pages_total": n_pages, "parser": adapter.name})
    except Exception:
        return [adapter.parse_page(reader, i) for i in range(n_pages)]
    return [out[i] for i in range(n_pages)]


# ---------------------------------------------------------------------------
# 内置 pypdf 适配器（离线兜底，ADR D27）
# ---------------------------------------------------------------------------
class PypdfAdapter(ParserAdapter):
    name = "pypdf"

    def __init__(self):
        import pypdf

        self.version = getattr(pypdf, "__version__", "0")
        self._pypdf = pypdf

    def page_meta(self, page) -> dict:
        def box(b):
            return [float(b.left), float(b.bottom), float(b.right), float(b.top)]
        view = box(page.cropbox) if page.cropbox is not None else box(page.mediabox)
        user_unit = float(page.get("/UserUnit", 1.0) or 1.0)
        rotation = int(page.rotation or 0) % 360
        return {"view_box": view, "user_unit": user_unit, "rotation": rotation}

    def parse_page(self, reader, page_index: int) -> dict:
        page = reader.pages[page_index]
        meta = self.page_meta(page)
        spans: list[dict] = []
        state = {"tm": (1.0, 0.0, 0.0, 1.0, 0.0, 0.0), "tl": 0.0}
        nonlocal_clamped = [0]

        def mat_mul(m1, m2):
            a1, b1, c1, d1, e1, f1 = m1
            a2, b2, c2, d2, e2, f2 = m2
            return (a1 * a2 + c1 * b2, b1 * a2 + d1 * b2,
                    a1 * c2 + c1 * d2, b1 * c2 + d1 * d2,
                    a1 * e2 + c1 * f2 + e1, b1 * e2 + d1 * f2 + f1)

        def operand_after(op, operands, cm, tm):
            """自行跟踪文本矩阵：pypdf 的 visitor_text 回传 tm 时有滞后（Td/Tm 之后才更新）。"""
            name = op.decode("latin-1") if isinstance(op, (bytes, bytearray)) else str(op)
            try:
                if name == "BT":
                    state["tm"] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)   # 文本对象开始即重置
                    state["tl"] = 0.0
                elif name == "Tm" and len(operands) == 6:
                    state["tm"] = tuple(float(x) for x in operands)
                elif name == "Td" and len(operands) == 2:
                    state["tm"] = mat_mul(state["tm"], (1.0, 0.0, 0.0, 1.0,
                                                        float(operands[0]), float(operands[1])))
                elif name == "TD" and len(operands) == 2:
                    state["tl"] = -float(operands[1])
                    state["tm"] = mat_mul(state["tm"], (1.0, 0.0, 0.0, 1.0,
                                                        float(operands[0]), float(operands[1])))
                elif name == "TL" and len(operands) == 1:
                    state["tl"] = float(operands[0])
                elif name == "T*":
                    state["tm"] = mat_mul(state["tm"], (1.0, 0.0, 0.0, 1.0, 0.0, -state["tl"]))
            except Exception:
                pass

        def text_width(s: str, size: float) -> float:
            """估算行宽：CJK 约 1em，其它约 0.5em（pypdf 不给出字形宽度表）。"""
            total = 0.0
            for ch in s:
                wide = ("\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f"
                        or "\uff00" <= ch <= "\uffef")
                total += size * (1.0 if wide else 0.5)
            return max(size * 0.5, total)

        def visitor(text, cm, tm, font_dict, font_size):
            if not text or not text.strip():
                return
            text = _decode_font_text(font_dict, text)
            a1, b1, c1, d1, e1, f1 = cm
            a2, b2, c2, d2, e2, f2 = state["tm"]
            x = a1 * e2 + c1 * f2 + e1
            y = b1 * e2 + d1 * f2 + f1
            size = float(font_size or 10.0)
            vb = meta["view_box"]
            x0, x1 = x, x + text_width(text, size)
            y0, y1 = y - size * 0.25, y + size
            clamped = False
            if x1 > vb[2]:
                x1, clamped = vb[2], True
            if x0 < vb[0]:
                x0, clamped = vb[0], True
            if y1 > vb[3]:
                y1, clamped = vb[3], True
            if y0 < vb[1]:
                y0, clamped = vb[1], True
            if x1 - x0 <= 0 or y1 - y0 <= 0:
                return                      # 起点越界：丢弃该片段
            if clamped:
                nonlocal_clamped[0] += 1    # 记录夹取次数，进解析报告
            spans.append({
                "text": text,
                "bbox": [x0, y0, x1, y1],
                "font_size": size,
            })

        try:
            page.extract_text(visitor_text=visitor, visitor_operand_after=operand_after)
        except Exception as exc:
            return {"status": "failed", "error": str(exc), "blocks": [], "meta": meta}

        blocks = _spans_to_blocks(spans)
        if not blocks:
            return {"status": "blank", "blocks": [], "meta": meta}
        result = {"status": "ok", "blocks": blocks, "meta": meta,
                  "source_space": "pdf_user_space", "source_to_pdf": [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]}
        if nonlocal_clamped[0]:
            result["warnings"] = [f"{nonlocal_clamped[0]} 个文本片段的估算边界被夹到页面范围"]
        return result


def _spans_to_blocks(spans: list[dict]) -> list[dict]:
    """行/段落合并 + 标题启发式（同页同栏、阅读序连续才合并，合并上限 800 字符）。"""
    if not spans:
        return []
    sizes = sorted(s["font_size"] for s in spans)
    median = sizes[len(sizes) // 2] or 10.0
    lines: list[list[dict]] = [[spans[0]]]
    for s in spans[1:]:
        prev = lines[-1][-1]
        gap = prev["bbox"][1] - s["bbox"][3]          # 上一行底 - 本行顶
        same_line = abs((prev["bbox"][1] + prev["bbox"][3]) / 2 - (s["bbox"][1] + s["bbox"][3]) / 2) < max(2.0, prev["font_size"] * 0.4)
        if same_line:
            lines[-1].append(s)
        else:
            lines.append([s])
        _ = gap

    # 适配器只做行级版面块；段落合并是 anchor 阶段的职责（5.2 切分规则）
    blocks: list[dict] = []
    for line in lines:
        text = "".join(sp["text"] for sp in line).strip()
        if not text:
            continue
        bbox = [min(sp["bbox"][0] for sp in line), min(sp["bbox"][1] for sp in line),
                max(sp["bbox"][2] for sp in line), max(sp["bbox"][3] for sp in line)]
        max_size = max(sp["font_size"] for sp in line)
        if len(text) <= 60 and max_size >= median * 1.25:
            blocks.append({"type": "heading", "level": 2, "text": text, "bbox": bbox})
        else:
            blocks.append({"type": "paragraph", "text": text, "bbox": bbox})
    return blocks


# ---------------------------------------------------------------------------
# Marker / Docling 适配器（默认候选，见 5.1 选型表）：经 parser_workers 子进程运行重型依赖
# ---------------------------------------------------------------------------
_WORKER_PY_CACHE: dict[str, str] = {}


def _worker_can_write(python: str) -> bool:
    """健康探针：该解释器能否写父进程创建的临时文件。

    实测教训：.venv-parsers 的 python.exe 曾被低完整性标签污染（受限环境创建 venv
    传染），低完整性进程禁止向上写中完整性对象——worker 全体落盘 PermissionError，
    连 tempfile.gettempdir() 都回退到 CWD。一次探针（~0.2s，按解释器缓存）换掉
    一串莫名其妙的解析失败。"""
    import subprocess
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "probe.json"
            code = ("import pathlib,sys;"
                    "pathlib.Path(sys.argv[1]).write_text('ok', encoding='utf-8')")
            proc = subprocess.run([python, "-c", code, str(target)],
                                  capture_output=True, timeout=60)
            return proc.returncode == 0 and target.exists()
    except Exception:
        return False


def resolve_worker_python(explicit: str | None = None) -> str:
    """worker 解释器：显式配置 > .venv-parsers > 当前解释器（ADR D31）。

    .venv-parsers 候选必须通过写盘探针，否则退回当前解释器：pymupdf 等主环境可用
    （图资源刷新照常工作），marker/docling 缺依赖时报清晰的「不可用」而不是
    低完整性写盘的 PermissionError。"""
    if explicit:
        return explicit
    root = Path(__file__).resolve().parents[2]
    for cand in (root / ".venv-parsers" / "Scripts" / "python.exe",
                 root / ".venv-parsers" / "bin" / "python"):
        if cand.exists():
            key = str(cand)
            if key not in _WORKER_PY_CACHE:
                _WORKER_PY_CACHE[key] = key if _worker_can_write(key) else sys.executable
            return _WORKER_PY_CACHE[key]
    return sys.executable


def run_worker(mode: str, payload: dict, python: str | None = None) -> dict:
    """调用 parser_workers 子进程：结果走独立文件，stdout 只作诊断。"""
    import subprocess
    import tempfile

    worker = Path(__file__).resolve().parent / "parser_workers.py"
    with tempfile.TemporaryDirectory() as td:
        out_file = Path(td) / "worker_result.json"
        req = dict(payload)
        req["output_path"] = str(out_file)
        proc = subprocess.run(
            [resolve_worker_python(python), str(worker), mode],
            input=json.dumps(req, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            cwd=str(Path(__file__).resolve().parents[2]),
            timeout=3600,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        if out_file.exists():
            return json.loads(out_file.read_text(encoding="utf-8"))
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")[-800:]
            raise PipelineError(f"worker[{mode}] 退出码 {proc.returncode}: {tail}")
        return _decode_worker_stdout(mode, proc.stdout, proc.stderr)


def _decode_worker_stdout(mode: str, stdout: bytes, stderr: bytes | None = None) -> dict:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = stdout.decode(enc)
        except Exception:
            continue
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                continue
    tail = (stderr or stdout or b"").decode("utf-8", "replace")[-800:]
    raise PipelineError(f"worker[{mode}] 输出非法 JSON: {tail}")


def _bulk_has_text(bulk: dict | None) -> bool:
    """整文档转换结果里是否真有文字块（「ok=True + 全空页」不算成功）。"""
    for pg in (bulk or {}).get("pages") or []:
        if pg.get("blocks"):
            return True
    return False


def ocr_pages_via_worker(pdf_path: str | Path, pages: list[int], pages_meta: list[dict],
                         *, python: str | None = None, dpi: int = 150,
                         render_dir: str | None = None) -> dict | None:
    """纯图片页 OCR（尽力而为）：失败返回 None 并由调用方记警告。

    render_dir：worker 顺手存每页 PNG（视觉补 OCR 的输入）。"""
    try:
        return run_worker("ocr_pages", {"pdf_path": str(Path(pdf_path).resolve()),
                                        "pages": pages, "pages_meta": pages_meta,
                                        "dpi": dpi, "render_dir": render_dir}, python=python)
    except Exception:
        return None


_WORKER_PROBE_CACHE: dict[tuple, dict] = {}


class WorkerAdapter(ParserAdapter):
    """在独立解释器（默认 .venv-parsers）中运行的解析器适配器（ADR D31）。"""

    def __init__(self, name: str, python: str | None = None):
        self.name = name
        self.python = python
        # 只探自己的模块并按 (解释器, 模块) 缓存：曾每构造一次就起子进程全量探测
        # （含 torch import，实测 8.6s/次）；版本随后由解析响应带回
        key = (str(python or ""), name)
        info = _WORKER_PROBE_CACHE.get(key)
        if info is None:
            info = self._call("probe", {"only": [name]})
            _WORKER_PROBE_CACHE[key] = info
        modules = info.get("modules", {}) if isinstance(info, dict) else {}
        if not modules.get(self.name):
            raise InputError(
                f"解析器 {self.name} 不可用：请安装（python -m pip install {self.name}-pdf 等）"
                f"或使用 --parser pypdf",
                detail={"probe": modules})
        self.version = str(modules.get(self.name))

    def _call(self, mode: str, payload: dict) -> dict:
        return run_worker(mode, payload, python=self.python)

    def parse_document(self, pdf_path: str | Path, pages_meta: list[dict] | None = None) -> dict:
        """整书解析：返回 {pages:[...], coordinate:{...}, warnings:[...]}。"""
        out = self._call(self.name, {"pdf_path": str(Path(pdf_path).resolve()),
                                    "pages_meta": pages_meta or []})
        if not out.get("ok"):
            raise PipelineError(f"{self.name} 解析失败: {out.get('error')}")
        if out.get("version"):
            self.version = str(out["version"])
        return out

    def parse_page(self, reader, page_index: int) -> dict:  # pragma: no cover - 整书路径覆盖
        raise PipelineError(f"{self.name} 走整书解析路径")


class MarkerAdapter(WorkerAdapter):
    def __init__(self, python: str | None = None):
        super().__init__("marker", python=python)


class DoclingAdapter(WorkerAdapter):
    def __init__(self, python: str | None = None):
        super().__init__("docling", python=python)


class PyMupdfAdapter(WorkerAdapter):
    """PyMuPDF（fitz）：无 ToUnicode 的中文 PDF（预定义 CMap）抽取保真，
    速度远快于 marker/docling——pypdf 对这类书抽出乱码时的首选。"""

    def __init__(self, python: str | None = None):
        super().__init__("pymupdf", python=python)

    def parse_document(self, pdf_path: str | Path, pages_meta: list[dict] | None = None) -> dict:
        """整书解析，长书分片并行：每个 worker 子进程要付启动 + fitz import 固定开销，
        分片把抽取/序列化也按页分摊（实测 300 页整书单进程 ~11s → 分片 ~6s）。
        marker/docling 重型解析器不走此路（分片会成倍烧 CPU/GPU）。"""
        import os

        pages_meta = pages_meta or []
        shards_n = max(1, min(3, (os.cpu_count() or 2) - 1))
        if len(pages_meta) < 120 or shards_n < 2:
            return super().parse_document(pdf_path, pages_meta=pages_meta)
        from concurrent.futures import ThreadPoolExecutor

        chunk = (len(pages_meta) + shards_n - 1) // shards_n
        shards = [(s, pages_meta[s:s + chunk]) for s in range(0, len(pages_meta), chunk)]
        resolved = str(Path(pdf_path).resolve())

        def _run(shard: tuple[int, list[dict]]) -> dict:
            return self._call(self.name, {"pdf_path": resolved, "pages_meta": shard[1],
                                          "page_start": shard[0]})

        with ThreadPoolExecutor(max_workers=len(shards)) as pool:
            outs = list(pool.map(_run, shards))
        merged: dict = {"ok": True, "pages": [], "warnings": [], "coordinate": {}}
        for out in outs:
            if not out.get("ok"):
                raise PipelineError(f"{self.name} 解析失败: {out.get('error')}")
            merged["pages"].extend(out.get("pages") or [])
            merged["warnings"].extend(out.get("warnings") or [])
            merged["coordinate"] = merged["coordinate"] or (out.get("coordinate") or {})
            if out.get("version"):
                merged["version"] = out["version"]
        merged["pages"].sort(key=lambda p: p.get("page", 0))
        return merged


def get_adapter(name: str, *, python: str | None = None) -> ParserAdapter:
    name = (name or "auto").lower()
    if name == "marker":
        return MarkerAdapter(python=python)
    if name == "docling":
        return DoclingAdapter(python=python)
    if name in ("pymupdf", "mupdf"):
        return PyMupdfAdapter(python=python)
    if name == "pypdf":
        return PypdfAdapter()
    if name == "auto":
        for factory in (MarkerAdapter, DoclingAdapter):
            try:
                return factory(python=python)
            except (InputError, PipelineError):
                continue
        return PypdfAdapter()
    raise InputError(f"未知解析器: {name}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _has_text_layer(reader, sample: int = 3, min_chars: int = 80) -> bool:
    """数字版 PDF 快速探测：抽样首/中/末几页，平均字符数达标即认为有可用文字层。

    auto 分流的关键（NFR-4 能力驱动）：有文字层的教材用 pypdf 秒级完成，
    只有扫描书才值得上 marker/docling（百页数十分钟到小时级）。
    """
    n = len(reader.pages)
    idxs = list(dict.fromkeys([0, n // 2, n - 1]))[:sample]
    total = 0
    for i in idxs:
        try:
            total += len((reader.pages[i].extract_text() or "").strip())
        except Exception:
            continue
    return total / max(1, len(idxs)) >= min_chars


# 逐字母空格化伪影（"T h e o r e m"）：pypdf 对某些字体 run 逐字符插空格，
# 字符类别全是 ASCII，必须单独识别——否则锚点/引文满是碎字
_GARBAGE_RUN = re.compile(r"(?:(?<=\s)|^)(?:[A-Za-z] ){3,}")


def _spacing_garbage_runs(text: str) -> int:
    return len(_GARBAGE_RUN.findall(text or ""))


def _text_quality_ok(reader, sample: int = 9, min_ratio: float = 0.85,
                     max_garbage_runs: int = 2) -> bool:
    """抽取质量探测：常规字符占比达标 且 无逐字母空格化伪影 才算可用。

    中文 PDF 缺 ToUnicode 时 pypdf 会把汉字抽成孟加拉/格鲁吉亚等乱码块
    （实测 FandolSong 字体的 LaTeX 书）；英文教材则常见逐字母空格化
    （实测 Durrett《Probability: Theory and Examples》13/432 页）——
    这两类都必须换保真抽取（pymupdf），否则锚点满是伪影。
    """
    n = len(reader.pages)
    idxs = [i for i in (0, n // 8, n // 4, n * 3 // 8, n // 2, n * 5 // 8,
                        n * 3 // 4, n * 7 // 8, n - 1) if 0 <= i < n]
    idxs = list(dict.fromkeys(idxs))[:sample]
    total = good = garbage = 0
    for i in idxs:
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:
            continue
        garbage += _spacing_garbage_runs(text)
        for ch in text:
            o = ord(ch)
            total += 1
            if (ch.isascii()
                    or 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF   # CJK
                    or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF   # 中文标点/全角
                    or 0x370 <= o <= 0x3FF                               # 希腊
                    or 0x2000 <= o <= 0x206F                             # 常用标点
                    or 0x2070 <= o <= 0x209F or 0x2200 <= o <= 0x22FF):  # 上下标/数学
                good += 1
    return (total > 0 and good / total >= min_ratio
            and garbage <= max_garbage_runs)


def parse_pdf(pdf_path: str | Path, out_dir: str | Path, parser: str = "auto", *,
              layout: Layout | None = None, doc_id: str | None = None,
              profile: str = "textbook", title: str | None = None,
              chapters_override: list | None = None, ocr: bool = False,
              config: dict | None = None,
              progress: Callable[[dict], None] | None = None,
              vision_ocr: Callable[[str], list[dict]] | None = None) -> dict:
    """解析 PDF → pages/*.json + manifest（5.1 MUST 顺序执行）。返回 manifest dict。"""
    pdf_path = Path(pdf_path).resolve()
    if not pdf_path.exists():
        raise InputError(f"PDF 不存在: {pdf_path}")
    out_dir = Path(out_dir)
    layout = layout or Layout(out_dir.parent if out_dir.name == "ingest_out" else out_dir)
    import pypdf

    reader = pypdf.PdfReader(str(pdf_path))
    n_pages = len(reader.pages)
    if n_pages == 0:
        raise InputError("PDF 没有任何页面")

    parser_python = (config or {}).get("parser", {}).get("python")
    chosen = parser
    if parser == "auto" and _has_text_layer(reader):
        if _text_quality_ok(reader):
            # 快速路径：数字版 PDF 无需重型解析器（实测 285 页教材 marker 数小时 vs pypdf 秒级）
            chosen = "pypdf"
            print("[ingest] auto → pypdf：文字层质量良好，走快速路径", file=sys.stderr)
        else:
            # 有文字层但 pypdf 抽出乱码（中文 PDF 缺 ToUnicode，实测 FandolSong 字体）
            # → PyMuPDF 保真抽取；不可用再退回重型链
            chosen = "pymupdf"
            print("[ingest] auto → pymupdf：文字层抽取质量差（疑似缺 ToUnicode），换保真抽取",
                  file=sys.stderr)
    # 转换级回退链（5.1 能力驱动降级）：构造成功 ≠ 转换成功——marker 装着但要 Docker，
    # 转换期才爆（实测图片 PDF 直接解析失败、OCR 兜底没机会跑）。auto 按链尝试
    # 「构造 + 整书转换」，链尾 pymupdf/pypdf 对扫描页出空块，由批量 OCR 兜底（5.1.1）
    # 补齐内容；显式指定解析器时不回退（用户点名即意图，失败要说清楚）。
    if parser == "auto":
        if chosen == "pypdf":
            chain = ["pypdf", "pymupdf"]
        elif chosen == "pymupdf":
            chain = ["pymupdf", "pypdf"]
        else:
            chain = ["marker", "docling", "pymupdf", "pypdf"]   # 无文字层的扫描书
    else:
        chain = [chosen]
    # 默认 doc_id 在命名空间检查前归一：CLI --doc-id 可选，doc_namespace(None)
    # 会在下面才生成默认 ID 之前就 AttributeError（pdf_path 已 resolve 且验存在）
    doc_id = doc_id or ("doc-" + sha256_file(pdf_path)[:12])
    # 5.2：doc_ns 碰撞必须拒绝导入，否则全局点查（anchor_id 前缀）产生歧义
    ns = doc_namespace(doc_id)
    for other in layout.data_dir.glob("*/manifest.json"):
        other_doc = other.parent.name
        if other_doc != doc_id and doc_namespace(other_doc) == ns:
            raise InputError(
                f"doc_id 命名空间冲突：{doc_id} 与已有文档 {other_doc} 的 doc_ns({ns}) 相同，"
                f"请换一个 doc_id（否则锚点全局点查会歧义）",
                code="DOC_NS_COLLISION")
    pages_meta_all = [_pdf_page_meta(reader.pages[i]) for i in range(n_pages)]
    adapter = None
    bulk = None
    # 「成功但零文本」的候选：不当作成功（继续试下一个转换器），全链都空时留作
    # OCR 兜底——纯图片书正是走这条路（实测 docling 对图片书返回 ok+全空块，
    # 静默导入成整本空页，用户侧表现为「生成失败/没有锚点」）
    empty_fallback: tuple | None = None
    chain_notes: list[str] = []
    for name in chain:
        try:
            ad = get_adapter(name, python=parser_python)
        except (InputError, PipelineError) as exc:
            chain_notes.append(f"{name} 不可用（{str(exc)[:80]}）")
            continue
        if not hasattr(ad, "parse_document"):
            adapter, bulk = ad, None
            break
        try:
            adapter = ad
            if progress:
                # 转换期单独报状态：重型解析器整书转换是导入最久的一段（页数多时十几分钟），
                # 没有页级信号——不报状态进度条就一直趴着，用户以为卡死
                progress({"stage": "ingest", "pages_done": 0, "pages_total": n_pages,
                          "parser": ad.name, "phase": "convert"})
            bulk = ad.parse_document(pdf_path, pages_meta=pages_meta_all)
            if not _bulk_has_text(bulk):
                chain_notes.append(f"{name} 未识别到文本（转下一个）")
                empty_fallback = empty_fallback or (adapter, bulk)
                adapter, bulk = None, None
                continue
            break
        except (InputError, PipelineError) as exc:
            chain_notes.append(f"{name} 解析失败（{str(exc)[:80]}）")
            adapter, bulk = None, None
            continue
    if adapter is None and empty_fallback is not None:
        # 全链都没有文本层：仍按兜底解析器继续，交给 OCR 环节（纯图片书的正常入口）
        adapter, bulk = empty_fallback
        chain_notes.append("全部转换器均无文本层：转 OCR 识别")
    if adapter is None:
        raise PipelineError("没有可用的解析器：" + ("；".join(chain_notes) or "未知原因"))
    for note in chain_notes:
        print(f"[ingest] 回退：{note}", file=sys.stderr)
    if progress:
        progress({"stage": "ingest", "pages_done": 0, "pages_total": n_pages,
                  "parser": adapter.name})
    # pypdf 路径逐页抽取并行化（纯 Python 抽取占整解析 86%，实测 297 页 38.8s/45.2s）；
    # bulk 适配器（marker/docling/pymupdf）本就整文档一次调用，不走此路
    page_results = (None if bulk is not None else
                    _extract_pages_parallel(adapter, pdf_path, reader, n_pages, progress=progress))
    if bulk:
        for warn in bulk.get("warnings", []):
            print(f"[ingest] {adapter.name}: {warn}", file=sys.stderr)
    pages_dir = out_dir / doc_id / "pages"
    if pages_dir.exists():
        shutil.rmtree(pages_dir)
    pages_dir.mkdir(parents=True, exist_ok=True)

    ocr_engine = FormulaOCR(python=parser_python) if ocr else None
    ocr_mode = ((config or {}).get("parser", {}) or {}).get("ocr_mode", "auto") or "auto"
    page_records: list[dict] = []
    pending: list[dict] = []
    failed = 0
    coord_skipped: list[str] = []
    render_dir: Path | None = None
    figure_seq = 0
    ocr_pages_used = 0

    for i in range(n_pages):
        if progress and (i % 5 == 0 or i == n_pages - 1):
            progress({"stage": "ingest", "pages_done": i, "pages_total": n_pages,
                      "parser": adapter.name})
        if bulk is not None:
            meta = _pdf_page_meta(reader.pages[i])
            page_data = (bulk.get("pages") or [])[i] if i < len(bulk.get("pages") or []) else {}
            coord = bulk.get("coordinate", {})
            result = {
                "status": page_data.get("status", "failed"),
                "blocks": page_data.get("blocks", []),
                "meta": meta,
                "source_space": coord.get("source_space", "pdf_user_space"),
                "source_to_pdf": coord.get("source_to_pdf", [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]),
                "coordinate_derivation": coord.get("derivation", ""),
                "error": page_data.get("error"),
            }
        else:
            result = (page_results[i] if page_results is not None
                      else adapter.parse_page(reader, i))
            # 并行 worker 已带回 meta：父进程不再逐页重解析页对象（省一份 read_object）
            meta = result.get("meta") or _pdf_page_meta(reader.pages[i])
            result["meta"] = meta
        view_box = meta["view_box"]
        blocks = result.get("blocks", [])
        # 纯图片/扫描页：无文字层时标记待 OCR（批量一次调用，见循环后）
        needs_ocr = ocr_mode != "off" and (ocr_mode == "force" or not blocks)
        source_to_pdf = result.get("source_to_pdf", [1.0, 0.0, 0.0, 1.0, 0.0, 0.0])
        source_space = result.get("source_space", "pdf_user_space")

        normalized: list[dict] = []
        for idx, blk in enumerate(blocks):
            bbox = transform_rect(blk.get("bbox", view_box), source_to_pdf) if source_space != "pdf_user_space" else list(blk.get("bbox", view_box))
            try:
                bbox = validate_bbox(bbox, view_box)
            except Exception as exc:
                # 单块坐标非法不得炸整本书（实测一个坏块毁掉 230/364 页的导入）：
                # 跳过该块并记录，页面与文档继续；页面级失败仍走 failed/5% 闸
                coord_skipped.append(f"第 {i + 1} 页块 {idx}: {exc}")
                continue
            item = {
                "type": blk.get("type", "paragraph"),
                "level": int(blk.get("level", 0) or 0),
                "text": blk.get("text", ""),
                "bbox": bbox,
            }
            if blk.get("confidence") is not None:
                item["confidence"] = float(blk["confidence"])
            if ocr_engine and item["type"] == "formula" and not item["text"].strip():
                latex = ocr_engine.recognize(pdf_path, i + 1, bbox)
                if latex:
                    item["text"] = latex
            if item["type"] in ("figure", "table") and blk.get("image_path") is None:
                figure_seq += 1
                fig_id = f"fig_{figure_seq:08d}"
                png = crop_figure(pdf_path, i + 1, bbox, layout.figures_dir(doc_id) / f"{fig_id}.png",
                                  caption=blk.get("caption"))
                item["image_path"] = rel_to_root(layout.root, png) if png else None
                item["figure_id"] = fig_id
            normalized.append(item)

        page_json = {
            "doc_id": doc_id,
            "page": i + 1,
            "page_width": float(view_box[2] - view_box[0]),
            "page_height": float(view_box[3] - view_box[1]),
            "coordinate_meta": {
                "source_space": source_space,
                "source_to_pdf": list(source_to_pdf),
                "view_box": list(view_box),
                "user_unit": float(meta.get("user_unit", 1.0)),
                "rotation": int(meta.get("rotation", 0)),
            },
            "parse_status": result.get("status", "ok"),
            "blocks": normalized,
        }
        pending.append({"index": i, "page_json": page_json, "needs_ocr": needs_ocr,
                        "meta": meta, "status": result.get("status", "ok")})
        if result.get("status") == "failed":
            failed += 1

    # 纯图片/扫描页 OCR（5.1.1）：批量一次调用，避免每页起一个 worker 进程
    if ocr_mode != "off" and any(p["needs_ocr"] for p in pending):
        targets = [p["index"] + 1 for p in pending if p["needs_ocr"]]
        if progress:
            # OCR 是整书第二大段时间（实测数百页 CPU 识别 ~25 分钟），无页级信号
            # 就会像卡死在最后一页——单独报阶段状态
            progress({"stage": "ingest", "phase": "ocr", "pages_done": 0,
                      "pages_total": len(targets), "parser": adapter.name})
        render_dir = out_dir / doc_id / "_ocr_png"
        ocr_out = ocr_pages_via_worker(
            pdf_path, targets,
            [_pdf_page_meta(reader.pages[i]) for i in range(n_pages)],
            python=parser_python, render_dir=str(render_dir))
        ocr_by_page = {}
        for pg in ((ocr_out or {}).get("pages") or []):
            ocr_by_page[pg.get("page")] = pg.get("blocks", [])
        vision_used = 0
        for p in pending:
            if not p["needs_ocr"]:
                continue
            ocr_blocks = ocr_by_page.get(p["index"] + 1) or []
            vb = p["meta"]["view_box"]
            # 视觉补 OCR（5.1.1 增强）：本地 rapidocr 认不好的页交给视觉模型重识别
            # （扫描书公式/复杂排版常见乱码；每页只花少量 token），失败保留本地结果
            if vision_ocr is not None:
                from src.ingest.vision_ocr import fracs_to_bbox, weak_ocr_text
                if weak_ocr_text(" ".join((b.get("text") or "") for b in ocr_blocks)):
                    png = render_dir / f"page_{p['index'] + 1:04d}.png"
                    if png.exists():
                        try:
                            fixed = []
                            for b in vision_ocr(str(png)):
                                frac = b.get("frac") or [0.05, 0.0, 0.95, 0.01]
                                fixed.append({"type": b.get("type", "paragraph"), "level": 0,
                                              "text": b.get("text", ""),
                                              "bbox": fracs_to_bbox(frac, vb)})
                            if fixed:
                                ocr_blocks = fixed
                                vision_used += 1
                                if progress:
                                    progress({"stage": "ingest", "phase": "ocr",
                                              "pages_done": vision_used,
                                              "pages_total": len(targets),
                                              "parser": "vision-ocr"})
                        except Exception as exc:
                            print(f"[ingest] 视觉补 OCR 失败（保留本地结果）：{exc}", file=sys.stderr)
            if not ocr_blocks:
                continue
            for blk in ocr_blocks:
                try:
                    bbox = validate_bbox(blk.get("bbox", vb), vb)
                except Exception:
                    continue
                item = {"type": blk.get("type", "paragraph"),
                        "level": int(blk.get("level", 0) or 0),
                        "text": blk.get("text", ""), "bbox": bbox}
                if blk.get("confidence") is not None:
                    item["confidence"] = float(blk["confidence"])
                p["page_json"]["blocks"].append(item)
            if p["page_json"]["blocks"]:
                p["page_json"]["parse_status"] = "ok"
                p["status"] = "ok"
                ocr_pages_used += 1

        if vision_used:
            print(f"[ingest] 视觉补 OCR 重识别 {vision_used} 页", file=sys.stderr)
    if render_dir is not None:
        shutil.rmtree(render_dir, ignore_errors=True)   # 渲染 PNG 只是视觉补 OCR 的中间产物

    for p in pending:
        page_json = p["page_json"]
        page_name = "%04d.json" % (p["index"] + 1)
        atomic_write_bytes(pages_dir / page_name,
                           (json.dumps(page_json, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                           fsync=False)   # 派生页 JSON 可重建：免逐文件刷盘（实测 2.3s/书）
        page_records.append({
            "page": p["index"] + 1,
            "view_box": list(page_json["coordinate_meta"]["view_box"]),
            "user_unit": float(p["meta"].get("user_unit", 1.0)),
            "rotation": int(p["meta"].get("rotation", 0)),
            "parse_status": p["status"],
        })

    if coord_skipped:
        print(f"[ingest] 警告：{len(coord_skipped)} 个块坐标非法已跳过（不炸整书）："
              + "；".join(coord_skipped[:5]), file=sys.stderr)
    if n_pages and failed / n_pages > 0.05:
        raise PipelineError(f"失败页数 {failed}/{n_pages} 超过 5%，请更换解析器")
    total_blocks = sum(len(p["page_json"]["blocks"]) for p in pending)
    if n_pages and total_blocks == 0:
        # 静默导入成整本空页是最糟的失败形态（任务显示「完成」、生成时才发现没锚点）：
        # 宁可响亮报错并给出可执行建议
        raise PipelineError(
            "整本未识别到任何文本（0 块）：可能是纯图片书且 OCR 未生效——"
            "请在「配置指南」确认 .venv-parsers 与 OCR（rapidocr）可用，"
            "或改用 marker/docling 解析器后重试")

    chapters = detect_chapters(reader, page_records, doc_id, pages_dir=pages_dir, profile=profile,
                               override=chapters_override or (config or {}).get("chapters_override"))
    now = utc_now_iso()
    manifest = {
        "schema_version": 2,
        "doc_id": doc_id,
        "profile": profile,
        "title": title or (pdf_path.stem),
        "pdf_path": rel_to_root(layout.root, pdf_path),
        "pdf_sha256": sha256_file(pdf_path),
        "parser": {"name": adapter.name, "version": adapter.version,
                   "config_hash": "sha256:" + hashlib.sha256(canonical_json({
                       "name": adapter.name, "version": adapter.version,
                       "profile": profile, "ocr": bool(ocr)}).encode("utf-8")).hexdigest()},
        "ocr": bool(ocr) or ocr_pages_used > 0,
        "data_revision": 1,
        "pages": page_records,
        "collision_map": {},
        "chapters": chapters,
        "stage_hashes": {
            "ingest": {
                "input_hash": "sha256:" + hashlib.sha256(
                    (sha256_file(pdf_path) + adapter.name + adapter.version + profile).encode("utf-8")).hexdigest(),
                "output_hash": "sha256:" + _pages_hash(pages_dir),
                "version": PAGE_JSON_VERSION,
            }
        },
        "created_at": now,
        "updated_at": now,
    }
    Manifest.model_validate(manifest)  # 写入前 schema 校验
    # 初版 manifest 直接落盘（此时尚无已提交状态）
    doc_dir = layout.doc_dir(doc_id)
    for sub in ("anchors", "blocks", "history", "figures", "refs"):
        (doc_dir / sub).mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(layout.manifest(doc_id),
                       (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return manifest


def parse_pdf_with_fallback(pdf_path: str | Path, out_dir: str | Path, parser: str = "auto",
                            *, fallback: str = "pypdf", **kwargs) -> dict:
    """解析失败自动降级（NFR-4）：记录告警并用兜底解析器重跑，manifest.parser 如实记录实际解析器。

    只回退"解析器失败"；InputError（参数/数据问题）必须直接报错即止，
    否则会用兜底解析器把真正的输入错误掩盖成二次失败。
    """
    try:
        return parse_pdf(pdf_path, out_dir, parser, **kwargs)
    except InputError:
        raise
    except Exception as exc:
        if parser in (fallback, "auto"):
            raise
        print(f"[ingest] {parser} 解析失败（{exc}），回退到 {fallback} 重试", file=sys.stderr)
        return parse_pdf(pdf_path, out_dir, fallback, **kwargs)


def _pages_hash(pages_dir: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(pages_dir.glob("*.json")):
        h.update(p.name.encode("utf-8"))
        h.update(p.read_bytes())
    return h.hexdigest()
