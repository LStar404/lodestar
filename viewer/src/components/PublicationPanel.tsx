import { useEffect, useState } from "react";
import { api } from "../api/client";
import { buildShareHtml, copyText, downloadText, type SharePayload } from "../lib/share";

type Publication = { id: string; title: string; doc_id: string; chapter_id: string | null;
  revision: number; revoked: boolean; updated_at: string; html?: string };

export default function PublicationPanel({ data, chapterId }: {
  data: SharePayload | null; chapterId: string | null;
}) {
  const [items, setItems] = useState<Publication[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [preview, setPreview] = useState<string | null>(null);
  const load = async () => {
    const result = await api.get<{items: Publication[]}>("/api/v1/publications");
    setItems(result.items);
  };
  useEffect(() => { void load().catch(e => setMessage(e.message)); }, []);
  const run = async (action: () => Promise<void>) => {
    setBusy(true); setMessage("");
    try { await action(); await load(); } catch (e: any) { setMessage(e.message); }
    finally { setBusy(false); }
  };
  const save = (item?: Publication) => run(async () => {
    if (!data) return;
    const body = { title: data.title, doc_id: data.doc_id, chapter_id: chapterId,
      html: await buildShareHtml(data), base_revision: item?.revision };
    if (item) await api.put("/api/v1/publications/" + item.id, body);
    else await api.post("/api/v1/publications", body);
    setMessage(item ? "分享内容已更新，固定路径保持不变。" : "已保存分享版本；部署阅读服务后可通过固定链接访问。");
  });
  return <section className="publication-panel" aria-label="长期分享">
    <h3>长期分享 · 待部署</h3>
    <p className="share-note">保存独立的阅读版本，后续可更新同一地址或撤销。当前仅保存在本机，尚无公网链接；不会自动发布原教材或其他笔记。</p>
    <button disabled={busy || !data?.blocks.length} onClick={() => void save()}>保存分享版本</button>
    <p role="status">{message}</p>
    {items.filter(item => !data || item.doc_id === data.doc_id).map(item => <article key={item.id}
      style={{ borderTop: "1px solid var(--border, #ddd)", padding: "12px 0" }}>
      <strong>{item.title}</strong> · {item.chapter_id || "整本"} · v{item.revision} · {item.revoked ? "已撤销" : "待部署"}
      <p className="share-note">固定路径：/s/{item.id}</p>
      {!item.revoked && <div className="share-actions">
        <button disabled={busy} onClick={() => void run(async () => {
          const detail = await api.get<Publication>("/api/v1/publications/" + item.id);
          setPreview(detail.html || "");
        })}>预览已保存版本</button>
        <button disabled={busy || !data || item.doc_id !== data.doc_id || item.chapter_id !== chapterId}
          onClick={() => void save(item)}>用当前内容更新</button>
        <button disabled={busy} onClick={() => void run(async () => {
          await copyText("/s/" + item.id); setMessage("已复制路径；需部署到固定域名后才能对外访问。");
        })}>复制部署路径</button>
        <button disabled={busy} onClick={() => void run(async () => {
          const detail = await api.get<Publication>("/api/v1/publications/" + item.id);
          downloadText(item.id + ".html", detail.html || "", "text/html");
        })}>下载此版本</button>
        <button disabled={busy} onClick={() => void run(async () => {
          await api.del("/api/v1/publications/" + item.id, {base_revision: item.revision});
          setPreview(null); setMessage("已撤销；阅读服务会拒绝该地址。已下载的副本不会被远程删除。");
        })}>撤销</button>
      </div>}
    </article>)}
    {preview !== null && <div>
      <button onClick={() => setPreview(null)}>关闭预览</button>
      <iframe title="分享版本预览" sandbox="" referrerPolicy="no-referrer"
        srcDoc={"<!doctype html><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">" + preview}
        style={{width:"100%",height:420,border:"1px solid #ddd",background:"white"}} />
    </div>}
  </section>;
}
