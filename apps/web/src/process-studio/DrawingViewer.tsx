import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, FileText, LoaderCircle, Maximize, Minus, Plus, RotateCw, Scan } from "lucide-react";
import { getDocument, GlobalWorkerOptions } from "pdfjs-dist/legacy/build/pdf.mjs";
import type { PDFDocumentProxy, PDFPageProxy, RenderTask } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/legacy/build/pdf.worker.min.mjs?url";
import type { Job } from "../types";
import "./drawing-viewer.css";

GlobalWorkerOptions.workerSrc = workerUrl;

export default function DrawingViewer({ job }: { job: Job | null }) {
  const host = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const drag = useRef<{ id: number; x: number; y: number; left: number; top: number } | null>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [page, setPage] = useState<PDFPageProxy | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [rotation, setRotation] = useState(0);
  const [zoom, setZoom] = useState<number | null>(null);
  const [size, setSize] = useState({ width: 1, height: 1 });
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [loading, setLoading] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const url = job?.drawing_url || (job?.drawing_filename ? `/api/v1/jobs/${job.id}/files/drawing.pdf` : null);

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setSize({ width: entry.contentRect.width, height: entry.contentRect.height }));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let disposed = false;
    queueMicrotask(() => { if (!disposed) { setPdf(null); setPage(null); setPageNumber(1); setZoom(null); setOffset({ x: 0, y: 0 }); setRotation(0); setError(""); setLoading(Boolean(url)); } });
    if (!url) return () => { disposed = true; };
    const task = getDocument({ url, cMapUrl: "/pdfjs/cmaps/", cMapPacked: true, standardFontDataUrl: "/pdfjs/standard_fonts/", wasmUrl: "/pdfjs/wasm/" });
    void task.promise.then(document => { if (!disposed) setPdf(document); }).catch(reason => { if (!disposed) setError(`PDF 加载失败：${(reason as Error).message}`); }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; void task.destroy(); };
  }, [url, retry]);

  useEffect(() => {
    if (!pdf) return;
    let disposed = false;
    queueMicrotask(() => { if (!disposed) { setPage(null); setError(""); setZoom(null); setOffset({ x: 0, y: 0 }); } });
    void pdf.getPage(pageNumber).then(async value => {
      // Some engineering PDFs store landscape text on a portrait page without rotation metadata.
      let orientation = 0;
      if (!value.rotate) {
        try {
          const text = await value.getTextContent();
          const weights = new Map<number, number>();
          for (const item of text.items) {
            if (!("str" in item) || !item.str.trim()) continue;
            const angle = (Math.round(Math.atan2(item.transform[1], item.transform[0]) * 2 / Math.PI) * 90 + 360) % 360;
            weights.set(angle, (weights.get(angle) || 0) + item.str.trim().length);
          }
          orientation = [...weights].sort((a, b) => b[1] - a[1])[0]?.[0] || 0;
        } catch { /* Scanned sheets remain in their original orientation. */ }
      }
      if (!disposed) { setRotation(orientation); setPage(value); }
    }).catch(reason => { if (!disposed) setError(`页面读取失败：${(reason as Error).message}`); });
    return () => { disposed = true; };
  }, [pdf, pageNumber]);

  const viewport = page?.getViewport({ scale: 1, rotation: (page.rotate + rotation) % 360 });
  const fit = viewport ? Math.max(0.05, Math.min((size.width - 64) / viewport.width, (size.height - 180) / viewport.height)) : 1;
  const scale = zoom ?? fit;
  const width = (viewport?.width || 1) * scale;
  const height = (viewport?.height || 1) * scale;

  useEffect(() => {
    if (!page || !canvas.current) return;
    let disposed = false;
    let task: RenderTask | undefined;
    const element = canvas.current;
    // Render into a detached canvas so cancelled page/zoom renders cannot overwrite the next one.
    const buffer = document.createElement("canvas");
    const resolution = Math.min(window.devicePixelRatio || 1, 2, 8192 / Math.max(width, height), Math.sqrt(16_000_000 / (width * height)));
    const renderViewport = page.getViewport({ scale: scale * resolution, rotation: (page.rotate + rotation) % 360 });
    buffer.width = Math.max(1, Math.ceil(renderViewport.width)); buffer.height = Math.max(1, Math.ceil(renderViewport.height));
    const timer = window.setTimeout(() => {
      setRendering(true);
      task = page.render({ canvas: buffer, viewport: renderViewport });
      void task.promise.then(() => {
        if (disposed) return;
        element.width = buffer.width; element.height = buffer.height;
        element.getContext("2d")?.drawImage(buffer, 0, 0);
        setError("");
      }).catch(reason => { if (!disposed && (reason as Error).name !== "RenderingCancelledException") setError(`PDF 显示失败：${(reason as Error).message}`); }).finally(() => { if (!disposed) setRendering(false); });
    }, 80);
    return () => { disposed = true; window.clearTimeout(timer); task?.cancel(); };
  }, [page, scale, rotation, width, height]);

  useEffect(() => {
    const element = host.current;
    if (!element || !page) return;
    const wheel = (event: WheelEvent) => { event.preventDefault(); setZoom(Math.max(0.1, scale * (event.deltaY < 0 ? 1.1 : 1 / 1.1))); };
    element.addEventListener("wheel", wheel, { passive: false });
    return () => element.removeEventListener("wheel", wheel);
  }, [page, scale]);

  function changeZoom(factor: number) { setZoom(Math.max(0.1, scale * factor)); }
  function reset() { setZoom(null); setOffset({ x: 0, y: 0 }); }

  return <section className="drawing-viewer" aria-label="二维 PDF 图纸查看器">
    <div ref={host} className="drawing-sheet-viewport"
      onPointerDown={event => { if (!page || event.button !== 0) return; drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, left: offset.x, top: offset.y }; event.currentTarget.setPointerCapture(event.pointerId); }}
      onPointerMove={event => { const start = drag.current; if (start?.id === event.pointerId) setOffset({ x: start.left + event.clientX - start.x, y: start.top + event.clientY - start.y }); }}
      onPointerUp={event => { drag.current = null; if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId); }} onPointerCancel={() => { drag.current = null; }} onLostPointerCapture={() => { drag.current = null; }} onDoubleClick={reset}>
      {page && <div className="drawing-sheet" style={{ width, height, transform: `translate(calc(-50% + ${offset.x}px), calc(-50% + ${offset.y}px))` }}><canvas ref={canvas} role="img" aria-label={`${job?.drawing_filename} · 第 ${pageNumber} 页`} style={{ width: "100%", height: "100%" }} /></div>}
    </div>
    {(!url || loading || error) && <div className="drawing-message" role={error ? "alert" : "status"}>{loading ? <LoaderCircle className="spin" size={32} /> : <FileText size={36} />}<h2>{error ? "图纸暂时无法显示" : loading ? "正在打开二维图纸" : "当前任务未上传二维图纸"}</h2><p>{error || (url ? job?.drawing_filename : "新建任务时，选择配套的 PDF 图纸，便可在这里查看。")}</p>{error && <div><button onClick={() => setRetry(value => value + 1)}>重新加载</button><a href={url || undefined} target="_blank" rel="noreferrer">打开原始 PDF</a></div>}</div>}
    {url && <>
      {rendering && <div className="drawing-loading" role="status" aria-label="正在绘制图纸"><LoaderCircle size={16} className="spin" /></div>}
      <div className="drawing-toolbar" aria-label="图纸浏览工具">
        <button title="适应窗口" aria-label="图纸适应窗口" onClick={reset} disabled={!page}><Scan size={15} strokeWidth={1.6} /></button>
        <button title="放大" aria-label="放大图纸" disabled={!page} onClick={() => changeZoom(1.2)}><Plus size={16} strokeWidth={1.6} /></button>
        <button title="缩小" aria-label="缩小图纸" disabled={!page} onClick={() => changeZoom(1 / 1.2)}><Minus size={16} strokeWidth={1.6} /></button>
        <button title="顺时针旋转 90°" aria-label="旋转图纸" disabled={!page} onClick={() => { setRotation(value => (value + 90) % 360); reset(); }}><RotateCw size={15} strokeWidth={1.6} /></button>
        <button title="切换全屏" aria-label="图纸全屏" onClick={() => document.fullscreenElement ? void document.exitFullscreen() : void document.documentElement.requestFullscreen()}><Maximize size={15} strokeWidth={1.6} /></button>
        <output aria-label="图纸缩放比例">{Math.round(scale * 100)}%</output>
        <span className="drawing-toolbar-divider" />
        <button aria-label="上一页图纸" disabled={!pdf || pageNumber <= 1} onClick={() => setPageNumber(value => value - 1)}><ChevronLeft size={15} /></button>
        <span>{pageNumber} / {pdf?.numPages || "—"}</span>
        <button aria-label="下一页图纸" disabled={!pdf || pageNumber >= pdf.numPages} onClick={() => setPageNumber(value => value + 1)}><ChevronRight size={15} /></button>
      </div>
    </>}
  </section>;
}
