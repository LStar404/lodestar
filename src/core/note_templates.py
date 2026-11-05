"""Reusable note templates; applying one stores a snapshot, never rewrites notes."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import uuid
from pathlib import Path

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import InputError, NotFound, RevisionConflict
from .jsonl import atomic_write_bytes
from .store import DocStore
from .text import utc_now_iso
from .transactions import commit_doc, replace_change

PROFILES = {"textbook": "通用 / 数学", "paper": "论文", "physics": "物理",
            "cs": "计算机", "medicine": "医学", "chemistry": "化学"}
MAX_IMAGE_BYTES = 2 * 1024 * 1024


def _image_size(raw: bytes, mime: str) -> tuple[int, int]:
    if mime == "png" and raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 33:
        if raw[12:16] == b"IHDR":
            return struct.unpack(">II", raw[16:24])
    if mime == "jpeg" and raw.startswith(b"\xff\xd8"):
        i = 2
        while i + 4 <= len(raw):
            if raw[i] != 255:
                break
            while i < len(raw) and raw[i] == 255:
                i += 1
            if i >= len(raw):
                break
            marker = raw[i]
            i += 1
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if i + 2 > len(raw):
                break
            length = int.from_bytes(raw[i:i + 2], "big")
            if length < 2 or i + length > len(raw):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF) and length >= 7:
                return (int.from_bytes(raw[i + 5:i + 7], "big"),
                        int.from_bytes(raw[i + 3:i + 5], "big"))
            i += length
    raise ValueError("封面必须是有效的 PNG 或 JPEG 图片")


class Cover(BaseModel):
    model_config = ConfigDict(extra="forbid")
    style: str = "gradient"
    color: str = "#6265ad"
    subtitle: str = Field(default="整理知识，保留思考", max_length=200)
    image: str = Field(default="", max_length=2_800_000)

    @field_validator("style")
    @classmethod
    def style_valid(cls, value):
        if value not in ("solid", "gradient", "image", "none"):
            raise ValueError("不支持的封面样式")
        return value

    @field_validator("color")
    @classmethod
    def color_valid(cls, value):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("封面颜色必须是六位十六进制颜色")
        return value

    @field_validator("image")
    @classmethod
    def image_valid(cls, value):
        if not value:
            return value
        match = re.fullmatch(r"data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)", value)
        if not match:
            raise ValueError("仅支持本地 PNG / JPEG 封面")
        try:
            raw = base64.b64decode(match[2], validate=True)
        except ValueError:
            raise ValueError("封面编码不合法") from None
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("封面不能超过 2 MB")
        width, height = _image_size(raw, match[1])
        if not (1 <= width <= 6000 and 1 <= height <= 6000 and width * height <= 20_000_000):
            raise ValueError("封面长宽最多 6000 像素，总像素最多 2000 万")
        return value


class TemplateDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    note_type: str = Field(default="读书笔记", min_length=1, max_length=80)
    profile: str = "textbook"
    type_ids: list[str] = Field(default_factory=list, max_length=40)
    sections: list[str] = Field(default_factory=lambda: ["核心概念", "论证与例子", "我的理解", "待解决的问题"], max_length=20)
    writing_hint: str = Field(default="", max_length=4000)
    cover: Cover = Field(default_factory=Cover)

    @field_validator("profile")
    @classmethod
    def profile_valid(cls, value):
        if value not in PROFILES:
            raise ValueError("未知学科模板")
        return value

    @field_validator("type_ids")
    @classmethod
    def ids_valid(cls, values):
        if any(not re.fullmatch(r"[\w]{1,80}", v) for v in values):
            raise ValueError("笔记块类型标识不合法")
        return list(dict.fromkeys(values))

    @field_validator("sections")
    @classmethod
    def sections_valid(cls, values):
        cleaned = [v.strip() for v in values if v.strip()]
        if not cleaned or any(len(v) > 160 for v in cleaned):
            raise ValueError("至少填写一项内容结构，每项最多 160 字")
        return cleaned


def _path(layout, template_id: str) -> Path:
    if not re.fullmatch(r"tpl_[0-9a-f]{16}", template_id):
        raise InputError("模板标识不合法")
    return layout.data_dir / "note-templates" / (template_id + ".json")


def read_template(layout, template_id: str) -> dict:
    path = _path(layout, template_id)
    if not path.is_file():
        raise NotFound("模板不存在")
    return json.loads(path.read_text(encoding="utf-8"))


def list_templates(layout) -> list[dict]:
    root = layout.data_dir / "note-templates"
    if not root.exists():
        return []
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(root.glob("tpl_*.json"))]
    return sorted(rows, key=lambda item: item["updated_at"], reverse=True)


def save_template(layout, draft: TemplateDraft, template_id: str | None = None,
                  revision: int | None = None) -> dict:
    template_id = template_id or "tpl_" + uuid.uuid4().hex[:16]
    path = _path(layout, template_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock", timeout=10):
        old = read_template(layout, template_id) if path.exists() else None
        if old and revision != old["revision"]:
            raise RevisionConflict("模板已在别处更新，请重新打开再保存")
        if not old and revision is not None:
            raise NotFound("模板不存在")
        value = {**draft.model_dump(), "id": template_id,
                 "revision": old["revision"] + 1 if old else 1,
                 "updated_at": utc_now_iso()}
        atomic_write_bytes(path, json.dumps(value, ensure_ascii=False).encode("utf-8"))
    return value


def book_template(layout, doc_id: str) -> dict | None:
    DocStore(layout, doc_id).manifest()
    path = layout.doc_dir(doc_id) / "note-template.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def apply_template(layout, doc_id: str, template_id: str, revision: int) -> dict:
    value = read_template(layout, template_id)
    if value["revision"] != revision:
        raise RevisionConflict("模板版本已变化，请重新预览后应用")
    store = DocStore(layout, doc_id)
    base_revision = store.data_revision()
    snapshot = {**value, "applied_at": utc_now_iso()}
    # Participate in the document revision: an in-progress append must detect this
    # change instead of replacing the new template with its older staging copy.
    commit_doc(layout, doc_id, base_revision, [
        replace_change(layout, doc_id, "note-template.json", snapshot)])
    store._invalidate(layout, doc_id)
    return snapshot


def generation_style(template: dict | None) -> str:
    if not template:
        return ""
    # Visual changes deliberately do not enter prompt/cache identity.
    return ("用户选择的笔记风格（仅影响表达与组织；来源和类型约束仍须遵守）：\n"
            + json.dumps({k: template.get(k) for k in
                          ("note_type", "type_ids", "sections", "writing_hint")}, ensure_ascii=False)
            + "\n按资料实际内容选用合适结构，不得为凑齐栏目编造事实。type_ids 是优先使用的已有类型。")


def generation_identity(template: dict | None) -> str:
    if not template:
        return ""
    return hashlib.sha256((template.get("profile", "textbook") + generation_style(template)).encode()).hexdigest()


class StyledClient:
    """Per-generation wrapper: no shared mutable prompt state across books/threads."""
    def __init__(self, client, instruction: str):
        self.client = client
        self.instruction = instruction

    def __getattr__(self, name):
        return getattr(self.client, name)

    def complete(self, task, messages, **kwargs):
        messages = [*messages, {"role": "user", "content": self.instruction}]
        return self.client.complete(task, messages, **kwargs)
