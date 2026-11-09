import PublicationPanel from "./PublicationPanel";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import {
  buildNgshare,
  buildPlainText,
  buildShareHtml,
  copyText,
  downloadText,
  renderSharedBlock,
  type SharePayload,
} from "../lib/share";
import { useStore } from "../state/store";

type Tab = "send" | "inbox" | "links";

function safeName(s: string): string {
  return (s || "lodestar-share").replace(/[\\/:*?"<>|]+/g, "_").slice(0, 80);
}

/** 分享：发出（Markdown / 离线 HTML / .ngshare 分享包）与收件箱（导入别人的分享包）。 */
export default function ShareModal({ onClose }: { onClose: () => void }) {
  const docId = useStore((s) => s.docId);
  const chapterId = useStore((s) => s.chapterId);
  const manifest = useStore((s) => s.manifest);
  const setToast = useStore((s) => s.setToast);
  const [tab, setTab] = useState<Tab>("send");
  const [scope, setScope] = useState<"chapter" | "book">("chapter");
  const [data, setData] = useState<SharePayload | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [shares, setShares] = useState<any[]>([]);
  const [viewing, setViewing] = useState<any | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (!docId) { setData(null); return; }
    setData(null);
    setErr("");
    void api
      .shareDoc(docId, scope === "chapter" ? chapterId : null)
      .then((d: SharePayload) => { if (!cancelled) setData(d); })
      .catch((e: any) => { if (!cancelled) setErr(String(e?.message ?? e)); });
    return () => { cancelled = true; };
  }, [docId, chapterId, scope]);

  const loadShares = () =>
    void api
      .listShares()
      .then((r) => setShares(r.items ?? []))
      .catch(() => setShares([]));

  useEffect(() => {
    setViewing(null);                 // 切标签时清掉正在看的分享，避免残留旧内容
    if (tab === "inbox") loadShares();
  }, [tab]);

  const chapterTitle = (() => {
    const ch = (manifest?.chapters ?? []).find((c: any) => c.chapter_id === chapterId);
    return ch?.title ?? "";
  })();
  const base = (data?.filename || "share").replace(/\.(md)$/i, "") || "share";
  const sizeKb = data ? Math.round(new Blob([data.markdown]).size / 1024) : 0;

  const run = async (label: string, fn: () => Promise<void> | void) => {
    setBusy(label);
    try {
      await fn();
    } catch (e: any) {
      setToast("操作失败：" + String(e?.message ?? e));
    } finally {
      setBusy(null);
    }
  };

  const importFile = (file: File) =>
    run("import", async () => {
      const r = await api.uploadShare(file);
      setToast("已导入分享：" + (r.share?.title ?? ""));
      setTab("inbox");
      loadShares();
    });

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal wide" onClick={(e) => e.stopPropagation()}>
        <h2>分享</h2>
        <div className="share-tabs">
          <button className={tab === "send" ? "active" : ""} onClick={() => setTab("send")}>
            发出
          </button>
          <button className={tab === "links" ? "active" : ""} onClick={() => setTab("links")}>长期链接</button>
          <button className={tab === "inbox" ? "active" : ""} onClick={() => setTab("inbox")}>
            收件箱{shares.length ? "（" + shares.length + "）" : ""}
          </button>
        </div>

        {tab === "send" && (
          <>
            <div className="share-scope">
              <label>
                <input type="radio" name="share-scope" checked={scope === "chapter"}
                       onChange={() => setScope("chapter")} />
                <span>当前章节</span>
              </label>
              <label>
                <input type="radio" name="share-scope" checked={scope === "book"}
                       onChange={() => setScope("book")} />
                <span>整本教材</span>
              </label>
            </div>
            {err && <p className="share-note">加载失败：{err}</p>}
            {!data && !err && <p className="share-note">正在准备…</p>}
            {data && (
              <>
                <p className="share-note">
                  {data.title} · {data.blocks.length} 个知识点 · Markdown {sizeKb} KB
                  {Object.keys(data.figures).length
                    ? " · 含 " + Object.keys(data.figures).length + " 张插图"
                    : ""}
                </p>
                <textarea className="share-preview" readOnly
                          value={data.markdown.slice(0, 2000)} />
                <div className="share-actions">
                  <button
                    className="primary"
                    disabled={busy !== null}
                    title="对方也装了 Lodestar：这个文件导入后即可阅读（图片内嵌）"
                    onClick={() => void run("ngshare", () => {
                      const pkg = buildNgshare(data, scope === "chapter" ? chapterTitle : "");
                      const name = safeName(pkg.chapter_title || pkg.title) + ".ngshare";
                      downloadText(name, JSON.stringify(pkg, null, 2), "application/json");
                      setToast("已导出 " + name + "（对方在「分享 → 收件箱」导入）");
                    })}>
                    导出 .ngshare（Lodestar 专用）
                  </button>
                  <button disabled={busy !== null}
                          title="单文件 HTML：公式字体与插图内嵌，任何浏览器都能看"
                          onClick={() => void run("html", async () => {
                            const html = await buildShareHtml(data);
                            downloadText(base + ".html", html, "text/html");
                            setToast("已下载 " + base + ".html（可离线打开）");
                          })}>
                    {busy === "html" ? "打包中…" : "下载 HTML"}
                  </button>
                  <button disabled={busy !== null}
                          title="复制 Markdown，可粘到 Notion / Obsidian / Typora"
                          onClick={() => void run("md", async () => {
                            await copyText(data.markdown);
                            setToast("已复制 Markdown");
                          })}>
                    复制 Markdown
                  </button>
                  <button disabled={busy !== null} title="下载 .md 文件"
                          onClick={() => void run("mdl", () => {
                            downloadText(base + ".md", data.markdown, "text/markdown");
                            setToast("已下载 " + base + ".md");
                          })}>
                    下载 .md
                  </button>
                  <button disabled={busy !== null} title="纯文本，适合粘进聊天窗口"
                          onClick={() => void run("txt", async () => {
                            await copyText(buildPlainText(data));
                            setToast("已复制纯文本");
                          })}>
                    复制纯文本
                  </button>
                </div>
                <p className="share-note">
                  .ngshare 给「也装了 Lodestar 的人」：导入后能像读自己的笔记一样翻看，插图随包带走；
                  HTML 则给没装软件的人看。两者都在本机生成，不上传任何东西。
                </p>
              </>
            )}
          </>
        )}

        {tab === "links" && <>
          <label>分享范围 <select value={scope} onChange={e => setScope(e.target.value as "book" | "chapter")}><option value="chapter">当前章节</option><option value="book">整本教材</option></select></label>
          {err && <p role="alert">{err}</p>}
          <PublicationPanel data={data} chapterId={scope === "chapter" ? chapterId : null} />
        </>}

        {tab === "inbox" && (
          <>
            <div className="share-actions">
              <label className="file-button primary" title="导入别人发来的 .ngshare 分享包（只读收录）">
                导入 .ngshare
                <input type="file" accept=".ngshare,application/json" hidden
                       onChange={(e) => {
                         const f = e.target.files?.[0];
                         if (f) void importFile(f);
                         e.currentTarget.value = "";
                       }} />
              </label>
              <button disabled={busy !== null} onClick={loadShares}>刷新</button>
            </div>
            {!shares.length && <p className="share-note">还没有收到分享。让对方把 .ngshare 发给你即可。</p>}
            {shares.length > 0 && (
              <ul className="share-list">
                {shares.map((s) => (
                  <li key={s.share_id}>
                    <button className="link" onClick={() => void run("open", async () => {
                      setViewing(await api.getShare(s.share_id));
                    })}>
                      {s.title}{s.chapter_title ? " · " + s.chapter_title : ""}
                    </button>
                    <span className="share-note"> {s.blocks} 个知识点 · {(s.size / 1024).toFixed(0)} KB</span>
                    <button className="danger" title="从本机删除这条分享（不影响发送方）"
                            onClick={() => void run("del", async () => {
                              await api.deleteShare(s.share_id);
                              setViewing(null);
                              setToast("已删除分享");
                              loadShares();
                            })}>
                      删除
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {viewing && (
              <div className="share-view">
                <h3>{viewing.title}{viewing.chapter_title ? " · " + viewing.chapter_title : ""}</h3>
                {(viewing.blocks ?? []).map((b: any, i: number) => (
                  <section key={i} className="blk">
                    {b.title ? <h4>{b.title}</h4> : null}
                    <div className="md"
                         dangerouslySetInnerHTML={{ __html: renderSharedBlock(b.content, viewing.figures) }} />
                    {(b.sources ?? []).length > 0 && (
                      <p className="share-note">
                        出处 {[...new Set((b.sources ?? []).map((s: any) => s.page).filter((p: any) => p != null))]
                          .map((p: any) => "p." + p).join("、")}
                      </p>
                    )}
                  </section>
                ))}
              </div>
            )}
          </>
        )}

        <div className="modal-actions">
          <button onClick={onClose}>关闭</button>
        </div>
      </div>
    </div>
  );
}
