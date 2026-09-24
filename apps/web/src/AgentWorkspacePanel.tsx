import { useState } from "react";
import {
  AlertTriangle,
  Bot,
  Box,
  BrainCircuit,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Database,
  Eye,
  FileJson2,
  LoaderCircle,
  Wrench,
  X,
} from "lucide-react";
import type { AgentArtifact, AgentTraceEvent } from "./types";

type Props = {
  events: AgentTraceEvent[];
  artifacts: AgentArtifact[];
  activeEventId?: string | null;
  progress?: number;
  live?: boolean;
  apiUrl: (path: string) => string;
  onSelectEvent?: (event: AgentTraceEvent) => void;
  onSelectArtifact?: (artifact: AgentArtifact) => void;
  onClose?: () => void;
};

function eventIcon(event: AgentTraceEvent) {
  if (event.status === "failed" || event.kind === "error") return <AlertTriangle size={15} />;
  if (event.status === "running") return <LoaderCircle className="spin" size={15} />;
  if (event.status === "completed") return <CheckCircle2 size={15} />;
  if (event.kind === "tool_call" || event.kind === "tool_result") return <Wrench size={15} />;
  return <BrainCircuit size={15} />;
}

function formatSize(size?: number) {
  if (!size) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

export function AgentWorkspacePanel({
  events,
  artifacts,
  activeEventId,
  progress = 0,
  live = false,
  apiUrl,
  onSelectEvent,
  onSelectArtifact,
  onClose,
}: Props) {
  const [tab, setTab] = useState<"trace" | "artifacts">("trace");
  const [expandedEvents, setExpandedEvents] = useState<Set<string>>(new Set());
  const [expandedArtifact, setExpandedArtifact] = useState<string | null>(null);
  const [artifactContent, setArtifactContent] = useState<Record<string, string>>({});
  const [artifactError, setArtifactError] = useState<Record<string, string>>({});

  const toggleEvent = (event: AgentTraceEvent, index: number) => {
    const key = event.event_id ?? `${event.stage}-${index}`;
    setExpandedEvents((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
    onSelectEvent?.(event);
  };

  const openArtifact = async (artifact: AgentArtifact) => {
    onSelectArtifact?.(artifact);
    if (artifact.kind === "model") return;
    if (expandedArtifact === artifact.id) {
      setExpandedArtifact(null);
      return;
    }
    setExpandedArtifact(artifact.id);
    if (artifactContent[artifact.id] || artifactError[artifact.id]) return;
    try {
      const response = await fetch(apiUrl(artifact.url), { cache: "no-store" });
      if (!response.ok) throw new Error(`读取失败（${response.status}）`);
      const contentType = response.headers.get("content-type") ?? "";
      const text = contentType.includes("json")
        ? JSON.stringify(await response.json(), null, 2)
        : await response.text();
      setArtifactContent((current) => ({ ...current, [artifact.id]: text }));
    } catch (reason) {
      setArtifactError((current) => ({
        ...current,
        [artifact.id]: reason instanceof Error ? reason.message : "无法读取文件",
      }));
    }
  };

  return <section className="agent-workspace-panel panel">
    <header className="agent-workspace-heading">
      <div><Bot size={17} /><span><strong>AI 工艺智能体</strong><small>{live ? "实时执行" : "执行记录"}</small></span></div>
      <div><b>{Math.round(progress)}%</b>{onClose && <button aria-label="关闭智能体工作台" onClick={onClose}><X size={15} /></button>}</div>
    </header>
    <div className="agent-progress-track"><i style={{ width: `${Math.max(0, Math.min(progress, 100))}%` }} /></div>
    <nav className="agent-workspace-tabs" aria-label="智能体工作区">
      <button className={tab === "trace" ? "active" : ""} onClick={() => setTab("trace")}><BrainCircuit size={14} />执行过程 <span>{events.length}</span></button>
      <button className={tab === "artifacts" ? "active" : ""} onClick={() => setTab("artifacts")}><Database size={14} />数据文件 <span>{artifacts.length}</span></button>
    </nav>

    {tab === "trace" && <div className="agent-trace-list">
      {events.map((event, index) => {
        const key = event.event_id ?? `${event.stage}-${index}`;
        const expanded = expandedEvents.has(key);
        const active = activeEventId === event.event_id || (!activeEventId && index === events.length - 1);
        const hasDetails = Boolean(event.evidence?.length || Object.keys(event.metrics ?? {}).length || event.artifacts?.length || event.detail);
        return <article key={key} className={`agent-trace-event ${event.status ?? ""} ${active ? "active" : ""}`}>
          <button className="agent-trace-summary" onClick={() => toggleEvent(event, index)}>
            <i>{eventIcon(event)}</i>
            <span><small>{event.subgraph ?? "agent"} · {event.node_id ?? event.stage}</small><strong>{event.title ?? event.message}</strong><em>{event.summary ?? event.detail ?? event.message}</em></span>
            {event.viewer && <Eye size={13} />}
            {hasDetails ? expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} /> : null}
          </button>
          {expanded && hasDetails && <div className="agent-event-details">
            {event.detail && event.detail !== event.summary && <p>{event.detail}</p>}
            {(event.evidence?.length ?? 0) > 0 && <dl>{event.evidence?.map((item, itemIndex) => <div key={`${item.label}-${itemIndex}`}><dt>{item.label ?? "依据"}</dt><dd>{String(item.value ?? "—")}</dd></div>)}</dl>}
            {Object.keys(event.metrics ?? {}).length > 0 && <dl>{Object.entries(event.metrics ?? {}).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{String(value ?? "—")}</dd></div>)}</dl>}
            {(event.artifacts?.length ?? 0) > 0 && <div className="agent-inline-artifacts">{event.artifacts?.map((artifact) => <button key={artifact.id} onClick={() => void openArtifact(artifact)}>{artifact.kind === "model" ? <Box size={13} /> : <FileJson2 size={13} />}{artifact.label}</button>)}</div>}
          </div>}
        </article>;
      })}
      {events.length === 0 && <div className="agent-empty"><LoaderCircle className="spin" size={20} /><strong>等待智能体开始执行</strong><small>节点状态与工具结果将在这里实时出现</small></div>}
    </div>}

    {tab === "artifacts" && <div className="agent-artifact-list">
      {artifacts.map((artifact) => <article key={artifact.id} className={expandedArtifact === artifact.id ? "expanded" : ""}>
        <button onClick={() => void openArtifact(artifact)}>
          <i>{artifact.kind === "model" ? <Box size={16} /> : <FileJson2 size={16} />}</i>
          <span><strong>{artifact.label}</strong><small>{artifact.filename ?? artifact.id}{formatSize(artifact.size_bytes) ? ` · ${formatSize(artifact.size_bytes)}` : ""}</small></span>
          {artifact.kind === "model" ? <Eye size={14} /> : expandedArtifact === artifact.id ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>
        {expandedArtifact === artifact.id && artifact.kind !== "model" && <pre>{artifactError[artifact.id] ?? artifactContent[artifact.id] ?? "正在读取…"}</pre>}
      </article>)}
      {artifacts.length === 0 && <div className="agent-empty"><Database size={20} /><strong>暂时没有数据文件</strong><small>工具产物生成后会自动归档到这里</small></div>}
    </div>}
  </section>;
}
