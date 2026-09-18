from app.inner_bore_chain import InnerBoreChainRequest, compile_inner_bore_chain
from app.l32_configuration import snapshot_l32_instance
from app.machine_models import MachineInstance
from app.planner import build_process_plan
from app.rotational_features import infer_rotational_features

from tests.test_l32_planner import bored_shaft_analysis


def test_continuous_inner_bore_chain_passes_a_reachable_tapered_bore() -> None:
    analysis = bored_shaft_analysis()
    analysis.rotational_profile_reviews["RP-INNER-1"] = "accepted"
    plan = build_process_plan(analysis, "S45C", "Citizen Cincom L32")
    profile = next(
        item for item in infer_rotational_features(analysis).profiles
        if item.id == "RP-INNER-1"
    )
    for setup in plan.setups:
        for operation in setup.operations:
            if operation.type not in {"axial_drilling", "turn_id_roughing", "turn_id_finishing"}:
                continue
            operation.enabled = True
            operation.parameters["engineering_review_status"] = "verified_engineer"
            if operation.type == "axial_drilling":
                operation.parameters.update({
                    "depth_mm": 23.3,
                    "full_diameter_depth_mm": 20,
                    "drill_tip_length_mm": 3.3,
                    "spindle_rpm": operation.parameters["maximum_spindle_rpm"],
                })
    snapshot = snapshot_l32_instance(MachineInstance(
        id="l32-chain-test", definition_id="citizen-cincom-l32",
        name="L32 chain test", variant="VIII", operation_mode="guide_bushing",
        installed_modules=[], bar_diameter_mm=32,
    ))

    result = compile_inner_bore_chain(
        "b" * 32,
        InnerBoreChainRequest(
            machine_instance_id=snapshot.instance.id,
            profile_id=profile.id,
            stock_radius_mm=11,
            z_min_mm=-22,
            z_max_mm=2,
        ),
        plan,
        profile,
        snapshot,
    )

    assert result.status == "passed"
    assert [item.operation_id for item in result.stages] == ["OP22", "OP25", "OP28"]
    assert all(item.status == "passed" for item in result.checks)
    assert result.final_verification.status == "passed"
    assert result.final_simulation.metrics.remaining_volume_mm3 < result.final_simulation.metrics.initial_volume_mm3
