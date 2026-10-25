import { useEffect, useRef, useState } from "react";
import { api } from "./api/client";
import Toolbar from "./components/Toolbar";
import PdfPane from "./pdf/PdfPane";
import NotesPane from "./components/NotesPane";
import BlockDetailPanel from "./components/BlockDetailPanel";
import BlockActionModals from "./components/BlockActionModals";
import OnboardingWizard from "./components/OnboardingWizard";
import NoteZoom from "./components/NoteZoom";
import SettingsPanel from "./components/SettingsPanel";
import SetupGuide from "./components/SetupGuide";
import ReviewQueue from "./components/ReviewQueue";
import { useStore } from "./state/store";

// 三栏初始列宽（px）：随窗口宽度给个合理默认
function initialWidths(): [number, number] {
  try {
    const v = JSON.parse(localStorage.getItem("note-gen-col-widths") || "null");
    if (Array.isArray(v) && v.length === 2 && isFinite(v[0]) && isFinite(v[1])) {
      return [Math.max(180, Number(v[0])), Math.max(180, Number(v[1]))];
    }
  } catch {
    /* 忽略损坏的本地设置 */
  }
  return [Math.round(window.innerWidth * 0.36), Math.round(window.innerWidth * 0.32)];
}

export default function App() {
  // 主题：跟随 localStorage，首次进入用浅色
  useEffect(() => {
    const saved = localStorage.getItem("note-gen-theme") || "light";
    document.documentElement.setAttribute("data-theme", saved);
  }, []);

  // 三栏宽度可拖拽调节（列宽记忆到 localStorage）
  const [widths, setWidths] = useState<[number, number]>(initialWidths);
  const colsRef = useRef<HTMLElement>(null);
  useEffect(() => {
    localStorage.setItem("note-gen-col-widths", JSON.stringify(widths));
  }, [widths]);
  // 把宽度夹到 [MIN, 可用上限]：上限 = 容器宽 - 其他两栏最小宽 - 两个拖拽把手
  const clampWidths = (w: [number, number]): [number, number] => {
    const total = colsRef.current?.getBoundingClientRect().width ?? window.innerWidth - 36;
    const MIN = 180;                       // 每栏最小宽度
    const RESERVED = 2 * 12 + 2 * MIN;     // 两个 splitter + 第三栏最小宽
    const max0 = Math.max(MIN, total - w[1] - RESERVED);
    const max1 = Math.max(MIN, total - w[0] - RESERVED);
    return [Math.min(Math.max(w[0], MIN), max0), Math.min(Math.max(w[1], MIN), max1)];
  };

  // 窗口缩小时收回被挤超界的栏宽，避免横向溢出
  useEffect(() => {
    const onResize = () => setWidths((w) => clampWidths(w));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startResize = (idx: 0 | 1) => (e: React.PointerEvent) => {
    e.preventDefault();
    const move = (ev: PointerEvent) => {
      const rect = colsRef.current?.getBoundingClientRect();
      if (!rect) return;
      const x = ev.clientX - rect.left;
      setWidths(([w0, w1]) =>
        idx === 0 ? clampWidths([x, w1]) : clampWidths([w0, x - w0])
      );
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const toast = useStore((s) => s.toast);
  const blocks = useStore((s) => s.blocks);
  const selectedBlockId = useStore((s) => s.selectedBlockId);
  // J/K 切块同样走草稿守卫（Review #6）：源码视图有未保存草稿时不得静默换块，
  // 否则 BlockEditor 卸载即丢草稿；取消（返回 false）时选中块保持不变
  const requestSelectBlock = useStore((s) => s.requestSelectBlock);
  const setView = useStore((s) => s.setView);
  const view = useStore((s) => s.view);
  const openSettings = useStore((s) => s.openSettings);
  const openReview = useStore((s) => s.openReview);
  const dirtyDraft = useStore((s) => s.dirtyDraft);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      // j/k/e/s 单键在输入框/输入法组合输入期间不得触发（7.3）
      const composing = e.isComposing || e.key === "Process" || e.key === "Unidentified";
      const typing =
        !!target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable ||
          composing);

      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("note-gen:save"));
        return;
      }
      if (e.key === "Escape") {
        openSettings(false);
        useStore.getState().openSetup(false);
        openReview(false);
        useStore.getState().setAgent({ plan: null, planId: null });
        return;
      }
      if (typing) return;

      const idx = blocks.findIndex((b) => b.id === selectedBlockId);
      if (e.key === "j") {
        e.preventDefault();
        const next = blocks[Math.min(blocks.length - 1, idx + 1)] ?? blocks[0];
        if (next) requestSelectBlock(next.id);
      } else if (e.key === "k") {
        e.preventDefault();
        const prev = blocks[Math.max(0, idx - 1)] ?? blocks[0];
        if (prev) requestSelectBlock(prev.id);
      } else if (e.key === "e") {
        setView("source");
      } else if (e.key === "s") {
        setView(view === "source" ? "preview" : "source");
      } else if (e.key === "/") {
        e.preventDefault();
        document.getElementById("global-search")?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [blocks, selectedBlockId, view]);

  // 新人引导：首次进入自动弹教学向导（认识产品/界面导览/上手/配置自检）；
  // 看过与否记 localStorage，工具栏「使用引导」可随时重看。
  useEffect(() => {
    if (!localStorage.getItem("note-gen-onboarded")) {
      useStore.getState().openOnboard(true);
      return;
    }
    // 老用户但未就绪：保留原行为自动弹配置指南；手动关闭后不再打扰
    api
      .setupStatus()
      .then((s: any) => {
        if (s.first_run && !s.ready && !localStorage.getItem("note-gen-setup-seen")) {
          useStore.getState().openSetup(true);
        }
      })
      .catch(() => undefined);
  }, []);

  // 脏草稿守卫（7.3）：关页前提示
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (dirtyDraft !== null) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirtyDraft]);

  return (
    <div className="app">
      <Toolbar />
      <main className="columns" ref={colsRef}>
        <div className="col" style={{ flex: "0 0 " + widths[0] + "px" }}>
          <PdfPane />
        </div>
        <div className="splitter" title="拖动调节栏宽" onPointerDown={startResize(0)} />
        <div className="col" style={{ flex: "0 0 " + widths[1] + "px" }}>
          <NotesPane />
        </div>
        <div className="splitter" title="拖动调节栏宽" onPointerDown={startResize(1)} />
        <div className="col" style={{ flex: "1 1 0" }}>
          <BlockDetailPanel />
        </div>
      </main>
      <NoteZoom />
      <BlockActionModals />
      <SettingsPanel />
      <SetupGuide />
      <OnboardingWizard />
      <ReviewQueue />
      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
