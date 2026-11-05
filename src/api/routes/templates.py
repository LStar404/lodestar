"""Template library, per-book snapshots and explicit AI drafting."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from src.core.block_types import REGISTRY, active_types, parse_custom_types
from src.core.errors import InputError
from src.core.llm import BudgetGuard, operation_client
from src.core.note_templates import (PROFILES, TemplateDraft, apply_template,
                                     book_template, list_templates, save_template)

router = APIRouter()


class TemplateUpdate(BaseModel):
    revision: int = Field(ge=1)
    draft: TemplateDraft


class TemplateApply(BaseModel):
    template_id: str
    revision: int = Field(ge=1)


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=6000)


class Discussion(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=20)
    draft: TemplateDraft


def _validate_types(draft):
    available = {s.type_id for s in active_types(draft.profile)}
    missing = set(draft.type_ids) - available
    if missing:
        raise InputError("以下类型不适用于所选学科，请先在设置添加或切换学科：" + ", ".join(sorted(missing)))


@router.get("/api/v1/templates")
def templates(request: Request):
    st = request.app.state.app_state
    custom, _ = parse_custom_types((st.config.get("generate") or {}).get("block_types"))
    types = [{"id": spec.type_id, "name": spec.cn_name, "icon": spec.icon,
              "profiles": list(spec.profiles)} for spec in {**REGISTRY, **custom}.values()]
    return {"items": list_templates(st.layout), "types": types, "profiles": PROFILES}


@router.post("/api/v1/templates")
def create(request: Request, body: TemplateDraft):
    _validate_types(body)
    return save_template(request.app.state.app_state.layout, body)


@router.put("/api/v1/templates/{template_id}")
def update(request: Request, template_id: str, body: TemplateUpdate):
    _validate_types(body.draft)
    return save_template(request.app.state.app_state.layout, body.draft, template_id, body.revision)


@router.get("/api/v1/docs/{doc_id}/template")
def get_applied(request: Request, doc_id: str):
    return {"template": book_template(request.app.state.app_state.layout, doc_id)}


@router.put("/api/v1/docs/{doc_id}/template")
def apply(request: Request, doc_id: str, body: TemplateApply):
    return {"template": apply_template(request.app.state.app_state.layout, doc_id,
                                       body.template_id, body.revision)}


@router.post("/api/v1/templates/discuss")
def discuss(request: Request, body: Discussion):
    st = request.app.state.app_state
    if body.messages[-1].role != "user":
        raise InputError("请先输入本轮想调整的模板要求")
    # Resolve the real configured model before doing work; no simulated suggestions.
    model = st.client.route("fill")
    draft = body.draft.model_dump()
    draft["cover"]["image"] = ""
    schema = {"type": "object", "required": ["reply", "draft"],
              "properties": {"reply": {"type": "string"},
                             "draft": TemplateDraft.model_json_schema()}}
    # Nested pydantic schema references are rooted at the response schema.
    schema["$defs"] = schema["properties"]["draft"].pop("$defs", {})
    type_ids = [spec.type_id for spec in {**REGISTRY, **parse_custom_types(
        (st.config.get("generate") or {}).get("block_types"))[0]}.values()]
    messages = [{"role": "system", "content": (
        "你与用户讨论读书笔记模板。根据多轮对话修改当前模板草稿，先用 reply 简洁说明取舍，"
        "再用 draft 返回完整模板。只输出符合 schema 的 JSON，不执行任何操作。"
        "不生成笔记正文；不捏造软件连接；不得生成图片 data URL。"
        "只能使用提供的已有块类型，无法满足时在 reply 中建议用户先在设置添加自定义类型。"
        "学科 profile 只能取 " + json.dumps(PROFILES, ensure_ascii=False)
        + "；已配置块类型：" + json.dumps(type_ids, ensure_ascii=False)
        + "。当前模板：" + json.dumps(draft, ensure_ascii=False))},
        *[m.model_dump() for m in body.messages]]
    response = operation_client(st.client).complete("fill", messages, model=model, schema=schema,
                                  max_tokens=3000, temperature=0.3,
                                  budget=BudgetGuard(float((st.config.get("generate") or {}).get("max_cost_per_chapter") or 0)))
    output = response.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("draft"), dict):
        raise InputError("模型没有返回有效模板，请补充要求后重试")
    # Preserve user-uploaded media; AI can choose color/style but cannot supply files.
    proposed = dict(output["draft"])
    proposed["cover"] = {**(proposed.get("cover") or {}), "image": body.draft.cover.image}
    clean = TemplateDraft.model_validate(proposed)
    _validate_types(clean)
    return {"reply": str(output.get("reply") or "已生成草稿，请预览后保存。")[:6000],
            "draft": clean.model_dump()}
