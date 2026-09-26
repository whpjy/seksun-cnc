import { useMemo, useState } from "react";
import {
  AlertTriangle,
  Bot,
  Box,
  BrainCircuit,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Circle,
  Code2,
  Database,
  Eye,
  FileText,
  LoaderCircle,
  Play,
  ScanSearch,
  Sparkles,
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
  onPerceive?: () => void;
  perceptionPending?: boolean;
  perceptionError?: string | null;
  onTrial?: () => void;
  trialAvailable?: boolean;
  trialPending?: boolean;
  trialError?: string | null;
  onAdvanceOperation?: () => void;
  rollingAvailable?: boolean;
  rollingPending?: boolean;
  rollingError?: string | null;
  onClose?: () => void;
};

type PhaseKey = "model" | "planning" | "validation" | "delivery";
type PhaseState = "pending" | "active" | "done" | "waiting" | "blocked";

const PHASES: Array<{ key: PhaseKey; short: string; title: string }> = [
  { key: "model", short: "理解", title: "理解三维模型" },
  { key: "planning", short: "规划", title: "制定制造策略" },
  { key: "validation", short: "验证", title: "刀路与仿真验证" },
  { key: "delivery", short: "交付", title: "形成可审查方案" },
];

const ACTION_LABELS: Record<string, string> = {
  perceive: "补充观察模型",
  plan: "规划下一步",
  compile: "编译候选工序",
  execute: "生成刀路并仿真",
  review: "审核仿真结果",
  commit: "提交已验证工序",
  repair: "修正方案并重试",
  human_review: "等待人工确认",
  complete: "任务已完成",
};

function phaseFor(event: AgentTraceEvent): PhaseKey {
  if (event.stage === "completed" || event.subgraph === "delivery") return "delivery";
  if (
    ["ai_integration", "process_generation", "coverage_validation", "cam_validation", "operation_trial", "operation_execution", "l32_operation_execution", "l32_repair", "validation_remediation"].includes(event.stage)
    || event.subgraph === "validation"
    || event.subgraph === "operation_execution"
  ) return "validation";
  if (event.stage === "uploading" || event.stage === "geometry_analysis" || event.subgraph === "intake" || event.subgraph === "feature_recognition") return "model";
  return "planning";
}

function eventIcon(event: AgentTraceEvent) {
  if (event.status === "failed" || event.status === "blocked" || event.kind === "error") return <AlertTriangle size={16} />;
  if (event.status === "running") return <LoaderCircle className="spin" size={16} />;
  if (event.kind === "tool_call" || event.kind === "tool_result") return <Wrench size={16} />;
  if (event.status === "completed") return <CheckCircle2 size={16} />;
  return <BrainCircuit size={16} />;
}

function eventRole(event: AgentTraceEvent) {
  if (event.kind === "tool_call") return "调用制造工具";
  if (event.kind === "tool_result") return "工具返回";
  const phase = phaseFor(event);
  if (phase === "model") return "模型理解";
  if (phase === "validation") return "验证判断";
  if (phase === "delivery") return "方案总结";
  return "工艺推理";
}

