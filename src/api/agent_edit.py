"""Agent 编辑两步式（5.5 / 9.4）：plan → 用户确认 → exec；服务端强制 scope。"""

from __future__ import annotations

import json
import uuid

from src.core.errors import ConflictError, InputError, NotFound, SchemaViolation
from src.core.llm import operation_client
from src.core.store import DocStore
from src.generate.prompts import AGENT_SYSTEM_PROMPT

AGENT_TOOLS = [
    {"type": "function", "function": {
        "name": "update_block",
        "description": "更新一个块的内容/标题/来源",
        "parameters": {"type": "object", "required": ["block_id", "message"],
                       "properties": {"block_id": {"type": "string"},
                                      "title": {"type": "string"},
                                      "content": {"type": "string"},
                                      "sources": {"type": "array"},
                                      "message": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "create_block",
        "description": "在目标章内新建一个块",
        "parameters": {"type": "object", "required": ["chapter_id", "type", "title", "content", "message"],
                       "properties": {"chapter_id": {"type": "string"},
                                      "type": {"type": "string"},
                                      "title": {"type": "string"},
                                      "content": {"type": "string"},
                                      "sources": {"type": "array"},
                                      "message": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "delete_block",
        "description": "软删除一个块",
        "parameters": {"type": "object", "required": ["block_id", "message"],
                       "properties": {"block_id": {"type": "string"}, "message": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "link_blocks",
        "description": "建立从 from_block_id 到 to_block_id 的单向链接",
        "parameters": {"type": "object", "required": ["from_block_id", "to_block_id", "message"],
                       "properties": {"from_block_id": {"type": "string"},
                                      "to_block_id": {"type": "string"},
                                      "message": {"type": "string"}}}}},
]


def _scope(store: DocStore, *, block_id: str | None = None, chapter_id: str | None = None) -> dict:
    if block_id:
        block = store.get_block(block_id, include_deleted=False)
        return {"kind": "block", "block_id": block_id, "chapter_id": block["chapter_id"]}
    if chapter_id:
        store.chapter(chapter_id)
        return {"kind": "chapter", "block_id": None, "chapter_id": chapter_id}
    raise InputError("必须指定 block_id 或 chapter_id")


def plan_edit(state, *, doc_id: str, instruction: str, block_id: str | None = None,
              chapter_id: str | None = None) -> dict:
    """步骤 1：生成 plan（不执行）；参数在此阶段生成并校验（9.4）。"""
    store = DocStore(state.layout, doc_id)
    scope = _scope(store, block_id=block_id, chapter_id=chapter_id)
    anchors_by_id = store.anchors_by_id()

    if scope["kind"] == "block":
        targets = [store.get_block(block_id, include_deleted=False)]
    else:
        targets = store.block_rows(scope["chapter_id"])[:20]

    source_lines = []
    for b in targets:
        source_lines.append(f"[block {b['id']}] ({b['type']}) {b['title']}\n{b['content'][:800]}")
        for src in b.get("sources", [])[:3]:
            anchor = anchors_by_id.get(src["anchor_id"], {})
            source_lines.append(f"  [source {src['anchor_id']}] {anchor.get('content', '')[:300]}")
    payload = ("<source_data>\n用户指令：%s\n%s\n</source_data>" %
               (instruction, "\n".join(source_lines)))

    messages = [{"role": "system", "content": AGENT_SYSTEM_PROMPT},
                {"role": "user", "content": payload}]
    client = operation_client(state.client)
    caps = {}
    try:
        caps = state.registry.capabilities(client.route("agent"))
    except Exception:
        caps = {}
    if not caps.get("tools"):
        raise InputError("agent 编辑需要具备 tools 能力的模型，请在设置中切换", code="TOOLS_REQUIRED")

    resp = client.complete("agent", messages, schema=None, max_tokens=2000,
                           tools=AGENT_TOOLS, tool_choice="auto", doc_id=doc_id)
    tool_calls = _extract_tool_calls(resp)
    if not tool_calls:
        # 空计划曾静默返回 200（用户确认后什么都不发生）：必须给出可操作的错误
        raise InputError("模型未返回任何修改计划（无工具调用），请重试或改写指令",
                         code="EMPTY_PLAN")
    proposed = []
    affected_revisions = {}
    for call in tool_calls:
        args = call.get("args", {})
        check_scope(store, scope, call["name"], args)
        if call["name"] in ("update_block", "delete_block", "link_blocks"):
            target = args.get("block_id") or args.get("from_block_id")
            if target:
                affected_revisions[target] = store.get_block(target, include_deleted=False)["revision"]
        proposed.append({"tool": call["name"], "args": args, "message": args.get("message", "")})

    plan_id = "p_" + uuid.uuid4().hex[:12]
    plan = {
        "plan_id": plan_id,
        "plan": {
            "intent": instruction,
            "affected_blocks": sorted(affected_revisions),
            "proposed_changes": proposed,
        },
    }
    state.runtime.save_session(plan_id, doc_id, f"{scope['kind']}:{scope['block_id'] or scope['chapter_id']}",
                               [{"role": "user", "content": instruction},
                                {"role": "assistant", "content": json.dumps(proposed, ensure_ascii=False)}],
                               "plan", confirmed_calls=proposed, affected_revisions=affected_revisions)
    return plan


def execute_plan(state, *, doc_id: str, plan_id: str, confirmed: bool = False) -> dict:
    """步骤 2：执行已确认的工具参数；每个工具一个可恢复提交，operation_id 去重。

    exec 先校验 affected_revisions（计划时快照），再逐条执行；期间被改动则中止剩余工具。
    """
    session = state.runtime.get_session(plan_id)
    if session is None:
        raise NotFound(f"plan 不存在: {plan_id}")
    if not confirmed:
        raise InputError("exec 需要 confirmed=true")
    snapshot = {k: int(v) for k, v in (session.get("affected_revisions") or {}).items()}
    store = DocStore(state.layout, doc_id)
    scope_kind, scope_target = session["scope"].split(":", 1)
    scope = {"kind": scope_kind, "block_id": scope_target if scope_kind == "block" else None,
             "chapter_id": scope_target if scope_kind == "chapter" else store.get_block(scope_target, include_deleted=False)["chapter_id"]}

    applied, errors = [], []
    for i, call in enumerate(session.get("confirmed_calls", [])):
        operation_id = f"{plan_id}:{i}"
        done = state.runtime.applied_operation(operation_id)
        if done:
            applied.append(done.get("result_json", {}))
            continue
        args = dict(call.get("args", {}))
        try:
            check_scope(store, scope, call["tool"], args)
            # 计划快照校验：目标块在 plan 之后被改过 → 中止，防止覆盖并发改动
            for target_id in _targets_of(call["tool"], args):
                if target_id in snapshot:
                    current = store.get_block(target_id, include_deleted=False)["revision"]
                    if int(current) != snapshot[target_id]:
                        raise ConflictError(
                            f"块 {target_id} 在计划确认后被修改（{snapshot[target_id]} → {current}），"
                            f"已中止剩余操作，请重新规划",
                            code="PLAN_STALE")
            unlocked = (state.runtime.is_unlocked(args.get("block_id", ""), plan_id)
                        or state.runtime.is_unlocked(args.get("from_block_id", ""), plan_id))
            result = run_tool(store, call["tool"], args, author="agent", plan_id=plan_id,
                              unlocked_for_agent=unlocked)
        except Exception as exc:
            # 部分执行语义：中止剩余工具，已成功部分保留；错误带稳定 code 供前端处理
            errors.append({"tool": call.get("tool"),
                           "code": getattr(exc, "code", "INTERNAL_ERROR"),
                           "error": str(exc)})
            break
        state.runtime.record_operation(operation_id, plan_id, result)
        applied.append(result)
    state.runtime.save_session(plan_id, doc_id, session["scope"], session["messages"], "done",
                               confirmed_calls=session.get("confirmed_calls", []),
                               affected_revisions=session.get("affected_revisions", {}))
    out = {"plan_id": plan_id, "diff": {"applied": applied}, "partial": bool(errors)}
    if errors:
        out["error"] = errors[0]
    return out


# 工具必填参数：LLM 漏字段/给 null 时要说人话
_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "update_block": ("block_id",),
    "delete_block": ("block_id",),
    "create_block": ("chapter_id", "type", "title", "content"),
    "link_blocks": ("from_block_id", "to_block_id"),
}


def validate_args(tool: str, args: dict) -> None:
    """工具参数校验：

    曾经的两种坏体验——LLM 少给 block_id 时，`args.get("block_id")` 的 None 一路流到
    store.get_block()，界面弹出「块不存在: None」（把 Python 的 None 抛给用户）；
    而 run_tool 用 `args["block_id"]` 取值，缺字段直接 KeyError→500。
    统一在这里拦下并给出可读原因。"""
    for key in _REQUIRED_ARGS.get(tool, ()):
        val = args.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            raise InputError(f"agent 计划缺少参数 {key}（工具 {tool}），请让它重新规划")


def _targets_of(tool: str, args: dict) -> list[str]:
    if tool in ("update_block", "delete_block"):
        return [args.get("block_id")] if args.get("block_id") else []
    if tool == "link_blocks":
        return [args.get("from_block_id")] if args.get("from_block_id") else []
    return []


def check_scope(store: DocStore, scope: dict, tool: str, args: dict) -> None:
    """服务端强制 scope（9.4）：越界调用一律拒绝。"""
    validate_args(tool, args)
    chapter = scope["chapter_id"]
    if tool == "update_block":
        bid = args.get("block_id")
        block = store.get_block(bid, include_deleted=False)
        if scope["kind"] == "block" and bid != scope["block_id"]:
            raise SchemaViolation("update_block 越界：只能修改目标块", code="SCOPE_VIOLATION")
        if block["chapter_id"] != chapter:
            raise SchemaViolation("update_block 越界：跨章", code="SCOPE_VIOLATION")
    elif tool == "create_block":
        if scope["kind"] == "block":
            # 9.4 块级范围=目标块：不得借 create_block 在同章夹带新块
            raise SchemaViolation("create_block 越界：块级范围只允许修改目标块",
                                  code="SCOPE_VIOLATION")
        if args.get("chapter_id") != chapter:
            raise SchemaViolation("create_block 越界：只能在目标章创建", code="SCOPE_VIOLATION")
    elif tool == "delete_block":
        bid = args.get("block_id")
        block = store.get_block(bid, include_deleted=False)
        if scope["kind"] == "block" and bid != scope["block_id"]:
            raise SchemaViolation("delete_block 越界：只能删除目标块", code="SCOPE_VIOLATION")
        if block["chapter_id"] != chapter:
            raise SchemaViolation("delete_block 越界：跨章", code="SCOPE_VIOLATION")
    elif tool == "link_blocks":
        src = args.get("from_block_id")
        dst = args.get("to_block_id")
        if scope["kind"] == "block" and src != scope["block_id"]:
            raise SchemaViolation("link_blocks 越界：from_block_id 必须是目标块", code="SCOPE_VIOLATION")
        src_block = store.get_block(src, include_deleted=False)
        if src_block["chapter_id"] != chapter:
            # 章级范围也要挡住跨章 from——否则可借 link 改到别章块的 links
            raise SchemaViolation("link_blocks 越界：跨章", code="SCOPE_VIOLATION")
        store.get_block(dst, include_deleted=False)
    else:
        raise SchemaViolation(f"未知工具: {tool}")


def run_tool(store: DocStore, tool: str, args: dict, *, author: str = "agent",
             plan_id: str | None = None, unlocked_for_agent: bool = False) -> dict:
    validate_args(tool, args)
    if tool == "update_block":
        block = store.update_block(args["block_id"],
                                   {k: v for k, v in args.items() if k in
                                    ("title", "content", "sources")},
                                   expected_revision=None, author=author,
                                   unlocked_for_agent=unlocked_for_agent,
                                   message=args.get("message", "agent 编辑"))
        return {"tool": tool, "block_id": block["id"], "revision": block["revision"]}
    if tool == "create_block":
        block = store.create_block({"type": args["type"], "title": args["title"],
                                    "content": args["content"],
                                    "sources": args.get("sources", []),
                                    "order": 0.0},
                                   author=author, message=args.get("message", "agent 新建"),
                                   chapter_id=args["chapter_id"])
        return {"tool": tool, "block_id": block["id"], "revision": block["revision"]}
    if tool == "delete_block":
        block = store.soft_delete_block(args["block_id"], author=author,
                                        message=args.get("message", "agent 删除"))
        return {"tool": tool, "block_id": block["id"], "revision": block["revision"]}
    if tool == "link_blocks":
        from_block = store.get_block(args["from_block_id"], include_deleted=False)
        store.get_block(args["to_block_id"], include_deleted=False)   # 目标必须是活块
        links = list(from_block.get("links", []))
        if args["to_block_id"] not in links:
            links.append(args["to_block_id"])
        block = store.update_block(args["from_block_id"], {"links": links},
                                   expected_revision=None, author=author,
                                   unlocked_for_agent=unlocked_for_agent,
                                   message=args.get("message", "agent 链接"))
        return {"tool": tool, "block_id": block["id"], "revision": block["revision"]}
    raise InputError(f"未知工具: {tool}")


def _extract_tool_calls(resp: dict) -> list[dict]:
    out = []
    for call in resp.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        out.append({"name": fn.get("name"), "args": args})
    if out:
        return out
    # 模型有时不走 function-calling，而是把计划写成 JSON 正文（实测 MiMo 3/3 如此）：
    # 从 output/raw_text 里按 proposed_changes/tool_calls 形状兜底解析，否则 agent 全瘫。
    for cand in (resp.get("output"), resp.get("raw_text")):
        obj = cand
        if isinstance(obj, str):
            text = obj.strip()
            if text.startswith("```"):                 # 剥掉 markdown 代码围栏再解析
                text = text.strip("`")
                nl = text.find("\n")
                tag = text[:nl] if nl >= 0 else text
                # 语言标签行整体丢弃（json/javascript/js/空），不能截固定长度——
                # 原实现两种分支都切 4 字符，"javascript" 会剩下 "ascript{..." 导致解析失败
                if nl >= 0 and tag.strip().lower() in ("json", "javascript", "js", ""):
                    text = text[nl + 1:]
                text = text.strip()
            try:
                obj = json.loads(text)
            except Exception:
                continue
        if not isinstance(obj, dict):
            continue
        items = obj.get("proposed_changes") or obj.get("tool_calls")
        if isinstance(obj.get("plan"), dict):
            items = items or obj["plan"].get("proposed_changes")
        for it in items or []:
            if not isinstance(it, dict):
                continue
            name = it.get("tool") or it.get("name")
            # 模型侧参数键名不稳：args / parameters / arguments（MiMo 实测用 arguments）
            args = it.get("args") or it.get("parameters") or it.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except Exception:
                    args = {}
            if name:
                out.append({"name": name, "args": args or {}})
        if out:
            break
    return out
