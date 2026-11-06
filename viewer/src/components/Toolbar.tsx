import Icon from "./Icon";
import ActionMenu from "./ActionMenu";
import GlobalSearch from "./GlobalSearch";
import { lazy, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useStore } from "../state/store";
import DeferredPanel from "./DeferredPanel";
const KnowledgePanel = lazy(() => import("./KnowledgePanel"));
const TemplateStudio = lazy(() => import("./TemplateStudio"));
const ShareModal = lazy(() => import("./ShareModal"));

// 任务进度可读化（9.2 进度透传）：提纲 2/6 批、填充 3/20、生成 ch01
function progressText(p: any): string {
  if (!p) return "";
  if (p.stage === "outline_start") return "提纲 0/" + (p.batches ?? 0) + " 批";
  if (p.stage === "outline" && p.batches) return "提纲 " + (p.batch ?? 0) + "/" + p.batches + " 批";
  if (p.stage === "outline_done") return "提纲完成 " + (p.count ?? 0) + " 项";
  if (p.stage === "fill_retry") return "重试失败块…（" + (p.count ?? 0) + " 个）";
  if (p.stage === "fill" && p.total) return "填充 " + (p.done ?? 0) + "/" + p.total;
  if (p.stage === "link") return "提交与链接…";
  if (p.stage === "done") return "完成";
  if (p.stage === "generate") return p.chapter_id ? "生成 " + p.chapter_id : "生成中";
  if (p.stage === "ingest" && p.phase === "convert") return "解析器转换中…（重型解析器，页数多时较久）";
  if (p.stage === "ingest" && p.phase === "ocr")
    return "扫描页 OCR 识别中…（" + (p.pages_total ?? 0) + " 页，页数多时较久）";
  if (p.stage === "ingest" && p.pages_total) return "解析 " + (p.pages_done ?? 0) + "/" + p.pages_total + " 页";
  if (p.stage === "anchor" && p.total) return "锚定 " + (p.done ?? 0) + "/" + p.total + " 章";
  return String(p.stage ?? "");
}

// 已运行时长（任务启动体感）：任务一开始就有秒表在走，不再「点了没反应」
function fmtElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

// 任务进度百分比（进度条）：按阶段映射，未知阶段返回 null（走无限动画）
function progressPercent(kind: string, p: any): number | null {
  if (!p) return null;
  if (kind === "generate") {
    if (p.stage === "generate") return 4;
    if (p.stage === "outline_start") return 6;
    if (p.stage === "outline" && p.batches) return 6 + Math.round(((p.batch ?? 0) / p.batches) * 26);
    if (p.stage === "outline_done") return 34;
    if (p.stage === "fill_retry") return 36;
    if (p.stage === "fill" && p.total) return 36 + Math.round(((p.done ?? 0) / p.total) * 60);
    if (p.stage === "link") return 98;
  }
  if (kind === "import") {
    // 解析占 5-90%、锚定 90-98%：进度条与「解析 363/364 页」文字必须对得上；
    // 转换期（重型解析器整书转换，页数多时十几分钟）单独显示状态不显示假进度
    if (p.stage === "ingest" && p.phase === "convert") return 4;
    if (p.stage === "ingest" && p.phase === "ocr") return 88;   // 解析段尾部，条与状态一致
    if (p.stage === "ingest" && p.pages_total) {
      return 5 + Math.round(((p.pages_done ?? 0) / p.pages_total) * 85);
    }
    if (p.stage === "ingest") return 15;
    if (p.stage === "anchor" && p.total) {
      return 90 + Math.round(((p.done ?? 0) / p.total) * 8);
    }
    if (p.stage === "anchor") return 92;
  }
  if (kind === "export" && p.stage === "export") return 60;
  return null;
}

// 界面配色（6 套）
const THEMES: [string, string][] = [
  ["light", "明亮"],
  ["dark", "深色"],
  ["sepia", "羊皮纸"],
  ["mint", "护眼绿"],
  ["sky", "天空蓝"],
  ["lavender", "薰衣草"],
];

