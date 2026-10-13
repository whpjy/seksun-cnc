"""Keep candidate machining steps separate from executable operation evidence."""
import json
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from .machining_geometry import axial_hole, job_axis
from .models import JobResponse
from .rotational_features import RotationalFeatureAnalysis

router = APIRouter()


class CandidateStep(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$", max_length=80)
    name: str = Field(min_length=1, max_length=120)
    feature_ids: list[str] = Field(default_factory=list, max_length=100)
    operation_types: list[str] = Field(min_length=1, max_length=10)
    prerequisite_ids: list[str] = Field(default_factory=list, max_length=30)
    reason: str = Field(min_length=1, max_length=1500)
    blockers: list[str] = Field(default_factory=list, max_length=30)
    conditional: bool = False
    workpiece_side: Literal["front", "back"] | None = None


class CandidateRoute(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    source: Literal["geometry", "harness"] = "harness"
    steps: list[CandidateStep] = Field(max_length=100)

    @model_validator(mode="after")
    def dependencies(self):
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("候选步骤编号不能重复")
        graph = {step.id: step.prerequisite_ids for step in self.steps}
        visited, visiting = set(), set()
        def visit(key):
            if key in visiting:
                raise ValueError("候选步骤前置关系不能形成循环")
            if key not in graph:
                raise ValueError("前置步骤编号不存在")
            if key in visited:
                return
            visiting.add(key)
            for parent in graph[key]:
                visit(parent)
            visiting.remove(key)
            visited.add(key)
        for key in graph:
            visit(key)
        return self


def propose_route(job: JobResponse, geometry: RotationalFeatureAnalysis) -> CandidateRoute:
    """Feature-driven checklist; no tool selection, setup assignment or acceptance."""
    steps = []
    def add(id, name, refs, types, reason, parents=(), blockers=(), conditional=False):
        steps.append(CandidateStep(id=id, name=name, feature_ids=refs, operation_types=types,
                                   reason=reason, prerequisite_ids=list(parents),
                                   blockers=list(blockers), conditional=conditional))
    outer = [p.id for p in geometry.profiles if p.side == "outer" and p.review_state != "excluded"]
    inner = [p.id for p in geometry.profiles if p.side == "inner" and p.review_state != "excluded"]
    if outer:
        add("FACE", "建立端面基准", outer, ["turn_facing"], "从实心毛坯建立轴向加工基准；核对端面与保留余量。")
        add("OUTER-ROUGH", "外形粗加工（按残料需要）", outer, ["turn_od_roughing"], "余量较大时分层去除；残料不足则不需要单独粗车。", ["FACE"], conditional=True)
        add("OUTER-FINISH", "外形精加工", outer, ["turn_od_finishing"], "按成品外形及图纸精度加工；先确认各区域的可达方向。", ["FACE"])
    grooves = [f.id for f in geometry.features if "groove" in f.kind and f.review_state != "excluded"]
    if grooves:
        add("GROOVES", "槽加工", grooves, ["turn_grooving"], "槽宽、槽深与刀具包络匹配，并核对图纸修订中取消的槽。", ["FACE"] if outer else [], ["核对图纸槽要求及专用槽刀可达性"])
    axis = job_axis(job.model_dump(mode="json"), geometry.model_dump(mode="json"))
    holes = [f for f in (job.analysis.cylindrical_features if job.analysis else []) if f.kind == "hole" and f.review_state != "excluded"]
    axial = [f.id for f in holes if axial_hole(f.model_dump(mode="json"), axis)]
    side = [f.id for f in holes if f.id not in axial]
    if axial:
        add("PILOT", "轴向开底孔", axial, ["axial_drilling"], "实心毛坯先真实开孔；底孔直径需容纳后续镗杆并保留内孔加工余量。", ["FACE"] if outer else [], ["核对钻深、钻尖与镗杆所需入口直径"])
    if inner:
        parents = ["PILOT"] if axial else []
        blockers = ["确认前置孔的实际材料状态及分区域加工方向"]
        add("INNER-ROUGH", "内孔粗加工（按残料需要）", inner, ["turn_id_roughing"], "按实测仿真孔径与余量决定粗镗；有倒扣时分区域、分方向规划。", parents, blockers, True)
        add("INNER-FINISH", "内孔精加工", inner, ["turn_id_finishing"], "完成内孔轮廓，核对公差与粗糙度；不能把底孔或粗镗当作内孔完成。", parents, blockers)
    if side:
        add("SIDE-HOLES", "偏置或径向孔加工", side, ["radial_drilling", "radial_hole_finishing"], "按孔轴线和偏置规划；钻尖与孔底冲突时比较预钻回退及平底修整，残料必须由明确的后续工序清除。", blockers=["必须核对钻尖与孔底冲突，并补齐实刀、机床坐标、刀位行程与夹具验证；参考检查不能接受工序"])
    if outer:
        add("BACK", "背面处理（按可达性需要）", outer + inner, ["turn_facing", "turn_od_finishing", "turn_id_finishing"], "仅在正面无法完成的区域需要；两套装夹模板不代表必须夹持两次。", blockers=["确认待加工背面区域、接料和夹持方案"], conditional=True)
        add("CUTOFF", "切断与接料（按供料方式需要）", outer, ["turn_cutoff"], "棒料加工时确认切离位置与接料；顺序取决于供料与装夹方案。", blockers=["确认棒料供料、切断面及接料流程"], conditional=True)
    return CandidateRoute(source="geometry", steps=steps)


def _inputs(job_id):
    from .main import load_job, job_directory
    job = load_job(job_id)
    directory = job_directory(job_id)
    path = directory / "rotational-features.json"
    if job.device_id != "citizen-cincom-l32" or not job.plan or not path.is_file():
        raise HTTPException(409, "候选步骤需要 L32 工艺草案及回转几何")
    return job, directory, RotationalFeatureAnalysis.model_validate_json(path.read_text(encoding="utf-8"))


def route_progress(job, geometry, route, state):
    from .agent.l32_operation_trial import enabled_operations, reconcile_state
    accepted = {r["operation_id"] for r in reconcile_state(job, state).get("accepted", [])
                if r.get("acceptance_state", "accepted") == "accepted"}
    operations = enabled_operations(job)
    material_profiles = []
    records = reconcile_state(job, state).get("accepted", [])
    if records:
        from .turning_simulation import TurningSimulationResult
        from .turning_verification import verify_turning_profile
        simulation = TurningSimulationResult.model_validate(records[-1]["cumulative_simulation"])
        for profile in geometry.profiles:
            if profile.review_state == "excluded":
                continue
            verification = verify_turning_profile(profile.model_copy(update={"review_state": "accepted"}), simulation, tolerance_mm=.05)
            z_ranges = []
            for item in sorted((item for item in verification.deviations if item.kind == "excess_stock"), key=lambda item: item.z):
                if not z_ranges or item.z - z_ranges[-1][1] > simulation.resolution_mm * 1.5:
                    z_ranges.append([round(item.z,6), round(item.z,6)])
                else:
                    z_ranges[-1][1] = round(item.z,6)
            material_profiles.append({"profile_id": profile.id, "side": profile.side,
                "status": verification.status, "evaluated_sample_count": verification.metrics.evaluated_sample_count,
                "overcut_sample_count": verification.metrics.overcut_sample_count,
                "excess_stock_sample_count": verification.metrics.excess_stock_sample_count,
                "estimated_excess_stock_volume_mm3": verification.metrics.estimated_excess_stock_volume_mm3,
                "remaining_z_ranges_mm": z_ranges,
                "source_operation_id": records[-1]["operation_id"], "production_ready": False})
    rows = []
    for step in route.steps:
        side = step.workpiece_side
        if side is None and (step.id.upper() == "BACK" or step.id.upper().startswith(("BACK-", "BACK_")) or "背面" in step.name or "背轴" in step.name):
            side = "back"
        # A back-side candidate cannot be matched to an unrelated front operation.
        matches = [op for op in operations if op.type in step.operation_types
                   and set(op.feature_ids + ([op.reference_profile_id] if op.reference_profile_id else [])).intersection(step.feature_ids)
                   and (side is None or op.workpiece_side == side)]
        # This tracks work started on a goal, never whole-feature completion.
        # A physical bore may be represented by several CAD cylinder segments.
        status = "trial_accepted" if any(op.id in accepted for op in matches) else "established" if matches else "pending"
        rows.append({**step.model_dump(), "workpiece_side": side, "operation_ids": [op.id for op in matches], "status": status})
    return {"schema_version": route.schema_version, "source": route.source, "steps": rows, "material_profiles": material_profiles,
            "note": "候选步骤用于保留加工目标；顺序、装夹和选刀仍需逐道规划。已接受试算也不代表整件生产放行。"}


@router.get("/api/v1/jobs/{job_id}/candidate-route")
def get_candidate_route(job_id: str):
    from .main import load_machine_snapshot
    from .agent.l32_operation_trial import evidence_context_signature, reconcile_state
    job, directory, geometry = _inputs(job_id)
    path = directory / "candidate-route.json"
    route = CandidateRoute.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else propose_route(job, geometry)
    state_path = directory / "harness-l32-operation-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else None
    # Context edits must invalidate accepted labels, just as they invalidate trials.
    snapshot = load_machine_snapshot(directory / "machine-configuration.json")
    if state:
        state = reconcile_state(job, state, evidence_context_signature(job, geometry, snapshot))
    from .workholding_evidence import assess_workholding
    result = route_progress(job, geometry, route, state)
    protection_path = directory/"exact-target-protection-latest.json"
    if protection_path.is_file():
        protection = json.loads(protection_path.read_text(encoding="utf-8"))
        prefix = [r["operation_signature"] for r in (state or {}).get("accepted",[])]
        if protection.get("context_signature") == evidence_context_signature(job,geometry,snapshot) and protection.get("accepted_prefix_signatures") == prefix:
            result["exact_target_protection"] = protection
    result["workholding"] = assess_workholding(job, directory, geometry, snapshot)
    if result["workholding"]["status"] == "not_required" and any(
        step["workpiece_side"] == "back" and not step["conditional"] for step in result["steps"]
    ):
        result["workholding"].update(status="blocked", blockers=[
            "候选路线包含背面加工目标，但尚未建立背面工序及转接证据。"])
    return result


@router.put("/api/v1/jobs/{job_id}/candidate-route")
def save_candidate_route(job_id: str, route: CandidateRoute):
    from .main import write_json
    from .operation_library import get_operation_definition
    job, directory, geometry = _inputs(job_id)
    valid = {p.id for p in geometry.profiles if p.review_state != "excluded"} | {f.id for f in geometry.features if f.review_state != "excluded"}
    valid |= {f.id for f in job.analysis.cylindrical_features if f.review_state != "excluded"}
    for step in route.steps:
        if not step.feature_ids or not set(step.feature_ids).issubset(valid):
            raise HTTPException(422, "候选步骤必须引用当前未排除的真实特征或轮廓")
        for operation_type in step.operation_types:
            try:
                get_operation_definition(operation_type)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
    for goal in propose_route(job, geometry).steps:
        if goal.conditional:
            continue
        represented = {ref for step in route.steps if set(step.operation_types).intersection(goal.operation_types)
                       for ref in step.feature_ids}
        if not set(goal.feature_ids).issubset(represented):
            raise HTTPException(422, f"候选路线遗漏加工目标：{goal.name}，请保留未完成步骤及原因")
    route.source = "harness"
    write_json(directory / "candidate-route.json", route.model_dump(mode="json"))
    return get_candidate_route(job_id)
