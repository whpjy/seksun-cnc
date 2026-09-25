import { AlertTriangle, CheckCircle2, LoaderCircle, RefreshCw, ScanSearch, ShieldCheck, Wrench } from "lucide-react";
import type { L32AgentReviewContext } from "./types";

type Props = {
  context: L32AgentReviewContext;
  pending?: boolean;
  error?: string | null;
  onDecision: (profileId: string, state: "accepted" | "excluded") => void;
};

function ProfilePlot({ points }: { points: Array<{ z: number; radius: number }> }) {
  if (points.length < 2) return <div className="l32-review-empty">轮廓点不足，无法预览</div>;
  const zMin = Math.min(...points.map((point) => point.z));
  const zMax = Math.max(...points.map((point) => point.z));
  const rMax = Math.max(...points.map((point) => point.radius), 0.1);
  const width = 340;
  const height = 116;
  const x = (z: number) => 12 + ((z - zMin) / Math.max(zMax - zMin, 0.001)) * (width - 24);
  const y = (radius: number) => height / 2 - (radius / rMax) * (height / 2 - 12);
  const upper = points.map((point) => `${x(point.z).toFixed(1)},${y(point.radius).toFixed(1)}`);
  const lower = [...points].reverse().map((point) => `${x(point.z).toFixed(1)},${(height - y(point.radius)).toFixed(1)}`);
  return <svg className="l32-profile-plot" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="回转轮廓截面预览">
    <line x1="12" y1={height / 2} x2={width - 12} y2={height / 2} className="l32-profile-axis" />
    <polygon points={[...upper, ...lower].join(" ")} className="l32-profile-fill" />
    <polyline points={upper.join(" ")} className="l32-profile-line" />
    <polyline points={lower.join(" ")} className="l32-profile-line" />
  </svg>;
}

export function L32AgentReviewPanel({ context, pending = false, error, onDecision }: Props) {
  const profile = context.profiles.find((item) => item.id === context.recommended_profile_id)
    ?? context.profiles.find((item) => item.review_state !== "excluded")
    ?? context.profiles[0];
  if (!profile) return null;
  const accepted = profile.review_state === "accepted";
  const blocked = Boolean(context.blocker || context.failed_operations.length);
  return <section className="l32-agent-review" aria-label="L32 回转轮廓工程审核">
    <header>
      <span className="l32-review-icon"><ScanSearch size={18} /></span>
      <div><small>智能体需要你的判断</small><strong>{context.title}</strong></div>
      <em className={accepted ? "accepted" : "waiting"}>{accepted ? "已确认" : "待确认"}</em>
    </header>
    <p className="l32-review-summary">{context.summary}</p>
    <div className="l32-review-profile-head">
      <div><strong>{profile.id}</strong><span>{profile.side === "outer" ? "外轮廓" : "内轮廓"} · {Math.round(profile.confidence * 100)}% 置信度</span></div>
      <span>Ø{profile.diameter_min_mm.toFixed(2)}–{profile.diameter_max_mm.toFixed(2)} × {(profile.z_max_mm - profile.z_min_mm).toFixed(2)} mm</span>
    </div>
    <ProfilePlot points={profile.points} />
    <div className="l32-review-facts">
      <span><b>{context.passed_operation_count}</b> 道前序试算通过</span>
      <span className={profile.undercut_spans.length ? "warning" : ""}><b>{profile.undercut_spans.length}</b> 处轴向反转</span>
      <span><b>{context.failed_operations.length}</b> 道工序待处理</span>
    </div>
    {blocked && <div className="l32-review-blocker"><AlertTriangle size={15} /><div><strong>为什么停下来</strong><p>{context.blocker || `${context.failed_operations.join("、")} 未通过可达性校核`}</p></div></div>}
    {context.repair_candidates.length > 0 && <div className="l32-review-candidates">
      <small>下一步工艺候选</small>
      {context.repair_candidates.slice(0, 3).map((candidate) => <div key={candidate.id}>
        <Wrench size={13} /><span><strong>{candidate.label}</strong><small>{candidate.reason}</small></span>
        <em>{candidate.auto_applicable ? "可自动验证" : "待工程确认"}</em>
      </div>)}
    </div>}
    {error && <div className="l32-review-error"><AlertTriangle size={13} />{error}</div>}
    <footer>
      {!accepted && <button type="button" className="secondary" disabled={pending} onClick={() => onDecision(profile.id, "excluded")}>排除此轮廓</button>}
      <button type="button" className="primary" disabled={pending} onClick={() => onDecision(profile.id, "accepted")}>
        {pending ? <LoaderCircle className="spin" size={15} /> : accepted ? <RefreshCw size={15} /> : <ShieldCheck size={15} />}
        {pending ? "正在重新验证…" : accepted ? "按此轮廓重新验证" : "确认轮廓并重新验证"}
      </button>
    </footer>
    {context.decision_status === "validated" && <div className="l32-review-success"><CheckCircle2 size={14} />整件编译与连续仿真已通过</div>}
  </section>;
}