export default function Toolbar() {
  const docs = useStore((s) => s.docs);
  const docId = useStore((s) => s.docId);
  const manifest = useStore((s) => s.manifest);
  const chapterId = useStore((s) => s.chapterId);
  const dirtyDraft = useStore((s) => s.dirtyDraft);
  const loadDocs = useStore((s) => s.loadDocs);
  const selectDoc = useStore((s) => s.selectDoc);
  const selectChapter = useStore((s) => s.selectChapter);
  const openSettings = useStore((s) => s.openSettings);
  const openSetup = useStore((s) => s.openSetup);
  const openReview = useStore((s) => s.openReview);
  const reviewCount = useStore((s) => s.reviewCount);
  const refreshReviewCount = useStore((s) => s.refreshReviewCount);
  const setToast = useStore((s) => s.setToast);
  // 阅读布局开关从原来的独立「阅读工作台」横条收进工具栏第二行：那一整条
  // 只为放一个开关就占了 48px，头部一压就是两行品牌区的高度。
  const focusMode = useStore((s) => s.readingMode === "focus");
  const setReadingMode = useStore((s) => s.setReadingMode);
  const [tasks, setTasks] = useState<any[]>([]);
  const timersRef = useRef<Set<number>>(new Set());
  const [dragOver, setDragOver] = useState(false);
  const dragDepth = useRef(0);

  // 拖拽上传 PDF（全窗口落点）：拖入即高亮，松手触发与「导入 PDF」同一条导入流
  useEffect(() => {
    const hasFiles = (e: DragEvent) =>
      Array.from(e.dataTransfer?.types ?? []).includes("Files");
    const onDragEnter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragDepth.current += 1;
      setDragOver(true);
    };
    const onDragOver = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();              // 不拦截默认行为就无法触发 drop
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    };
    const onDragLeave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      dragDepth.current = Math.max(0, dragDepth.current - 1);
      if (dragDepth.current === 0) setDragOver(false);
    };
    const onDrop = (e: DragEvent) => {
      e.preventDefault();
      dragDepth.current = 0;
      setDragOver(false);
      const file = e.dataTransfer?.files?.[0];
      if (!file) return;
      const isPdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name);
      const isPptx = /\.pptx$/i.test(file.name)
        || file.type === "application/vnd.openxmlformats-officedocument.presentationml.presentation";
      if (!isPdf && !isPptx) {
        setToast("只支持 PDF 与 PPTX 讲义：" + file.name);
        return;
      }
      void importPdf(file);
    };
    window.addEventListener("dragenter", onDragEnter);
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("dragleave", onDragLeave);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onDragEnter);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("dragleave", onDragLeave);
      window.removeEventListener("drop", onDrop);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 组件卸载时清掉所有轮询定时器，避免僵尸请求/内存泄漏
  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      timers.forEach((t) => window.clearInterval(t));
      timers.clear();
    };
  }, []);

  // 任务收尾（9.2 反馈）：生成完的块要立刻可见，不用用户手动刷新页面
  const refreshAfterTask = (kind: string, t: any) => {
    const st = useStore.getState();
    if (kind === "generate") {
      void st.loadBlocks();
      if (t.status === "succeeded") setToast("生成完成，笔记已刷新");
      else if (t.status === "partial") setToast("生成完成（部分块失败，详见「生成记录」），已刷新");
    } else if (kind === "import" || kind === "rebuild") {
      void st.loadDocs().then(() => {
        const newDocId = t.result?.doc_id;
        if (!newDocId) return;
        // 草稿守卫（Review #6 残余）：这是后台任务完成事件，selectDoc 会清空选中块并
        // 卸载源码/预览里的 BlockEditor。有未保存草稿时不得静默换书，让用户自己决定何时切。
        if (useStore.getState().dirtyDraft !== null) {
          setToast(
            (kind === "rebuild" ? "重建完成" : "导入完成") +
              "（当前块有未保存草稿，未自动切换教材；保存后可在顶栏切换）"
          );
          return;
        }
        void st.selectDoc(newDocId);
      });
      if (t.status === "succeeded") {
        setToast(kind === "rebuild" ? "重建完成，已打开文档" : "导入完成，已打开文档");
      }
    } else if (kind === "export" && t.status === "succeeded") {
      // 「导出的文件在哪」必须答清：弹窗列文件 + 目录 + 一键打开文件夹
      const paths: string[] = (t.result?.paths ?? []).map((p: string) => String(p));
      const dir = paths[0] ? paths[0].split(/[\\/]/).slice(0, -1).join("\\") : "";
      setExportResult({
        files: paths.map((p) => p.split(/[\\/]/).pop() ?? p),
        dir,
      });
      setToast("导出完成：" + (paths.length || 0) + " 个文件");
    }
    if (t.status === "failed") {
      setToast((kind === "generate" ? "生成失败：" : "任务失败：") + (t.error?.message ?? t.status));
    }
    // 生成完可能落了 needs_review 块：顺手刷新未审核角标
    void useStore.getState().refreshReviewCount();
  };

  // 任务进度/取消（9.2 /tasks/{id}）；params 供「继续生成」续跑（doc_id/chapter_id）
  const pollingRef = useRef<Set<string>>(new Set());
  const pollTask = (taskId: string, kind: string) => {
    if (pollingRef.current.has(taskId)) return;   // 同一任务只挂一个轮询器
    pollingRef.current.add(taskId);
    // 连续失败容忍：曾一次接口抖动（含服务重启）就永久停掉轮询器——
    // 任务行从此冻结在「运行中 · 最后一步」，实际任务早已跑完（用户实测「老是卡最后一步」）
    let misses = 0;
    const timer = window.setInterval(async () => {
      try {
        const t = await api.getTask(taskId);
        misses = 0;
        setTasks((list) => list.map((x) => {
          if (x.task_id !== taskId) return x;
          const pct = progressPercent(kind, t.progress);
          // 进度只进不退：提纲/填充流水线阶段交错上报，瞬时 percent 可能回落
          return { ...x, ...t, bestPct: Math.max(x.bestPct ?? 0, pct ?? 0) };
        }));
        if (["succeeded", "failed", "partial", "cancelled", "interrupted"].includes(t.status)) {
          window.clearInterval(timer);
          timersRef.current.delete(timer);
          pollingRef.current.delete(taskId);
          refreshAfterTask(kind, t);
        }
      } catch (e: any) {
        misses += 1;
        // 任务已被删除（404）：移除该行；其余错误重试 5 次再放弃（防永久冻结）
        if (e?.status === 404) {
          window.clearInterval(timer);
          timersRef.current.delete(timer);
          pollingRef.current.delete(taskId);
          setTasks((list) => list.filter((x) => x.task_id !== taskId));
          return;
        }
        if (misses < 5) return;
        window.clearInterval(timer);
        timersRef.current.delete(timer);
        pollingRef.current.delete(taskId);
      }
    }, 1000);
    timersRef.current.add(timer);
  };

  const track = (taskId: string, kind: string, params?: any, name?: string) => {
    setTasks((list) => [
      ...list.filter((x) => x.task_id !== taskId),
      { task_id: taskId, kind, name: name ?? null, status: "queued", progress: {},
        params: params ?? null, startedAt: Date.now(), bestPct: 0 },
    ]);
    pollTask(taskId, kind);
  };

  // 持久任务恢复（runtime.db）：刷新/重启后任务记录不丢，未完任务自动续挂轮询
  const loadTasks = async () => {
    try {
      const { items } = await api.listTasks(docId);
      // 全部任务都显示（用户要求）：不需要的记录用「删除」清理
      const fresh = (items ?? []).filter((t: any) => t.task_id);
      setTasks((list) => {
        const seen = new Set(fresh.map((t: any) => t.task_id));
        const locals = list.filter((x) => !seen.has(x.task_id));
        const merged = fresh.map((t: any) => {
          const old = list.find((x) => x.task_id === t.task_id);
          return {
            task_id: t.task_id,
            kind: t.kind,
            name: t.name ?? old?.name ?? null,
            status: t.status,
            progress: t.progress ?? {},
            params: t.request ?? old?.params ?? null,
            error: t.error ?? old?.error,
            bestPct: old?.bestPct ?? 0,
            startedAt: old?.startedAt ?? (Date.parse(t.created_at ?? "") || Date.now()),
          };
        });
        return [...locals, ...merged];
      });
      for (const t of fresh) {
        if (["queued", "running"].includes(t.status)) pollTask(t.task_id, t.kind);
      }
    } catch {
      /* 列表失败不阻塞主界面 */
    }
  };

  const statusLabel = (t: any) =>
    ({ queued: "排队中", running: "运行中", succeeded: "完成", partial: "部分完成",
       failed: "失败", interrupted: "已中断",
       cancelled: t.kind === "generate" ? "已暂停" : "已取消" } as Record<string, string>)[t.status] ?? t.status;

  const resumeGenerate = async (t: any) => {
    try {
      // 原记录原地续跑（同 task_id/名字，不开新记录）：引擎吃掉已完成的块、只补缺口
      await api.resumeTask(t.task_id);
      setTasks((list) =>
        list.map((x) => (x.task_id === t.task_id
          // 计时重置：「已运行」只算本次运行，不再把暂停时长混进去
          ? { ...x, status: "queued", error: null, startedAt: Date.now() }
          : x)));
      pollTask(t.task_id, "generate");
      setToast("继续生成：复用已完成的块，只补未完成部分");
    } catch (e: any) {
      setToast("继续生成失败：" + e.message);
    }
  };

  useEffect(() => {
    void loadDocs();
    // 块类型注册表（学科模板/自定义）：卡片图标与生成同一事实源
    void api.blockTypes().then((r: any) => {
      const map: Record<string, { name: string; icon: string }> = {};
      for (const t of r.types ?? []) map[t.id] = { name: t.name, icon: t.icon };
      useStore.setState({ typeInfo: map });
    }).catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 切换教材/刷新页面：从持久层恢复任务记录（中断的生成保留在列表里，可续跑）
  useEffect(() => {
    void loadTasks();
    void refreshReviewCount();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId]);

  // 兜底同步：轮询器是尽力而为，定期拉服务端真相同步——任何冻结/失联自愈
  useEffect(() => {
    const t = window.setInterval(() => {
      void loadTasks();
      void refreshReviewCount();
    }, 30000);
    timersRef.current.add(t);
    return () => {
      window.clearInterval(t);
      timersRef.current.delete(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId]);

  // 脏草稿守卫（7.3）：切章/换教材必须提示，不能静默丢弃。
  // P3-7：确认后不在这里清标记——交给 selectDoc/selectChapter 成功切换时清；
  // 切换请求失败时草稿仍在编辑器里，标记必须保留（否则再切块会静默丢草稿）。
  const guardDirty = (): boolean => {
    if (dirtyDraft === null) return true;
    return window.confirm("当前块有未保存草稿，切换将丢弃草稿，继续吗？");
  };

  const doExport = async (format: string, scope: "book" | "chapter") => {
    if (!docId) return;
    if (scope === "chapter" && !chapterId) {
      setToast("请先选择要导出的章节");
      return;
    }
    try {
      const { task_id } = await api.exportDoc({
        doc_id: docId,
        format,
        ...(scope === "chapter" && chapterId ? { chapter_id: chapterId } : {}),
      });
      track(task_id, "export");
      setExportOpen(false);
      setToast("导出任务已启动：" + task_id);
    } catch (e: any) {
      setToast("导出失败：" + e.message);
    }
  };

  const doGenerate = async () => {
    if (!docId) return;
    try {
      const custom = genName.trim();
      const { task_id } = await api.generate(docId, { chapter_id: chapterId, name: custom || undefined });
      const st = useStore.getState();
      const docTitle = st.docs.find((d) => d.doc_id === docId)?.title ?? docId;
      const chTitle = (st.manifest?.chapters ?? []).find(
        (c: any) => c.chapter_id === chapterId)?.title;
      track(task_id, "generate", { doc_id: docId, chapter_id: chapterId },
        custom || (docTitle + " · " + (chTitle ?? chapterId ?? "整本")));
      setToast("生成任务已启动：" + (custom || task_id));
    } catch (e: any) {
      setToast("生成失败：" + e.message);
    }
  };

  const doGenerateBook = async (skipDone: boolean) => {
    if (!docId) return;
    try {
      const custom = genName.trim();
      const { task_id } = await api.generate(docId, {
        name: custom || undefined,
        skip_done: skipDone,
      });
      setBookOpen(false);
      const st = useStore.getState();
      const docTitle = st.docs.find((d) => d.doc_id === docId)?.title ?? docId;
      track(task_id, "generate", { doc_id: docId, chapter_id: null },
        custom || (docTitle + " · 整本"));
      setToast("全书生成已启动：" + (custom || docTitle + " · 整本"));
    } catch (e: any) {
      setToast("生成失败：" + e.message);
    }
  };

  const appendLecture = async (file: File | null) => {
    if (!file || !docId) return;
    if (!/\.pdf$/i.test(file.name)) {
      setToast("追加只支持 PDF 讲义（PPTX 请合并后重新导入）");
      return;
    }
    try {
      const { task_id } = await api.appendPdf(docId, file);
      track(task_id, "import");
      setToast("追加任务已启动：新讲义将接到本书尾部");
    } catch (e: any) {
      setToast("追加失败：" + e.message);
    }
  };

  const importPdf = async (file: File | null) => {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    try {
      const resp = await api.post<any>("/api/v1/docs", form);
      track(resp.task_id, "import");
      setToast("导入任务已启动：" + resp.task_id);
      await loadDocs();
    } catch (e: any) {
      setToast("导入失败：" + e.message);
    }
  };

  const [theme, setTheme] = useState<string>(
    () => localStorage.getItem("note-gen-theme") || "light"
  );
  const [genName, setGenName] = useState("");   // 生成任务手动命名（可选）
  const [deleted, setDeleted] = useState<null | {
    title: string;
    removed: Record<string, number>;
    trashedTo: string;
  }>(null);
  const [records, setRecords] = useState<any[] | null>(null);
  // 统一导出弹窗：所有导出共享一个入口，点进去选格式与范围
  const [exportOpen, setExportOpen] = useState(false);
  // 分享：复制 Markdown / 下载离线单文件 HTML（公式字体与插图内嵌）
  const [shareOpen, setShareOpen] = useState(false);
  const [knowledgeOpen, setKnowledgeOpen] = useState(false);
  const [templatesOpen, setTemplatesOpen] = useState(false);
  const [expFormat, setExpFormat] = useState("md");
  const [expScope, setExpScope] = useState<"book" | "chapter">("book");
  // 导出完成提示：文件在哪 + 一键打开文件夹
  const [exportResult, setExportResult] = useState<{ files: string[]; dir: string } | null>(null);
  // AI 出题：选项 + 结果 + 参考题库
  const [quizOpen, setQuizOpen] = useState(false);
  const [quizBusy, setQuizBusy] = useState(false);
  const [quiz, setQuiz] = useState<any | null>(null);
  const [quizKinds, setQuizKinds] = useState<string[]>(["choice", "short"]);
  const [quizCount, setQuizCount] = useState(10);
  const [quizDifficulty, setQuizDifficulty] = useState("期末水平");
  const [quizScope, setQuizScope] = useState<"book" | "chapter">("chapter");
  const [qbankCount, setQbankCount] = useState(0);
  const [showAnswers, setShowAnswers] = useState<Record<string, boolean>>({});

  const openQuiz = async () => {
    if (!docId) return;
    setQuizOpen(true);
    setQuiz(null);
    setShowAnswers({});
    try {
      const r = await api.qbankList(docId);
      setQbankCount(r.count ?? 0);
    } catch {
      setQbankCount(0);
    }
  };

  const runQuiz = async () => {
    if (!docId) return;
    if (!quizKinds.length) {
      setToast("至少选一种题型");
      return;
    }
    if (quizScope === "chapter" && !chapterId) {
      // 不静默改成整本：用户以为在考本章，实际拿到全书卷 = 范围错觉
      setToast("选了「仅当前章节」但还没选章节——请在顶部选章节，或改选整本范围");
      return;
    }
    setQuizBusy(true);
    try {
      const r = await api.quiz(docId, {
        chapter_id: quizScope === "chapter" ? chapterId : undefined,
        kinds: quizKinds,
        count: quizCount,
        difficulty: quizDifficulty,
      });
      setQuiz(r);
      setShowAnswers({});
    } catch (e: any) {
      setToast("出题失败：" + e.message);
    } finally {
      setQuizBusy(false);
    }
  };
  // 全书生成选项弹窗：跳过已生成章节（默认）/ 全量重生成
  const [bookOpen, setBookOpen] = useState(false);
  const [regenAll, setRegenAll] = useState(false);
  // 教材改名
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameText, setRenameText] = useState("");
  const doRename = async () => {
    if (!docId) return;
    const title = renameText.trim();
    if (!title) {
      setToast("名称不能为空");
      return;
    }
    try {
      await api.renameDoc(docId, title);
      await loadDocs();
      useStore.setState({ manifest: { ...(useStore.getState().manifest ?? {}), title } });
      setRenameOpen(false);
      setToast("已改名：" + title);
    } catch (e: any) {
      setToast("改名失败：" + e.message);
    }
  };

  // 生成记录（9.2 报告透传）：各章生成时间/模型/结果
  const openRecords = async () => {
    if (!docId) {
      setToast("请先选择教材");
      return;
    }
    try {
      const r = await api.generateReports(docId);
      setRecords(r.reports ?? []);
    } catch (e: any) {
      setToast("生成记录加载失败：" + e.message);
    }
  };

  const applyTheme = (next: string) => {
    setTheme(next);
    localStorage.setItem("note-gen-theme", next);
    document.documentElement.setAttribute("data-theme", next);
  };

  return (
    <>
      <header className="toolbar">
        <div className="toolbar-top">
          <div className="brand" title="note-gen · 自动笔记软件">
            <span className="logo"><Icon name="book" size={16} /></span>
            <span className="brand-copy"><strong>note-gen<span className="brand-dot">.</span></strong></span>
          </div>
          <GlobalSearch />

          <div className="toolbar-utilities">
            <select
              className="theme-select" aria-label="界面配色"
              value={theme}
              onChange={(e) => applyTheme(e.target.value)}
              title="界面配色"
            >
              {THEMES.map(([k,label]) => (
                <option key={k} value={k}>{label}</option>
              ))}
            </select>
            <ActionMenu label="帮助">
              <button onClick={() => useStore.setState({ onboardOpen: true })}
                title="使用引导：认识产品、界面导览、三分钟上手">使用引导</button>
              <button onClick={() => openSetup(true)} title="配置指南：环境自检与首次使用引导">配置指南</button>
            </ActionMenu>
            <button className="settings-button" title="设置" onClick={() => openSettings(true)}><Icon name="settings" size={17} />设置</button>
          </div>
        </div>
        <div className="workspace-actions">
          <div className="document-context">
            <Icon name="book" size={17} />
            <select className="document-select" aria-label="选择教材"
              value={docId??""}
              onChange={(e) => {
                if(!guardDirty()) return;
                void selectDoc(e.target.value);
              }}
            >
              <option value="">— 选择教材 —</option>
              {docs.map((d) => (
                <option key={d.doc_id} value={d.doc_id}>{d.title}</option>
              ))}
            </select>
            <span className="context-divider">/</span>
            <select className="chapter-select" aria-label="选择章节"
              value={chapterId??""}
              onChange={(e) => {
                if(!guardDirty()) return;
                void selectChapter(e.target.value);
              }}
            >
              {(manifest?.chapters??[]).map((c: any) => (
                <option key={c.chapter_id} value={c.chapter_id}>{c.title}</option>
              ))}
            </select>

            <label className="file-button import-button" title="导入 PDF（数字版/扫描版）或 PPTX 讲义">
              <Icon name="upload" size={16} />导入讲义
              <input type="file" accept="application/pdf,.pptx" hidden
                onChange={(e) => void importPdf(e.target.files?.[0]??null)} />
            </label>
            <ActionMenu label="教材管理">
              {docId&&(
                <button
                  title="给这本教材改名（只改显示名称，不影响内容）"
                  onClick={() => {
                    setRenameText(manifest?.title??docs.find((d) => d.doc_id===docId)?.title??"");
                    setRenameOpen(true);
                  }}
                >
                  改名
                </button>
              )}

              {docId&&(
                <label className="file-button"
                  title="把新的讲义 PDF 追加到本教材尾部：已有笔记、生成记录全部保留，新页带来新章节">
                  追加讲义
                  <input type="file" accept="application/pdf" hidden
                    onChange={(e) => void appendLecture(e.target.files?.[0]??null)} />
                </label>
              )}

              {docId&&(
                <button
                  className="danger"
                  title="删除该教材（移入回收站 data/.trash，可找回）"
                  onClick={() => {
                    const doc=docs.find((d) => d.doc_id===docId);
                    const chapters=doc?.chapters?.length??0;
                    const ok=window.confirm(
                      "确认删除教材《"+(doc?.title??docId)+"》？\n\n"+
                      "· 将删除其全部笔记块、锚点、历史与图资源（约 "+chapters+" 章）\n"+
                      "· 移入回收站 data/.trash，可在磁盘上找回\n"+
                      "· 此操作会立即从列表中移除该教材"+
                      // 草稿守卫（Review #6 残余）：删书会卸载编辑器，草稿只存在内存里，删了找不回
                      (dirtyDraft!==null? "\n· 当前块有未保存草稿，删除后无法找回":"")
                    );
                    if(!ok) return;
                    void api
                      .deleteDoc(docId)
                      .then(async (resp: any) => {
                        setToast("已删除《"+(doc?.title??docId)+"》，已移入回收站（data/.trash 可找回）");
                        // 文档已删：选中块随之失效、编辑器卸载。草稿标记也要一起清掉，
                        // 否则残留的 dirtyDraft 会让之后每次切块都误报「有未保存草稿」。
                        useStore.setState({
                          docId: null,manifest: null,chapterId: null,
                          blocks: [],selectedBlockId: null,
                          dirtyDraft: null,zoomBlockId: null
                        });
                        await loadDocs();
                        // 删除反馈（结果对话框）：删了什么、删到哪、能否找回，一次说清
                        setDeleted({
                          title: doc?.title??docId,
                          removed: resp?.removed??{},
                          trashedTo: resp?.trashed_to??"data/.trash",
                        });
                      })
                      .catch((e: any) => setToast("删除失败："+e.message));
                  }}
                >
                  删除教材
                </button>
              )}

              {docId&&(
                <button
                  title="重建导入：重新解析 PDF 并重新识别章节（无需重新上传）。仅当文档没有笔记块时可用"
                  onClick={() => {
                    const doc=docs.find((d) => d.doc_id===docId);
                    const ok=window.confirm(
                      "重建导入《"+(doc?.title??docId)+"》？\n\n"+
                      "· 重新解析 PDF（拾取最新解析器，如中文保真抽取）\n"+
                      "· 重新识别章节（拾取最新分章规则）\n"+
                      "· 重新生成锚点与索引；需要文档当前没有笔记块\n"+
                      "· PDF 不变，无需重新上传"
                    );
                    if(!ok) return;
                    void api
                      .rebuildDoc(docId)
                      .then((r: any) => {
                        track(r.task_id,"import");
                        setToast("重建任务已启动："+r.task_id);
                      })
                      .catch((e: any) => setToast("重建失败："+e.message));
                  }}
                >
                  重建导入
                </button>
              )}

              {docId&&(
                <button
                  title="刷新图资源：从 PDF 提取插图并修复笔记中的图片引用（无需重新导入）"
                  onClick={() => {
                    setToast("正在刷新图资源…");
                    void api
                      .refreshFigures(docId)
                      .then((r) => {
                        setToast("图资源已刷新：登记 "+r.figures+" 张图，修复 "+r.patched_blocks+" 个块的引用");
                        void useStore.getState().loadBlocks();
                      })
                      .catch((e: any) => setToast("图资源刷新失败："+e.message));
                  }}
                >
                  刷新图资源
                </button>
              )}
              {!docId&&<p className="menu-hint">选择教材后，可追加讲义或管理内容。</p>}
            </ActionMenu>
          </div>
          <div className="primary-actions">
            <button className="primary generate-button" onClick={doGenerate}><Icon name="spark" size={16} />生成本章</button>
            <ActionMenu label="学习工具">
              <span className="menu-heading">生成与复习</span>
              <input
                className="gen-name"
                value={genName}
                onChange={(e) => setGenName(e.target.value)}
                placeholder="任务名（可选）"
                title="给这次生成起个名字（如「期末复习重点章」）；留空则自动用「书名 · 章节名」"
                aria-label="生成任务名称"
              />
              <button
                onClick={() => setBookOpen(true)}
                title="一键生成全书：可跳过已生成章节；已有笔记不丢；可暂停/续跑"
              >
                全书生成
              </button>
              <button onClick={() => void openQuiz()}
                title="AI 出题：按笔记出练习题（可上传往年题/课后习题定向风格，期末备考）">
                AI 出题
              </button>
              <button onClick={() => void openRecords()} title="查看各章生成的时间、模型与结果">生成记录</button>
            </ActionMenu>
            <button onClick={() => void openReview(true)} title="审核 AI 生成的笔记块（needs_review）">
              待审核
              {reviewCount>0&&(
                <span className="count-badge">{reviewCount>99? "99+":reviewCount}</span>
              )}
            </button>
            <button className="export-button" onClick={() => setExportOpen(true)}
              title="导出笔记：Markdown / LaTeX / Obsidian / Anki，可选整本或当前章节">
              <Icon name="export" size={16} />导出
            </button>
            <button onClick={() => setKnowledgeOpen(true)}>全书导图 · 软件联动</button>
            <button onClick={() => setTemplatesOpen(true)}>模板工作室</button>
            <button onClick={() => setShareOpen(true)}
              title="分享：复制 Markdown 或下载单文件 HTML（公式字体与插图内嵌，离线可看）">
              <Icon name="share" size={16} />分享
            </button>
            <span className="context-divider">|</span>
            <div className="layout-switch" role="group" aria-label="阅读布局">
              {/* 「阅读工作台 · 把知识，慢慢读懂」不在工具栏占位，悬停/聚焦时才浮出来 */}
              <span className="layout-switch-caption" aria-hidden="true">
                <span className="workspace-dot" />阅读工作台<span className="reading-mode-caption">把知识，慢慢读懂</span>
              </span>
              <button className={!focusMode ? "active" : ""} aria-pressed={!focusMode}
                title="左边教材原文、中间笔记、右边阅读索引三栏对照" onClick={() => setReadingMode("split")}>
                <Icon name="book" size={13} />对照阅读
              </button>
              <button className={focusMode ? "active" : ""} aria-pressed={focusMode}
                title="只留笔记一栏，宽度居中" onClick={() => setReadingMode("focus")}>
                <Icon name="notes" size={13} />专注笔记
              </button>
            </div>
          </div>
        </div>
      {tasks.length > 0 && (
        <div className="task-tray">
          {tasks.map((t) => (
            <div
              key={t.task_id}
              className={"task-row status-" + t.status}
              title={t.error?.message ?? ""}
            >
              <span>
                {t.name || t.kind} · {statusLabel(t)}
                {["queued", "running"].includes(t.status) && t.startedAt
                  ? " · 已运行 " + fmtElapsed(Date.now() - t.startedAt)
                  : ""}
                {/* 终态不再挂阶段文案：曾出现「部分完成 · 完成」的自相矛盾 */}
                {!["succeeded", "partial", "failed"].includes(t.status) && t.progress?.stage
                  ? " · " + progressText(t.progress)
                  : ""}
                {/* 部分完成要说清：跑完了、其余块已保存、N 块需处理（在待审核）——
                    否则「部分完成」看着像没干完谎报 */}
                {t.status === "partial" &&
                  (() => {
                    const n = (t.result?.reports ?? []).reduce(
                      (acc: number, r: any) => acc + (r.failures ?? []).length, 0);
                    return n > 0 ? " · 其余块已保存，" + n + " 块需处理（见待审核）" : "";
                  })()}
                {t.status === "failed" && t.error?.message ? " · " + t.error.message : ""}
              </span>
              {["queued", "running"].includes(t.status) &&
                (() => {
                  const pct = (t.bestPct ?? 0) > 0 ? t.bestPct : null;
                  return (
                    <div
                      className={"progress" + (pct === null ? " indeterminate" : "")}
                      title={pct === null ? "处理中" : pct + "%"}
                    >
                      <i style={{ width: (pct ?? 40) + "%" }} />
                    </div>
                  );
                })()}
              {t.status === "succeeded" && (
                <div className="progress" title="100%">
                  <i style={{ width: "100%" }} />
                </div>
              )}
              {t.status === "partial" && (
                <div className="progress" title="部分块待审核">
                  <i style={{ width: (t.bestPct ?? 90) + "%" }} />
                </div>
              )}
              <div className="task-actions">
                {["queued", "running"].includes(t.status) && (
                  <button
                    onClick={() => {
                      if (t.kind === "generate") {
                        // 暂停 = 优雅取消：引擎会把已产出结果落缓存，续跑时自动复用
                        void api.cancelTask(t.task_id).then(() =>
                          setToast("已暂停：进度已保存，点「继续生成」接着跑（已完成的块自动复用）"));
                      } else {
                        void api.cancelTask(t.task_id).then(() => setToast("已请求取消 " + t.task_id));
                      }
                    }}
                  >
                    {t.kind === "generate" ? "暂停" : "取消"}
                  </button>
                )}
                {t.kind === "generate" &&
                  ["cancelled", "interrupted", "failed", "partial"].includes(t.status) && (
                    <button className="primary" onClick={() => void resumeGenerate(t)}>
                      继续生成
                    </button>
                  )}
                {!["queued", "running"].includes(t.status) && (
                  <button
                    className="danger"
                    title="只删除这条任务记录；已导出的文件留在 notes_out/ 下，不会被删"
                    onClick={() =>
                      void api.deleteTask(t.task_id).then(() => {
                        setTasks((list) => list.filter((x) => x.task_id !== t.task_id));
                        setToast("已删除任务记录（导出文件仍保留在 notes_out/ 下）");
                      })
                    }
                  >
                    删除
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </header>
      {dragOver && (
        <div className="drop-overlay" aria-hidden="true">
          <div className="drop-card">
            <div className="drop-icon">📄</div>
            <b>松开导入 PDF</b>
            <span className="muted">支持 PDF（数字版/扫描版）与 PPTX 讲义，导入后自动解析、分章、锚定</span>
          </div>
        </div>
      )}
      {deleted && (
        <div className="modal-mask" onClick={() => setDeleted(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>删除完成</h2>
            <p>
              教材《{deleted.title}》已删除，其笔记块、锚点、历史与图资源一并移出列表。
            </p>
            <div className="muted">
              <div>· 移除笔记块：{deleted.removed.blocks ?? 0} 个</div>
              <div>· 移除锚点：{deleted.removed.anchors ?? 0} 个</div>
              <div>· 移除章节：{deleted.removed.chapters ?? 0} 个</div>
              <div>
                · 回收站位置：<code>{deleted.trashedTo}</code>（未物理删除，可在磁盘找回）
              </div>
            </div>
            <button onClick={() => setDeleted(null)}>知道了</button>
          </div>
        </div>
      )}
      {records && (
        <div className="modal-mask" onClick={() => setRecords(null)}>
          <div className="modal wide" onClick={(e) => e.stopPropagation()}>
            <h2>生成记录</h2>
            {records.length === 0 && (
              <div className="empty">还没有生成记录——点击「生成本章」后可在此查看每次生成的时间、模型与结果</div>
            )}
            {records.map((r, i) => {
              const chip =
                r.status === "complete" ? "ok" : r.status === "partial" ? "warn" : "bad";
              const label =
                r.status === "complete" ? "完成" : r.status === "partial" ? "部分成功" : "失败";
              return (
                <div key={i} className="record-row">
                  <div className="record-head">
                    <b>{r.chapter_id}</b>
                    <span className={"status-chip " + chip}>{label}</span>
                    {r.cache_hit ? <span className="status-chip">缓存复用</span> : null}
                    <span className="muted">
                      {(String(r.created_at ?? "")).replace("T", " ").slice(0, 19)}
                    </span>
                  </div>
                  <div className="record-meta">
                    <span>提纲模型：{r.model?.outline ?? "-"}</span>
                    <span>填充模型：{r.model?.fill ?? "-"}</span>
                    <span>新增 {(r.created ?? []).length} · 更新 {(r.updated ?? []).length}</span>
                    <span>
                      跳过 {(r.skipped_identical ?? []).length + (r.skipped_protected ?? []).length}
                    </span>
                    <span>失败 {(r.failures ?? []).length}</span>
                  </div>
                  {(r.failures ?? []).length > 0 && (
                    <div className="record-fail">
                      {(r.failures ?? []).slice(0, 3).map((f: any, j: number) => (
                        <div key={j}>
                          · {f.stage ?? f.temp_id ?? ""}：{f.reason ?? ""}
                        </div>
                      ))}
                      {(r.failures ?? []).length > 3 && (
                        <div>…共 {(r.failures ?? []).length} 条失败</div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
            <button onClick={() => setRecords(null)}>关闭</button>
          </div>
        </div>
      )}
      {quizOpen && (
        <div className="modal-mask" onClick={() => setQuizOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>AI 出题</h2>
            {!quiz && (
              <>
                <div className="export-grid">
                  <label>
                    <input type="radio" name="quiz-scope" checked={quizScope === "chapter"}
                           onChange={() => setQuizScope("chapter")} />
                    <span>
                      <b>仅当前章节</b>
                      <em>{chapterId
                        ? (manifest?.chapters ?? []).find((c: any) => c.chapter_id === chapterId)?.title ?? chapterId
                        : "（请先在顶部选择章节）"}</em>
                    </span>
                  </label>
                  <label>
                    <input type="radio" name="quiz-scope" checked={quizScope === "book"}
                           onChange={() => setQuizScope("book")} />
                    <span><b>整本范围</b><em>综合全书笔记出题</em></span>
                  </label>
                </div>
                <div className="muted">题型（可多选）：</div>
                <div style={{ display: "flex", gap: 12, flexWrap: "wrap", margin: "6px 0" }}>
                  {([['choice', '选择题'], ['blank', '填空题'], ['calculation', '计算题'],
                    ['short', '简答题'], ['proof', '证明题']] as const).map(([k, label]) => (
                    <label key={k} style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                      <input type="checkbox" checked={quizKinds.includes(k)}
                             onChange={(e) => setQuizKinds(e.target.checked
                               ? [...quizKinds, k]
                               : quizKinds.filter((x) => x !== k))} />
                      {label}
                    </label>
                  ))}
                </div>
                <div className="kv">
                  <span>题量</span>
                  <select value={quizCount} onChange={(e) => setQuizCount(Number(e.target.value))}>
                    {[5, 10, 15, 20].map((n) => <option key={n} value={n}>{n} 道</option>)}
                  </select>
                  <span>难度</span>
                  <select value={quizDifficulty} onChange={(e) => setQuizDifficulty(e.target.value)}>
                    <option value="基础水平">基础</option>
                    <option value="期末水平">期末</option>
                    <option value="考研水平">考研</option>
                  </select>
                </div>
                <div className="muted" style={{ margin: "8px 0" }}>
                  参考题库：<b>{qbankCount}</b> 题——上传往年题/课后习题，出题会贴近期末风格
                </div>
                <label className="file-button">
                  上传参考题目（PDF/txt/md）
                  <input type="file" accept=".pdf,.txt,.md" hidden onChange={async (e) => {
                    const f = e.target.files?.[0];
                    if (!f || !docId) return;
                    try {
                      const r = await api.qbankUpload(docId, f);
                      setQbankCount((n) => n + (r.added ?? 0));
                      setToast(`已入库 ${r.added} 道参考题`);
                    } catch (err: any) {
                      setToast("题库上传失败：" + err.message);
                    }
                  }} />
                </label>
                <div className="export-actions">
                  <button onClick={() => setQuizOpen(false)}>取消</button>
                  <button className="primary" disabled={quizBusy} onClick={() => void runQuiz()}>
                    {quizBusy ? "出题中…" : "开始出题"}
                  </button>
                </div>
              </>
            )}
            {quiz && (
              <>
                <div className="muted">
                  共 {quiz.questions?.length ?? 0} 道 · {quiz.difficulty}
                  {qbankCount > 0 ? " · 已参考题库" : ""}
                </div>
                {(quiz.questions ?? []).map((q: any, i: number) => (
                  <div key={q.id || i} className="review-row">
                    <div>
                      <b>{i + 1}.</b> <span className="muted">
                        [{q.type === "choice" ? "选择" : q.type === "blank" ? "填空"
                          : q.type === "calculation" ? "计算" : q.type === "proof" ? "证明" : "简答"}]
                      </span> {q.question}
                    </div>
                    {(q.options ?? []).map((o: string, j: number) => (
                      <div key={j} className="muted" style={{ paddingLeft: 14 }}>
                        {String.fromCharCode(65 + j)}. {o}
                      </div>
                    ))}
                    <div>
                      <button onClick={() => setShowAnswers((s) => ({ ...s, [q.id]: !s[q.id] }))}>
                        {showAnswers[q.id] ? "收起答案" : "显示答案"}
                      </button>
                      {showAnswers[q.id] && (
                        <div style={{ marginTop: 6 }}>
                          <b>答案：</b>{q.answer}
                          {q.explanation ? <div className="muted">解析：{q.explanation}</div> : null}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
                <div className="export-actions">
                  <button onClick={() => setQuiz(null)}>再出一套</button>
                  <button className="primary" onClick={() => setQuizOpen(false)}>完成</button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
      {exportResult && (
        <div className="modal-mask" onClick={() => setExportResult(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>导出完成</h2>
            <div className="muted">共 {exportResult.files.length} 个文件，保存在：</div>
            <code className="export-path">{exportResult.dir}</code>
            <div className="export-files">
              {exportResult.files.map((f) => (
                <div key={f} className="muted">· {f}</div>
              ))}
            </div>
            <div className="export-actions">
              <button onClick={() => setExportResult(null)}>知道了</button>
              <button
                className="primary"
                onClick={() =>
                  void api.openPath(exportResult.dir).catch((e: any) => setToast("打开文件夹失败：" + e.message))}
              >
                打开文件夹
              </button>
            </div>
          </div>
        </div>
      )}
      {bookOpen && (
        <div className="modal-mask" onClick={() => setBookOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>全书生成</h2>
            <div className="muted">
              共 {(manifest?.chapters ?? []).length} 章。已有笔记不会丢：按合并策略原地更新
              （人工修改/受保护块照旧保留）；任务可随时暂停/续跑。
            </div>
            <div className="export-grid">
              <label>
                <input type="radio" name="book-mode" checked={!regenAll}
                       onChange={() => setRegenAll(false)} />
                <span>
                  <b>跳过已生成的章节（推荐）</b>
                  <em>已生成且锚点未变的章节直接跳过，不重烧 token；只补没生成的章节</em>
                </span>
              </label>
              <label>
                <input type="radio" name="book-mode" checked={regenAll}
                       onChange={() => setRegenAll(true)} />
                <span>
                  <b>全部重新生成</b>
                  <em>换提示词/模型后想要全书焕新时用；消耗与时间都是全书量级</em>
                </span>
              </label>
            </div>
            <div className="export-actions">
              <button onClick={() => setBookOpen(false)}>取消</button>
              <button className="primary" onClick={() => void doGenerateBook(!regenAll)}>
                开始生成
              </button>
            </div>
          </div>
        </div>
      )}
      {renameOpen && (
        <div className="modal-mask" onClick={() => setRenameOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>教材改名</h2>
            <input
              className="gen-name"
              style={{ width: "100%" }}
              value={renameText}
              onChange={(e) => setRenameText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void doRename()}
              placeholder="教材名称"
              autoFocus
            />
            <div className="export-actions">
              <button onClick={() => setRenameOpen(false)}>取消</button>
              <button className="primary" onClick={() => void doRename()}>保存</button>
            </div>
          </div>
        </div>
      )}
      {knowledgeOpen && <DeferredPanel onClose={() => setKnowledgeOpen(false)}><KnowledgePanel onClose={() => setKnowledgeOpen(false)} /></DeferredPanel>}
      {templatesOpen && <DeferredPanel onClose={() => setTemplatesOpen(false)}><TemplateStudio onClose={() => setTemplatesOpen(false)} /></DeferredPanel>}
      {shareOpen && <DeferredPanel onClose={() => setShareOpen(false)}><ShareModal onClose={() => setShareOpen(false)} /></DeferredPanel>}
      {exportOpen && (
        <div className="modal-mask" onClick={() => setExportOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>导出笔记</h2>
            <div className="export-grid">
              <label>
                <input type="radio" name="exp-format" checked={expFormat === "md"}
                       onChange={() => setExpFormat("md")} />
                <span><b>Markdown</b><em>纯 .md 文集，公式保持 LaTeX，通用性最好</em></span>
              </label>
              <label>
                <input type="radio" name="exp-format" checked={expFormat === "tex"}
                       onChange={() => setExpFormat("tex")} />
                <span><b>LaTeX</b><em>可直接编译的 .tex（含图），适合投稿/打印</em></span>
              </label>
              <label>
                <input type="radio" name="exp-format" checked={expFormat === "both"}
                       onChange={() => setExpFormat("both")} />
                <span><b>Markdown + LaTeX</b><em>一次导出两套</em></span>
              </label>
              <label>
                <input type="radio" name="exp-format" checked={expFormat === "md-obsidian"}
                       onChange={() => setExpFormat("md-obsidian")} />
                <span><b>Obsidian</b><em>按块拆分的双链库，可直接放进仓库</em></span>
              </label>
              <label>
                <input type="radio" name="exp-format" checked={expFormat === "anki"}
                       onChange={() => setExpFormat("anki")} />
                <span><b>Anki 牌组</b><em>问答卡片（标题→内容），导入 Anki 记忆</em></span>
              </label>
            </div>
            <div className="export-grid">
              <label>
                <input type="radio" name="exp-scope" checked={expScope === "book"}
                       onChange={() => setExpScope("book")} />
                <span><b>整本</b></span>
              </label>
              <label>
                <input type="radio" name="exp-scope" checked={expScope === "chapter"}
                       onChange={() => setExpScope("chapter")} />
                <span>
                  <b>仅当前章节</b>
                  <em>{chapterId ? (manifest?.chapters ?? []).find(
                    (c: any) => c.chapter_id === chapterId)?.title ?? chapterId : "（请先在顶部选择章节）"}</em>
                </span>
              </label>
            </div>
            <div className="export-actions">
              <button onClick={() => setExportOpen(false)}>取消</button>
              <button className="primary" onClick={() => void doExport(expFormat, expScope)}>
                开始导出
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
