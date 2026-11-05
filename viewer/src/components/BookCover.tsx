import { useEffect, useState } from "react";
import { api } from "../api/client";
import { useStore } from "../state/store";

export default function BookCover() {
  const docId = useStore(s => s.docId);
  const title = useStore(s => s.manifest?.title);
  const [template, setTemplate] = useState<any>(null);
  useEffect(() => {
    let cancelled = false;
    let sequence = 0;
    const load = async () => {
      const current = ++sequence;
      if (!docId) { setTemplate(null); return; }
      try {
        const value = await api.get<any>(`/api/v1/docs/${docId}/template`);
        if (!cancelled && sequence === current) setTemplate(value.template);
      } catch { if (!cancelled && sequence === current) setTemplate(null); }
    };
    setTemplate(null);
    void load();
    window.addEventListener("note-gen:template-applied", load);
    return () => { cancelled = true; window.removeEventListener("note-gen:template-applied", load); };
  }, [docId]);
  const cover = template?.cover;
  if (!cover || cover.style === "none") return null;
  const color = /^#[a-fA-F0-9]{6}$/.test(cover.color) ? cover.color : "#6265ad";
  return <div style={{position:"relative", margin:"12px 16px 0", borderRadius:14, overflow:"hidden",
    minHeight:130, flexShrink:0, color:"white", background:cover.style === "gradient"
      ? `linear-gradient(125deg, ${color}, #172033)` : color}}>
    {cover.style === "image" && cover.image && <img src={cover.image} alt="自定义笔记封面"
      style={{position:"absolute",width:"100%",height:"100%",objectFit:"cover"}} />}
    <div style={{position:"relative",padding:20,background:"linear-gradient(transparent,rgba(0,0,0,.55))"}}>
      <small>{template.note_type} · {template.name}</small>
      <h3 style={{margin:"12px 0 8px"}}>{title || "读书笔记"}</h3>
      <span>{cover.subtitle}</span>
    </div>
  </div>;
}
