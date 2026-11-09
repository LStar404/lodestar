"""分享包（.ngshare）：对方装了 Lodestar 就能直接导入阅读（本地文件，不走网络）。

格式（JSON v1）是纯数据：标题 + 块（含出处页码与引文）+ 插图 data URL。导入后只读，
渲染复用前端 markdown/KaTeX 管线；校验从严（格式/版本/尺寸/插图 MIME），避免坏包
或超大包塞进数据目录。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile

from src.core.errors import InputError, NotFound
from src.core.jsonl import atomic_write_bytes, canonical_json
from src.core.text import utc_now_iso

router = APIRouter()

FORMAT = "note-gen-share"  # 兼容旧包的格式标识，不要改
VERSION = 1
MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_BLOCKS = 2000
MAX_CONTENT_CHARS = 50 * 1024
MAX_FIGURES = 40
MAX_FIGURE_BYTES = 30 * 1024 * 1024
SHARE_ID_RE = re.compile(r"^share-[0-9a-f]{12}$")
DATA_URL_RE = re.compile(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$")


def state(request: Request):
    return request.app.state.app_state


def shares_root(layout) -> Path:
    return layout.data_dir / "shares"


def _share_dir(layout, share_id: str) -> Path:
    if not SHARE_ID_RE.match(share_id or ""):
        raise NotFound(f"分享不存在: {share_id}")
    p = shares_root(layout) / share_id
    if not p.exists():
        raise NotFound(f"分享不存在: {share_id}")
    return p


def _text(value, *, limit: int, what: str) -> str:
    if not isinstance(value, str):
        raise InputError(f"分享包字段不合法: {what} 必须是字符串")
    if len(value) > limit:
        raise InputError(f"分享包字段超限: {what} 超过 {limit} 字符")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InputError(f"分享包字段不合法: {what} 包含无效 Unicode") from None
    return value


def _page(value, *, what: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
        value = int(value)
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise InputError(f"分享包字段不合法: {what} 必须是有效页码或 null")
    return value


def validate_share(payload) -> dict:
    """把任意 JSON 收窄成合法的分享包；不合规一律 400（说清哪里不对）。"""
    if not isinstance(payload, dict):
        raise InputError("不是合法的 Lodestar 分享包（根节点必须是对象）")
    if payload.get("format") != FORMAT:
        raise InputError("不是合法的 Lodestar 分享包（format 不匹配）")
    version = payload.get("version")
    if type(version) is not int:
        raise InputError("分享包版本不合法")
    if version != VERSION:
        raise InputError(f"分享包版本不支持: v{version}（本机支持 v{VERSION}）")
    raw_blocks = payload.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise InputError("分享包里没有笔记块")
    if len(raw_blocks) > MAX_BLOCKS:
        raise InputError(f"分享包块数超限（{len(raw_blocks)}/{MAX_BLOCKS}）")
    blocks: list[dict] = []
    for i, b in enumerate(raw_blocks):
        if not isinstance(b, dict):
            raise InputError(f"第 {i + 1} 个笔记块格式不合法")
        content = _text(b.get("content"), limit=MAX_CONTENT_CHARS, what=f"blocks[{i}].content")
        sources = []
        raw_sources = b.get("sources")
        if raw_sources is None:
            raw_sources = []
        if not isinstance(raw_sources, list):
            raise InputError(f"分享包字段不合法: blocks[{i}].sources 必须是数组")
        for s in raw_sources[:8]:
            if not isinstance(s, dict):
                raise InputError(f"分享包字段不合法: blocks[{i}].sources 的条目必须是对象")
            quote = s.get("quote")
            sources.append({
                "page": _page(s.get("page"), what=f"blocks[{i}].sources.page"),
                "quote": _text(quote, limit=300, what=f"blocks[{i}].sources.quote")
                         if quote is not None else "",
            })
        blocks.append({
            "type": _text(b.get("type"), limit=40, what=f"blocks[{i}].type") if b.get("type") else "",
            "title": _text(b.get("title"), limit=300, what=f"blocks[{i}].title") if b.get("title") else "",
            "content": content,
            "sources": sources,
        })
    raw_figs = payload.get("figures") or {}
    if not isinstance(raw_figs, dict):
        raise InputError("分享包 figures 字段不合法")
    if len(raw_figs) > MAX_FIGURES:
        raise InputError(f"分享包插图过多（{len(raw_figs)}/{MAX_FIGURES}）")
    figures: dict[str, str] = {}
    total = 0
    for fid, data_url in raw_figs.items():
        if not isinstance(fid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", fid):
            raise InputError("分享包插图标识不合法")
        if not isinstance(data_url, str) or not DATA_URL_RE.fullmatch(data_url):
            raise InputError(f"插图 {fid} 不是合法的图片 data URL")
        total += len(data_url)
        if total > MAX_FIGURE_BYTES:
            raise InputError("分享包插图体积超限（>30MB）")
        figures[fid] = data_url
    return {
        "format": FORMAT,
        "version": VERSION,
        "created_at": _text(payload.get("created_at") or utc_now_iso(), limit=40,
                            what="created_at"),
        "title": _text(payload.get("title") or "未命名分享", limit=300, what="title"),
        "chapter_title": _text(payload.get("chapter_title") or "", limit=300,
                               what="chapter_title"),
        "blocks": blocks,
        "figures": figures,
    }


def share_id_for(payload: dict) -> str:
    """按内容派生 id（**不含 created_at**）：同一份笔记重复导入/重新导出都幂等。

    导出包每次都盖新的 created_at 时间戳——若把时间戳也算进 id，用户把同一章
    重新导出一次就会在对方收件箱里多出一条副本（实测同内容 id 从 fef9db 变 7ded51）。"""
    basis = {k: payload.get(k) for k in
             ("format", "version", "title", "chapter_title", "blocks", "figures")}
    return "share-" + hashlib.sha256(canonical_json(basis).encode("utf-8")).hexdigest()[:12]


def _write_share(layout, payload: dict) -> dict:
    share_id = share_id_for(payload)
    d = shares_root(layout) / share_id
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(d / "share.json",
                       (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return {"share_id": share_id, "title": payload["title"],
            "chapter_title": payload["chapter_title"],
            "blocks": len(payload["blocks"]),
            "figures": len(payload["figures"]),
            "created_at": payload["created_at"]}


def _summary(d: Path) -> dict | None:
    try:
        data = json.loads((d / "share.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    p = d / "share.json"
    return {"share_id": d.name, "title": data.get("title") or "未命名分享",
            "chapter_title": data.get("chapter_title") or "",
            "blocks": len(data.get("blocks") or []),
            "figures": len(data.get("figures") or {}),
            "created_at": data.get("created_at") or "",
            "size": p.stat().st_size if p.exists() else 0}


@router.post("/api/v1/shares")
async def upload_share(request: Request, file: UploadFile = File(...)):
    """导入 .ngshare 分享包（只读收录，不动任何现有教材）。"""
    st = state(request)
    raw = await file.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise InputError("分享包超过 40MB 限制")
    if not raw.strip():
        raise InputError("分享包是空文件")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise InputError("解析失败：不是合法的 JSON（请确认是 .ngshare 文件）") from None
    clean = validate_share(payload)
    meta = _write_share(st.layout, clean)
    return {"share": meta}


@router.get("/api/v1/shares")
def list_shares(request: Request):
    st = state(request)
    root = shares_root(st.layout)
    items: list[dict] = []
    if root.exists():
        for d in sorted(root.iterdir()):
            if d.is_dir():
                summary = _summary(d)
                if summary:
                    items.append(summary)
    items.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return {"items": items}


@router.get("/api/v1/shares/{share_id}")
def get_share(request: Request, share_id: str):
    st = state(request)
    d = _share_dir(st.layout, share_id)
    try:
        return json.loads((d / "share.json").read_text(encoding="utf-8"))
    except Exception:
        raise NotFound(f"分享文件损坏: {share_id}") from None


@router.delete("/api/v1/shares/{share_id}")
def delete_share(request: Request, share_id: str):
    st = state(request)
    d = _share_dir(st.layout, share_id)
    root = shares_root(st.layout).resolve()
    if root not in d.resolve().parents:
        raise InputError("非法的分享路径")
    shutil.rmtree(d, ignore_errors=True)
    return {"deleted": share_id}
