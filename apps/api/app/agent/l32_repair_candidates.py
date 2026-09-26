from __future__ import annotations

from typing import Any

from ..catalogs import TOOL_DEFINITIONS
from ..l32_front_groove import build_front_groove_geometry_draft
from ..models import L32OperationRepairCandidate, Operation
from ..rotational_features import RotationalFeatureAnalysis, RotationalProfile


def _compatible_tools(operation: Operation) -> list[dict[str, Any]]:
    expected_hand = (
        "right"
        if str(operation.parameters.get("cut_direction", "negative_z")) == "negative_z"
        else "left"
    )
    return sorted(
        (
            item for item in TOOL_DEFINITIONS
            if item.get("kind") == operation.tool.kind
            and item.get("hand", expected_hand) in {expected_hand, "neutral"}
        ),
        key=lambda item: (
            float(item.get("nose_radius_mm") or item.get("diameter_mm") or 999),
            str(item["id"]),
        ),
    )


def _deviation_range(trial: dict[str, Any], kind: str) -> tuple[float, float] | None:
    verification = trial.get("verification") or {}
    deviations = verification.get("deviations") or []
    values = [
        float(item["z"]) for item in deviations
        if isinstance(item, dict) and item.get("kind") == kind
        and isinstance(item.get("z"), (int, float)) and not isinstance(item.get("z"), bool)
    ]
    return (min(values), max(values)) if values else None


def _external_groove_context(
    operation: Operation,
    rotational: RotationalFeatureAnalysis | None,
) -> tuple[TurningProfileFeature | None, RotationalProfile | None]:
    if operation.type != "turn_grooving" or rotational is None:
        return None, None
    feature = next((
        item for item in rotational.features
        if item.id in operation.feature_ids and item.kind == "external_groove_candidate"
    ), None)
    exact_profile = next((
        item for item in rotational.profiles
        if feature is not None and item.id == feature.profile_id
    ), None)
    return feature, exact_profile


