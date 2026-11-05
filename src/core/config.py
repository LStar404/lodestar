"""运行配置 config.yaml 加载（10.2：字段即规范）。"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml

from .errors import InputError

DEFAULTS: dict[str, Any] = {
    "app": {
        "host": "127.0.0.1",
        "port": 8787,
        "cors_origins": ["http://127.0.0.1:5173"],
    },
    "parser": {"default": "marker", "ocr_mode": "auto", "python": None,
               "ocr_vision": "auto"},  # 视觉补 OCR：弱识别页交视觉模型重识别（off 关闭）
    "chapters_override": [],
    "generate": {
        "models": {
            "default": None,
            "outline": None,
            "fill": None,
            "agent": None,
            "link": None,
            "ocr": None,   # 视觉补 OCR 用（需 vision 能力）；空则自动选 fill/default 里有视觉能力的
            "quiz": None,  # AI 出题用；空则用 fill/default
        },
        "quiz_max_out": 8000,   # 出题单次输出上限
        "outline_max_in": 32000,
        "fill_max_in": 8000,
        "outline_max_out": 32000,
        "fill_max_out": 12000,
        "concurrency": 2,
        "timeout": 600,          # LLM 单次调用超时秒数（推理型模型需更长）
        "reasoning_effort": None,  # 推理深度 low/medium/high（空=供应商默认；low 显著提速）
        "thinking": None,          # 思考开关 disabled/enabled（deepseek-flash 实测 disabled 提速 5 倍）
        "fill_batch": 1,           # 小块合批：N 块一次填充调用（1=逐块）
        "outline_batch_out": 8000, # 提纲分批目标（预计输出 token/批）
        "chapter_concurrency": 2,  # 整本生成并行章数
        "max_concurrent_requests": 8,  # 全局在途 LLM 请求上限
        "profile": "textbook",   # 笔记模板：textbook/paper/physics/cs/medicine/chemistry 或自定义
        "block_types": [],     # 用户自定义块类型（覆盖/追加预设）：[{id,name,icon,hint,source_policy,profiles}]
        "max_cost_per_chapter": 0.0,
        "temperature": 0.2,
        "output_language": "zh",      # zh | en | ja  生成笔记与导出标签语言
    },
    "export": {"encoding": "utf-8"},
    "embedding": {"provider": "none", "model_dir": None, "dim": 1024, "threshold": 0.55},
    "cache": {"dir": "cache/gen"},
    "match": {"contain_ratio": 0.8, "cover_ratio": 0.5, "top_k": 8},
    "history": {"keep_recent": 50},
}


ENCODINGS = ("utf-8", "utf-8-sig", "gbk", "big5")
OUTPUT_LANGUAGES = ("zh", "en", "ja")
REASONING_EFFORTS = ("low", "medium", "high")
THINKING_MODES = ("disabled", "enabled")


def validate_settings_patch(patch: dict) -> None:
    """Validate API edits before normalization, persistence or live changes."""
    for section in ("parser", "generate", "export", "match", "history", "embedding"):
        if section in patch and not isinstance(patch[section], dict):
            raise InputError(f"{section} 必须是对象")
    for section, field, choices in (
        ("parser", "default", ("auto", "marker", "docling", "pypdf", "pymupdf")),
        ("parser", "ocr_mode", ("auto", "force", "off")),
        ("generate", "output_language", OUTPUT_LANGUAGES),
        ("export", "encoding", ENCODINGS),
    ):
        values = patch.get(section, {})
        if field in values:
            value = values[field]
            if not isinstance(value, str) or value.lower() not in choices:
                raise InputError(f"不支持的 {section}.{field}: {value}")
    gen = patch.get("generate", {})
    for field in ("reasoning_effort", "thinking"):
        if gen.get(field) is not None and not isinstance(gen[field], str):
            raise InputError(f"generate.{field} 必须是字符串或 null")
    for field in ("concurrency", "chapter_concurrency", "max_concurrent_requests",
                  "outline_max_in", "fill_max_in", "outline_max_out", "fill_max_out",
                  "quiz_max_out", "outline_batch_out", "fill_batch"):
        if field in gen and (type(gen[field]) is not int or gen[field] < 1):
            raise InputError(f"generate.{field} 必须是正整数")
    for field in ("timeout", "temperature", "max_cost_per_chapter"):
        if field not in gen:
            continue
        value = gen[field]
        if (type(value) not in (int, float) or not math.isfinite(value)
                or value < 0 or (field == "timeout" and value == 0)):
            raise InputError(f"generate.{field} 必须是有效的"
                             + ("正数" if field == "timeout" else "非负数"))


def normalize_settings(cfg: dict) -> dict:
    # 笔记模板字段兜底：profile 非法名交由 block_types 兜底，block_types 必须是列表
    _gen = cfg.setdefault("generate", {})
    if not isinstance(_gen.get("block_types"), list):
        _gen["block_types"] = []
    if not isinstance(_gen.get("profile"), str) or not str(_gen.get("profile")).strip():
        _gen["profile"] = "textbook"
    """校验并归一化可选项（10.2 字段即规范）。"""
    enc = ((cfg.get("export") or {}).get("encoding") or "utf-8").lower()
    cfg.setdefault("export", {})["encoding"] = enc if enc in ENCODINGS else "utf-8"
    lang = ((cfg.get("generate") or {}).get("output_language") or "zh").lower()
    cfg.setdefault("generate", {})["output_language"] = lang if lang in OUTPUT_LANGUAGES else "zh"
    # 推理深度：仅接受 low/medium/high，空值=供应商默认（不限制思考长度）
    effort = ((cfg.get("generate") or {}).get("reasoning_effort") or "").lower() or None
    cfg["generate"]["reasoning_effort"] = effort if effort in REASONING_EFFORTS else None
    # 思考开关：拼错的值不得静默放行（否则悄悄回到昂贵的思考默认）
    think = ((cfg.get("generate") or {}).get("thinking") or "").lower() or None
    cfg["generate"]["thinking"] = think if think in THINKING_MODES else None
    return cfg


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(root: str | Path) -> dict:
    path = Path(root) / "config.yaml"
    data: dict = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} 顶层必须是映射")
        data = loaded
    cfg = _deep_merge(DEFAULTS, data)
    cfg = normalize_settings(cfg)
    cfg["_root"] = str(Path(root).resolve())
    return cfg


def save_config(root: str | Path, cfg: dict) -> None:
    """原子保存 config.yaml（模型路由更新用）；不含密钥。"""
    out = {k: v for k, v in cfg.items() if not k.startswith("_")}
    path = Path(root) / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    from .jsonl import atomic_write_bytes

    atomic_write_bytes(path, (yaml.safe_dump(out, allow_unicode=True, sort_keys=False)).encode("utf-8"))
