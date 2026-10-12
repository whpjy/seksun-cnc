"""Read-only, revision-checked playback evidence for the process workbench."""
import json
import re
from functools import lru_cache

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/api/v1/jobs/{job_id}/agent/l32/operations/{operation_id}/preview")
def operation_preview(job_id: str, operation_id: str) -> dict:
    from .main import job_directory, load_job, load_machine_snapshot
    from .agent.l32_operation_trial import (
        STATE_SCHEMA_VERSION, evidence_context_signature, operation_signature, reconcile_state,
        _profile_for_operation, _initial_bore_radius,
    )
    from .rotational_features import RotationalFeatureAnalysis
    from .agent.l32_operation_trial import _reference_profile

    if not re.fullmatch(r"[A-Za-z0-9_-]+", operation_id):
        raise HTTPException(404, "工序不存在")
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(409, "当前接口仅提供 L32 工序试算证据")
    operation = next((item for setup in (job.plan.setups if job.plan else [])
                      for item in setup.operations if item.id == operation_id and item.enabled), None)
    if operation is None:
        raise HTTPException(404, "工序不存在")
    directory = job_directory(job_id)
    def read(name: str) -> dict:
        path = directory / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    result = read("agent-l32-operation-trial.json")
    if result.get("operation_id") != operation_id:
        result = read(f"agent-l32-trial-{operation_id}.json")
    if not result:
        raise HTTPException(404, "该工序尚无试算证据")
    rotational_path = directory / "rotational-features.json"
    machine_path = directory / "machine-configuration.json"
    if not rotational_path.is_file() or not machine_path.is_file():
        raise HTTPException(409, "几何或机床证据尚未就绪")
    rotational = RotationalFeatureAnalysis.model_validate_json(rotational_path.read_text(encoding="utf-8"))
    context = evidence_context_signature(job, rotational, load_machine_snapshot(machine_path))
    state = reconcile_state(job, read("harness-l32-operation-state.json"), context)
    accepted = state.get("accepted", [])
    # Accepted current operations follow their predecessors, not their own result.
    index = next((i for i, item in enumerate(accepted) if item["operation_id"] == operation_id), len(accepted))
    prefix = accepted[:index]
    if (result.get("schema_version") != STATE_SCHEMA_VERSION
            or result.get("operation_signature") != operation_signature(operation)
            or result.get("context_signature") != context
            or result.get("accepted_prefix_signatures") != [item["operation_signature"] for item in prefix]):
        raise HTTPException(409, "工序或前序材料已改变，等待重新试算")
    simulation = result.get("cumulative_simulation") or {}
    after = simulation.get("samples") or []
    before = (prefix[-1].get("cumulative_simulation") or {}).get("samples", []) if prefix else []
    if after and not before:
        radius = float(job.plan.stock["diameter_mm"]) / 2
        bore = _initial_bore_radius(operation, None)
        before = [{"z": sample["z"], "outer_radius": radius, "inner_radius": bore} for sample in after]
    profile = _profile_for_operation(operation, rotational) or _reference_profile(job, rotational)
    axis = next((item for item in rotational.axes if profile and item.id == profile.axis_id), None)
    frames = simulation.get("material_frames") or []
    if result.get("toolpath") and before and after and not frames:
        frames = replay_frames(json.dumps({"program": result["toolpath"], "before": before,
                                         "after": after, "stock_radius": float(job.plan.stock["diameter_mm"]) / 2,
                                         "resolution": simulation["resolution_mm"]}, sort_keys=True))
    channel = operation.channel_id or "main"
    cutoff = next((item for setup in job.plan.setups for item in setup.operations if item.type == "turn_cutoff"), None)
    source_cutoff_z = float(cutoff.parameters.get("finished_back_datum_z_mm", cutoff.parameters.get("z_mm", 0))) if cutoff else None
    return {
        "operation_id": operation_id, "revision": f"{context}:{result['operation_signature']}:{hashlib_digest(result)}",
        "status": result.get("status"), "reason": result.get("reason"), "can_accept": result.get("can_accept", False),
        "evidence": result.get("evidence", {}), "toolpath": result.get("toolpath"),
        "axis": axis.model_dump(mode="json") if axis else None,
        "channel_id": channel, "source_cutoff_z": source_cutoff_z, "coordinate_frame": "source_model",
        "before_samples": before, "after_samples": after,
        "material_frames": frames,
    }


@lru_cache(maxsize=32)
def replay_frames(payload: str) -> list[dict]:
    """Recover old trial checkpoints with the same solver, requiring an identical final stock."""
    from .toolpath_ir import ToolpathProgram
    from .turning_simulation import TurningStockSample, simulate_turning_stock
    data = json.loads(payload)
    before = [TurningStockSample.model_validate(sample) for sample in data["before"]]
    result = simulate_turning_stock(ToolpathProgram.model_validate(data["program"]),
        stock_radius_mm=data["stock_radius"],
        z_min_mm=before[0].z, z_max_mm=before[-1].z, resolution_mm=data["resolution"],
        initial_samples=before, record_material_frames=True)
    if len(result.samples) != len(data["after"]) or any(
        abs(sample.z - expected["z"]) > 1e-6
        or abs(sample.outer_radius - expected["outer_radius"]) > 1e-6
        or abs(sample.inner_radius - expected["inner_radius"]) > 1e-6
        for sample, expected in zip(result.samples, data["after"])
    ):
        raise HTTPException(409, "历史试算无法复现，需重新试算")
    return result.material_frames


@router.get("/api/v1/jobs/{job_id}/agent/l32/playback")
def cumulative_playback(job_id: str, through_operation_id: str) -> dict:
    from .main import load_job
    from .agent.l32_operation_trial import enabled_operations
    job = load_job(job_id)
    operations = enabled_operations(job)
    index = next((i for i, item in enumerate(operations) if item.id == through_operation_id), None)
    if index is None:
        raise HTTPException(404, "工序不存在")
    previews = []
    for operation in operations[:index + 1]:
        try:
            preview = operation_preview(job_id, operation.id)
            if not preview["material_frames"] or not preview["axis"]:
                return {"complete": False, "previews": previews, "missing_operation_id": operation.id,
                        "message": "该工序尚无可累计回放的材料记录"}
            previews.append(preview)
            if not preview["can_accept"] and operation.id != through_operation_id:
                return {"complete": False, "previews": previews, "missing_operation_id": operation.id,
                        "message": "前序工序未通过，累计回放停在阻断位置"}
        except HTTPException as error:
            if error.status_code not in {404, 409}:
                raise
            return {"complete": False, "previews": previews, "missing_operation_id": operation.id, "message": error.detail}
    return {"complete": True, "previews": previews}


def hashlib_digest(result: dict) -> str:
    """Distinguish new trial evidence without exposing stock samples in the revision."""
    import hashlib
    return hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()[:16]
