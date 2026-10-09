import { useEffect, useMemo, useRef, useState } from "react";
import type { JournalEvent } from "./harness";
import { transcriptEntries } from "./transcript";
import type { LiveTranscript } from "./transcript";
import { Brain, Sparkles, Wrench } from "lucide-react";
import { toolLabel } from "./tool-labels";

function displayLines(entries: ReturnType<typeof transcriptEntries>) {
  return entries.flatMap(entry => {
    if (entry.label.startsWith("工具返回")) {
      const name = entry.blocks[0]?.name;
      const operation = name ? toolLabel(name) : "当前操作";
      return [{ id: entry.id, kind: "tool", label: "执行结果", text: `${operation} · ${entry.label.includes("错误") ? "执行出错" : "结果已返回"}` }];
    }
    return entry.blocks.filter(block => block.text.trim() || block.name).map(block => ({
      id: `${entry.id}:${block.index}`, kind: block.kind,
      label: block.kind === "reasoning" ? "思考" : block.kind === "tool" ? "执行步骤" : "输出",
      text: block.kind === "tool" ? toolLabel(block.name) : block.text.trim().split(/\r?\n/).filter(line => line.trim()).at(-1)?.trim() || "",
    }));
  });
}

export default function HarnessTranscript({ events, live, expanded, running }: { events: JournalEvent[]; live: LiveTranscript | null; expanded: boolean; running: boolean }) {
  const history = useMemo(() => displayLines(transcriptEntries(events, null)), [events]);
  const committed = useMemo(() => new Set(events.map(event => event.seq)), [events]);
  const lines = useMemo(() => !live || (live.committedSeq !== undefined && committed.has(live.committedSeq))
    ? history : [...history, ...displayLines(transcriptEntries([], live))], [history, committed, live]);
  const host = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  const [following, setFollowing] = useState(true);
  const [historyLimit, setHistoryLimit] = useState(200);
  useEffect(() => { const element = host.current; if (element && pinned.current) element.scrollTop = element.scrollHeight; }, [lines, expanded]);
  const visible = expanded ? lines.slice(-historyLimit) : lines.slice(-1);
  return <div className="studio-harness-transcript">
    <div className="studio-transcript-scroll" ref={host} onScroll={() => {
      const element = host.current;
      if (!element) return;
      pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
      setFollowing(pinned.current);
    }} role="log" aria-label="Harness 思考与执行动态">
      {expanded && lines.length > historyLimit && <button className="studio-transcript-resume" onClick={() => { pinned.current = false; setFollowing(false); setHistoryLimit(count => count + 200); }}>查看更早记录</button>}
      {visible.map((line, index) => <div key={line.id} className={`studio-transcript-line ${running && index === visible.length - 1 ? "is-running" : ""}`}>
        {running && index === visible.length - 1 ? <Sparkles size={13} aria-label="正在执行" /> : line.kind === "tool" ? <Wrench size={13} /> : <Brain size={13} />}
        <small>{running && index === visible.length - 1 ? line.label === "执行步骤" ? "正在执行" : line.label === "思考" ? "正在思考" : line.label === "输出" ? "正在输出" : line.label : line.label} ·</small><span>{line.text}</span>
      </div>)}
      {!visible.length && <div className={`studio-transcript-line ${running ? "is-running" : ""}`}>{running && <Sparkles size={13} aria-label="正在执行" />}<span>{running ? "等待 Harness 输出…" : "暂无执行记录"}</span></div>}
    </div>
    {expanded && !following && <button className="studio-transcript-resume" onClick={() => { pinned.current = true; setFollowing(true); if (host.current) host.current.scrollTop = host.current.scrollHeight; }}>回到最新输出 ↓</button>}
  </div>;
}
