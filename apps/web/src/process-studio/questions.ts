import { rpc } from "./harness";

export type Question = { id: string; question: string; header?: string; detail?: string; options?: { label: string; description?: string }[]; multiSelect?: boolean };
export type Answer = { id: string; selected: string[]; custom?: string };
export type PendingQuestions = { clientId: string; eventId: string; questions: Question[] };

export function questionsIn(value: unknown, sessionId: string, clientId: string): PendingQuestions | null {
  if (!value || typeof value !== "object") return null;
  const frame = value as Record<string, unknown>;
  if (frame.type !== "waterfall" || frame.event !== "user-questions/request" || frame.agentId !== sessionId || typeof frame.eventId !== "string") return null;
  const questions = (frame.request as { questions?: Question[] })?.questions;
  if (!Array.isArray(questions) || !questions.length || questions.some(q => !q || typeof q.id !== "string" || typeof q.question !== "string")) return null;
  return { clientId, eventId: frame.eventId, questions };
}

/** Only explicitly submitted selections are answers; recommended choices are never preselected. */
export function answersComplete(questions: Question[], answers: Answer[]): boolean {
  return questions.every(q => {
    const answer = answers.find(a => a.id === q.id);
    return answer && (answer.selected.length > 0 || Boolean(answer.custom?.trim())) &&
      (q.multiSelect || answer.selected.length <= 1) && answer.selected.every(label => q.options?.some(option => option.label === label));
  });
}

export async function answerQuestions(pending: PendingQuestions, answers: Answer[]): Promise<void> {
  if (!answersComplete(pending.questions, answers)) throw new Error("请回答所有问题后再提交。");
  await rpc("$events/result", { clientId: pending.clientId, eventId: pending.eventId, outcome: { kind: "result", value: { answers } } });
}

/** Reconnecting replays pending questions. Other sessions and interaction kinds are left untouched. */
export function followQuestions(sessionId: string, onPending: (pending: PendingQuestions | null) => void, onOnline: (online: boolean) => void, onError: (error: string) => void): () => void {
  let disposed = false;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let socket: WebSocket;
  const connect = () => {
    let clientId = "";
    const pending = new Map<string, PendingQuestions>();
    socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/harness/api/remote.mux`);
    socket.onopen = () => {
      socket.send(JSON.stringify({ type: "open", streamId: "questions", endpoint: "$events", payload: { args: {} } }));
      socket.send(JSON.stringify({ type: "end", streamId: "questions" }));
    };
    socket.onmessage = message => {
      if (disposed) return;
      try {
        const frame = JSON.parse(String(message.data));
        if (frame.type === "error") { onError(frame.error?.message || "问题连接失败"); socket.close(); return; }
        if (frame.streamId !== "questions") return;
        const value = frame.value;
        if (value?.type === "ready") { clientId = value.clientId; onOnline(true); }
        if (value?.type === "cancel") { pending.delete(value.eventId); onPending(pending.values().next().value || null); }
        const request = clientId ? questionsIn(value, sessionId, clientId) : null;
        if (request) { pending.set(request.eventId, request); onPending(pending.values().next().value!); }
      } catch (error) { onError(error instanceof Error ? error.message : "问题读取失败"); }
    };
    socket.onclose = () => { if (disposed) return; onOnline(false); onPending(null); retry = setTimeout(connect, 3000); };
    socket.onerror = () => onOnline(false);
  };
  connect();
  return () => { disposed = true; clearTimeout(retry); socket.close(); };
}
