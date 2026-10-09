import { useEffect, useRef, useState } from "react";
import { ChevronDown, LogOut, UserRound } from "lucide-react";

export default function AccountMenu({ onLogout }: { onLogout: () => void }) {
  const [open, setOpen] = useState(false);
  const host = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!host.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [open]);
  return <div className="studio-account" ref={host}>
    <button ref={trigger} className={open ? "active" : ""} aria-expanded={open} aria-controls="studio-account-menu" onClick={() => setOpen(!open)}><UserRound />admin<ChevronDown className={open ? "expanded" : ""} /></button>
    {open && <div className="studio-account-menu" id="studio-account-menu">
      <div className="studio-account-identity"><UserRound /><div><small>当前用户</small><strong>admin</strong></div></div>
      <button onClick={onLogout}><LogOut />退出登录</button>
    </div>}
  </div>;
}
