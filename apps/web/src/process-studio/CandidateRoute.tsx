import { useEffect, useState } from "react";

type Step = {
  id: string; name: string; reason: string; feature_ids: string[];
  prerequisite_ids: string[]; blockers: string[]; conditional: boolean;
  operation_ids: string[]; status: "pending" | "established" | "trial_accepted";
};
  type Route = { steps: Step[]; note: string; exact_target_protection?: {status:string;first_failed_operation_id?:string;radial_tolerance_mm:number;stages:{operation_id:string;status:string;missing_target_mm3:number|null;protected_missing_target_mm3:number}[]}; workholding?: { status: string; blockers: string[]; material_source?: {source: string; source_operation_id?: string} }; material_profiles?: {
  profile_id: string; side: string; status: string; source_operation_id: string;
  evaluated_sample_count: number;
  overcut_sample_count: number; excess_stock_sample_count: number;
  estimated_excess_stock_volume_mm3: number; remaining_z_ranges_mm: number[][];
}[] };

export default function CandidateRoute({ jobId, onSelect }: {
  jobId: string; onSelect: (features: string[], operation?: string) => void;
}) {
  const [route, setRoute] = useState<Route | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController | undefined;
    const refresh = async () => {
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 15000);
      try {
        const response = await fetch(`/api/v1/jobs/${jobId}/candidate-route`, { cache: "no-store", signal: controller.signal });
        if (response.status === 409) { if (active) setRoute(null); return; }
        if (!response.ok) throw new Error("候选加工步骤暂时无法读取");
        const next: Route = await response.json();
        if (active) { setRoute(next); setError(""); }
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : "候选加工步骤加载失败");
      } finally {
        clearTimeout(timeout);
        if (active) timer = setTimeout(refresh, 5000);
      }
    };
    void refresh();
    return () => { active = false; clearTimeout(timer); controller?.abort(); };
  }, [jobId]);
  if (!route) return error ? <p className="studio-warning">{error}</p> : null;
  const names = new Map(route.steps.map(step => [step.id, step.name]));
    const latestProtection = route.exact_target_protection?.stages.at(-1);
  const pending = route.steps.filter(step => !step.conditional && step.status === "pending").length;
  return <details className="studio-candidate-route" open aria-label="候选加工步骤">
    <summary>候选加工步骤 · {pending} 项待建立</summary>
    <p className="studio-panel-empty">{route.note}</p>
    {route.exact_target_protection && <div aria-label="STEP 实体保护检查">
      <h4 className="studio-validation-title">STEP 实体保护检查</h4>
      <p className="studio-panel-empty">完整 STEP 与累计车削包络比较，径向几何容差 {route.exact_target_protection.radial_tolerance_mm} mm；此检查不代表机床碰撞验证。</p>
      {route.exact_target_protection.status === "failed" ? <>
        <p className="studio-warning">发现成品实体缺失 · 最早失败工序 {route.exact_target_protection.first_failed_operation_id}，需要重算前序。</p>
        <p className="studio-warning">{typeof latestProtection?.missing_target_mm3 === "number" ? `当前累计缺失成品体积 ${latestProtection.missing_target_mm3.toFixed(2)} mm³` : "原始差集未返回有效体积"}</p>
        <p className="studio-warning">容差比较后缺失成品体积 {latestProtection?.protected_missing_target_mm3.toFixed(2)} mm³</p>
      </> : <p className="studio-panel-empty">径向几何容差内未发现成品实体缺失 · 仍为草案验证</p>}
      {latestProtection?.missing_target_mm3 === null && <p className="studio-panel-empty">原始差集体积未知；容差保护检查已完成，未将未知体积计为零。</p>}
    </div>}
    {route.workholding && route.workholding.status !== "not_required" && <div aria-label="背面加工转接检查">
      <h4 className="studio-validation-title">背面加工转接检查</h4>
      <p className="studio-panel-empty">{route.workholding.status === "blocked" ? "转接未验证 · 整体验证受阻" : "转接草案已验证 · 仍需设备实机验证"}</p>
      {route.workholding.material_source && <p className="studio-panel-empty">转接材料：{route.workholding.material_source.source_operation_id ? `继承工序 ${route.workholding.material_source.source_operation_id} 的加工结果` : "从当前实心毛坯开始"}</p>}
      {route.workholding.blockers.map((reason, index) => <p className="studio-warning" key={index}>{reason}</p>)}
    </div>}
    {!!route.material_profiles?.length && <div aria-label="完整轮廓残料检查">
      <h4 className="studio-validation-title">完整轮廓残料检查</h4>
      <p className="studio-panel-empty">来自当前已接受的累计材料状态，按 0.05 mm 几何容差检查；仍需设备、夹持及图纸质量验证。</p>
      {route.material_profiles.map(profile => <button className="studio-target" key={profile.profile_id} onClick={() => onSelect([profile.profile_id], profile.source_operation_id)}>
        <strong>{profile.side === "inner" ? "内轮廓" : "外轮廓"} · {profile.profile_id}</strong>
        <small>{profile.evaluated_sample_count < 2 ? "轮廓范围内材料样本不足，尚不能判断完成" : profile.overcut_sample_count ? `过切 ${profile.overcut_sample_count} 个样本` : profile.excess_stock_sample_count ? `仍有残料约 ${profile.estimated_excess_stock_volume_mm3.toFixed(2)} mm³` : "未检测到过切或残料 · 几何检查"}</small>
        {profile.remaining_z_ranges_mm.map((range, index) => <small key={index}>残料 Z：{range[0].toFixed(3)} ～ {range[1].toFixed(3)} mm</small>)}
      </button>)}
    </div>}
    {route.steps.map(step => <div key={step.id} className="studio-candidate-step">
      <button className="studio-target" onClick={() => onSelect(step.feature_ids, step.operation_ids[0])}>
        <strong>{step.name}</strong>
        <small>{step.status === "trial_accepted" ? "已有接受试算 · 仍需整件验证" : step.status === "established" ? "已建立工序 · 待验证" : step.conditional ? "条件步骤 · 待判断" : "待建立"} · 点击定位</small>
      </button>
      <small>{step.reason}</small>
      {!!step.prerequisite_ids.length && <small>前置：{step.prerequisite_ids.map(id => names.get(id) || id).join("、")}</small>}
      {step.status === "pending" && step.blockers.map((blocker, index) => <small className="studio-warning" key={index}>{blocker}</small>)}
    </div>)}
    {error && <p className="studio-warning">{error}</p>}
  </details>;
}
