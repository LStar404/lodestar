"""LLM 接入层（9.5.1）：LLMProvider 协议 + OpenAICompatAdapter + DummyProvider + 统一记账。

能力标志驱动行为：json_schema / tools / vision；代码不硬编码供应商名。
所有调用（含传输重试）统一写入 runtime.db.usage_log；调用前原子预留成本预算（5.3.5）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Protocol

from .errors import (AuthError, LLMError, NoteGenError, PriceUnknown, QuotaExhausted,
                     RetryableError)
from .model_registry import ModelRegistry
from .text import estimate_tokens


class LLMProvider(Protocol):
    def complete(self, model: str, messages: list[dict], schema: dict | None,
                 max_tokens: int, temperature: float | None = None,
                 tools: list[dict] | None = None, tool_choice: str | dict | None = None,
                 json_schema: bool = True, context: dict | None = None,
                 reasoning_effort: str | None = None) -> dict: ...


# 配额/欠费用语（上游各家的说法）：用于把「额度用尽」从普通限流里区分出来。
_QUOTA_WORDS = ("quota", "insufficient", "exhausted", "balance", "credit", "billing",
                "arrears", "欠费", "额度", "余额", "配额")


def _upstream_message(resp) -> str:
    """从错误响应里取出可读的上游信息（quota exhausted / Invalid API Key 之类）。"""
    try:
        data = resp.json()
    except Exception:
        return (resp.text or "").strip().replace("\n", " ")[:160]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "")[:160]
        if isinstance(err, str):
            return err[:160]
        if data.get("message"):
            return str(data["message"])[:160]
    return (resp.text or "").strip().replace("\n", " ")[:160]


def _looks_like_quota(message: str) -> bool:
    low = (message or "").lower()
    return any(w in low for w in _QUOTA_WORDS)


# 并发上限类限流（如 DeepSeek 按余额档位给并发上限："current concurrency is 11,
# which exceeds your concurrency limit of 11 based on your remaining balance"）：
# 措辞里带 balance 会被 _looks_like_quota 误认成配额耗尽 → 熔断整章（实测误杀一次）。
# 明确点了「并发上限」的 429 是可重试的：退避后并发降下来即可继续，绝不熔断。
_CONCURRENCY_WORDS = ("concurrency", "并发")


def _looks_like_concurrency_limit(message: str) -> bool:
    low = (message or "").lower()
    return any(w in low for w in _CONCURRENCY_WORDS)


def _json_mode_messages(messages: list[dict]) -> list[dict]:
    """json_object 模式要求提示里出现 "json" 字样（DeepSeek/Qwen 皆然），缺失时补一条系统指令。"""
    if "json" in json.dumps(messages, ensure_ascii=False).lower():
        return messages
    return [*messages, {"role": "system",
                        "content": "只输出一个合法的 json 对象：不要 markdown 代码围栏，不要任何解释。"}]


class OpenAICompatAdapter:
    """面向 Chat Completions 兼容端点（OpenAI/DeepSeek/Qwen/Kimi/GLM/Ollama/vLLM）。"""

    def __init__(self, provider_id: str, base_url: str, api_key: str | None = None,
                 timeout: float = 120.0, transport: Any = None):
        self.provider_id = provider_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._transport = transport      # 测试注入口（httpx.MockTransport）；生产为 None
        self._client = None              # 持久连接池（懒建）：跨调用复用 TCP+TLS
        self._client_lock = threading.Lock()

    def _get_client(self):
        """持久 httpx.Client：每调用新建连接曾白付一次 TCP+TLS 握手（几十次调用/章）。
        httpx.Client 的连接池天生线程安全，可并发复用。"""
        import httpx

        with self._client_lock:
            if self._client is None:
                self._client = httpx.Client(timeout=self.timeout, transport=self._transport)
            return self._client

    def close(self) -> None:
        with self._client_lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def complete(self, model: str, messages: list[dict], schema: dict | None,
                 max_tokens: int, temperature: float | None = None,
                 tools: list[dict] | None = None, tool_choice: str | dict | None = None,
                 json_schema: bool = True, context: dict | None = None,
                 reasoning_effort: str | None = None,
                 thinking: dict | None = None) -> dict:
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if thinking:
            # 思考开关（DeepSeek 实测：disabled 同题 1.9s/230 tokens vs 开思考 9.4s/2020）；
            # 不认识该字段的网关直接忽略（OpenAI 兼容），不会报错
            body["thinking"] = thinking
        if temperature is not None:
            body["temperature"] = temperature
        if reasoning_effort:
            # 推理深度（MiMo 等推理模型实测支持 low/medium/high）：
            # 思考型模型单次调用的大头是思考 token，low 可显著缩短解析/填充耗时
            body["reasoning_effort"] = reasoning_effort
        if schema is not None:
            if json_schema:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "response", "schema": schema, "strict": False},
                }
            else:
                # 能力标志 json_schema=false（如 DeepSeek）：上游对 json_schema 直接回 400
                # "This response_format type is unavailable now"，只能退回 json_object
                body["response_format"] = {"type": "json_object"}
                body["messages"] = _json_mode_messages(messages)
        if tools:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request_id = "req_" + uuid.uuid4().hex[:16]
        try:
            resp = self._get_client().post(self.base_url + "/chat/completions", json=body,
                                           headers=headers, timeout=self.timeout)
        except Exception as exc:  # 传输层失败按可重试处理（10.4）
            raise RetryableError(f"LLM 请求失败: {exc}")
        upstream = _upstream_message(resp)
        if resp.status_code in (401, 403):
            # 鉴权失败重试不可能成功：立即上抛，别让用户等满退避才看到含混提示
            raise AuthError(
                f"LLM 鉴权失败 {resp.status_code}：API Key 无效或无权访问模型 {model}"
                f"（供应商 {self.provider_id}）"
                + (f"｜上游：{upstream}" if upstream else "")
                + "——请在「设置 → 供应商」核对密钥与模型 id（改完立即生效，无需重启）",
                detail={"provider_id": self.provider_id, "status": resp.status_code,
                        "body": resp.text[:500]})
        if resp.status_code == 402 or (resp.status_code == 429
                                      and _looks_like_quota(upstream)
                                      and not _looks_like_concurrency_limit(upstream)):
            # 配额用尽 ≠ 限流：重试多少次都一样，而且按块隔离会把整章块逐个标失败。
            # 整章快速上抛，任务结局直接给出「配额已用尽」这种可操作结论。
            raise QuotaExhausted(
                f"模型 {model} 配额已用尽（HTTP {resp.status_code}"
                + (f" {upstream}" if upstream else "")
                + f"，供应商 {self.provider_id}）——请为该供应商充值/更换 API Key 或改用其它模型",
                detail={"provider_id": self.provider_id, "status": resp.status_code,
                        "body": resp.text[:500]})
        if resp.status_code in (429, 500, 502, 503, 504):
            # 上游原话进消息：失败原因要能看出是「余额档位并发上限」还是普通限流，
            # 只报状态码的诊断价值为零
            raise RetryableError(f"LLM 服务返回 {resp.status_code}"
                                 + (f"：{upstream}" if upstream else ""),
                                 detail={"status": resp.status_code, "body": resp.text[:500]})
        if resp.status_code >= 400:
            # 其余 4xx（400 参数/404 模型不存在/422 等）同样不可重试
            raise LLMError(
                f"LLM 调用被拒绝 {resp.status_code}（供应商 {self.provider_id}，模型 {model}）",
                detail={"provider_id": self.provider_id, "status": resp.status_code,
                        "body": resp.text[:500]})
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content")
        usage = data.get("usage") or {}
        tool_calls = message.get("tool_calls") or []
        output: Any = None
        if content:
            try:
                output = json_repair_loads(content)
            except Exception:
                output = content
        return {
            "output": output,
            "raw_text": content if isinstance(content, str) else None,
            "tool_calls": tool_calls,
            "finish_reason": choice.get("finish_reason", "stop"),
            "usage": {
                "in": usage.get("prompt_tokens"),
                "out": usage.get("completion_tokens"),
                "estimated": (usage.get("prompt_tokens") is None
                              or usage.get("completion_tokens") is None),
            },
            "request_id": data.get("id") or request_id,
        }


class DummyProvider:
    """测试用固定输出；按调用顺序出队，队列为空时返回最后一项。"""

    def __init__(self, responses: list[Any] | None = None, provider_id: str = "dummy"):
        self.responses = list(responses or [])
        self.provider_id = provider_id
        self.calls: list[dict] = []

    def complete(self, model: str, messages: list[dict], schema: dict | None,
                 max_tokens: int, temperature: float | None = None,
                 tools: list[dict] | None = None, tool_choice: str | dict | None = None,
                 json_schema: bool = True, context: dict | None = None,
                 reasoning_effort: str | None = None,
                 thinking: dict | None = None) -> dict:
        self.calls.append({"model": model, "messages": messages, "tools": tools,
                           "tool_choice": tool_choice, "schema": schema,
                           "reasoning_effort": reasoning_effort, "thinking": thinking})
        item = self.responses.pop(0) if self.responses else {}
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict) and ("output" in item or "tool_calls" in item):
            out = dict(item)
            out.setdefault("finish_reason", "stop")
            out.setdefault("tool_calls", [])
            out.setdefault("usage", {"in": 10, "out": 10, "estimated": True})
            out.setdefault("request_id", "dummy-" + uuid.uuid4().hex[:8])
            return out
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return {
            "output": item,
            "raw_text": text,
            "tool_calls": [],
            "finish_reason": "stop",
            "usage": {"in": estimate_tokens(str(messages)), "out": estimate_tokens(text), "estimated": True},
            "request_id": "dummy-" + uuid.uuid4().hex[:8],
        }


def json_repair_loads(text: str) -> Any:
    from .text import repair_json_escapes

    text = repair_json_escapes(text or "")
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        from json_repair import repair_json

        return json.loads(repair_json(text))
    except Exception:
        return text


class BudgetGuard:
    """单章成本上限：调用前在共享预算锁内原子预留（5.3.5）。"""

    def __init__(self, limit_usd: float = 0.0):
        self.limit = float(limit_usd or 0.0)
        self._lock = threading.Lock()
        self._reserved = 0.0

    def reserve(self, cost_estimate: float | None) -> float | None:
        """调用前预留最坏成本，返回预留额（供 settle 结算）；预算未启用返回 None。"""
        if self.limit <= 0:
            return None
        if cost_estimate is None:
            raise PriceUnknown("成本未知，无法在非零预算下调用")
        with self._lock:
            if self._reserved + cost_estimate > self.limit:
                # code=BUDGET_EXCEEDED：运行期预算用尽，与配置类 PriceUnknown 区分——
                # 前者按块隔离保住已完成成果，后者（NO_MODEL/PRICE_UNKNOWN）必须冒泡
                raise PriceUnknown("超出单章成本上限 max_cost_per_chapter",
                                   code="BUDGET_EXCEEDED",
                                   detail={"limit": self.limit, "reserved": self._reserved})
            self._reserved += cost_estimate
            return cost_estimate

    def settle(self, cost_actual: float | None, reserved: float | None) -> None:
        """预留额结算成实际花费（失败/重试用尽则退回预留）。

        原实现「预留即已用、永不结算」：每次调用按 输入估算 + **整个 max_tokens** 的
        成本永久计入，而实际输出常只有其几分之一 → 打开成本上限后，账被放大数倍，
        远没花到上限就先 BUDGET_EXCEEDED 中断（configured 0.2 美元、实际花 0.05 就停）。
        结算后：在途调用仍按最坏成本预留（并发不超上限），已完成的按实际花费记账。"""
        if self.limit <= 0 or not reserved:
            return
        with self._lock:
            self._reserved = max(0.0, self._reserved - float(reserved) + float(cost_actual or 0.0))


class LLMClient:
    """按任务路由分发到模型；统一记账、重试与预算。"""

    def __init__(self, registry: ModelRegistry, runtime, config: dict,
                 overrides: dict[str, LLMProvider] | None = None):
        self.registry = registry
        self.runtime = runtime
        self.config = config
        self.overrides = overrides or {}
        self._providers: dict[str, LLMProvider] = {}
        # 调用遥测（生成报告 timing 用）：排队长 = 全局闸太紧；调用长 = 模型/输出慢
        self.stats: dict[str, float] = {"calls": 0, "retries": 0, "failures": 0,
                                        "call_seconds": 0.0, "wait_seconds": 0.0,
                                        "tokens_in": 0, "tokens_out": 0}
        self._stats_lock = threading.Lock()
        # 配额熔断：一次 QuotaExhausted 后本次任务内不再发请求（剩余块快速标失败，
        # 已产出结果落缓存，下次生成自动续填——不再整章作废）
        self.halted_reason: str | None = None
        self._inflight = 0
        self._gate = threading.Condition()
        self._limit = 1
        self.apply_config(config)

    def apply_config(self, config: dict) -> None:
        """热应用生成参数（thinking/推理深度/超时/并发上限）。

        这些字段在构造时固化，只换 self.config 引用不会生效——用户在设置里改完
        保存，客户端仍用旧值发请求（「改了没反应，要重启才生效」实测过这类坑）。
        设置保存路径必须调本方法。"""
        self.config = config
        gen = config.get("generate", {}) or {}
        # 单次调用超时（5.3.2 可配置）：推理型模型（MiMo 等）单次可超 2 分钟，
        # 默认 120s 会被误判超时并白重试（实测 ch08 提纲阶段多次 success=0 空转）
        self.timeout = float(gen.get("timeout", 600.0))
        # 推理深度（generate.reasoning_effort）：low/medium/high，空=供应商默认
        self.reasoning_effort = gen.get("reasoning_effort") or None
        # 思考开关（generate.thinking）：disabled/enabled/default
        _th = (gen.get("thinking") or "").strip().lower()
        self.thinking = {"disabled": {"type": "disabled"},
                         "enabled": {"type": "enabled"}}.get(_th)
        # 全局在途请求闸（generate.max_concurrent_requests 为上限）+ 自适应调节：
        # 并行分章后「章数 × 章内并发」会一次性打出十几个并发请求，触发限流反而被
        # 退避拖慢；且上游并发额度随余额档位浮动（实测 DeepSeek 当前档 11 且随余额
        # 下降再缩，打满即 429）。429 → 有效并发砍半、每次成功回升 1：钉在实际上限
        # 之下跑满吞吐，而不是跟一个会漂移的数字较劲
        with self._gate:
            self._max_slots = max(1, int(gen.get("max_concurrent_requests", 8) or 8))
            self._limit = self._max_slots if self._limit <= 1 else min(self._limit, self._max_slots)
            self._gate.notify_all()

    def _acquire_slot(self) -> None:
        with self._gate:
            while self._inflight >= self._limit:
                self._gate.wait()
            self._inflight += 1

    def _release_slot(self) -> None:
        with self._gate:
            self._inflight -= 1
            self._gate.notify_all()

    def _throttle(self, down: bool) -> None:
        """429 砍半、成功回升 1（不超上限）：自适应钉在上游实际上限之下。"""
        with self._gate:
            if down:
                self._limit = max(1, self._limit // 2)
            elif self._limit < self._max_slots:
                self._limit += 1
                self._gate.notify_all()

    # ---- 路由 ----
    def route(self, task: str) -> str:
        models = self.config.get("generate", {}).get("models", {}) or {}
        name = models.get(task) or models.get("default")
        if not name:
            raise PriceUnknown("未配置生成模型（config.yaml generate.models）", code="NO_MODEL")
        return name

    def _provider_for(self, model_name: str) -> tuple[dict, dict, LLMProvider]:
        provider, model = self.registry.find_model(model_name)
        pid = provider["id"]
        if pid in self.overrides:
            return provider, model, self.overrides[pid]
        if pid not in self._providers:
            self._providers[pid] = OpenAICompatAdapter(
                pid, provider.get("base_url", ""), self.registry.api_key(provider),
                timeout=self.timeout)
        return provider, model, self._providers[pid]

    # ---- 调用 ----
    def complete(self, task: str, messages: list[dict], *, schema: dict | None = None,
                 max_tokens: int = 2048, temperature: float | None = None,
                 tools: list[dict] | None = None, tool_choice: str | dict | None = None,
                 model: str | None = None, doc_id: str | None = None,
                 chapter_id: str | None = None, task_id: str | None = None,
                 budget: BudgetGuard | None = None, _circuit=None) -> dict:
        circuit = self if _circuit is None else _circuit
        if circuit.halted_reason:
            raise QuotaExhausted(circuit.halted_reason)
        model_name = model or self.route(task)
        provider_cfg, model_cfg, adapter = self._provider_for(model_name)
        # 路由名是 "provider/model"，供应商 API 只认裸模型 id
        # （原实现把全名原样上传，DeepSeek 直接 400：but you passed deepseek/deepseek-flash）
        api_model = (model_cfg or {}).get("id") or model_name
        caps = self.registry.capabilities(model_name)
        limits = self.registry.limits(model_name)
        max_tokens = int(min(max_tokens, limits["max_output_tokens"]))
        self.registry.require_priced(model_name, budget.limit if budget else 0.0)

        est_in = sum(estimate_tokens(str(m.get("content", ""))) for m in messages)
        est_out = max_tokens
        reserved_cost = self.registry.estimate_cost(model_name, est_in, est_out)
        reserved = budget.reserve(reserved_cost) if budget else None

        attempts = 0
        while True:
            attempts += 1
            request_id = "req_" + uuid.uuid4().hex[:16]
            try:
                t0 = time.monotonic()
                self._acquire_slot()
                t1 = time.monotonic()
                try:
                    # A queued call may have entered before another call hit quota.
                    if circuit.halted_reason:
                        raise QuotaExhausted(circuit.halted_reason)
                    result = adapter.complete(api_model, messages, schema, max_tokens,
                                              temperature=temperature, tools=tools,
                                              tool_choice=tool_choice,
                                              json_schema=bool(caps.get("json_schema")),
                                              context={"task": task, "doc_id": doc_id, "chapter_id": chapter_id},
                                              reasoning_effort=self.reasoning_effort,
                                              thinking=self.thinking)
                except QuotaExhausted as exc:
                    # Set the circuit before releasing the slot to queued callers.
                    circuit.halted_reason = exc.message
                    raise
                finally:
                    self._release_slot()
                with self._stats_lock:
                    self.stats["calls"] += 1
                    self.stats["wait_seconds"] += t1 - t0
                    self.stats["call_seconds"] += time.monotonic() - t1
            except RetryableError as exc:
                with self._stats_lock:
                    self.stats["retries"] += 1
                    self.stats["failures"] += 1
                # 并发类 429 → 砍半自适应（上游额度会随余额档位漂移）；其余瞬态错误不动闸
                if (getattr(exc, "detail", None) or {}).get("status") == 429:
                    self._throttle(down=True)
                self.runtime.log_usage(
                    provider_id=provider_cfg["id"], model=model_name, task=task,
                    request_id=request_id, doc_id=doc_id, chapter_id=chapter_id,
                    tokens_in=est_in, tokens_out=None, cost_est=None,
                    success=False, estimated=True, task_id=task_id)
                if attempts >= 3:
                    # 重试用尽：退回预留额（本次调用没成功，不该永久计入预算）
                    if budget:
                        budget.settle(None, reserved)
                    raise
                # 退避 2s/4s（曾 1s/2s 太急：限流窗口没过就又撞回去，实测 29 连败）
                time.sleep(min(8.0, 2.0 * attempts))
                continue
            except NoteGenError as exc:
                # 不可重试错误（鉴权/参数/模型不存在）：只记一次失败，立即上抛
                if budget:
                    budget.settle(None, reserved)      # 退回预留额
                if isinstance(exc, QuotaExhausted):
                    circuit.halted_reason = exc.message
                self.runtime.log_usage(
                    provider_id=provider_cfg["id"], model=model_name, task=task,
                    request_id=request_id, doc_id=doc_id, chapter_id=chapter_id,
                    tokens_in=est_in, tokens_out=None, cost_est=None,
                    success=False, estimated=True, task_id=task_id)
                raise

            self._throttle(down=False)      # 成功一次回升一格（自适应探测上游余量）
            usage = dict(result.get("usage") or {})
            missing_in, missing_out = usage.get("in") is None, usage.get("out") is None
            tin = est_in if missing_in else usage["in"]
            tout = est_out if missing_out else usage["out"]
            usage.update({"in": tin, "out": tout,
                          "estimated": bool(usage.get("estimated")) or missing_in or missing_out})
            result["usage"] = usage
            with self._stats_lock:
                self.stats["tokens_in"] += tin
                self.stats["tokens_out"] += tout
            cost = self.registry.estimate_cost(model_name, tin, tout)
            if budget:
                # 结算：把最坏预留换成实际花费（在途并发仍按预留额控上限）
                budget.settle(cost, reserved)
            self.runtime.log_usage(
                provider_id=provider_cfg["id"], model=model_name, task=task,
                request_id=result.get("request_id") or request_id, doc_id=doc_id,
                chapter_id=chapter_id, tokens_in=tin, tokens_out=tout,
                cost_est=cost, success=True, estimated=bool(usage.get("estimated")),
                task_id=task_id)
            result["model"] = model_name
            result["provider_id"] = provider_cfg["id"]
            result["usage"]["in"] = tin
            result["usage"]["out"] = tout
            return result

    def test_connection(self, provider_id: str, model_name: str) -> dict:
        """最小调用控成本；区分连通性、模型权限与能力（10.6.6）。"""
        provider = self.registry.provider(provider_id)
        full = f"{provider_id}/{model_name}"
        self.registry.find_model(full)
        adapter = self.overrides.get(provider_id) or OpenAICompatAdapter(
            provider_id, provider.get("base_url", ""), self.registry.api_key(provider), timeout=30.0)
        request_id = "req_" + uuid.uuid4().hex[:16]
        try:
            result = adapter.complete(model_name, [{"role": "user", "content": "ping"}], None, 8)
        except Exception as exc:
            self.runtime.log_usage(provider_id=provider_id, model=model_name, task="test",
                                   request_id=request_id, success=False)
            return {"ok": False, "error": str(exc)}
        self.runtime.log_usage(provider_id=provider_id, model=model_name, task="test",
                               request_id=result.get("request_id") or request_id, success=True,
                               tokens_in=(result.get("usage") or {}).get("in"),
                               tokens_out=(result.get("usage") or {}).get("out"),
                               estimated=bool((result.get("usage") or {}).get("estimated")))
        caps = self.registry.capabilities(full)
        return {"ok": True, "usage": result.get("usage"), "capabilities": caps,
                "note": "连通成功不代表 json_schema/tools 均可用，能力以显式配置为准"}


class _LLMOperation:
    """One user operation's quota circuit, sharing the application's request gate."""

    def __init__(self, client: LLMClient):
        self._client = client
        self.halted_reason: str | None = None

    def complete(self, *args, **kwargs):
        return self._client.complete(*args, **kwargs, _circuit=self)

    def __getattr__(self, name):
        return getattr(self._client, name)


def operation_client(client):
    """Start a fresh circuit for an explicit action, including same-task resumes.

    Providers, connection pools, usage recording and concurrency remain shared.
    Lightweight injected clients only implementing complete() remain supported.
    """
    return _LLMOperation(client) if isinstance(client, LLMClient) else client
