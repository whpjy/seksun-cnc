import { useEffect, useMemo, useRef, useState } from "react";
import { Code2, LoaderCircle } from "lucide-react";

type Program = { filename: string; operation_id: string; name: string; channel: string; coordinate_frame: string; line_count: number; assumptions: string[]; url: string };
type Manifest = { status: string; notice: string; generated_count: number; operation_count?: number; programs: Program[]; missing: { operation_id: string; name: string; reason: string }[]; bundle_url?: string; source_signature?: string };

async function load<T>(url: string, signal: AbortSignal, method = "GET"): Promise<T> {
  const response = await fetch(url, { method, signal, cache: "no-store" });
  if (!response.ok) {
    let message = `程序请求失败 (${response.status})`;
    try { const body = await response.json(); if (typeof body.detail === "string") message = body.detail; } catch { /* Non-JSON server errors. */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

function CodeContent({ url, revision }: { url: string; revision?: string }) {
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setText(""); setError("");
    void fetch(url, { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]), cache: "no-store" }).then(async response => {
      if (!response.ok) throw new Error(`程序读取失败 (${response.status})`);
      return response.text();
    }).then(value => { if (!controller.signal.aborted) setText(value); }).catch(reason => {
      if (!controller.signal.aborted) setError(reason.message);
    });
    return () => controller.abort();
  }, [url, revision, retry]);
  const numbers = useMemo(() => text.trimEnd().split("\n").map((_, index) => String(index + 1).padStart(3, "0")).join("\n"), [text]);
  const highlighted = useMemo(() => {
    const escaped = text.trimEnd().replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]!);
    // Keep large programs as text so syntax decoration does not create a huge DOM.
    if (text.length > 60000) return escaped;
    return escaped.replace(/\([^\n)]*\)|\b[GMTOSFXYZIKRABCP][-+]?\d+(?:\.\d+)?/g, token => {
      const kind = token.startsWith("(") ? "comment" : token[0] === "G" ? "motion" : token[0] === "M" ? "machine" : token[0] === "T" ? "tool" : /[SF]/.test(token[0]) ? "speed" : token[0] === "O" ? "program" : "axis";
      return `<span class="nc-token-${kind}">${token}</span>`;
    });
  }, [text]);
  if (error) return <div className="studio-nc-card-empty" role="alert"><span>{error}</span><button onClick={() => setRetry(value => value + 1)}>重试</button></div>;
  if (!text) return <div className="studio-nc-card-empty"><LoaderCircle size={18} className="spin" />正在读取程序</div>;
  return <pre className="studio-nc-text"><span className="studio-nc-line-numbers" aria-hidden="true">{numbers}</span><code dangerouslySetInnerHTML={{ __html: highlighted }} /></pre>;
}

function ProgramCard({ program, revision }: { program: Program; revision?: string }) {
  // Some existing operation names already contain their OP number.
  const name = program.name.startsWith(program.operation_id + " ") ? program.name.slice(program.operation_id.length + 1) : program.name;
  return <article id={`nc-${program.operation_id}`} className="studio-nc-card" aria-label={`${program.operation_id} ${name}`}>
    <header><div className="studio-nc-card-title"><span className="studio-nc-operation">{program.operation_id}</span><strong title={name}>{name}</strong></div><div className="studio-nc-inline-meta"><span>{program.channel === "sub" ? "副轴" : "主轴"}</span><span>{program.coordinate_frame === "DIAMETER_X_PROFILE_Z" ? "X 直径 / 轮廓 Z" : "模型 XYZ"}</span><span>{program.line_count} 行</span></div></header>
    <CodeContent url={program.url} revision={revision} />
    {program.assumptions.length > 0 && <details className="studio-nc-card-assumptions"><summary>坐标与参数说明</summary>{program.assumptions.map((item, i) => <p key={i}>{item}</p>)}</details>}
  </article>;
}

