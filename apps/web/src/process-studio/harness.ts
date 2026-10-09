import { streamProgressFrom } from "./progress";
import type { StreamProgress } from "./progress";
import { transcriptStreamFrom } from "./transcript";
import type { LiveTranscript } from "./transcript";

export type Stage = "features" | "process" | "toolpath" | "simulation" | "gcode";
export type JournalEvent = { type: string; seq: number; time: number; data: Record<string, unknown> };
export type Activity = { id: string; label: string; stage: Stage; time: number; completedAt?: number; navigable?: boolean; status: "running" | "done" | "error"; operationId?: string; featureIds?: string[] };

const actions: Record<string, [string, Stage]> = {
  create_job_from_step: ["导入 STEP 模型", "features"], inspect_job_progress: ["解析零件几何", "features"],
  open_job_context: ["读取零件上下文", "features"], inspect_geometry: ["识别制造特征", "features"], observe_model: ["观察零件与局部特征", "features"],
  inspect_machine: ["检查机床与刀具能力", "process"], inspect_operation_catalog: ["匹配加工能力", "process"],
  initialize_process_draft: ["建立装夹与工艺草案", "process"], add_process_operation: ["设计下一道工序", "process"],
  revise_process_operation: ["修正工序参数", "process"], apply_l32_operation_candidate: ["应用修复工序", "process"],
  trial_l32_operation: ["计算刀路并试算工序", "simulation"], evaluate_l32_operation_candidates: ["比较工序试算结果", "simulation"],
  auto_repair_l32_operation: ["修复并重新验证工序", "simulation"], accept_l32_operation_trial: ["接受工序验证结果", "simulation"],
  inspect_l32_operation_trial_state: ["检查累计材料状态", "simulation"], inspect_validation: ["检查碰撞、过切与残料", "simulation"],
  finalize_harness_process_plan: ["验证整件加工方案", "simulation"],
  inspect_job: ["查看加工任务", "features"],
  inspect_l32_profile_review_request: ["复核加工轮廓", "features"], provisionally_accept_l32_profile: ["确认加工轮廓", "features"],
  remove_process_operation: ["删除加工工序", "process"], reorder_process_operations: ["调整工序顺序", "process"],
  inspect_tool_catalog: ["选择加工刀具", "process"], inspect_l32_tool_inventory: ["匹配机床刀具", "process"],
  inspect_l32_candidate_tool_requirements: ["核对加工刀具需求", "process"], bind_l32_candidate_inventory_tool: ["绑定加工刀具", "process"],
  record_l32_physical_tool: ["登记加工刀具", "process"],
  validate_l32_plan: ["验证加工方案", "simulation"], inspect_l32_operation_loop: ["检查工序执行流程", "simulation"],
  advance_l32_operation: ["推进加工工序", "process"], inspect_l32_repair_options: ["检查工序修复方案", "process"], select_l32_repair_candidate: ["选择工序修复方案", "process"],
};

