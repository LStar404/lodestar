import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useStore } from "../state/store";
import { writeBrand } from "../lib/brand";

/** 配置指南（首次使用引导，ADR D35）：分步向导 + 环境自检。
 *
 * 数据源 GET /api/v1/setup/status（只读自检）；写操作复用既有接口：
 * PUT /api/v1/models/{id}、PUT /api/v1/model-routes、PUT /api/v1/settings。
 */

const EMPTY_PROVIDER = {
  id: "",
  label: "",
  protocol: "openai_compat",
  base_url: "https://api.openai.com/v1",
  api_key: "",
  api_key_env: "",
  capabilities: { json_schema: true, tools: true, vision: false },
  models: [] as any[],
};

const CHECKS_BY_STEP: Record<string, string[]> = {
  env: ["python", "data_dir", "local_token"],
  model: ["models", "keys"],
  routes: ["routes", "agent_tools"],
  parser: ["parser_pypdf", "parser_marker", "parser_docling", "ocr", "formula_ocr"],
  output: [],
  budget: ["price"],
};

/** 分步骤引导（每步：做什么 → 怎么做 → 完成标志 → 提示） */
const STEP_GUIDE: Record<
  string,
  { goal: string; steps: string[]; expect: string; tip?: string }
> = {
  env: {
    goal: "确认运行环境就绪——这步不用填任何东西，看自检结果即可。",
    steps: [
      "查看下方「自检」卡片：Python 版本、数据目录、本地令牌三项是否都是绿色",
      "若数据目录不可写：检查 data/ 目录的读写权限后点「重新自检」",
      "若本地令牌缺失：重启一次应用会自动生成（仅本机访问校验用）",
    ],
    expect: "三项自检全部为绿色，顶部状态徽标变为「已就绪」或进入下一步继续配置。",
    tip: "红/黄项不用慌——每张卡片下方都写了对应的修复动作，照做即可。",
  },
  model: {
    goal: "接入至少一个大模型供应商——没有这步，生成笔记无从谈起。",
    steps: [
      "点「添加供应商」：填供应商 id（如 deepseek / mimo，小写字母数字）",
      "填 base_url（OpenAI 兼容地址，如 https://api.deepseek.com/v1）",
      "填 API Key；更推荐填「环境变量名」（如 DEEPSEEK_API_KEY）避免明文落盘",
      "在「模型 id」里每行填一个模型名（如 deepseek-v3 / mimo-v2.6-flash）",
      "点「保存」，再点「测试连接」验证连通性",
    ],
    expect: "自检中「模型」与「API Key」两项变绿；测试连接返回成功。",
    tip: "测试返回 401 = 密钥无效，402 = 余额不足，404 = 模型 id 写错。接口只回显密钥末 4 位，不会泄露全文。",
  },
  routes: {
    goal: "把不同任务分配给不同模型——决定每类任务用谁来干活。",
    steps: [
      "default：必填，所有任务的兜底模型",
      "outline / fill：可分别指定；留空则用 default（填充调用量大，可指给便宜的模型）",
      "agent：必须选「具备 tools 能力」的模型（右侧会显示能力检测结果）",
      "点「保存路由」",
    ],
    expect: "「任务路由」检查项变绿，agent 模型能力显示「有」。",
    tip: "换路由后生成缓存按新模型自动失效，不需要手动清理。",
  },
  parser: {
    goal: "选择 PDF 解析方式——直接影响导入速度与识别质量。",
    steps: [
      "电子版 PDF（能选中文字的教材/论文）：选 auto——自动走 pypdf 快速路径，百页秒级",
      "扫描版 PDF（图片书）：才需要 marker / docling（重型解析，百页数十分钟起）",
      "OCR 策略保持 auto 即可：只对没有文字层的页做 OCR",
    ],
    expect: "所选解析器的可用性检查为绿色（auto 只要任意一个可用即绿）。",
    tip: "导入卡在「解析」多半是扫描书或选了重型解析器——先用 auto 试，慢再换。",
  },
  output: {
    goal: "设置笔记的语言与导出编码（随时可改）。",
    steps: [
      "输出语言：笔记正文的语言（zh / en）",
      "导出编码：utf-8 通用；需要在旧版 Windows 记事本打开就选 gbk",
    ],
    expect: "「输出」检查项变绿。",
    tip: "语言只影响生成的笔记，不影响 PDF 解析；教材本身是中文不影响选 en。",
  },
  budget: {
    goal: "控制并发与花费——防失控的保险丝。",
    steps: [
      "并发：同章内并行的填充调用数，默认 4（API 限流明显时调小）",
      "单章成本上限：填 0 = 不限制；填金额（USD）后超预算会停止新调用",
      "启用非零预算时，所有已路由模型必须配置价格（input/output per million）",
    ],
    expect: "「成本与价格」检查项为绿色，顶部显示「已就绪」。",
    tip: "超预算不会丢成果：已完成的块照常保留，只是停止后续调用（5.3.5）。",
  },
};

