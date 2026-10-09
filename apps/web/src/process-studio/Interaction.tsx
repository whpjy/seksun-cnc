import { useEffect, useRef, useState } from "react";
import { MessageSquare, Send } from "lucide-react";
import { answerQuestions, answersComplete, followQuestions } from "./questions";
import type { Answer, PendingQuestions } from "./questions";

export default function Interaction({ sessionId, onWaiting, onSubmitted }: { sessionId: string; onWaiting: (waiting: boolean) => void; onSubmitted?: () => void }) {
  const [pending, setPending] = useState<PendingQuestions | null>(null);
  const [answers, setAnswers] = useState<Answer[]>([]);
  const [online, setOnline] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const locked = useRef(false);
  const questionId = useRef("");
  useEffect(() => followQuestions(sessionId, request => {
    if (request && request.eventId !== questionId.current) {
      questionId.current = request.eventId; setAnswers([]); setError(""); setNotice("");
    }
    setPending(request);
  }, connected => { setOnline(connected); if (connected) setError(""); }, setError), [sessionId]);
  useEffect(() => { onWaiting(Boolean(pending)); return () => onWaiting(false); }, [pending, onWaiting]);
  const update = (id: string, patch: Partial<Answer>) => setAnswers(previous => {
    const current = previous.find(a => a.id === id) || { id, selected: [] };
    return [...previous.filter(a => a.id !== id), { ...current, ...patch }];
  });
  const submit = async () => {
    if (locked.current || !online || !pending) return;
    locked.current = true; setBusy(true); setError(""); setNotice("");
    try {
      if (pending) {
        const answeredId = pending.eventId;
        await answerQuestions(pending, answers.map(answer => ({ ...answer, custom: answer.custom?.trim() || undefined })));
        setPending(current => current?.eventId === answeredId ? null : current);
        setNotice("回答已提交，智能体将继续执行。");
        onSubmitted?.();
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : "提交失败，请重试。"); }
    finally { locked.current = false; setBusy(false); }
  };
  if (!pending && !error && !notice) return null;
  return <div className="studio-interaction">
    {pending ? <form onSubmit={event => { event.preventDefault(); void submit(); }}>
      <strong className="studio-question-title"><MessageSquare size={14} />需要你确认 · {pending.questions.length} 个问题</strong>
      <div className="studio-questions">
        {pending.questions.map(question => {
          const answer = answers.find(a => a.id === question.id) || { id: question.id, selected: [] };
          return <fieldset key={question.id} disabled={busy || !online}>
            <legend>{question.header || "工艺确认"}</legend><p>{question.question}</p>
            {question.detail && <p className="studio-question-detail">{question.detail}</p>}
            {question.options?.map(option => <label className="studio-question-option" key={option.label}>
              <input type={question.multiSelect ? "checkbox" : "radio"} name={question.id} checked={answer.selected.includes(option.label)} onChange={() => update(question.id, { selected: question.multiSelect ? answer.selected.includes(option.label) ? answer.selected.filter(label => label !== option.label) : [...answer.selected, option.label] : [option.label], custom: question.multiSelect ? answer.custom : "" })} />
              <span>{option.label}{option.description && <small>{option.description}</small>}</span>
            </label>)}
            <textarea aria-label={`${question.header || question.question}的补充回答`} placeholder="也可以填写你的回答或补充说明…" value={answer.custom || ""} onChange={event => update(question.id, { custom: event.target.value, selected: question.multiSelect ? answer.selected : [] })} />
          </fieldset>;
        })}
      </div>
      <button className="studio-interaction-submit" disabled={busy || !online || !answersComplete(pending.questions, answers)} type="submit"><Send size={13} />{busy ? "正在提交…" : "提交回答并继续"}</button>
    </form> : null}
    {!online && pending && <p className="studio-interaction-notice">交互连接恢复中，请稍候…</p>}
    {error && <p className="studio-interaction-error" role="alert">{error}</p>}
    {notice && <p className="studio-interaction-notice" role="status">{notice}</p>}
  </div>;
}
