import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, Box, History, LoaderCircle, Search, Trash2, X } from "lucide-react";

type Item = { id: string; filename: string; operation_count: number; created_at?: string; sessionId?: string };
export default function HistoryDialog({ items, loading, error, onClose, onSelect, onRetry, onDelete }: { items: Item[]; loading: boolean; error: string; onClose: () => void; onSelect: (item: Item) => void; onRetry: () => void; onDelete: (item: Item) => Promise<void> }) {
  const [query, setQuery] = useState("");
  const [confirmId, setConfirmId] = useState<string>();
  const [deletingId, setDeletingId] = useState<string>();
  const [deleteError, setDeleteError] = useState("");
  const deleting = useRef(false);
  const remove = async (item: Item) => {
    if (deleting.current) return;
    deleting.current = true; setDeletingId(item.id); setDeleteError("");
    try { await onDelete(item); setConfirmId(undefined); }
    catch (reason) { setDeleteError(reason instanceof Error ? reason.message : "删除失败，请重试"); }
    finally { deleting.current = false; setDeletingId(undefined); }
  };
  const dialog = useRef<HTMLElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const filtered = items.filter(item => item.filename.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    search.current?.focus();
    return () => previous?.focus();
  }, []);
  return <div className="studio-modal-backdrop studio-history-backdrop" onPointerDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section ref={dialog} className="studio-history-dialog" role="dialog" aria-modal="true" aria-labelledby="studio-history-title" onKeyDown={event => {
      if (event.key === "Escape") onClose();
      if (event.key === "Tab") {
        const controls = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input') || [])];
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    }}>
      <header><strong id="studio-history-title"><History size={17} />历史记录</strong><button onClick={onClose} aria-label="关闭历史任务"><X size={18} /></button></header>
      <div className="studio-history-search"><Search size={15} /><input ref={search} value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索三维模型文件名" aria-label="搜索历史任务文件名" /><span aria-label="记录数量">{loading ? 0 : filtered.length}</span></div>
      {deleteError && <p className="studio-history-delete-error" role="alert">{deleteError}</p>}
      <div className="studio-history-list" aria-busy={loading}>
        {loading ? <div className="studio-history-state" role="status"><LoaderCircle size={28} className="spin" /><p>正在读取历史记录</p></div>
          : error ? <div className="studio-history-state" role="alert"><p>{error}</p><button onClick={onRetry}>重新读取</button></div>
          : !filtered.length ? <div className="studio-history-state"><History size={28} /><p>{query.trim() ? "没有匹配的历史记录" : "暂无历史记录"}</p></div>
          : filtered.map(item => <div className="studio-history-row" key={item.id}><button className="studio-history-item" disabled={Boolean(deletingId)} onClick={() => onSelect(item)}><span className="studio-history-model"><Box size={19} /></span><span><strong>{item.filename}</strong><small>{item.created_at ? new Date(item.created_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Shanghai" }) : item.id.slice(0, 8)} · {item.operation_count} 道工序 · {item.sessionId ? "可继续交互" : "成果只读"}</small></span><ArrowUpRight size={16} /></button>{confirmId === item.id ? <div className="studio-history-delete-confirm"><span>删除任务及其文件？</span><button disabled={Boolean(deletingId)} onClick={() => void remove(item)}>{deletingId === item.id ? <LoaderCircle size={14} className="spin" /> : "删除"}</button><button disabled={Boolean(deletingId)} onClick={() => setConfirmId(undefined)}>取消</button></div> : <button className="studio-history-delete" disabled={Boolean(deletingId)} aria-label={`删除历史任务 ${item.filename}`} title="删除此任务" onClick={() => { setConfirmId(item.id); setDeleteError(""); }}><Trash2 size={16} /></button>}</div>)}
      </div>
    </section>
  </div>;
}
