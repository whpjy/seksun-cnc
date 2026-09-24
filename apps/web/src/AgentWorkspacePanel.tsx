import { useState } from "react";
import {
  AlertTriangle,
  Bot,
  Box,
  BrainCircuit,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Circle,
  Database,
  Eye,
  FileJson2,
  LoaderCircle,
  Wrench,
  X,
} from "lucide-react";
import type { AgentArtifact, AgentTraceEvent, AgentWorkspace } from "./types";

type Props = {
  events: AgentTraceEvent[];
  artifacts: AgentArtifact[];
  orchestration?: AgentWorkspace["orchestration"];
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

type AgentPhaseKey = "model" | "planning" | "validation" | "delivery";

const AGENT_ACTION_LABELS: Record<string, string> = {
  perceive: "补充观察",
  plan: "规划下一步",
  compile: "编译工序",
  execute: "生成刀路并仿真",
  review: "审核仿真结果",
  commit: "提交工序状态",
  repair: "修正并重试",
  human_review: "等待人工确认",
  complete: "任务完成",
};

type AgentPhase = {
  key: AgentPhaseKey;
  title: string;
  description: string;
  events: Array<{ event: AgentTraceEvent; index: number }>;
  latest?: AgentTraceEvent;
  status: "pending" | "running" | "completed" | "failed";
};

const AGENT_PHASES: Array<Omit<AgentPhase, "events" | "latest" | "status">> = [
  { key: "model", title: "解析三维模型", description: "读取模型并提取几何与制造特征" },
  { key: "planning", title: "AI 规划工艺", description: "研判加工策略、装夹方案与工序路线" },
  { key: "validation", title: "编译与校验", description: "生成可执行工艺并检查设备与特征覆盖" },
  { key: "delivery", title: "生成工艺方案", description: "汇总规划结果并交付可审查方案" },
];

function phaseForEvent(event: AgentTraceEvent): AgentPhaseKey {
  if (event.stage === "uploading" || event.stage === "geometry_analysis" || event.subgraph === "intake" || event.subgraph === "feature_recognition") return "model";
  if (event.stage === "completed" || event.subgraph === "delivery") return "delivery";
  if (["ai_integration", "process_generation", "coverage_validation"].includes(event.stage) || event.subgraph === "validation") return "validation";
  return "planning";
}

function buildAgentPhases(events: AgentTraceEvent[]): AgentPhase[] {
  const groups = new Map<AgentPhaseKey, Array<{ event: AgentTraceEvent; index: number }>>();
  let currentKey: AgentPhaseKey = "model";
  events.forEach((event, index) => {
    const genericError = (event.stage === "error" || event.kind === "error") && !event.subgraph;
    const key = genericError ? currentKey : phaseForEvent(event);
    currentKey = key;
    groups.set(key, [...(groups.get(key) ?? []), { event, index }]);
  });
  const currentIndex = AGENT_PHASES.findIndex((phase) => phase.key === currentKey);
  const failed = events.at(-1)?.status === "failed" || events.at(-1)?.kind === "error" || events.at(-1)?.stage === "error";

  return AGENT_PHASES.map((phase, index) => {
    const phaseEvents = groups.get(phase.key) ?? [];
    const latest = phaseEvents.at(-1)?.event;
    let status: AgentPhase["status"] = "pending";
    if (index < currentIndex) status = "completed";
    else if (index === currentIndex) status = failed ? "failed" : phase.key === "delivery" && latest?.status === "completed" ? "completed" : "running";
    return { ...phase, events: phaseEvents, latest, status };
  });
}

function phaseIcon(status: AgentPhase["status"]) {
  if (status === "failed") return <AlertTriangle size={15} />;
  if (status === "running") return <LoaderCircle className="spin" size={15} />;
  if (status === "completed") return <CheckCircle2 size={15} />;
  return <Circle size={14} />;
}

export function AgentWorkspacePanel({
  events,
  artifacts,
  orchestration,
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
  const [showTechnicalDetails, setShowTechnicalDetails] = useState(false);
  const [artifactContent, setArtifactContent] = useState<Record<string, string>>({});
  const [artifactError, setArtifactError] = useState<Record<string, string>>({});
  const phases = buildAgentPhases(events);
  const activePhase = phases.find((phase) => phase.status === "running" || phase.status === "failed")
    ?? [...phases].reverse().find((phase) => phase.status === "completed")
    ?? phases[0];

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
      <div><Bot size={17} /><span><strong>AI 工艺智能体</strong><small>{live ? `顺序执行 · ${activePhase.title}` : "执行记录"}</small></span></div>
      <div><b>{Math.round(progress)}%</b>{onClose && <button aria-label="关闭智能体工作台" onClick={onClose}><X size={15} /></button>}</div>
    </header>
    <div className="agent-progress-track"><i style={{ width: `${Math.max(0, Math.min(progress, 100))}%` }} /></div>
    {orchestration && <section className={`agent-current-objective ${orchestration.lifecycle ?? ""}`}>
      <div>
        <small>当前目标{orchestration.current_operation_id ? ` · ${orchestration.current_operation_id}` : ""}</small>
        <strong>{orchestration.current_objective ?? "等待智能体确定下一目标"}</strong>
      </div>
      <dl>
        <div><dt>下一动作</dt><dd>{AGENT_ACTION_LABELS[orchestration.next_action ?? ""] ?? orchestration.next_action ?? "—"}</dd></div>
        <div><dt>证据</dt><dd>{orchestration.evidence_count ?? 0}</dd></div>
        <div><dt>未决问题</dt><dd>{orchestration.open_question_count ?? 0}</dd></div>
      </dl>
    </section>}
    <nav className="agent-workspace-tabs" aria-label="智能体工作区">
      <button className={tab === "trace" ? "active" : ""} onClick={() => setTab("trace")}><BrainCircuit size={14} />执行进度 <span>{phases.length}</span></button>
      <button className={tab === "artifacts" ? "active" : ""} onClick={() => setTab("artifacts")}><Database size={14} />数据文件 <span>{artifacts.length}</span></button>
    </nav>

    {tab === "trace" && <div className="agent-trace-list">
      <div className="agent-phase-list">
        {phases.map((phase, index) => <article key={phase.key} className={`agent-phase ${phase.status}`}>
          <button disabled={!phase.latest} onClick={() => phase.latest && onSelectEvent?.(phase.latest)}>
            <i>{phaseIcon(phase.status)}</i>
            <span>
              <small>阶段 {index + 1}</small>
              <strong>{phase.title}</strong>
              <em>{phase.status === "running" || phase.status === "failed"
                ? phase.latest?.summary ?? phase.latest?.detail ?? phase.latest?.message ?? phase.description
                : phase.status === "completed" ? `已完成 · ${phase.events.length} 条执行记录` : phase.description}</em>
            </span>
            {phase.latest?.viewer && <Eye size={13} />}
          </button>
        </article>)}
      </div>

      {events.length > 0 && <section className={`agent-technical-details ${showTechnicalDetails ? "expanded" : ""}`}>
        <button className="agent-technical-toggle" onClick={() => setShowTechnicalDetails((current) => !current)}>
          <span><Wrench size={13} /><strong>技术详情</strong><small>{events.length} 条底层记录</small></span>
          {showTechnicalDetails ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>
        {showTechnicalDetails && <div className="agent-technical-list">
          {events.map((event, index) => {
            const key = event.event_id ?? `${event.stage}-${index}`;
            const expanded = expandedEvents.has(key);
            const active = activeEventId === event.event_id || (!activeEventId && index === events.length - 1);
            const hasDetails = Boolean(event.evidence?.length || Object.keys(event.metrics ?? {}).length || event.artifacts?.length || event.detail);
            const visualEvent = index < events.length - 1 && event.status === "running" ? { ...event, status: "completed" as const } : event;
            return <article key={key} className={`agent-trace-event compact ${visualEvent.status ?? ""} ${active ? "active" : ""}`}>
              <button className="agent-trace-summary" onClick={() => toggleEvent(event, index)}>
                <i>{eventIcon(visualEvent)}</i>
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
        </div>}
      </section>}
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