/** Decode the existing Harness HTTP RPC envelope without changing its controllers. */
export async function rpc<T>(method: string, args: Record<string, unknown>): Promise<T> {
  const response = await fetch(`/harness/api/${method}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type: "client-request", rpcId: crypto.randomUUID(), method, payload: { args } }),
  });
  if (!response.ok) throw new Error(response.status === 401 ? "执行引擎未连接，请使用工作台启动脚本重新连接。" : `执行引擎请求失败 (${response.status})`);
  const envelope = await response.json();
  if (!envelope.result?.ok) throw new Error(envelope.result?.error?.message || "执行引擎返回无效响应");
  return envelope.result.value as T;
}

export async function upload(sessionId: string, file: File): Promise<string> {
  const query = new URLSearchParams({ sessionId, name: file.name });
  const response = await fetch(`/harness/api/session/uploadFileBinary?${query}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
  if (!response.ok) throw new Error(`模型上传失败 (${response.status})`);
  const result = await response.json();
  if (!result.ok || typeof result.value?.receiptId !== "string") throw new Error(result.error?.message || "模型上传未完成");
  return result.value.receiptId;
}

/** Follow durable events; reconnects replay a snapshot, deduplicated by the caller's seq map. */
export function follow(sessionId: string, onEvents: (events: JournalEvent[]) => void, onConnection: (online: boolean) => void, onError: (message: string) => void, onStream: (progress: StreamProgress | null) => void, onTranscript: (transcript: LiveTranscript | null) => void): () => void {
  let disposed = false;
  let socket: WebSocket;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let progress: StreamProgress | null = null;
  let transcript: LiveTranscript | null = null;
  let publishTimer: ReturnType<typeof setTimeout> | undefined;
  const publish = (immediate = false) => {
    if (immediate) { clearTimeout(publishTimer); publishTimer = undefined; onStream(progress); onTranscript(transcript); }
    else if (!publishTimer) publishTimer = setTimeout(() => { publishTimer = undefined; if (!disposed) { onStream(progress); onTranscript(transcript); } }, 150);
  };
  const connect = () => {
    socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/harness/api/remote.mux`);
    socket.onopen = () => {
      socket.send(JSON.stringify({ type: "open", streamId: "studio", endpoint: "session/follow", payload: { args: { request: { address: { kind: "session", sessionId }, maxMessages: 100, assistantStream: true } } } }));
      socket.send(JSON.stringify({ type: "end", streamId: "studio" }));
    };
    socket.onmessage = (message) => {
      if (disposed) return;
      try {
        const frame = JSON.parse(String(message.data));
        if (frame.type === "error") { onError(frame.error.message); socket.close(); return; }
        const value = frame.value;
        if (value?.type === "snapshot") {
          onConnection(true); onEvents(value.records.filter((record: { type: string }) => record.type === "event").map((record: { event: JournalEvent }) => record.event));
          progress = streamProgressFrom(null, value.assistantStream, Date.now()); transcript = transcriptStreamFrom(null, value.assistantStream); publish(true);
          if (value.hasMore) {
            const carrier = socket;
            void (async () => {
              let beforeSeq = Math.min(...value.records.map((record: { event: JournalEvent }) => record.event.seq));
              let more = true;
              while (more && !disposed && carrier === socket) {
                const page = await rpc<{ records: { event: JournalEvent }[]; hasMore: boolean }>("session/page", { request: { address: { kind: "session", sessionId }, throughSeq: value.cursor, beforeSeq, maxMessages: 100 } });
                if (disposed || carrier !== socket) return;
                const events = page.records.map(record => record.event);
                onEvents(events);
                const next = Math.min(...events.map(event => event.seq));
                if (!events.length || next >= beforeSeq) break;
                beforeSeq = next; more = page.hasMore;
              }
            })().catch(error => { if (!disposed && carrier === socket) onError(`历史思考记录加载失败：${error.message}`); });
          }
        }
        if (value?.type === "assistant-stream") { progress = streamProgressFrom(progress, value.frame, Date.now()); transcript = transcriptStreamFrom(transcript, value.frame); publish(value.frame.type !== "chunk"); }
        if (value?.type === "event") {
          onEvents([value.event]);
          if (value.event.type === "turn/end") { progress = null; if (transcript) transcript = { ...transcript, active: false }; publish(true); }
        }
      } catch (error) { onError(error instanceof Error ? error.message : "执行记录无法解析"); }
    };
    socket.onclose = () => { if (disposed) return; progress = null; if (transcript) transcript = { ...transcript, active: false }; publish(true); onConnection(false); retry = setTimeout(connect, 3000); };
    socket.onerror = () => onConnection(false);
  };
  connect();
  return () => { disposed = true; clearTimeout(retry); clearTimeout(publishTimer); socket.close(); };
}

function record(value: unknown): Record<string, unknown> | undefined {
  if (typeof value === "string") { try { return record(JSON.parse(value)); } catch { return undefined; } }
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

/** Only named job_id fields are used; attachment and Session ids cannot become CNC jobs. */
export function jobIdIn(value: unknown): string | undefined {
  if (typeof value === "string") {
    const parsed = record(value);
    return parsed ? jobIdIn(parsed) : value.match(/\bjob_id["'\s:：=*`|]+([a-f0-9]{32})\b/i)?.[1];
  }
  if (Array.isArray(value)) { for (const item of value) { const id = jobIdIn(item); if (id) return id; } }
  const object = record(value);
  if (!object) return undefined;
  if (typeof object.job_id === "string" && /^[a-f0-9]{32}$/i.test(object.job_id)) return object.job_id;
  for (const item of Object.values(object)) { const id = jobIdIn(item); if (id) return id; }
}

