import { useEffect, useMemo, useState } from "react";
import {
  Box,
  Code2,
  Database,
  Download,
  FileText,
  Image as ImageIcon,
  LoaderCircle,
  X,
} from "lucide-react";

import type { AgentArtifact } from "./types";

type AgentArtifactViewerProps = {
  artifact: AgentArtifact;
  apiUrl: (path: string) => string;
  onClose: () => void;
};

function resolveArtifactUrl(apiUrl: (path: string) => string, url?: string) {
  if (!url) return "";
  if (/^https?:\/\//i.test(url)) return url;
  return apiUrl(url);
}

function ArtifactIcon({ kind }: { kind: string }) {
  if (kind === "model") return <Box size={18} />;
  if (kind === "image") return <ImageIcon size={18} />;
  if (kind === "json") return <Code2 size={18} />;
  return <FileText size={18} />;
}

export default function AgentArtifactViewer({ artifact, apiUrl, onClose }: AgentArtifactViewerProps) {
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const url = useMemo(() => resolveArtifactUrl(apiUrl, artifact.url), [apiUrl, artifact.url]);
  const isImage = artifact.kind === "image" || /\.(png|jpe?g|webp|gif|svg)$/i.test(artifact.filename || artifact.url || "");
  const isJson = artifact.kind === "json" || /\.json(?:$|[?#])/i.test(artifact.filename || artifact.url || "");
  const isText = !isJson && (artifact.kind === "text" || /\.(txt|log|md|csv|yaml|yml)$/i.test(artifact.filename || artifact.url || ""));

  useEffect(() => {
    let cancelled = false;
    setContent("");
    setError("");
    if (!url || !isText) return;
    setLoading(true);
    fetch(url)
      .then(async (response) => {
        if (!response.ok) throw new Error(`文件读取失败（${response.status}）`);
        const text = await response.text();
        if (!cancelled) setContent(text);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "文件读取失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isText, url]);

  let formatted = content;
  return (
    <section className="agent-artifact-viewer" aria-label={`${artifact.label} 文件预览`}>
      <header className="agent-artifact-viewer__header">
        <span className="agent-artifact-viewer__icon"><ArtifactIcon kind={artifact.kind} /></span>
        <div>
          <span>AI 运行产物</span>
          <strong>{artifact.label}</strong>
          <small>{artifact.filename || artifact.kind.toUpperCase()}</small>
        </div>
        <div className="agent-artifact-viewer__actions">
          {url && (
            <a href={url} target="_blank" rel="noreferrer" title="下载或在新窗口打开">
              <Download size={17} />
            </a>
          )}
          <button type="button" onClick={onClose} title="关闭文件预览"><X size={18} /></button>
        </div>
      </header>
      <div className="agent-artifact-viewer__canvas">
        {loading && <div className="agent-artifact-viewer__state"><LoaderCircle className="spin" />正在读取文件…</div>}
        {error && <div className="agent-artifact-viewer__state agent-artifact-viewer__state--error">{error}</div>}
        {!loading && !error && isImage && url && <img src={url} alt={artifact.label} />}
        {!loading && !error && isText && <pre>{formatted || "该文件暂无可预览内容。"}</pre>}
        {!loading && !error && isJson && (
          <div className="agent-artifact-viewer__state">
            <Database size={24} />
            <strong>内部数据已隐藏</strong>
            <span>JSON 仅供智能体和制造工具内部交换，不再加载到界面。</span>
          </div>
        )}
        {!loading && !error && !isImage && !isText && !isJson && (
          <div className="agent-artifact-viewer__state">
            <ArtifactIcon kind={artifact.kind} />
            <strong>此文件适合在外部工具中查看</strong>
            <span>可以通过右上角按钮打开或下载。</span>
          </div>
        )}
      </div>
    </section>
  );
}
