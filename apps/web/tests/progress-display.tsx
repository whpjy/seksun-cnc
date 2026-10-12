import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import HarnessTranscript from "../src/process-studio/HarnessTranscript";
import { processStudioDisplayConfig } from "../src/process-studio/display-config";
import type { JournalEvent } from "../src/process-studio/harness";
import "../src/process-studio/studio.css";

function Fixture() {
  const [translateToChinese, setTranslateToChinese] = useState(processStudioDisplayConfig.translateToChinese);
  const [text, setText] = useState("N");
  const [expanded, setExpanded] = useState(false);
  const [running, setRunning] = useState(true);
  const [events, setEvents] = useState<JournalEvent[]>([]);
  const [attemptId, setAttemptId] = useState("fixture");
  Object.assign(window, { progressFixture: { setText, setRunning, setEvents, setAttemptId, setTranslateToChinese } });
  return <main style={{ padding: 40, fontFamily: "sans-serif" }}>
    <button onClick={() => setExpanded(value => !value)}>切换历史</button>
    <section className={`studio-console ${expanded ? "expanded" : ""}`} style={{ position: "static", width: 560, marginTop: 20, transform: "none" }}>
      <header><strong>工艺设计智能体</strong><span>{running ? "智能体正在设计" : "任务已完成"}</span></header>
      <HarnessTranscript key={String(translateToChinese)} translateToChinese={translateToChinese} events={events} expanded={expanded} running={running} live={{ attemptId, revision: 1, step: 1, active: running, blocks: [{ index: 0, kind: "reasoning", text }] }} />
    </section>
  </main>;
}
createRoot(document.getElementById("root")!).render(<StrictMode><Fixture /></StrictMode>);
