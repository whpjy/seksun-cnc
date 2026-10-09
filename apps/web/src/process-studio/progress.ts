/** Presentation metadata only: model text and reasoning are never placed in the progress log. */
export type StreamProgress = {
  attemptId: string; revision: number; phase: "waiting" | "analysis" | "response" | "tool";
  startedAt: number; lastAt: number; receivedChars: number; toolName?: string;
};

function object(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

function acceptChunk(previous: StreamProgress, chunk: Record<string, unknown>, time: number): StreamProgress {
  const next = { ...previous, lastAt: time };
  if (chunk.type === "reasoning-delta" || (chunk.type === "block-start" && chunk.blockType === "reasoning")) next.phase = "analysis";
  if (chunk.type === "text-delta" || (chunk.type === "block-start" && chunk.blockType === "text")) next.phase = "response";
  if (chunk.type === "tool-call-delta" || (chunk.type === "block-start" && chunk.blockType === "tool-call")) {
    next.phase = "tool";
    if (typeof chunk.name === "string") next.toolName = chunk.name;
  }
  if (typeof chunk.text === "string") next.receivedChars += Array.from(chunk.text).length;
  if (typeof chunk.argumentsDelta === "string") next.receivedChars += Array.from(chunk.argumentsDelta).length;
  return next;
}

/** Restore a compact opening snapshot or fold one live frame; ignore old revisions and attempts. */
export function streamProgressFrom(previous: StreamProgress | null, value: unknown, now: number): StreamProgress | null {
  const frame = object(value);
  if (!frame) return previous;
  if ("activeAttempt" in frame || (typeof frame.revision === "number" && !frame.type)) {
    const attempt = object(frame.activeAttempt);
    if (!attempt || typeof attempt.attemptId !== "string") return null;
    let progress: StreamProgress = { attemptId: attempt.attemptId, revision: Number(frame.revision), phase: "waiting", startedAt: now, lastAt: now, receivedChars: 0 };
    const records = Array.isArray(attempt.stream) ? attempt.stream : [];
    const first = object(records[0]);
    progress.startedAt = typeof first?.time0 === "number" ? first.time0 : typeof first?.time === "number" ? first.time : now;
    for (const value of records) {
      const entry = object(value);
      if (!entry) continue;
      const time = typeof entry.time === "number" ? entry.time : typeof entry.time0 === "number" ? entry.time0 + (Array.isArray(entry.dt) ? entry.dt.reduce((sum: number, item) => sum + (typeof item === "number" ? item : 0), 0) : 0) : now;
      const chunk = object(entry.chunk);
      if (chunk) progress = acceptChunk(progress, chunk, time);
      if (entry.type === "text-chunks" || entry.type === "reasoning-chunks" || entry.type === "tool-call-chunks") {
        const members = entry.type === "tool-call-chunks" ? entry.args : entry.texts;
        progress = acceptChunk(progress, { type: entry.type === "text-chunks" ? "text-delta" : entry.type === "reasoning-chunks" ? "reasoning-delta" : "tool-call-delta", name: entry.name, text: Array.isArray(members) ? members.filter(item => typeof item === "string").join("") : "" }, time);
      }
    }
    return progress;
  }
  if (typeof frame.attemptId !== "string" || typeof frame.revision !== "number") return previous;
  if (frame.type === "start") {
    if (previous?.attemptId === frame.attemptId && previous.revision >= frame.revision) return previous;
    return { attemptId: frame.attemptId, revision: frame.revision, phase: "waiting", startedAt: now, lastAt: now, receivedChars: 0 };
  }
  if (!previous || previous.attemptId !== frame.attemptId || frame.revision <= previous.revision) return previous;
  if (frame.type === "end") return null;
  const chunk = object(frame.chunk);
  return chunk ? { ...acceptChunk(previous, chunk, typeof frame.time === "number" ? frame.time : now), revision: frame.revision } : previous;
}

export function elapsedLabel(startedAt: number, now: number): string {
  const seconds = Math.max(0, Math.floor((now - startedAt) / 1000));
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}
