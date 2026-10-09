import { useState } from "react";
import { createPortal } from "react-dom";
import { Download, FileText, X } from "lucide-react";
import type { AgentWorkspace, Job } from "../types";
import type { JournalEvent } from "./harness";
import { rpc } from "./harness";
import { deliveriesIn, finalText, outcome } from "./results";
import type { Delivery, TrialState } from "./results";

export default function ResultCard({job,workspace,trials,events,sessionId,programAvailable,genericProgramAvailable,onValidation}: {job:Job|null;workspace:AgentWorkspace|null;trials:TrialState|null;events:JournalEvent[];sessionId?:string;programAvailable:boolean;genericProgramAvailable?:boolean;onValidation:()=>void}) {
  const text = finalText(events);
  const result = outcome(job, workspace, trials);
  const files = deliveriesIn(events,text);
  const [preview,setPreview] = useState<{label:string;text:string}|null>(null);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState("");
  const openFile = async (file:Delivery, download:boolean) => {
    if (!sessionId || busy) return;
    setBusy(true);setError("");
    try {
      let offset=1,body="",version:string|undefined;
      while (true) {
        const page=await rpc<{text:string;lines:number;eof:boolean;version:string}>("workspaceFiles/read",{workspaceFileScopeId:sessionId,path:file.path,range:{offset}});
        if(version && version!==page.version) throw Error("文件正在更新，请稍后重试。");
        version=page.version;body+=page.text;
        if(page.eof) break;
        if(!page.lines || body.length>5_000_000) throw Error("文件较大，暂无法完整读取。");
        offset+=page.lines;
      }
      if(download) { const url=URL.createObjectURL(new Blob([body],{type:"text/plain;charset=utf-8"}));const a=document.createElement("a");a.href=url;a.download=file.path.split(/[\\/]/).at(-1)!;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000); }
      else setPreview({label:file.label,text:body});
    }catch(reason){setError(reason instanceof Error ? reason.message : "交付物读取失败");}finally{setBusy(false);}
  };
  return <div className="studio-result-card">
    <strong>{result.label}</strong><span>{result.ready ? "已满足现有验证门禁" : "任务已结束 · 尚不可用于生产"}</span>
    <p>{result.accepted.length} 道工序试算已接受{result.coverage ? ` · ${result.coverage.covered_count}/${result.coverage.target_count} 个制造目标完整覆盖` : ""}</p>
    <p>{programAvailable ? "加工程序已归档，可在 G 代码页查看" : genericProgramAvailable ? "已生成通用 G 代码 · 未完成实机适配" : "尚未归档 G 代码"}</p>
    <div className="studio-result-actions"><button onClick={()=>setPreview({label:"任务最终报告",text:text||"当前会话未提供最终报告。"})}><FileText size={13}/>查看报告</button><button onClick={onValidation}>查看未完成项</button></div>
    {files.map(file=><div className="studio-delivery" key={file.path}><button title={file.label} disabled={busy||!sessionId} onClick={()=>void openFile(file,false)}><FileText size={13}/><span>{file.path.endsWith(".md") ? "工艺草案报告" : file.path.endsWith(".csv") ? "工序卡 CSV" : file.path.split(/[\\/]/).at(-1)}</span></button><button aria-label={`下载${file.label}`} disabled={busy||!sessionId} onClick={()=>void openFile(file,true)}><Download size={13}/></button></div>)}
    {busy&&<p>正在读取交付物…</p>}{error&&<p role="alert" className="studio-interaction-error">{error}</p>}
    {preview&&createPortal(<div className="studio studio-modal-backdrop studio-report-backdrop"><section className="studio-report"><header><strong>{preview.label}</strong><button aria-label="关闭报告" onClick={()=>setPreview(null)}><X size={18}/></button></header><pre>{preview.text}</pre></section></div>,document.body)}
  </div>;
}
