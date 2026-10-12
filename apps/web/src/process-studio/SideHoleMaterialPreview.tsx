import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { ModelViewer } from "../ModelViewer";
import type { ManufacturingFeature } from "../types";

const features:ManufacturingFeature[]=[];
const ids:string[]=[];
const ignoreSelection=()=>{};

export function SideHoleMaterialPreview({url,onClose}:{url:string;onClose:()=>void}) {
  const dialog=useRef<HTMLDialogElement>(null);
  useEffect(()=>{dialog.current?.showModal();},[]);
  return createPortal(<dialog ref={dialog} className="side-hole-material-dialog" aria-label="本次侧孔检查剩余材料" onCancel={onClose}>
    <header><strong>本次侧孔检查 · 剩余材料</strong><button type="button" onClick={onClose}>关闭材料预览</button></header>
    <p>拖动旋转、滚轮缩放。显示本次实体检查的剩余材料；这是参考试算状态，尚未计入正式工序。</p>
    <div className="side-hole-material-view"><ModelViewer modelUrl={url} features={features} selectedFeatureIds={ids} onSelectFeature={ignoreSelection} showFeatureSummary={false} operationContextVisible releaseContextOnCleanup viewMode="工艺"/></div>
  </dialog>,document.body);
}
