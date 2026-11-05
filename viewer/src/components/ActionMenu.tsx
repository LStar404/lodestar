import { useEffect, useRef, type ReactNode } from "react";
import Icon from "./Icon";

/** Native disclosure retains keyboard support; menus dismiss on outside click or Escape. */
export default function ActionMenu({ label, children }: { label: string; children: ReactNode }) {
  const ref = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const outside = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) ref.current.open = false;
    };
    const escape = (e: KeyboardEvent) => {
      if (e.key === "Escape" && ref.current?.open) {
        ref.current.open = false;
        ref.current.querySelector("summary")?.focus();
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, []);
  return <details className="action-menu" ref={ref}>
    <summary>{label}<Icon name="down" size={14} /></summary>
    <div className="action-menu-content" onClick={(e) => {
      if ((e.target as HTMLElement).closest("button, .file-button") && ref.current) ref.current.open = false;
    }}>{children}</div>
  </details>;
}