export default function GCodePanel({ jobId, existingUrl, onAvailability, onProgramStates, selectedOperationId, onSelectOperationId, detailsVisible }: { jobId?: string; existingUrl?: string; onAvailability: (available: boolean) => void; onProgramStates: (states: Record<string, string>) => void; selectedOperationId?: string; onSelectOperationId: (id: string) => void; detailsVisible: boolean }) {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const generating = useRef(false);
  const base = jobId ? `/api/v1/jobs/${jobId}/gcode/generic` : undefined;

  useEffect(() => {
    setManifest(null); setError("");
    if (!base || existingUrl) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let active = true;
    const accept = (next: Manifest) => {
      if (!active) return;
      setManifest(next);
    };
    const refresh = async () => {
      try { if (!generating.current) accept(await load<Manifest>(base, controller.signal)); }
      catch (reason) { if (active) setError(reason instanceof Error ? reason.message : "程序读取失败"); }
      if (active) timer = setTimeout(refresh, 10000);
    };
    const start = async () => {
      generating.current = true; setBusy(true);
      try {
        let next = await load<Manifest>(base, controller.signal);
        if (next.status === "not_generated" || next.status === "stale") next = await load<Manifest>(base, controller.signal, "POST");
        accept(next);
      } catch (reason) { if (active) setError(reason instanceof Error ? reason.message : "程序生成失败"); }
      finally {
        if (active) { generating.current = false; setBusy(false); timer = setTimeout(refresh, 10000); }
      }
    };
    void start();
    return () => { active = false; controller.abort(); clearTimeout(timer); generating.current = false; };
  }, [base, existingUrl]);

  useEffect(() => { if (manifest) onAvailability(manifest.programs.length > 0 && manifest.status !== "stale"); }, [manifest, onAvailability]);

  useEffect(() => {
    if (!manifest) return;
    const states: Record<string, string> = {};
    for (const item of manifest.programs) states[item.operation_id] = manifest.status === "stale" ? "方案已变化" : "代码已生成";
    for (const item of manifest.missing) states[item.operation_id] = "尚未生成";
    onProgramStates(states);
  }, [manifest, onProgramStates]);

  const program = manifest?.programs.find(item => item.operation_id === selectedOperationId) || (!selectedOperationId ? manifest?.programs[0] : undefined);
  const missing = manifest?.missing.find(item => item.operation_id === selectedOperationId);
  useEffect(() => {
    if (!manifest || existingUrl) return;
    const items = [...manifest.programs, ...manifest.missing];
    if (items.length && !items.some(item => item.operation_id === selectedOperationId)) onSelectOperationId(items[0].operation_id);
  }, [manifest, existingUrl, selectedOperationId, onSelectOperationId]);

  return <section className={`studio-code studio-code-premium studio-code-flat studio-code-selected${detailsVisible ? " with-inspector" : ""}`}>
    {error && <p className="studio-code-error" role="alert">{error}</p>}
    {existingUrl ? <div className="studio-code-content"><article className="studio-nc-card studio-nc-card-full"><header><div className="studio-nc-card-title"><Code2 size={16} /><strong>完整加工程序</strong></div></header><CodeContent url={existingUrl} /></article></div>
      : program ? <div className="studio-code-content" key={program.filename}><ProgramCard program={program} revision={manifest?.source_signature} /></div>
      : missing ? <div className="studio-code-empty"><Code2 size={30} /><strong>{missing.operation_id} {missing.name.replace(missing.operation_id + " ", "")}</strong><p>{missing.reason}</p></div>
      : <div className="studio-code-empty">{busy ? <LoaderCircle size={30} className="spin" /> : <Code2 size={30} />}<strong>{busy ? "正在生成通用加工程序" : manifest?.status === "stale" ? "工艺方案已变化" : "尚无可预览的程序"}</strong><p>{manifest?.status === "stale" ? "重新进入 G 代码页面，使用当前方案更新程序。" : "选择右侧工序，在这里查看对应的加工程序。"}</p></div>}
  </section>;
}
