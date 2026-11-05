import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useStore } from "../state/store";
import "./template-studio.css";

type Cover = { style: string; color: string; subtitle: string; image: string };
type Draft = { name: string; note_type: string; profile: string; type_ids: string[];
  sections: string[]; writing_hint: string; cover: Cover };
type Saved = Draft & { id: string; revision: number };
type TypeInfo = { id: string; name: string; icon: string; profiles: string[] };
type ChatMessage = { role: "user" | "assistant"; content: string };
const initial = (): Draft => ({ name: "我的读书笔记", note_type: "读书笔记", profile: "textbook",
  type_ids: [], sections: ["核心概念", "论证与例子", "我的理解", "待解决的问题"], writing_hint: "",
  cover: { style: "gradient", color: "#6265ad", subtitle: "整理知识，保留思考", image: "" } });
const asDraft = (item: Draft): Draft => ({ name: item.name, note_type: item.note_type,
  profile: item.profile, type_ids: item.type_ids, sections: item.sections,
  writing_hint: item.writing_hint, cover: item.cover });

export default function TemplateStudio({ onClose }: { onClose: () => void }) {
  const docId = useStore(s => s.docId);
  const manifest = useStore(s => s.manifest);
  const [items, setItems] = useState<Saved[]>([]);
  const [types, setTypes] = useState<TypeInfo[]>([]);
  const [profiles, setProfiles] = useState<Record<string, string>>({});
  const [draft, setDraft] = useState<Draft>(initial);
  const [selected, setSelected] = useState<Saved | null>(null);
  const [baseline, setBaseline] = useState(() => JSON.stringify(initial()));
  const [chat, setChat] = useState<ChatMessage[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [applied, setApplied] = useState<Saved | null>(null);

  useEffect(() => {
    let active = true;
    api.get<{ items: Saved[]; types: TypeInfo[]; profiles: Record<string, string> }>("/api/v1/templates")
      .then(data => { if (active) { setItems(data.items); setTypes(data.types); setProfiles(data.profiles); } })
      .catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    let active = true;
    setApplied(null);
    if (docId) api.get<{ template: Saved | null }>(`/api/v1/docs/${docId}/template`)
      .then(data => { if (active) setApplied(data.template); })
      .catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [docId]);

  const dirty = JSON.stringify(draft) !== baseline;
  const leaveDraft = () => !dirty || window.confirm("模板草稿还未保存，放弃这些修改？");
  const update = (patch: Partial<Draft>) => { setDraft(d => ({ ...d, ...patch })); setNotice(""); };
  const updateCover = (patch: Partial<Cover>) => update({ cover: { ...draft.cover, ...patch } });
  const choose = (item: Saved | null) => {
    if (!leaveDraft()) return;
    const next = item ? asDraft(item) : initial();
    setSelected(item); setDraft(next); setBaseline(JSON.stringify(next)); setChat([]); setError(""); setNotice("");
  };
  const available = types.filter(t => t.profiles.includes(draft.profile) || t.profiles.includes("*"));
  const save = async (apply: boolean) => {
    setBusy(apply ? "apply" : "save"); setError(""); setNotice("");
    try {
      const saved = selected
        ? await api.put<Saved>(`/api/v1/templates/${selected.id}`, { revision: selected.revision, draft })
        : await api.post<Saved>("/api/v1/templates", draft);
      setSelected(saved); setDraft(asDraft(saved)); setBaseline(JSON.stringify(asDraft(saved)));
      setItems(old => [saved, ...old.filter(item => item.id !== saved.id)]);
      if (apply && docId) {
        await api.put(`/api/v1/docs/${docId}/template`, { template_id: saved.id, revision: saved.revision });
        setApplied(saved);
        window.dispatchEvent(new CustomEvent("note-gen:template-applied", { detail: { docId } }));
        setNotice("已保存并应用到当前书。后续生成使用此模板，已有笔记保持原样。");
      } else setNotice("已保存到模板库，可在其他书中复用。");
    } catch (e: any) { setError(e.message); }
    finally { setBusy(""); }
  };
  const discuss = async () => {
    if (!message.trim()) return;
    const messages: ChatMessage[] = [...chat, { role: "user", content: message.trim() }];
    if (messages.length > 19) { setError("本轮讨论已达 10 次，请保存草稿后开始新讨论。"); return; }
    setBusy("ai"); setError(""); setNotice("");
    try {
      const result = await api.post<{ reply: string; draft: Draft }>("/api/v1/templates/discuss", { messages, draft });
      setChat([...messages, { role: "assistant", content: result.reply }]);
      setDraft(result.draft); setMessage(""); setNotice("AI 草稿已更新预览，尚未保存或应用。");
    } catch (e: any) { setError(e.message); }
    finally { setBusy(""); }
  };
  const upload = async (file: File) => {
    if (!/image\/(png|jpeg)/.test(file.type) || file.size > 2 * 1024 * 1024) {
      setError("请选择 2 MB 以内的 PNG 或 JPEG 图片。"); return;
    }
    try {
      const value = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader(); reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(new Error("读取封面失败")); reader.readAsDataURL(file);
      });
      updateCover({ image: value, style: "image" }); setError("");
    } catch (e: any) { setError(e.message); }
  };

  return <div className="modal-mask" role="presentation">
    <section className="modal template-studio" role="dialog" aria-modal="true" aria-label="笔记模板工作室">
      <header className="template-header"><div><small>你的笔记，你的方式</small><h2>模板工作室</h2></div>
        <button disabled={!!busy} onClick={() => { if (leaveDraft()) onClose(); }}>关闭</button></header>
      <p className="template-muted">自由安排笔记结构，选择封面，也可以和 AI 一起打磨模板。模板只影响后续生成。</p>
      <div className="template-layout">
        <aside className="template-library"><h3>我的模板</h3><button disabled={!!busy} onClick={() => choose(null)}>＋ 新模板</button>
          {items.length === 0 && <p className="template-muted">保存第一份模板，在不同书籍间复用。</p>}
          {items.map(item => <button disabled={!!busy} key={item.id} className={selected?.id === item.id ? "active" : ""}
            onClick={() => choose(item)}>{item.name}<small>{item.note_type} · v{item.revision}</small></button>)}
          <div className="template-applied"><small>当前书籍</small><strong>{manifest?.title || "尚未选择"}</strong>
            <span>{applied ? `${applied.name} · v${applied.revision}` : "使用默认模板"}</span></div>
        </aside>
        <div className="template-form"><fieldset disabled={!!busy}>
          <label>模板名称<input maxLength={100} value={draft.name} onChange={e => update({ name: e.target.value })} /></label>
          <div className="template-two"><label>笔记类型<input maxLength={80} value={draft.note_type}
            placeholder="如：精读笔记、考试复习、案例分析" onChange={e => update({ note_type: e.target.value })} /></label>
            <label>学科<select value={draft.profile} onChange={e => {
              const profile = e.target.value;
              update({ profile, type_ids: draft.type_ids.filter(id => types.some(t => t.id === id && (t.profiles.includes(profile) || t.profiles.includes("*")))) });
            }}>{Object.entries(profiles).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label></div>
          <label>内容结构 <small>每行一个栏目，生成时按原文内容选用</small><textarea rows={4} value={draft.sections.join("\n")}
            onChange={e => update({ sections: e.target.value.split("\n") })} /></label>
          <label>写作要求<textarea rows={3} maxLength={4000} placeholder="例如：先解释直觉，再列条件；例子尽量短，保留所有出处。"
            value={draft.writing_hint} onChange={e => update({ writing_hint: e.target.value })} /></label>
          <label>偏好的知识点类型 <small>不勾选时按原文自动选择</small></label>
          <div className="template-types">{available.map(t => <label key={t.id}><input type="checkbox" checked={draft.type_ids.includes(t.id)}
            onChange={e => update({ type_ids: e.target.checked ? [...draft.type_ids, t.id] : draft.type_ids.filter(id => id !== t.id) })} />{t.icon} {t.name}</label>)}</div>
          <button className="link" onClick={() => { if (leaveDraft()) { onClose(); useStore.getState().openSettings(true); } }}>在设置中添加自定义知识点类型</button>
          <h3>封面</h3><div className="template-two"><label>样式<select value={draft.cover.style} onChange={e => updateCover({ style: e.target.value })}>
            <option value="gradient">柔和渐变</option><option value="solid">纯色</option><option value="image">我的图片</option><option value="none">不显示封面</option>
          </select></label><label>主题色<input type="color" value={draft.cover.color} onChange={e => updateCover({ color: e.target.value })} /></label></div>
          <label>封面副标题<input maxLength={200} value={draft.cover.subtitle} onChange={e => updateCover({ subtitle: e.target.value })} /></label>
          {draft.cover.style === "image" && <label>上传封面 <small>PNG / JPEG，最大 2 MB，最长边 6000 像素</small>
            <input type="file" accept="image/png,image/jpeg" onChange={e => { if (e.target.files?.[0]) void upload(e.target.files[0]); e.target.value = ""; }} /></label>}
        </fieldset></div>
        <aside className="template-preview"><h3>实时预览</h3>
          {draft.cover.style !== "none" && <div className={`template-cover ${draft.cover.style}`} style={{ backgroundColor: draft.cover.color }}>
            {draft.cover.style === "image" && draft.cover.image && <img src={draft.cover.image} alt="自定义封面" />}
            <div><small>{draft.note_type}</small><h2>{manifest?.title || "你的下一本书"}</h2><p>{draft.cover.subtitle}</p></div></div>}
          <ol className="template-structure">{draft.sections.filter(Boolean).map((section, i) => <li key={i}>{section}</li>)}</ol>
          <h3>和 AI 讨论</h3><p className="template-muted">使用设置中已配置的模型，会产生模型调用用量。回答只更新草稿，由你决定保存。</p>
          <div className="template-chat" aria-live="polite">{chat.map((item, i) => <div key={i} className={item.role}><strong>{item.role === "user" ? "我" : "AI"}</strong><p>{item.content}</p></div>)}</div>
          <textarea disabled={!!busy} rows={3} maxLength={6000} value={message} placeholder="我想做适合考前复习的模板，突出条件、易错点和例题……"
            onChange={e => setMessage(e.target.value)} />
          <div className="template-chat-actions"><button disabled={!!busy || !message.trim()} onClick={() => void discuss()}>{busy === "ai" ? "正在讨论…" : "讨论并生成草稿"}</button>
            {chat.length > 0 && <button disabled={!!busy} onClick={() => { setChat([]); setNotice("已开始新讨论，保留当前草稿。"); }}>新讨论</button>}</div>
        </aside>
      </div>
      {error && <p className="template-error" role="alert">{error}</p>}{notice && <p className="template-notice" role="status">{notice}</p>}
      <footer className="template-footer"><span className="template-muted">{dirty ? "有未保存的修改" : "模板可保存复用"}</span>
        <button disabled={!!busy || !draft.name.trim()} onClick={() => void save(false)}>{busy === "save" ? "保存中…" : "保存模板"}</button>
        <button className="primary" disabled={!!busy || !docId || !draft.name.trim()} onClick={() => void save(true)}>{busy === "apply" ? "应用中…" : "保存并应用到当前书"}</button></footer>
    </section>
  </div>;
}
