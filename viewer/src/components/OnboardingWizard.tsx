import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useStore } from "../state/store";
import { BRAND, brandKey } from "../lib/brand";

/** 新人引导（首次进入自动弹出）：认识产品 → 界面导览 → 三分钟上手 → 配置自检。

 与「配置指南」分工：本向导讲清楚产品怎么用（教学），配置指南做深度配置（六步自检）。
 看过与否记在 localStorage（lodestar-onboarded），工具栏「使用引导」可随时重看。 */

const ONBOARDED_KEY = brandKey("onboarded");

const FLOW = [
  ["📥 导入讲义", "拖入 PDF（数字版/扫描版）或 PPTX；老师的讲义可以逐份「追加」到同一本书"],
  ["📖 解析分章", "自动抽文字、OCR 扫描页（烂页自动用视觉模型重认）、按目录/标题分章、锚定原文"],
  ["✨ 生成笔记", "每章按学科模板生成知识点卡片（定义/定理/物理定律/算法…），每条都带教材页码出处"],
  ["🧠 复习巩固", "待审核把关、知识点互链、AI 出题自测（可传往年题定向期末风格）"],
  ["📤 导出分享", "Markdown / LaTeX / Anki 闪卡 / Obsidian 库，一键拿到文件位置"],
];

const TOUR: [string, string][] = [
  ["左侧 · 教材区", "PDF 预览与页码跳转；点笔记的「来源」徽章会跳到教材对应位置"],
  ["中间 · 笔记区", "卡片视图随手读，点卡片放大；「源码」视图逐块编辑；「预览」看最终效果"],
  ["右侧 · 关联区", "当前块的来源锚点、知识点互链都在这；选中文字可从教材检索出相关笔记"],
  ["顶部 · 工具栏", "导入/追加讲义、全书生成、AI 出题、待审核（带未审数）、任务记录、导出、配置指南"],
];

const STEPS_3MIN: [string, string][] = [
  ["① 导入", "点「导入讲义」或把 PDF 拖进窗口；等任务走完（解析 → 锚定）"],
  ["② 生成", "顶部选章节 →「生成本章」；或在「学习工具」中选「全书生成」（默认跳过已生成的章，不重复烧钱）"],
  ["③ 复习", "「待审核」确认 AI 块；「学习工具 → AI 出题」自测；「导出」拿到 md/tex/Anki 文件"],
];

export default function OnboardingWizard() {
  const open = useStore((s) => s.onboardOpen);
  const setOpen = useStore((s) => s.openOnboard);
  const openSetup = useStore((s) => s.openSetup);
  const [step, setStep] = useState(0);
  const [status, setStatus] = useState<any | null>(null);

  useEffect(() => {
    if (!open) return;
    setStep(0);
    if (!status) void api.setupStatus().then(setStatus).catch(() => undefined);
    // Esc 关闭与其他弹窗一致（此前关不掉，只能点按钮）
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") done();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const done = () => {
    localStorage.setItem(ONBOARDED_KEY, "1");
    setOpen(false);
  };

  // checks 是列表（_make_check(id, level, ...)，level=ok/warn/bad）——
  // 曾按对象取键导致自检行永远显示「…」
  const checkLevel = (id: string) =>
    (status?.checks ?? []).find((x: any) => x?.id === id)?.level as string | undefined;
  const parserLevel = checkLevel("parser_pypdf") || checkLevel("parser_marker")
    || checkLevel("parser_docling");
  const checks: [string, string | undefined, string][] = [
    ["模型已配置", checkLevel("models"), "生成笔记、AI 出题、AI 编辑都靠它"],
    ["API Key 可用", checkLevel("keys"), "密钥只存本机；推荐用环境变量名"],
    ["任务路由就绪", checkLevel("routes"), "提纲/填充/出题各用哪个模型"],
    ["解析器可用", parserLevel, "PDF/PPTX 导入与分章锚定"],
  ];

  return (
    <div className="modal-mask" onClick={done}>
      <div className="modal wide" onClick={(e) => e.stopPropagation()}>
        <h2>欢迎使用 {BRAND.en} {BRAND.zh}</h2>
        <div className="muted">
          教材进、带出处的笔记出——每一句话都能点回原文页码；人工修改永远不被 AI 覆盖。
        </div>

        {step === 0 && (
          <div style={{ margin: "12px 0" }}>
            {FLOW.map(([t, d]) => (
              <div key={t} style={{ display: "flex", gap: 10, margin: "8px 0" }}>
                <div style={{ minWidth: 130, fontWeight: 600 }}>{t}</div>
                <div className="muted" style={{ flex: 1 }}>{d}</div>
              </div>
            ))}
          </div>
        )}

        {step === 1 && (
          <div style={{ margin: "12px 0" }}>
            {TOUR.map(([t, d]) => (
              <div key={t} style={{ margin: "10px 0" }}>
                <b>{t}</b>
                <div className="muted">{d}</div>
              </div>
            ))}
            <div className="muted">
              快捷键：J/K 上下选块 · E 源码 · S 切换源码/预览 · / 全局搜索 · Ctrl+S 保存草稿 · Esc 关弹窗
            </div>
          </div>
        )}

        {step === 2 && (
          <div style={{ margin: "12px 0" }}>
            {STEPS_3MIN.map(([t, d]) => (
              <div key={t} style={{ margin: "12px 0" }}>
                <b>{t}</b>
                <div className="muted">{d}</div>
              </div>
            ))}
            <div className="muted">
              小贴士：生成的块点「✨AI 改」可以逐块润色（改动可回溯）；点「🔗关联」把相关知识点链成网。
            </div>
          </div>
        )}

        {step === 3 && (
          <div style={{ margin: "12px 0" }}>
            <div className="muted" style={{ marginBottom: 8 }}>
              上手前看一眼配置自检（不达标也能先逛，随时回来配）：
            </div>
            {checks.map(([label, level, hint]) => (
              <div key={label} style={{ display: "flex", gap: 10, margin: "8px 0", alignItems: "baseline" }}>
                <span style={{ minWidth: 24 }}>
                  {!status ? "…" : level === "ok" ? "✅" : level === "warn" ? "⚠️" : "❌"}
                </span>
                <b style={{ minWidth: 110 }}>{label}</b>
                <span className="muted" style={{ flex: 1 }}>{hint}</span>
              </div>
            ))}
            {!status && <div className="muted">正在自检…</div>}
            {status && status.ready && (
              <div className="muted" style={{ marginTop: 8 }}>
                ✅ 配置已就绪——直接开始用吧！
              </div>
            )}
          </div>
        )}

        <div className="export-actions">
          <button onClick={done}>跳过</button>
          {step > 0 && <button onClick={() => setStep(step - 1)}>上一步</button>}
          {step < 3 && (
            <button className="primary" onClick={() => setStep(step + 1)}>下一步</button>
          )}
          {step === 3 && (
            <>
              <button onClick={() => { done(); openSetup(true); }}>
                打开完整配置向导
              </button>
              <button className="primary" onClick={done}>开始使用</button>
            </>
          )}
        </div>
        <div className="muted" style={{ marginTop: 8, textAlign: "center" }}>
          {["认识产品", "界面导览", "三分钟上手", "配置自检"][step]}
          {" · "}第 {step + 1}/4 步（工具栏「使用引导」可随时重看）
        </div>
      </div>
    </div>
  );
}
