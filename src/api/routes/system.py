"""模型管理、用量、导出与任务端点（9.2）。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from src.core.errors import InputError, NotFound

router = APIRouter()


def state(request: Request):
    return request.app.state.app_state


@router.get("/api/v1/models")
def list_models(request: Request):
    st = state(request)
    return {"providers": [st.registry.masked_provider(p) for p in st.registry.providers()],
            "routes": st.config.get("generate", {}).get("models", {})}


@router.post("/api/v1/models/test")
def test_model(request: Request, body: dict):
    st = state(request)
    provider_id = body.get("provider_id")
    model = body.get("model")
    if not provider_id or not model:
        raise InputError("需要 provider_id 与 model")
    return st.client.test_connection(provider_id, model)


@router.put("/api/v1/models/{provider_id}")
def upsert_provider(request: Request, provider_id: str, body: dict):
    st = state(request)
    body["id"] = provider_id
    st.registry.upsert_provider(body)
    # 换 key / 改 base_url 必须重建客户端：适配器把密钥固化在实例里，
    # 不重建则要重启服务才生效（用户实测「换了 key 仍无效」的直接原因）
    st.invalidate_client()
    return st.registry.masked_provider(st.registry.provider(provider_id))


@router.delete("/api/v1/models/{provider_id}")
def delete_provider(request: Request, provider_id: str):
    st = state(request)
    st.registry.delete_provider(provider_id, st.routed_models())
    st.invalidate_client()
    return {"deleted": provider_id}


@router.put("/api/v1/model-routes")
def update_routes(request: Request, body: dict):
    st = state(request)
    for slot, name in (body or {}).items():
        if name:
            st.registry.find_model(name)   # 校验模型存在
            caps = st.registry.capabilities(name)
            if slot == "agent" and not caps.get("tools"):
                raise InputError(f"模型 {name} 缺少 tools 能力，不能用于 agent 任务")
            if slot == "ocr" and not caps.get("vision"):
                raise InputError(f"模型 {name} 缺少 vision 能力，不能用于视觉补 OCR")
    st.save_model_routes(body or {})
    st.invalidate_client()
    return {"routes": st.config.get("generate", {}).get("models", {})}


@router.get("/api/v1/block-types")
def block_types(request: Request):
    """块类型注册表（学科预设 + 用户自定义）：前端图标/名称与生成同一事实源。"""
    from src.core.block_types import active_types

    st = state(request)
    gen = st.config.get("generate", {}) or {}
    profile = gen.get("profile") or "textbook"
    types = [{"id": s.type_id, "name": s.cn_name, "icon": s.icon,
              "source_policy": s.source_policy, "synthesis_allowed": s.synthesis_allowed,
              "hint": s.hint, "profiles": list(s.profiles)}
             for s in active_types(profile)]
    return {"profile": profile, "types": types,
            "presets": ["textbook", "paper", "physics", "cs", "medicine", "chemistry"],
            "custom": gen.get("block_types") or []}


@router.get("/api/v1/settings")
def get_settings(request: Request):
    """应用设置（解析器/OCR/输出语言/导出编码/生成参数）。"""
    st = state(request)
    cfg = st.config
    return {
        "parser": cfg.get("parser", {}),
        "generate": {k: v for k, v in (cfg.get("generate", {}) or {}).items() if k != "models"},
        "export": cfg.get("export", {}),
        "match": cfg.get("match", {}),
        "history": cfg.get("history", {}),
        "embedding": cfg.get("embedding", {}),
        "options": {
            "parsers": ["auto", "marker", "docling", "pypdf", "pymupdf"],
            "ocr_modes": ["auto", "force", "off"],
            "languages": ["zh", "en", "ja"],
            "encodings": ["utf-8", "utf-8-sig", "gbk", "big5"],
        },
    }


@router.put("/api/v1/settings")
def put_settings(request: Request, body: dict):
    """更新应用设置（原子保存 config.yaml，不涉及密钥）。"""
    from copy import deepcopy

    from src.core.config import (normalize_settings, save_config,
                                 validate_settings_patch)

    st = state(request)
    validate_settings_patch(body)
    cfg = deepcopy(st.config)
    for section in ("parser", "generate", "export", "match", "history", "embedding"):
        if section in body and isinstance(body[section], dict):
            cfg[section] = {**(cfg.get(section) or {}), **body[section]}

    parser = cfg.get("parser", {})
    if parser.get("default") not in ("auto", "marker", "docling", "pypdf", "pymupdf"):
        raise InputError(f"不支持的解析器: {parser.get('default')}")
    if parser.get("ocr_mode") not in ("auto", "force", "off"):
        raise InputError(f"不支持的 ocr_mode: {parser.get('ocr_mode')}")
    cfg = normalize_settings(cfg)

    # 自定义块类型校验结果要反馈（曾静默丢弃非法条目，用户填错 id 毫无感知）
    from src.core.block_types import parse_custom_types

    _, bad_types = parse_custom_types((cfg.get("generate", {}) or {}).get("block_types"))
    save_config(st.root, cfg)
    st.reload_config()
    return {"settings": {"parser": st.config.get("parser"),
                         "generate": st.config.get("generate"),
                         "export": st.config.get("export")},
            "block_type_warnings": [f"无效的自定义类型 id（已忽略）: {b}" for b in bad_types]}


@router.get("/api/v1/usage")
def usage(request: Request, doc_id: str | None = None, days: int = 30):
    st = state(request)
    return {"rows": st.runtime.usage_summary(doc_id=doc_id, days=days),
            "total_cost_est": st.runtime.usage_total(doc_id=doc_id),
            "note": "成本为估算值，未知价格显示为未知，不按 0 计"}


@router.post("/api/v1/export")
def trigger_export(request: Request, body: dict):
    st = state(request)
    doc_id = body.get("doc_id")
    chapter_id = body.get("chapter_id")
    fmt = body.get("format", "md")
    if fmt not in ("md", "tex", "both", "md-obsidian", "anki"):
        raise InputError(f"不支持的导出格式: {fmt}")
    # 导出语言/编码：请求可覆盖，默认取全局设置
    lang = body.get("language") or st.config.get("generate", {}).get("output_language", "zh")
    encoding = body.get("encoding") or st.config.get("export", {}).get("encoding", "utf-8")

    def run(req, progress, is_cancelled, register_artifact, task_id=None):
        from src.core.store import DocStore
        from src.export.anki_export import export_anki
        from src.export.md_export import export_md_book, export_md_chapter, export_md_obsidian
        from src.export.tex_export import export_tex_book, export_tex_chapter

        store = DocStore(st.layout, doc_id)
        out_dir = st.layout.notes_doc_dir(doc_id)
        paths = []
        chapters = [chapter_id] if chapter_id else [c["chapter_id"] for c in store.manifest()["chapters"]]
        for ch in chapters:
            progress({"stage": "export", "chapter_id": ch})
            if fmt in ("md", "both"):
                paths.append(str(export_md_chapter(store, ch, out_dir=out_dir,
                                                   lang=lang, encoding=encoding)))
            if fmt in ("tex", "both"):
                paths.append(str(export_tex_chapter(store, ch, out_dir=out_dir,
                                                    lang=lang, encoding=encoding)))
            if fmt == "md-obsidian":
                paths.append(str(export_md_obsidian(store, ch, out_dir=out_dir.parent,
                                                    lang=lang, encoding=encoding)))
            if fmt == "anki":
                paths.append(str(export_anki(store, ch, out_dir=out_dir,
                                             lang=lang, encoding=encoding)))
        if chapter_id is None:
            if fmt in ("md", "both"):
                paths.append(str(export_md_book(store, out_dir=out_dir,
                                                lang=lang, encoding=encoding)))
            if fmt in ("tex", "both"):
                paths.append(str(export_tex_book(store, out_dir=out_dir,
                                                 lang=lang, encoding=encoding)))
            if fmt == "anki":
                paths.append(str(export_anki(store, None, out_dir=out_dir,
                                             lang=lang, encoding=encoding)))
        for p in paths:
            register_artifact(Path(p).name, p)
        return {"paths": paths}

    try:
        from src.core.store import DocStore

        _title = DocStore(st.layout, doc_id).manifest().get("title") or doc_id
    except Exception:
        _title = doc_id
    task = st.tasks.submit("export", run, {"doc_id": doc_id, "chapter_id": chapter_id, "format": fmt},
                           doc_id=doc_id, name=_title + " · 导出 " + fmt)
    return JSONResponse({"task_id": task["task_id"]}, status_code=202)


@router.post("/api/v1/system/open-path")
def open_path(request: Request, body: dict):
    """在系统文件管理器中打开工作区内路径（导出完成提示「文件在哪」用）。

    只允许工作区内路径（防任意路径探测）；打开目录而非文件本身，
    避免 .apkg/.tex 被关联程序意外拉起。
    """
    import os
    import subprocess
    import sys as _sys

    st = state(request)
    rel = str((body or {}).get("path") or "")
    if not rel:
        raise InputError("缺少 path")
    root = Path(st.layout.root).resolve()
    target = (root / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise InputError("只允许打开工作区内的路径")
    if not target.exists():
        raise NotFound(f"路径不存在: {rel}")
    if target.is_file():
        target = target.parent
    if _sys.platform == "win32":
        os.startfile(str(target))          # 本机应用：打开自己的导出目录
    elif _sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])
    return {"ok": True, "path": str(target)}


@router.get("/api/v1/tasks")
def list_tasks(request: Request, doc_id: str | None = None, limit: int = 20):
    """最近任务（持久化于 runtime.db）：刷新/重启后恢复任务记录与续跑入口。"""
    st = state(request)
    items = st.runtime.list_tasks(doc_id=doc_id)
    return {"items": items[: max(1, min(int(limit), 100))]}


@router.get("/api/v1/tasks/{task_id}")
def get_task(request: Request, task_id: str):
    st = state(request)
    try:
        return st.tasks.get(task_id)
    except KeyError:
        raise NotFound(f"任务不存在: {task_id}")


@router.delete("/api/v1/tasks/{task_id}")
def delete_task(request: Request, task_id: str):
    """删除任务记录（清理不需要的生成项目）；运行中的先优雅取消再删除。"""
    st = state(request)
    try:
        st.tasks.delete(task_id)
    except KeyError:
        raise NotFound(f"任务不存在: {task_id}")
    return {"deleted": True, "task_id": task_id}


@router.post("/api/v1/tasks/{task_id}/cancel")
def cancel_task(request: Request, task_id: str):
    st = state(request)
    try:
        return st.tasks.cancel(task_id)
    except KeyError:
        raise NotFound(f"任务不存在: {task_id}")


@router.get("/api/v1/tasks/{task_id}/artifacts/{name}")
def task_artifact(request: Request, task_id: str, name: str):
    st = state(request)
    try:
        path = st.tasks.artifact(task_id, name)
    except KeyError:
        raise NotFound(f"产物不存在: {name}")
    return FileResponse(path)