export function activitiesFrom(events: JournalEvent[]): Activity[] {
  const result: Activity[] = [];
  const byCall = new Map<string, Activity>();
  for (const event of events) {
    if (event.type === "tool/call") {
      const name = String(event.data.name).trim().toLowerCase().replace(/^.*[.:/]/, "").replace(/^mcp__cnc(?:__|_)/, "").split(/__/).at(-1)!;
      const action = actions[name];
      // Non-CNC calls still represent actual work (file reads, shell commands, etc.).
      const presentation = action || [name.includes("read") ? "读取工程资料" : name.includes("shell") || name.includes("exec") ? "执行工程辅助任务" : "执行辅助操作", "process"] as const;
      const args = record(event.data.arguments);
      const requestedMode = args?.view_mode || args?.mode;
      const observationStage: Stage | undefined = name === "observe_model" ? requestedMode === "toolpath" || requestedMode === "刀路" ? "toolpath"
        : requestedMode === "simulation" || requestedMode === "仿真" ? "simulation" : requestedMode === "process" || requestedMode === "工艺" ? "process" : undefined : undefined;
      const activity: Activity = { id: String(event.data.callId), label: presentation[0], stage: observationStage || presentation[1], time: event.time, status: "running", navigable: Boolean(action),
        operationId: typeof args?.operation_id === "string" ? args.operation_id : undefined,
        featureIds: Array.isArray(args?.feature_ids) ? args.feature_ids.filter((id): id is string => typeof id === "string") : typeof args?.feature_id === "string" ? [args.feature_id] : undefined };
      result.push(activity); byCall.set(activity.id, activity);
    }
    if (event.type === "tool/result") {
      const message = record(event.data.message);
      const source = record(message?.source);
      const activity = byCall.get(String(source?.callId));
      if (activity) { activity.status = message?.isError ? "error" : "done"; activity.completedAt = event.time; }
    }
  }
  return result;
}

export const PROCESS_PROMPT = `请使用 CNC 工具自主分析本次上传的 STEP/STP 模型，并完成可验证的 Citizen Cincom L32 加工工艺规划。全程简体中文。
创建一个新的 CNC 分析任务，记录 job_id，持续查询直到几何解析完成或返回明确错误。直接读取任务上下文和几何证据，识别全部制造特征、回转轮廓、非回转区域、孔槽型腔台阶端面倒角，以及装夹和加工方向。
检查实际机床配置、可用刀具与工序目录。建立零工序 DRAFT，按真实几何和加工可达性逐道设计工序；每道工序计算真实刀路、执行连续材料去除仿真，检查碰撞、过切、残料和特征覆盖，验证后才接受。失败时根据证据修正并重试，禁止重复无效参数。
按需要使用 observe_model 观察整体或局部，在调用中明确 feature_ids 和 operation_id，保持画布展示与当前工作对象一致。使用 CNC 工具保存工艺草案和验证成果，而不只给文字建议。
最后执行 finalize_harness_process_plan 的整件累计验证，归档可用 G 代码和证据，明确 VERIFIED、PARTIALLY_VERIFIED 或 DRAFT 状态。缺少材料、公差或设备信息时记录待确认项，禁止编造。不要在未执行整件仿真和覆盖检查时宣告完成。简短汇报当前工程动作及其结果。`;

