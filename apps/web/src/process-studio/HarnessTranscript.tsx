import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { JournalEvent } from "./harness";
import { transcriptEntries } from "./transcript";
import type { LiveTranscript } from "./transcript";
import { Brain, Sparkles, Wrench } from "lucide-react";
import { toolLabel } from "./tool-labels";
import { cleanTranslation, needsTranslation, streamingTranslationSegments, translatedSegments, TranslationQueue } from "./translation";

type ReadyLine = { id: string; kind: string; label: string; text: string };

function TranscriptLine({ id, text, kind, label, active, running, expanded, queue, previous, translateToChinese, onReady }: { id: string; text: string; kind: string; label: string; active: boolean; running: boolean; expanded: boolean; translateToChinese: boolean; queue: TranslationQueue; previous: ReadyLine | null; onReady: (line: ReadyLine) => void }) {
  // Incomplete English tokens also use the Chinese placeholder, before there
  // is enough prose to translate. This prevents a flash at stream start.
  const englishOnly = !/[\u4e00-\u9fff]/.test(text) && /[A-Za-z]/.test(text);
  const required = translateToChinese && kind !== "tool" && (needsTranslation(text) || englishOnly);
  const [settledText, setSettledText] = useState("");
  useEffect(() => {
    if (!translateToChinese) return;
    const timer = setTimeout(() => setSettledText(text), 800);
    return () => clearTimeout(timer);
  }, [text, translateToChinese]);
  // Stable completed segments keep their subscriptions as more tokens arrive.
  const signature = translateToChinese ? JSON.stringify(streamingTranslationSegments(text, active, settledText)) : "[]";
  const segments = useMemo<string[]>(() => JSON.parse(signature), [signature]);
  const body = useRef<HTMLDivElement>(null);
  const [inView, setInView] = useState(false);
  useEffect(() => {
    const element = body.current;
    if (!element) return;
    const observer = new IntersectionObserver(([entry]) => setInView(entry.isIntersecting), { rootMargin: "80px" });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const subscribe = useCallback((notify: () => void) => {
    if (!required || !inView) return () => {};
    const cleanup = segments.map((source, index) => queue.subscribe(`${id}:segment:${index}`, source, notify, (active ? 100000 : 0) - index));
    return () => cleanup.forEach(unsubscribe => unsubscribe());
  }, [queue, id, segments, required, inView, active]);
  const snapshot = useCallback(() => !translateToChinese ? text : required ? translatedSegments(queue, id, segments) : kind === "tool" ? text : cleanTranslation(text) || "", [queue, id, segments, text, kind, required, translateToChinese]);
  const translated = useSyncExternalStore(subscribe, snapshot);
  const display = (expanded ? translated : translated.split(/\n+/).filter(Boolean).at(-1) || "").trim();
  useEffect(() => { if (!expanded && display) onReady({ id, kind, label, text: display }); }, [id, kind, label, display, expanded, onReady]);
  const record = display ? { kind, label, text: display } : !expanded ? previous : null;
  const recordRunning = Boolean(display) && running;
  // Keep a tiny invisible observation target so translation can start, but
  // expose neither the label nor an empty progress record before content exists.
  return <div ref={body} data-ready={Boolean(record)} aria-hidden={!record} className={`studio-transcript-line ${!record ? "is-pending" : !display ? "studio-transcript-previous" : recordRunning ? "is-running" : ""}`}>
    {record && <>{recordRunning ? <Sparkles size={13} aria-label="正在执行" /> : record.kind === "tool" ? <Wrench size={13} /> : <Brain size={13} />}
    <small>{recordRunning ? record.label === "执行步骤" ? "正在执行" : record.label === "思考" ? "正在思考" : record.label === "输出" ? "正在输出" : record.label : record.label} ·</small>
    <div className="studio-transcript-body"><span title={record.text}>{record.text}</span></div></>}
  </div>;
}

function displayLines(entries: ReturnType<typeof transcriptEntries>) {
  return entries.flatMap(entry => {
    if (entry.label.startsWith("工具返回")) {
      const name = entry.blocks[0]?.name;
      const operation = name ? toolLabel(name) : "当前操作";
      return [{ id: entry.id, kind: "tool", label: "执行结果", streaming: false, text: `${operation} · ${entry.label.includes("错误") ? "执行出错" : "结果已返回"}` }];
    }
    return entry.blocks.filter(block => block.text.trim() || block.name).map(block => ({
      id: `${entry.id}:${block.index}`, kind: block.kind,
      streaming: Boolean(entry.live),
      label: block.kind === "reasoning" ? "思考" : block.kind === "tool" ? "执行步骤" : "输出",
      // Always retain the whole block: collapsed mode still translates every
      // paragraph, showing the most recently translated one.
      text: block.kind === "tool" ? toolLabel(block.name) : block.text,
    }));
  });
}

export default function HarnessTranscript({ events, live, expanded, running, translateToChinese = false }: { events: JournalEvent[]; live: LiveTranscript | null; expanded: boolean; running: boolean; translateToChinese?: boolean }) {
  const [translationQueue] = useState(() => new TranslationQueue(undefined, 0, 1, 40, 2));
  const history = useMemo(() => displayLines(transcriptEntries(events, null)), [events]);
  const committed = useMemo(() => new Set(events.map(event => event.seq)), [events]);
  const lines = useMemo(() => !live || (live.committedSeq !== undefined && committed.has(live.committedSeq))
    ? history : [...history, ...displayLines(transcriptEntries([], live))], [history, committed, live]);
  const host = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  const [following, setFollowing] = useState(true);
  const [historyLimit, setHistoryLimit] = useState(200);
  const [lastReady, setLastReady] = useState<ReadyLine | null>(null);
  const previousText = translateToChinese ? cleanTranslation(lastReady?.text) : lastReady?.text;
  const previous = lastReady && previousText ? { ...lastReady, text: previousText } : null;
  const rememberReady = useCallback((line: ReadyLine) => setLastReady(previous => previous?.id === line.id && previous.text === line.text && previous.label === line.label ? previous : line), []);
  useEffect(() => { const element = host.current; if (element && pinned.current) element.scrollTop = element.scrollHeight; }, [lines, expanded]);
  useEffect(() => {
    const element = host.current;
    if (!element) return;
    let frame = 0;
    const observer = new MutationObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => { if (pinned.current) element.scrollTop = element.scrollHeight; });
    });
    observer.observe(element, { subtree: true, childList: true, characterData: true });
    return () => { observer.disconnect(); cancelAnimationFrame(frame); };
  }, []);
  const visible = expanded ? lines.slice(-historyLimit) : lines.slice(-1);
  return <div className="studio-harness-transcript">
    <div className="studio-transcript-scroll" ref={host} onScroll={() => {
      const element = host.current;
      if (!element) return;
      pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
      setFollowing(pinned.current);
    }} role="log" aria-label="智能体思考与执行动态">
      {expanded && lines.length > historyLimit && <button className="studio-transcript-resume" onClick={() => { pinned.current = false; setFollowing(false); setHistoryLimit(count => count + 200); }}>查看更早记录</button>}
      {!expanded && !visible.length && previous && <div className="studio-transcript-line studio-transcript-previous" data-ready="true">
        {previous.kind === "tool" ? <Wrench size={13} /> : <Brain size={13} />}<small>{previous.label} ·</small><div className="studio-transcript-body"><span title={previous.text}>{previous.text}</span></div>
      </div>}
      {visible.map((line, index) => <TranscriptLine key={line.id} {...line} expanded={expanded} translateToChinese={translateToChinese} running={running && index === visible.length - 1} active={line.streaming && running && index === visible.length - 1} queue={translationQueue} previous={previous} onReady={rememberReady} />)}
      {!visible.length && !running && !lastReady && <div className="studio-transcript-line"><span>暂无执行记录</span></div>}
    </div>
    {expanded && !following && <button className="studio-transcript-resume" onClick={() => { pinned.current = true; setFollowing(true); if (host.current) host.current.scrollTop = host.current.scrollHeight; }}>回到最新输出 ↓</button>}
  </div>;
}
