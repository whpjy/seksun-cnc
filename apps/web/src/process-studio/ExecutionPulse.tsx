import { useEffect, useState } from "react";
import { LoaderCircle, Radio, WifiOff } from "lucide-react";
import type { Activity, JournalEvent } from "./harness";
import { elapsedLabel } from "./progress";
import type { StreamProgress } from "./progress";

/** Keep the one-second clock local so the CAD scene does not rerender with the timer. */
export default function ExecutionPulse({ stream, events, activities, internal, connected }: {
  stream: StreamProgress | null; events: JournalEvent[]; activities: Activity[]; internal?: Activity; connected: boolean;
}) {
  const [now, setNow] = useState(Date.now);
  const [mountedAt] = useState(Date.now);
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  const tool = activities.filter(activity => activity.status === "running").at(-1);
  const step = events.filter(event => event.type === "step/start").at(-1);
  const turn = events.filter(event => event.type === "turn/start").at(-1);
  const startedAt = stream?.startedAt || tool?.time || step?.time || turn?.time || mountedAt;
  const lastAt = Math.max(stream?.lastAt || 0, events.at(-1)?.time || 0, internal?.time || 0) || startedAt;
  const gap = Math.max(0, Math.floor((now - lastAt) / 1000));
  const label = !connected ? "执行记录连接中，正在恢复进度"
    : stream ? { waiting: "等待模型响应", analysis: "正在分析工艺上下文", response: "正在生成工艺响应", tool: "正在准备下一步操作" }[stream.phase]
    : tool ? internal?.status === "running" && internal.time >= tool.time ? internal.label : `${tool.label} · 执行中`
    : "等待下一步工艺响应";
  return <div className={`studio-execution-pulse ${connected ? "" : "disconnected"}`} role="status" aria-label="当前执行状态">
    <div className="studio-pulse-main">{connected ? <LoaderCircle className="spin" size={14} /> : <WifiOff size={14} />}<strong>{label}</strong><time>{elapsedLabel(startedAt, now)}</time></div>
    <div className="studio-pulse-detail"><span>{typeof step?.data.step === "number" ? `第 ${step.data.step} 步 · ` : ""}{connected && stream?.receivedChars ? `已接收 ${stream.receivedChars.toLocaleString()} 字` : connected ? "等待实时执行消息" : "正在重新连接"}</span><span><Radio size={10} />{connected ? gap < 3 ? "刚刚收到更新" : `${gap} 秒前更新` : "连接中"}</span></div>
    {connected && gap >= 30 && <small className="studio-pulse-wait">本步仍在等待响应，暂无新的进度消息。</small>}
    <div className="studio-pulse-track" aria-hidden="true"><i /></div>
  </div>;
}