def propose_l32_operation_repairs(
    operation: Operation,
    trial: dict[str, Any],
    profile: RotationalProfile | None,
    *,
    operations: list[Operation] | None = None,
    rotational: RotationalFeatureAnalysis | None = None,
) -> dict[str, Any]:
    """Create bounded, evidence-led repair candidates without hiding defects.

    This function deliberately never proposes tolerance inflation or artificial
    finishing allowance. Those changes can make verification green without
    producing the intended part.
    """
    candidates: list[L32OperationRepairCandidate] = []
    observations: list[str] = []
    capability_requirements: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None, str | None, tuple[tuple[str, object], ...]]] = set()

    def add(identifier: str, rationale: str, *, tool_id: str | None = None,
            target_operation_id: str | None = None,
            reference_profile_id: str | None = None,
            parameters: dict[str, float | int | str | bool] | None = None) -> None:
        changes = parameters or {}
        signature = (target_operation_id, tool_id, reference_profile_id, tuple(sorted(changes.items())))
        if signature in seen or (
            target_operation_id in {None, operation.id}
            and tool_id == operation.tool.id
            and reference_profile_id in {None, operation.reference_profile_id}
            and not changes
        ):
            return
        seen.add(signature)
        candidates.append(L32OperationRepairCandidate(
            id=identifier, rationale=rationale, target_operation_id=target_operation_id,
            tool_id=tool_id, reference_profile_id=reference_profile_id, parameters=changes,
        ))

    evidence = trial.get("evidence") or {}
    if operation.type in {"pocket_roughing", "pocket_finishing"}:
        residual = float(evidence.get("remaining_feature_material_mm3") or 0)
        if residual > 0.0001:
            current_diameter = float(operation.tool.diameter_mm)
            smaller_tools = sorted(
                (
                    item for item in TOOL_DEFINITIONS
                    if item.get("kind") == "end_mill"
                    and item.get("catalog_match", True) is True
                    and 0 < float(item.get("diameter_mm") or 0) < current_diameter - 1e-9
                ),
                key=lambda item: (-float(item["diameter_mm"]), str(item["id"])),
            )
            for tool in smaller_tools[:2]:
                diameter = float(tool["diameter_mm"])
                add(
                    f"pocket-smaller-tool-{tool['id'].lower()}",
                    "Use a smaller verified end mill and recompute the exact pocket sweep from the accepted floor geometry.",
                    tool_id=str(tool["id"]),
                    parameters={"step_over_mm": round(max(0.02, diameter * 0.25), 4)},
                )
            add(
                "pocket-tighter-stepover",
                "Reduce raster stepover to distinguish path-density residual from finite cutter-radius corner stock.",
                parameters={"step_over_mm": round(max(0.02, current_diameter * 0.2), 4)},
            )
            observations.append(
                f"Exact pocket sweep leaves {residual:.6f} mm3; candidates must reduce this measured residual without target contact."
            )
            if operation.type == "pocket_finishing" and not smaller_tools:
                capability_requirements.append({
                    "target_operation_id": operation.id,
                    "feature_ids": list(operation.feature_ids),
                    "required_strategy": "sharp_corner_cleanup_or_accepted_internal_corner_radius",
                    "catalog_match": False,
                    "reason": "smallest_catalogued_end_mill_still_leaves_finite_radius_corner_material",
                    "next_action": "define_verified_edm_broach_form_tool_or_approve_corner_radius",
                })

    if operation.type in {"live_tool_contour_roughing", "live_tool_contour_finishing"}:
        remaining = float(evidence.get("remaining_excess_region_mm3") or 0)
        tolerance = float(evidence.get("coverage_tolerance_mm3") or 0.001)
        if remaining > tolerance:
            raw_clearance = operation.parameters.get("silhouette_clearance_mm")
            current_clearance = float(raw_clearance) if isinstance(raw_clearance, (int, float)) else 0.18
            for clearance in (current_clearance / 2, 0.03, 0.0):
                if clearance >= current_clearance - 1e-9:
                    continue
                add(
                    f"exterior-clearance-{clearance:g}",
                    "Reduce only the exterior silhouette offset, then prove gouge protection and residual removal with an exact OCC sweep.",
                    parameters={"silhouette_clearance_mm": round(clearance, 4)},
                )
            current_diameter = float(operation.tool.diameter_mm)
            smaller = next((
                item for item in sorted(TOOL_DEFINITIONS, key=lambda value: float(value.get("diameter_mm") or 999))
                if item.get("kind") == "end_mill"
                and item.get("catalog_match", True) is True
                and 0 < float(item.get("diameter_mm") or 0) < current_diameter - 1e-9
            ), None)
            if smaller is not None:
                diameter = float(smaller["diameter_mm"])
                add(
                    f"exterior-smaller-tool-{str(smaller['id']).lower()}",
                    "Use a smaller verified end mill to improve exterior silhouette reach while preserving exact target protection.",
                    tool_id=str(smaller["id"]),
                    parameters={
                        "step_over_mm": round(max(0.03, diameter * 0.35), 4),
                        "silhouette_clearance_mm": 0.03,
                    },
                )
            observations.append(
                f"Exact exterior sweep leaves {remaining:.6f} mm3 versus a {tolerance:.6f} mm3 acceptance limit."
            )

    detail = str(trial.get("detail") or "").lower()
    traceability_failure = "traceability does not reference the supplied profile" in detail
    if (
        traceability_failure
        and profile is not None
        and operation.reference_profile_id != profile.id
    ):
        add(
            "bind-reference-profile",
            "Bind the parent rotational profile as simulation evidence without adding it to machining targets.",
            reference_profile_id=profile.id,
        )
    compatible = _compatible_tools(operation)
    if "tool" in detail and ("hand" in detail or "左右手" in detail or "方向" in detail):
        replacement = next((item for item in compatible if item["id"] != operation.tool.id), None)
        if replacement:
            add(
                "correct-tool-hand",
                "Match tool hand to the programmed axial cutting direction.",
                tool_id=str(replacement["id"]),
            )

    verification = trial.get("verification") or {}
    metrics = verification.get("metrics") or {}
    overcut_count = int(metrics.get("overcut_sample_count") or 0)
    excess_count = int(metrics.get("excess_stock_sample_count") or 0)
    current_nose = float(operation.tool.nose_radius_mm or 0)
    if overcut_count or excess_count:
        smaller = next(
            (
                item for item in compatible
                if item["id"] != operation.tool.id
                and float(item.get("nose_radius_mm") or 999) < current_nose - 1e-9
            ),
            None,
        )
        if smaller:
            parameters: dict[str, float | int | str | bool] = {}
            if operation.type in {"turn_od_finishing", "turn_id_finishing"}:
                parameters["maximum_finish_nose_radius_mm"] = float(smaller["nose_radius_mm"])
            add(
                "smaller-nose-tool",
                "Reduce tool-nose radius because geometric verification reports residual stock or overcut.",
                tool_id=str(smaller["id"]), parameters=parameters,
            )

    # Grooving tools do not have a turning nose radius. For an exact groove,
    # produce catalogued cutter-width alternatives explicitly and let the
    # deterministic material trial decide whether their swept envelopes close
    # the residual. This is deliberately geometry-led, not an LLM guess.
    groove_feature, groove_profile = _external_groove_context(operation, rotational)
    if (excess_count or traceability_failure) and groove_feature is not None and groove_profile is not None:
        current_width = float(operation.tool.cutting_width_mm or operation.tool.diameter_mm)
        groove_tools = sorted(
            (
                item for item in TOOL_DEFINITIONS
                if item.get("kind") == "grooving"
                and item.get("catalog_match", True) is True
                and isinstance(item.get("cutting_width_mm"), (int, float))
                and 0 < float(item["cutting_width_mm"]) <= groove_feature.width_mm + 1e-9
                and float(item["cutting_width_mm"]) < current_width - 1e-9
            ),
            key=lambda item: (-float(item["cutting_width_mm"]), str(item["id"])),
        )
        for tool in groove_tools:
            width = float(tool["cutting_width_mm"])
            try:
                geometry = build_front_groove_geometry_draft(
                    groove_profile, groove_feature,
                    stock_radius_mm=max(point.radius for point in groove_profile.points) + groove_feature.depth_mm,
                    actual_planned_tool_width_mm=width,
                    proposed_tool_width_mm=width,
                )
            except ValueError:
                continue
            add(
                f"groove-width-{width:g}",
                "Use a narrower catalogued grooving insert and generate overlapping plunges from the exact groove envelope.",
                tool_id=str(tool["id"]),
                reference_profile_id=groove_profile.id,
                parameters={
                    "exact_groove_envelope": True,
                    "profile_z_min_mm": min(strip.z_min_mm for strip in geometry.strips),
                    "profile_z_max_mm": max(strip.z_max_mm for strip in geometry.strips),
                },
            )
        if groove_tools:
            observations.append(
                f"The exact groove is {groove_feature.width_mm:g} mm wide; "
                f"{len(groove_tools)} narrower catalogued cutter envelope(s) will be sandbox-tested"
                + (" together with profile traceability repair." if traceability_failure else ".")
            )
        engineering_tool = next((
            item for item in TOOL_DEFINITIONS
            if item.get("kind") == "grooving"
            and item.get("groove_profile") == "full_radius"
            and item.get("axial_contouring_supported") is True
            and isinstance(item.get("cutting_width_mm"), (int, float))
            and float(item["cutting_width_mm"]) <= groove_feature.width_mm + 1e-9
        ), None)
        if engineering_tool is not None:
            engineering_width = float(engineering_tool["cutting_width_mm"])
            try:
                engineering_geometry = build_front_groove_geometry_draft(
                    groove_profile, groove_feature,
                    stock_radius_mm=max(point.radius for point in groove_profile.points) + groove_feature.depth_mm,
                    actual_planned_tool_width_mm=engineering_width,
                    proposed_tool_width_mm=engineering_width,
                )
            except ValueError:
                engineering_geometry = None
        else:
            engineering_geometry = None
        if engineering_tool is not None and engineering_geometry is not None:
            add(
                "full-radius-contour-grooving",
                "Rough by overlapping plunges, then finish the accepted groove profile with a compensated full-radius insert path.",
                tool_id=str(engineering_tool["id"]),
                reference_profile_id=groove_profile.id,
                parameters={
                    "exact_groove_envelope": True,
                    "groove_strategy": "full_radius_contour",
                    "profile_z_min_mm": min(strip.z_min_mm for strip in engineering_geometry.strips),
                    "profile_z_max_mm": max(strip.z_max_mm for strip in engineering_geometry.strips),
                },
            )
            capability_requirements.append({
                "target_operation_id": operation.id,
                "feature_id": groove_feature.id,
                "required_tool_kind": "full_radius_contour_grooving",
                "required_cutting_width_mm": engineering_width,
                "required_tip_radius_mm": float(engineering_tool["nose_radius_mm"]),
                "catalog_match": bool(engineering_tool.get("catalog_match", True)),
                "reason": "contour_strategy_requires_verified_full_radius_insert_and_axial_cutting_capability",
                "next_action": "sandbox_strategy_then_bind_verified_inventory_tool",
            })

    allowance = operation.parameters.get("radial_allowance_mm")
    if (
        excess_count and operation.type in {"turn_od_finishing", "turn_id_finishing"}
        and isinstance(allowance, (int, float)) and not isinstance(allowance, bool)
        and float(allowance) > 1e-9
    ):
        add(
            "remove-finish-allowance",
            "A finishing operation still leaves programmed radial allowance; set it to zero.",
            parameters={"radial_allowance_mm": 0.0},
        )

    for kind, label in (("overcut", "overcut"), ("excess_stock", "residual stock")):
        z_range = _deviation_range(trial, kind)
        if z_range:
            observations.append(f"{label} is concentrated in Z [{z_range[0]:.3f}, {z_range[1]:.3f}] mm")

    if operation.parameters.get("profile_region_complete") is False and (overcut_count or excess_count):
        observations.append(
            "The operation is explicitly regional; do not expand it into the non-rotational area automatically. "
            "Observe the model and split the uncovered region into an appropriate live-tool or special-process operation."
        )
    elif profile and (overcut_count or excess_count):
        operation_min = operation.parameters.get("profile_z_min_mm")
        operation_max = operation.parameters.get("profile_z_max_mm")
        profile_min = min(point.z for point in profile.points)
        profile_max = max(point.z for point in profile.points)
        if isinstance(operation_min, (int, float)) and float(operation_min) > profile_min + 1e-6:
            observations.append("The operation starts inside the accepted profile; inspect whether a separate preceding region is missing.")
        if isinstance(operation_max, (int, float)) and float(operation_max) < profile_max - 1e-6:
            observations.append("The operation ends inside the accepted profile; inspect whether a separate following region is missing.")

    downstream_gaps = (trial.get("evidence") or {}).get("downstream_capability_gaps") or []
    operation_index = {item.id: item for item in operations or []}
    profile_index = {item.id: item for item in rotational.profiles} if rotational else {}
    feature_index = {item.id: item for item in rotational.features} if rotational else {}
    deviations = [
        item for item in (trial.get("verification") or {}).get("deviations", [])
        if isinstance(item, dict) and item.get("kind") == "excess_stock"
        and isinstance(item.get("z"), (int, float))
        and isinstance(item.get("target_radius_mm"), (int, float))
    ]
    for gap in downstream_gaps:
        target_id = str(gap.get("responsible_operation_id") or "")
        target = operation_index.get(target_id)
        feature = feature_index.get(str(gap.get("feature_id") or ""))
        exact_profile = profile_index.get(feature.profile_id) if feature else None
        if target is None or feature is None or exact_profile is None or not deviations:
            continue
        relevant = [
            item for item in deviations
            if float(gap["uncovered_z_range_mm"][0]) - 1e-6 <= float(item["z"])
            <= float(gap["uncovered_z_range_mm"][1]) + 1e-6
        ]
        viable: list[tuple[dict[str, Any], object]] = []
        for tool in TOOL_DEFINITIONS:
            width = tool.get("cutting_width_mm")
            if tool.get("kind") != "grooving" or not isinstance(width, (int, float)) or float(width) <= 0:
                continue
            try:
                geometry = build_front_groove_geometry_draft(
                    exact_profile, feature,
                    stock_radius_mm=max(point.radius for point in exact_profile.points) + feature.depth_mm,
                    actual_planned_tool_width_mm=float(width),
                    proposed_tool_width_mm=float(width),
                )
            except ValueError:
                continue
            if all(any(
                strip.z_min_mm - 1e-6 <= float(item["z"]) <= strip.z_max_mm + 1e-6
                and strip.cut_to_radius_mm <= float(item["target_radius_mm"]) + 0.05
                for strip in geometry.strips
            ) for item in relevant):
                viable.append((tool, geometry))
        viable.sort(key=lambda item: (-float(item[0]["cutting_width_mm"]), str(item[0]["id"])))
        if viable:
            tool, geometry = viable[0]
            add(
                f"downstream-{target_id.lower()}-{str(tool['id']).lower()}",
                f"Replace {target_id} with a catalogued narrower grooving tool whose cutter envelope covers the exact residual profile.",
                target_operation_id=target_id,
                tool_id=str(tool["id"]),
                parameters={
                    "exact_groove_envelope": True,
                    "profile_z_min_mm": min(strip.z_min_mm for strip in geometry.strips),
                    "profile_z_max_mm": max(strip.z_max_mm for strip in geometry.strips),
                },
            )
            observations.append(
                f"Catalog tool {tool['id']} can close the cutter-envelope gap owned by {target_id}."
            )
            continue
        maximum_width: float | None = None
        for step in range(max(1, int(feature.width_mm * 20)), 0, -1):
            width = step / 20
            try:
                geometry = build_front_groove_geometry_draft(
                    exact_profile, feature,
                    stock_radius_mm=max(point.radius for point in exact_profile.points) + feature.depth_mm,
                    actual_planned_tool_width_mm=width, proposed_tool_width_mm=width,
                )
            except ValueError:
                continue
            if all(any(
                strip.z_min_mm - 1e-6 <= float(item["z"]) <= strip.z_max_mm + 1e-6
                and strip.cut_to_radius_mm <= float(item["target_radius_mm"]) + 0.05
                for strip in geometry.strips
            ) for item in relevant):
                maximum_width = width
                break
        capability_requirements.append({
            "target_operation_id": target_id,
            "feature_id": feature.id,
            "required_tool_kind": "grooving",
            "maximum_cutting_width_mm": maximum_width,
            "catalog_match": False,
            "reason": "no_catalogued_tool_closes_exact_cutter_envelope",
            "next_action": "catalog_and_bind_verified_tool_or_define_special_process",
        })
        observations.append(
            f"No catalogued grooving tool can close {target_id}; bind a verified tool"
            + (f" no wider than {maximum_width:g} mm" if maximum_width else " after an engineering envelope study")
            + " or define a separately validated special process."
        )

    if not candidates:
        observations.append(
            "No safe parameter-only repair was found. A geometry observation or a new/split operation is required."
        )
    fallback_next_action = (
        "catalog_and_bind_verified_tool_or_define_special_process"
        if capability_requirements else
        "observe_model_or_split_operation"
        if operation.parameters.get("profile_region_complete") is False and (overcut_count or excess_count)
        else "observe_model_or_propose_new_candidates"
    )
    return {
        "schema_version": "1.0.0",
        "operation_id": operation.id,
        "source_trial_status": trial.get("status"),
        "candidates": [item.model_dump(mode="json") for item in candidates[:5]],
        "capability_requirements": capability_requirements,
        "observations": observations,
        "automation_boundary": (
            "Only evidence-preserving repairs are proposed; tolerances are never inflated and regional profiles "
            "are never expanded into non-rotational geometry automatically."
        ),
        "next_action": (
            "evaluate_candidates" if candidates else
            "catalog_and_bind_verified_tool_or_define_special_process" if capability_requirements else
            "observe_model_or_split_operation"
        ),
        "fallback_next_action": fallback_next_action,
    }
