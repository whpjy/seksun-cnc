import type { JournalEvent } from "./harness";

export type TranscriptBlock = { index: number; kind: "reasoning" | "text" | "tool"; text: string; name?: string };
export type LiveTranscript = { attemptId: string; revision: number; step: number; blocks: TranscriptBlock[]; active: boolean; committedSeq?: number };
export type TranscriptEntry = { id: string; label: string; blocks: TranscriptBlock[]; live?: boolean };
function object(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}
function chunkInto(blocks: TranscriptBlock[], chunk: Record<string, unknown>): TranscriptBlock[] {
  const index = typeof chunk.index === "number" ? chunk.index : blocks.length;
  const finished = object(chunk.block);
  const kind = chunk.type === "reasoning-delta" || chunk.blockType === "reasoning" || finished?.type === "reasoning" ? "reasoning"
    : chunk.type === "tool-call-delta" || chunk.blockType === "tool-call" || finished?.type === "tool-call" ? "tool" : "text";
  if (!["block-start", "block-end", "reasoning-delta", "text-delta", "tool-call-delta"].includes(String(chunk.type))) return blocks;
  const next = blocks.map(block => ({ ...block }));
  let block = next.find(block => block.index === index);
  if (!block) { block = { index, kind, text: "" }; next.push(block); }
  if (chunk.type !== "block-end") block.kind = kind;
  if (typeof chunk.text === "string") block.text += chunk.text;
  if (typeof chunk.argumentsDelta === "string") block.text += chunk.argumentsDelta;
  if (typeof chunk.name === "string") block.name = chunk.name;
  // A final block contains the complete body, not another delta.
  if (finished) {
    if (typeof finished.text === "string") block.text = finished.text;
    if (typeof finished.arguments === "string") block.text = finished.arguments;
    if (typeof finished.name === "string") block.name = finished.name;
  }
  return next;
}

export function blocksFromRecords(records: unknown): TranscriptBlock[] {
  let blocks: TranscriptBlock[] = [];
  for (const value of Array.isArray(records) ? records : []) {
    const record = object(value);
    if (!record) continue;
    const chunk = object(record.chunk);
    if (chunk) blocks = chunkInto(blocks, chunk);
    const members = record.type === "tool-call-chunks" ? record.args : record.texts;
    if (["reasoning-chunks", "text-chunks", "tool-call-chunks"].includes(String(record.type))) {
      blocks = chunkInto(blocks, { type: record.type === "reasoning-chunks" ? "reasoning-delta" : record.type === "text-chunks" ? "text-delta" : "tool-call-delta", index: record.index, name: record.name,
        ...(record.type === "tool-call-chunks" ? { argumentsDelta: Array.isArray(members) ? members.join("") : "" } : { text: Array.isArray(members) ? members.join("") : "" }) });
    }
  }
  return blocks;
}

/** Preserve the external Harness's original response blocks, including its reasoning output. */
export function transcriptStreamFrom(previous: LiveTranscript | null, value: unknown): LiveTranscript | null {
  const frame = object(value);
  if (!frame) return previous;
  if (!frame.type) {
    const attempt = object(frame.activeAttempt);
    return attempt && typeof attempt.attemptId === "string" ? { attemptId: attempt.attemptId, revision: Number(frame.revision), step: Number(attempt.step), blocks: blocksFromRecords(attempt.stream), active: true } : null;
  }
  if (typeof frame.attemptId !== "string" || typeof frame.revision !== "number") return previous;
  if (frame.type === "start") {
    if (previous?.attemptId === frame.attemptId && previous.revision >= frame.revision) return previous;
    return { attemptId: frame.attemptId, revision: frame.revision, step: Number(frame.step), blocks: [], active: true };
  }
  if (!previous || previous.attemptId !== frame.attemptId || previous.revision >= frame.revision) return previous;
  if (frame.type === "end") {
    const outcome = object(frame.outcome);
    return { ...previous, active: false, revision: frame.revision, committedSeq: typeof outcome?.seq === "number" ? outcome.seq : undefined };
  }
  const chunk = object(frame.chunk);
  return chunk ? { ...previous, revision: frame.revision, blocks: chunkInto(previous.blocks, chunk) } : previous;
}

export function transcriptEntries(events: JournalEvent[], live: LiveTranscript | null): TranscriptEntry[] {
  const entries: TranscriptEntry[] = [];
  const calls = new Map<string, string>();
  for (const event of events) {
    if (event.type === "assistant/message" || event.type === "assistant/attempt") {
      let blocks = blocksFromRecords(event.data.stream);
      const message = object(event.data.message);
      if (!blocks.length && Array.isArray(message?.content)) blocks = message.content.flatMap((value, index) => {
        const block = object(value);
        return block && typeof block.text === "string" ? [{ index, kind: block.type === "reasoning" ? "reasoning" as const : "text" as const, text: block.text }] : [];
      });
      if (blocks.length) entries.push({ id: `event:${event.seq}`, label: `第 ${event.data.step} 步`, blocks });
    }
    if (event.type === "tool/call") {
      const name = typeof event.data.name === "string" ? event.data.name : undefined;
      if (name && typeof event.data.callId === "string") calls.set(event.data.callId, name);
      entries.push({ id: `event:${event.seq}`, label: "调用工具", blocks: [{ index: 0, kind: "tool", name, text: String(event.data.arguments || "") }] });
    }
    if (event.type === "tool/result") {
      const message = object(event.data.message);
      const source = object(message?.source);
      const name = typeof source?.callId === "string" ? calls.get(source.callId) : undefined;
      const resultName = name || (typeof source?.toolName === "string" ? source.toolName : undefined);
      const content = Array.isArray(message?.content) ? message.content : [];
      const text = content.flatMap(value => { const block = object(value); return typeof block?.text === "string" ? [block.text] : []; }).join("\n");
      if (text) entries.push({ id: `event:${event.seq}`, label: message?.isError ? "工具返回错误" : "工具返回", blocks: [{ index: 0, kind: "text", name: resultName, text }] });
    }
  }
  if (live && !(live.committedSeq !== undefined && events.some(event => event.seq === live.committedSeq))) entries.push({ id: `live:${live.attemptId}`, label: `第 ${live.step} 步${live.active ? " · 正在输出" : ""}`, blocks: live.blocks, live: live.active });
  return entries;
}
