"""Keep geometric back-side trials separate from a verified transfer draft."""
import json

from .agent.l32_operation_trial import enabled_operations, evidence_context_signature, operation_signature, reconcile_state


def bind_transfer_stock(job, geometry, snapshot, request, state):
    """Use exactly the stock before the cutoff, never caller-supplied voids."""
    from .agent.l32_operation_trial import _simulation_range
    from .turning_simulation import TurningSimulationResult
    operations = enabled_operations(job)
    position = next((i for i, op in enumerate(operations) if op.id == request.operation.id), None)
    if position is None or operation_signature(operations[position]) != operation_signature(request.operation):
        raise ValueError("transfer cutoff must match an enabled operation in the current process plan")
    if request.profile is None or request.profile.side != "outer":
        raise ValueError("transfer requires a full outer protection profile")
    diameter = job.plan.stock.get("diameter_mm")
    if not isinstance(diameter, (int, float)) or isinstance(diameter, bool) or diameter <= 0:
        raise ValueError("transfer requires the current process stock diameter")
    context = evidence_context_signature(job, geometry, snapshot)
    accepted = reconcile_state(job, state, context)["accepted"]
    if len(accepted) < position or any(r.get("acceptance_state", "accepted") != "accepted" for r in accepted[:position]):
        raise ValueError("transfer requires all preceding operations to be accepted in the current material context")
    z_min, z_max = _simulation_range(job, request.profile)
    updates = {"stock_radius_mm": diameter / 2, "initial_bore_radius_mm": 0,
               "initial_samples": None, "z_min_mm": z_min, "z_max_mm": z_max,
               "record_material_frames": True}
    source = {"source": "process_solid_stock", "source_operation_id": None}
    if position:
        previous = accepted[position - 1]
        material = TurningSimulationResult.model_validate(previous["cumulative_simulation"])
        if material.status != "completed" or len(material.samples) < 2:
            raise ValueError("preceding cumulative material state is incomplete")
        updates.update(initial_samples=material.samples, resolution_mm=material.resolution_mm)
        source = {"source": "accepted_cumulative_stock", "source_operation_id": previous["operation_id"]}
    return request.model_copy(update=updates), source


def transfer_identity(job, geometry, snapshot):
    return {"context_signature": evidence_context_signature(job, geometry, snapshot),
            "stock_binding_revision": "cumulative-v1",
            "operation_signatures": [operation_signature(op) for op in enabled_operations(job)]}


def assess_workholding(job, directory, geometry, snapshot):
    operations = enabled_operations(job)
    backs = [op for op in operations if op.workpiece_side == "back" or op.spindle_id == "sub"]
    payload = {"status": "not_required", "back_operation_ids": [op.id for op in backs],
               "blockers": [], "production_ready": False}
    if not backs:
        return payload
    reasons = []
    first_back = min(operations.index(op) for op in backs)
    cutoffs = [op for op in operations[:first_back] if op.type == "turn_cutoff"]
    if not cutoffs:
        reasons.append("背面加工之前缺少切断工序；尚未建立主轴到背轴的转接顺序。")
    for op in backs:
        if op.spindle_id != "sub" or op.channel_id != "sub":
            reasons.append(f"{op.id} 的背面加工未绑定背轴及背轴通道。")
    path = directory / "turning-transfer-evidence.json"
    try:
        evidence = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        evidence = {}
    if not isinstance(evidence, dict):
        evidence = {}
    identity = transfer_identity(job, geometry, snapshot)
    if not evidence:
        reasons.append("缺少当前工艺的切断、接料与夹持转接试算证据；几何试算不代表转接完成。")
    elif any(evidence.get(key) != value for key, value in identity.items()):
        reasons.append("转接证据已失效：工序、毛坯、几何或设备配置发生变化，需要重新试算。")
    else:
        payload["material_source"] = evidence.get("material_source")
        from .turning_transfer import TurningTransferDraftResult, cutoff_kerf_intrusion_mm
        try:
            result = TurningTransferDraftResult.model_validate(evidence.get("result", {}))
            cutoff = next((op for op in cutoffs if op.id == result.cutoff_operation_id), None)
            states = [item.state for item in result.state_transitions]
            profile = next((p for p in geometry.profiles if p.id == evidence.get("profile_id") and p.side == "outer"), None)
            if profile is None or cutoff is None or cutoff_kerf_intrusion_mm(cutoff, profile) > .05:
                reasons.append("切断轮廓依据缺失或刀缝侵入成品轮廓超过 0.05 mm。")
            if (result.job_id != job.id or result.machine_instance_id != job.machine_instance_id
                    or result.machine_configuration_hash != snapshot.configuration_hash
                    or cutoff is None or states != ["main_spindle_held", "dual_spindle_clamped",
                        "phase_synchronized", "part_separated", "sub_spindle_held"]
                    # Residual stock may belong to the subsequent back operations.
                    # Cutting into the protected part can never be a valid transfer.
                    or result.verification is None or result.verification.metrics.overcut_sample_count > 0):
                reasons.append("转接试算的工序、状态顺序或材料验证不完整。")
            state_path = directory / "harness-l32-operation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else None
            accepted = reconcile_state(job, state, identity["context_signature"]).get("accepted", [])
            if cutoff is None or cutoff.id not in {r["operation_id"] for r in accepted if r.get("acceptance_state", "accepted") == "accepted"}:
                reasons.append("切断尚未通过当前累计材料状态的独立试算接受。")
        except (OSError, ValueError, TypeError, KeyError):
            reasons.append("转接证据损坏或不完整，需要重新试算。")
    payload.update(status="blocked" if reasons else "draft_verified", blockers=reasons)
    return payload


def require_workholding(job, directory, geometry, snapshot):
    evidence = assess_workholding(job, directory, geometry, snapshot)
    if evidence["status"] == "blocked":
        raise ValueError("workholding_evidence_missing: " + "；".join(evidence["blockers"]))