function formatTime(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function formatSize(size?: number) {
  if (!size) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function artifactIcon(artifact: AgentArtifact) {
  if (artifact.kind === "model") return <Box size={16} />;
  if (artifact.kind === "json") return <Code2 size={16} />;
  return <FileText size={16} />;
}

function isInternalJsonArtifact(artifact: AgentArtifact) {
  return artifact.kind === "json" || /\.json(?:$|[?#])/i.test(artifact.filename || artifact.url || "");
}

function compactEvents(events: AgentTraceEvent[]) {
  const visibleAiPhases = new Set([
    "ai_review_completed",
    "agent_operation_audit_summary",
    "agent_decision",
  ]);
  const important = events.filter((event) => {
    if (event.status === "failed" || event.status === "blocked" || event.kind === "error") return true;
    if (event.stage === "orchestrator") return false;
    if (event.stage === "ai_planning") return Boolean(event.phase && visibleAiPhases.has(event.phase));
    if (event.kind === "tool_call" || event.kind === "tool_result") return true;
    if (event.artifacts?.length || event.viewer) return true;
    return true;
  });
  return important.filter((event, index) => {
    const previous = important[index - 1];
    if (!previous) return true;
    return !(
      previous.stage === event.stage
      && previous.node_id === event.node_id
      && previous.summary === event.summary
      && previous.message === event.message
    );
  });
}

function eventHeadline(event: AgentTraceEvent) {
  if (event.stage !== "ai_planning") return event.title || event.summary || event.message;
  if (event.phase === "ai_review_completed") return "制造策略研判完成";
  if (event.phase === "agent_operation_audit_summary") return "候选工序检查完成";
  if (event.phase === "agent_decision") return "AI 方案完成晋级判断";
  return event.summary || event.title || event.message;
}

export function AgentWorkspacePanel({
  events,
  artifacts,
  orchestration,
  activeEventId,
  progress = 0,
  live = false,
  onSelectEvent,
  onSelectArtifact,
  onPerceive,
  perceptionPending = false,
  perceptionError,
  onTrial,
  trialAvailable = false,
  trialPending = false,
  trialError,
  onAdvanceOperation,
  rollingAvailable = false,
  rollingPending = false,
  rollingError,
  onClose,
}: Props) {
  const [tab, setTab] = useState<"conversation" | "files">("conversation");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const readableEvents = useMemo(() => compactEvents(events), [events]);
  const visibleArtifacts = useMemo(() => artifacts.filter((artifact) => !isInternalJsonArtifact(artifact)), [artifacts]);
  const currentEvent = events.at(-1);
  const activePhase = phaseFor(currentEvent ?? { stage: "uploading", message: "", percent: 0 });
  const phaseStates = useMemo(() => {
    const states = Object.fromEntries(PHASES.map((phase) => [phase.key, "pending"])) as Record<PhaseKey, PhaseState>;
    for (const phase of PHASES) {
      const latest = [...events].reverse().find((event) => phaseFor(event) === phase.key);
      if (!latest) continue;
      if (latest.status === "failed" || latest.status === "blocked") states[phase.key] = "blocked";
      else if (latest.status === "waiting") states[phase.key] = "waiting";
      else if (latest.status === "running") states[phase.key] = "active";
      else if (latest.status === "completed") states[phase.key] = "done";
    }
    const activeIndex = PHASES.findIndex((phase) => phase.key === activePhase);
    PHASES.forEach((phase, index) => {
      if (states[phase.key] === "pending" && index < activeIndex) states[phase.key] = "done";
    });
    if (states.validation === "waiting" || states.validation === "blocked") {
      states.delivery = states.validation;
    }
    return states;
  }, [activePhase, events]);
  const objective = orchestration?.current_objective || currentEvent?.summary || currentEvent?.message || "正在建立零件的制造上下文";

  const toggleEvent = (event: AgentTraceEvent, index: number) => {
    const id = event.event_id || `${event.stage}-${index}`;
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
    onSelectEvent?.(event);
  };

  const jumpToPhase = (key: PhaseKey) => {
    const event = [...events].reverse().find((item) => phaseFor(item) === key);
    if (event) onSelectEvent?.(event);
  };

  return (
    <section className="agent-workspace-panel agent-conversation-panel">
      <header className="agent-conversation-header">
        <div className="agent-conversation-identity">
          <span className="agent-conversation-logo"><Sparkles size={18} /></span>
          <div><strong>CNC 工艺智能体</strong><small>{live ? "正在工作" : "任务记录"}</small></div>
        </div>
        <span className={`agent-live-state ${live ? "is-live" : ""}`}><i />{live ? "实时运行" : "已保存"}</span>
        {onClose && <button type="button" className="agent-icon-button" onClick={onClose} title="关闭"><X size={18} /></button>}
      </header>

      <div className="agent-mission">
        <div className="agent-mission-label"><Bot size={15} />当前任务</div>
        <strong>{objective}</strong>
        <p>{currentEvent?.message || "我会结合几何证据、制造约束与仿真结果逐步形成可执行方案。"}</p>
        <div className="agent-mission-progress"><span style={{ width: `${Math.max(3, Math.min(100, progress))}%` }} /></div>
      </div>

      <div className="agent-phase-strip" aria-label="任务阶段">
        {PHASES.map((phase, index) => {
          const state = phaseStates[phase.key];
          return (
            <button key={phase.key} type="button" className={`agent-phase-step is-${state}`} onClick={() => jumpToPhase(phase.key)} title={phase.title}>
              <span>{state === "done" ? <CheckCircle2 size={14} /> : state === "blocked" ? <AlertTriangle size={13} /> : state === "waiting" ? <Circle size={12} /> : index + 1}</span><small>{phase.short}</small>
            </button>
          );
        })}
      </div>

      {orchestration?.rolling_loop && (
        <section className={`agent-rolling-window is-${orchestration.rolling_loop.status || "not_started"}`}>
          <div>
            <span>逐工序验证窗口</span>
            <strong>{orchestration.rolling_loop.current_operation?.operation_id || orchestration.rolling_loop.next_operation_id || "等待开始"}</strong>
          </div>
          <p>{orchestration.rolling_loop.message || "每次只接受一道具有刀路与连续材料仿真证据的工序。"}</p>
          <footer>
            <span>已接受 {orchestration.rolling_loop.accepted_count ?? 0}/{orchestration.rolling_loop.expected_operation_count ?? 0}</span>
            <span>{orchestration.rolling_loop.status === "plan_blocked" ? "上游方案待补全" : orchestration.rolling_loop.status === "blocked" ? "当前工序阻断" : orchestration.rolling_loop.status === "completed" ? "逐项完成" : "等待下一步"}</span>
          </footer>
        </section>
      )}

      {orchestration?.rolling_loop?.status === "plan_blocked" && orchestration.repair && (
        <section className="agent-repair-options">
          <header>
            <div><AlertTriangle size={15} /><span>工艺方案需要修正</span></div>
            <small>{orchestration.repair.diagnosis?.defect || "待诊断"}</small>
          </header>
          <p>阻断来自上游工艺方案，不代表当前工序仿真失败。智能体已生成受安全门约束的修复候选。</p>
          <div className="agent-repair-options__list">
            {(orchestration.repair.candidates || []).map((candidate) => (
              <article key={candidate.id || candidate.kind}>
                <div>
                  <strong>{candidate.id || "未命名候选"}</strong>
                  <span>{candidate.auto_applicable ? "可自动验证" : "需要工程确认"}</span>
                </div>
                {candidate.reason && <p>{candidate.reason}</p>}
                {!!candidate.required_evidence?.length && <small>待补证据：{candidate.required_evidence.join("、")}</small>}
              </article>
            ))}
          </div>
        </section>
      )}

      <nav className="agent-conversation-tabs">
        <button type="button" className={tab === "conversation" ? "active" : ""} onClick={() => setTab("conversation")}><BrainCircuit size={16} />对话与执行</button>
        <button type="button" className={tab === "files" ? "active" : ""} onClick={() => setTab("files")}><Database size={16} />文件与证据 <span>{visibleArtifacts.length}</span></button>
      </nav>

      {tab === "conversation" ? (
        <div className="agent-conversation-stream">
          <article className="agent-chat-turn agent-chat-turn--intro">
            <span className="agent-chat-avatar"><Bot size={17} /></span>
            <div className="agent-chat-bubble">
              <div className="agent-chat-meta"><strong>工艺智能体</strong><span>任务开始</span></div>
              <p>我会先理解零件，再按需调用几何分析、工序编译、刀路和仿真工具。每个关键结论都可以在右侧查看对应证据。</p>
            </div>
          </article>
          {readableEvents.map((event, index) => {
            const key = event.event_id || `${event.stage}-${index}`;
            const isExpanded = expanded.has(key);
            const isActive = activeEventId === event.event_id || (!activeEventId && event === currentEvent);
            const eventArtifacts = (event.artifacts ?? []).filter((artifact) => !isInternalJsonArtifact(artifact));
            const displayStatus = event.status === "running" && event !== currentEvent ? "completed" : event.status;
            const displayEvent = displayStatus === event.status ? event : { ...event, status: displayStatus };
            const headline = eventHeadline(event);
            const supportingText = event.stage === "ai_planning" ? event.summary : (event.title || event.summary) ? event.message : "";
            return (
              <article key={key} className={`agent-chat-turn ${isActive ? "is-active" : ""} is-${displayStatus || "pending"}`}>
                <span className="agent-chat-avatar">{eventIcon(displayEvent)}</span>
                <div className="agent-chat-bubble">
                  <button type="button" className="agent-chat-main" onClick={() => toggleEvent(event, index)}>
                    <div className="agent-chat-meta"><strong>{eventRole(event)}</strong><span>{formatTime(event.created_at)} {isExpanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</span></div>
                    <h4>{headline}</h4>
                    {supportingText && supportingText !== headline && <p>{supportingText}</p>}
                  </button>
                  {isExpanded && (event.detail || event.evidence?.length || event.metrics) && (
                    <div className="agent-chat-detail">
                      {event.detail && <p>{event.detail}</p>}
                      {event.evidence?.map((item, evidenceIndex) => <span key={evidenceIndex}>{item.label || "证据"}：{String(item.value ?? "已确认")}</span>)}
                      {event.metrics && Object.entries(event.metrics).map(([label, value]) => <span key={label}>{label}：{String(value)}</span>)}
                    </div>
                  )}
                  {(event.viewer || eventArtifacts.length > 0) && (
                    <div className="agent-chat-attachments">
                      {event.viewer && <button type="button" onClick={() => onSelectEvent?.(event)}><Eye size={14} />在右侧查看现场</button>}
                      {eventArtifacts.map((artifact) => <button type="button" key={artifact.id} onClick={() => onSelectArtifact?.(artifact)}>{artifactIcon(artifact)}{artifact.label}<ChevronRight size={13} /></button>)}
                    </div>
                  )}
                </div>
              </article>
            );
          })}
          {readableEvents.length === 0 && <div className="agent-empty-conversation"><LoaderCircle className="spin" />智能体正在建立任务上下文…</div>}
        </div>
      ) : (
        <div className="agent-file-browser">
          <div className="agent-file-browser__intro"><Database size={18} /><div><strong>运行时文件</strong><span>选择文件后在右侧工作区打开，不打断左侧阅读。</span></div></div>
          {visibleArtifacts.map((artifact) => (
            <button type="button" key={artifact.id} className="agent-file-row" onClick={() => onSelectArtifact?.(artifact)}>
              <span>{artifactIcon(artifact)}</span>
              <div><strong>{artifact.label}</strong><small>{artifact.filename || artifact.group || artifact.kind}{formatSize(artifact.size_bytes) ? ` · ${formatSize(artifact.size_bytes)}` : ""}</small></div>
              <ChevronRight size={16} />
            </button>
          ))}
          {visibleArtifacts.length === 0 && <div className="agent-empty-conversation"><Database />暂无需要用户查看的文件；内部 JSON 数据已自动隐藏。</div>}
        </div>
      )}

      <footer className="agent-next-action">
        <div><span>下一步</span><strong>{ACTION_LABELS[orchestration?.next_action || ""] || (live ? "智能体继续推理" : "可继续审查")}</strong></div>
        <div className="agent-next-action__buttons">
          {onPerceive && <button type="button" disabled={perceptionPending} onClick={onPerceive}><ScanSearch size={15} />{perceptionPending ? "观察中" : "观察模型"}</button>}
          {onAdvanceOperation && rollingAvailable && <button type="button" disabled={rollingPending} onClick={onAdvanceOperation}><Play size={15} />{rollingPending ? "验证中" : ["blocked", "plan_blocked"].includes(orchestration?.rolling_loop?.status || "") ? "重新验证当前工序" : "验证下一工序"}</button>}
          {onTrial && trialAvailable && <button type="button" disabled={trialPending} onClick={onTrial}><Play size={15} />{trialPending ? "验证中" : "试运行"}</button>}
        </div>
        {(perceptionError || rollingError || trialError) && <p><AlertTriangle size={13} />{perceptionError || rollingError || trialError}</p>}
      </footer>
    </section>
  );
}