export default function SetupGuide() {
  const open = useStore((s) => s.setupOpen);
  const setOpen = useStore((s) => s.openSetup);
  const setToast = useStore((s) => s.setToast);
  const [status, setStatus] = useState<any | null>(null);
  const [registry, setRegistry] = useState<any | null>(null);
  const [stepIdx, setStepIdx] = useState(0);
  const [routes, setRoutes] = useState<any>({});
  const [form, setForm] = useState<any>({});
  const [providers, setProviders] = useState<any[]>([]);
  const [draft, setDraft] = useState<any | null>(null);
  const [testMsg, setTestMsg] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const reload = async () => {
    try {
      const [s, m] = await Promise.all([api.setupStatus(), api.models()]);
      setStatus(s);
      setRegistry(m);
      setRoutes({ ...(m.routes ?? {}) });
      setProviders((m.providers ?? []).map((p: any) => ({
        ...p,
        api_key: "",
        api_key_env: p.api_key_env ?? "",
        modelsText: (p.models ?? []).map((x: any) => x.id).join("\n"),
      })));
      setForm({
        parser: { ...(s.settings.parser ?? {}) },
        generate: { ...(s.settings.generate ?? {}) },
        export: { ...(s.settings.export ?? {}) },
      });
    } catch (e: any) {
      setToast("加载配置状态失败：" + e.message);
    }
  };

  useEffect(() => {
    if (open) void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const steps = status?.steps ?? [];
  const step = steps[stepIdx] ?? steps[0];
  const close = () => {
    writeBrand("setup-seen", "1");
    setOpen(false);
  };

  const run = async (fn: () => Promise<void>, okMsg: string) => {
    setBusy(true);
    try {
      await fn();
      setToast(okMsg);
      await reload();
    } catch (e: any) {
      setToast("保存失败：" + e.message);
    } finally {
      setBusy(false);
    }
  };

  const providerPayload = (p: any) => ({
    id: p.id,
    label: p.label || p.id,
    protocol: p.protocol || "openai_compat",
    base_url: p.base_url,
    api_key: p.api_key || null,
    api_key_env: p.api_key_env || null,
    capabilities: p.capabilities ?? { json_schema: true, tools: true, vision: false },
    models: (p.modelsText ?? "")
      .split(/\r?\n/)
      .map((x: string) => x.trim())
      .filter(Boolean)
      .map((id: string) => {
        const old = (p.models ?? []).find((m: any) => m.id === id);
        return old ?? {
          id,
          context_window: 8192,
          max_output_tokens: 2048,
          capabilities: { json_schema: true, tools: true, vision: false },
          pricing: null,
        };
      }),
  });

  const saveProvider = (p: any) => {
    if (!p.id.trim()) return setToast("供应商 id 必填");
    void run(async () => {
      await api.upsertProvider(p.id.trim(), providerPayload(p));
    }, "供应商已保存");
  };

  const testProvider = async (p: any) => {
    const model = (p.modelsText ?? "").split(/\r?\n/).map((x: string) => x.trim()).filter(Boolean)[0];
    if (!p.id.trim() || !model) return setToast("需要供应商 id 与至少一个模型");
    setTestMsg("测试中…");
    try {
      const r = await api.testModel({ provider_id: p.id.trim(), model });
      setTestMsg(r.ok ? "连接成功：" + p.id.trim() + "/" + model : "连接失败：" + r.error);
    } catch (e: any) {
      setTestMsg("连接失败：" + e.message);
    }
  };

  const modelOptions = (registry?.providers ?? []).flatMap((p: any) =>
    (p.models ?? []).map((m: any) => ({
      value: p.id + "/" + m.id,
      tools: !!m.capabilities?.tools,
    }))
  );

  const checksFor = (ids: string[]) =>
    (status?.checks ?? []).filter((c: any) => ids.includes(c.id));

  const renderCheck = (c: any) => (
    <div key={c.id} className={"setup-check " + c.level}>
      <div className="setup-check-head">
        <span className={"badge " + (c.level === "ok" ? "protected" : "synthesis")}>
          {c.level === "ok" ? "通过" : c.level === "warn" ? "注意" : "未就绪"}
        </span>
        <b>{c.title}</b>
      </div>
      <div className="muted">{c.detail}</div>
      {c.fix && <div className="muted">修复：{c.fix}</div>}
    </div>
  );

  const saveAppSettings = (patch: any, okMsg: string) =>
    void run(async () => {
      await api.saveSettings(patch);
    }, okMsg);

  return (
    <div className="modal-mask" onClick={close}>
      <div className="modal wide setup-modal" onClick={(e) => e.stopPropagation()}>
        <h2>
          配置指南
          <span className={"badge " + (status?.ready ? "protected" : "synthesis")}>
            {status?.ready ? "已就绪" : "未就绪"}
          </span>
          {status?.first_run && <span className="badge synthesis">首次使用</span>}
        </h2>

        <div className="setup-layout">
          <nav className="setup-steps">
            {steps.map((s: any, i: number) => (
              <button
                key={s.id}
                className={"setup-step " + (i === stepIdx ? "active" : "") + (s.done ? " done" : "")}
                onClick={() => setStepIdx(i)}
              >
                <span className="setup-step-no">{s.done ? "✓" : i + 1}</span>
                <span>
                  <b>{s.title}</b>
                  <i className="muted">{s.required ? "必填" : "可选"}</i>
                </span>
              </button>
            ))}
          </nav>

          <section className="setup-body">
            {step && (
              <>
                <h3>{step.title}</h3>
                {STEP_GUIDE[step.id] ? (
                  <div className="guide-box">
                    <p className="guide-goal">{STEP_GUIDE[step.id].goal}</p>
                    <ol className="guide-steps">
                      {STEP_GUIDE[step.id].steps.map((s, i) => (
                        <li key={i}>{s}</li>
                      ))}
                    </ol>
                    <p className="guide-expect">
                      <b>完成标志：</b>
                      {STEP_GUIDE[step.id].expect}
                    </p>
                    {STEP_GUIDE[step.id].tip && (
                      <p className="guide-tip">
                        <b>提示：</b>
                        {STEP_GUIDE[step.id].tip}
                      </p>
                    )}
                  </div>
                ) : (
                  <p className="muted">{step.summary}</p>
                )}

                {checksFor(CHECKS_BY_STEP[step.id] ?? []).map(renderCheck)}

                {step.id === "env" && (
                  <div className="muted">
                    解析器解释器：{status?.parser_python ?? "当前解释器（未配置 .venv-parsers）"}
                  </div>
                )}

                {step.id === "model" && (
                  <div className="setup-stack">
                    {providers.map((p: any, idx: number) => (
                      <div className="provider-card" key={p.id || idx}>
                        <div className="form-grid">
                          <label><span>供应商 id</span>
                            <input value={p.id} disabled={!!p.key_masked && !!p.models?.length}
                              onChange={(e) => setProviders((list) => list.map((x, i) => (i === idx ? { ...x, id: e.target.value } : x)))} />
                          </label>
                          <label><span>名称</span>
                            <input value={p.label ?? ""} placeholder={p.id}
                              onChange={(e) => setProviders((list) => list.map((x, i) => (i === idx ? { ...x, label: e.target.value } : x)))} />
                          </label>
                          <label><span>base_url</span>
                            <input value={p.base_url ?? ""}
                              onChange={(e) => setProviders((list) => list.map((x, i) => (i === idx ? { ...x, base_url: e.target.value } : x)))} />
                          </label>
                          <label><span>API Key（留空不改）</span>
                            <input type="password" value={p.api_key ?? ""} placeholder={p.key_masked || "sk-…"}
                              onChange={(e) => setProviders((list) => list.map((x, i) => (i === idx ? { ...x, api_key: e.target.value } : x)))} />
                          </label>
                          <label><span>环境变量名（推荐）</span>
                            <input value={p.api_key_env ?? ""} placeholder="OPENAI_API_KEY"
                              onChange={(e) => setProviders((list) => list.map((x, i) => (i === idx ? { ...x, api_key_env: e.target.value } : x)))} />
                          </label>
                          <label><span>模型 id（每行一个）</span>
                            <textarea rows={3} value={p.modelsText ?? ""}
                              onChange={(e) => setProviders((list) => list.map((x, i) => (i === idx ? { ...x, modelsText: e.target.value } : x)))} />
                          </label>
                        </div>
                        <div className="row-actions">
                          <button className="primary" disabled={busy} onClick={() => saveProvider(p)}>保存</button>
                          <button disabled={busy} onClick={() => void testProvider(p)}>测试连接</button>
                          <button className="danger" disabled={busy}
                            onClick={() => {
                              if (!window.confirm("删除供应商 " + p.id + "？")) return;
                              void run(async () => {
                                await api.deleteProvider(p.id);
                              }, "供应商已删除");
                            }}>
                            删除
                          </button>
                          <span className="muted">
                            {p.key_configured ? "密钥已配置（" + (p.key_source === "env" ? "环境变量" : "本地明文") + "）" : "密钥未配置"}
                          </span>
                        </div>
                      </div>
                    ))}

                    {draft ? (
                      <div className="provider-card">
                        <h3>新供应商</h3>
                        <div className="form-grid">
                          <label><span>供应商 id</span>
                            <input value={draft.id} placeholder="openai"
                              onChange={(e) => setDraft({ ...draft, id: e.target.value })} />
                          </label>
                          <label><span>base_url</span>
                            <input value={draft.base_url}
                              onChange={(e) => setDraft({ ...draft, base_url: e.target.value })} />
                          </label>
                          <label><span>API Key</span>
                            <input type="password" value={draft.api_key}
                              onChange={(e) => setDraft({ ...draft, api_key: e.target.value })} />
                          </label>
                          <label><span>环境变量名（推荐）</span>
                            <input value={draft.api_key_env} placeholder="OPENAI_API_KEY"
                              onChange={(e) => setDraft({ ...draft, api_key_env: e.target.value })} />
                          </label>
                          <label><span>模型 id（每行一个）</span>
                            <textarea rows={3} value={draft.modelsText}
                              onChange={(e) => setDraft({ ...draft, modelsText: e.target.value })} />
                          </label>
                        </div>
                        <div className="row-actions">
                          <button className="primary" disabled={busy}
                            onClick={() => {
                              const p = { ...draft, key_masked: null, key_configured: false };
                              setProviders((list) => [...list, p]);
                              setDraft(null);
                              saveProvider(p);
                            }}>
                            保存
                          </button>
                          <button onClick={() => setDraft(null)}>取消</button>
                        </div>
                      </div>
                    ) : (
                      <button onClick={() => setDraft({ ...EMPTY_PROVIDER })}>＋ 添加供应商</button>
                    )}
                    {testMsg && <div className="muted">{testMsg}</div>}
                  </div>
                )}

                {step.id === "routes" && (
                  <div className="setup-stack">
                    <div className="form-grid">
                      {(status?.slots ?? []).map((s: any) => (
                        <label key={s.id}>
                          <span>{s.label}</span>
                          <select value={routes[s.id] ?? ""}
                            onChange={(e) => setRoutes({ ...routes, [s.id]: e.target.value || null })}>
                            <option value="">（回退 default）</option>
                            {modelOptions.map((o: any) => (
                              <option key={o.value} value={o.value}>
                                {o.value}
                                {s.id === "agent" && !o.tools ? "（缺 tools）" : ""}
                                {s.id === "ocr" && !o.vision ? "（缺 vision）" : ""}
                              </option>
                            ))}
                          </select>
                        </label>
                      ))}
                    </div>
                    <button className="primary" disabled={busy}
                      onClick={() => void run(async () => {
                        await api.updateRoutes(routes);
                      }, "任务路由已保存")}>
                      保存路由
                    </button>
                  </div>
                )}

                {step.id === "parser" && (
                  <div className="setup-stack">
                    <div className="form-grid">
                      <label><span>解析器</span>
                        <select value={form.parser?.default ?? "auto"}
                          onChange={(e) => setForm({ ...form, parser: { ...form.parser, default: e.target.value } })}>
                          {(status?.options?.parsers ?? []).map((p: string) => (
                            <option key={p} value={p}>
                              {p}
                              {p !== "auto" && status?.parser_available && !status.parser_available[p] ? "（未安装）" : ""}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label><span>扫描页 OCR</span>
                        <select value={form.parser?.ocr_mode ?? "auto"}
                          onChange={(e) => setForm({ ...form, parser: { ...form.parser, ocr_mode: e.target.value } })}>
                          {(status?.options?.ocr_modes ?? []).map((m: string) => (
                            <option key={m} value={m}>{m}</option>
                          ))}
                        </select>
                      </label>
                      <label><span>解析器解释器（可选）</span>
                        <input value={form.parser?.python ?? ""} placeholder=".venv-parsers\\Scripts\\python.exe"
                          onChange={(e) => setForm({ ...form, parser: { ...form.parser, python: e.target.value || null } })} />
                      </label>
                    </div>
                    <button className="primary" disabled={busy}
                      onClick={() => saveAppSettings({ parser: form.parser }, "解析器设置已保存")}>
                      保存
                    </button>
                  </div>
                )}

                {step.id === "output" && (
                  <div className="setup-stack">
                    <div className="form-grid">
                      <label><span>笔记输出语言</span>
                        <select value={form.generate?.output_language ?? "zh"}
                          onChange={(e) => setForm({ ...form, generate: { ...form.generate, output_language: e.target.value } })}>
                          {(status?.options?.languages ?? []).map((l: string) => (
                            <option key={l} value={l}>{l}</option>
                          ))}
                        </select>
                      </label>
                      <label><span>导出编码</span>
                        <select value={form.export?.encoding ?? "utf-8"}
                          onChange={(e) => setForm({ ...form, export: { ...form.export, encoding: e.target.value } })}>
                          {(status?.options?.encodings ?? []).map((enc: string) => (
                            <option key={enc} value={enc}>{enc}</option>
                          ))}
                        </select>
                      </label>
                    </div>
                    <button className="primary" disabled={busy}
                      onClick={() => saveAppSettings(
                        { generate: { output_language: form.generate?.output_language }, export: form.export },
                        "输出设置已保存")}>
                      保存
                    </button>
                  </div>
                )}

                {step.id === "budget" && (
                  <div className="setup-stack">
                    <div className="form-grid">
                      <label><span>并发</span>
                        <input type="number" min={1} max={16} value={form.generate?.concurrency ?? 2}
                          onChange={(e) => setForm({ ...form, generate: { ...form.generate, concurrency: Number(e.target.value) } })} />
                      </label>
                      <label><span>单章成本上限（USD，0=不限）</span>
                        <input type="number" min={0} step={0.01} value={form.generate?.max_cost_per_chapter ?? 0}
                          onChange={(e) => setForm({ ...form, generate: { ...form.generate, max_cost_per_chapter: Number(e.target.value) } })} />
                      </label>
                      <label><span>温度</span>
                        <input type="number" min={0} max={2} step={0.1} value={form.generate?.temperature ?? 0.2}
                          onChange={(e) => setForm({ ...form, generate: { ...form.generate, temperature: Number(e.target.value) } })} />
                      </label>
                    </div>
                    <button className="primary" disabled={busy}
                      onClick={() => saveAppSettings(
                        { generate: {
                            concurrency: form.generate?.concurrency,
                            max_cost_per_chapter: form.generate?.max_cost_per_chapter,
                            temperature: form.generate?.temperature,
                          } },
                        "成本设置已保存")}>
                      保存
                    </button>
                    <div className="muted">模型价格（input/output per million）在「设置 → 模型」中配置。</div>
                  </div>
                )}
              </>
            )}
          </section>
        </div>

        <div className="setup-footer">
          <span className="muted">{step?.summary}</span>
          <div className="row-actions">
            <button disabled={stepIdx === 0} onClick={() => setStepIdx((i) => Math.max(0, i - 1))}>上一步</button>
            <button disabled={stepIdx >= steps.length - 1}
              onClick={() => setStepIdx((i) => Math.min(steps.length - 1, i + 1))}>下一步</button>
            <button className="primary" onClick={close}>完成</button>
          </div>
        </div>
      </div>
    </div>
  );
}
