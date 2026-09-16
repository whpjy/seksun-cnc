import pytest

from app.catalogs import get_tool
from app.models import Operation
from app.rotational_features import RotationalProfile, RotationalProfilePoint
from cam.providers.turning import TurningContext, TurningProvider


def operation(kind: str, tool_id: str = "TURN-OD-R", **parameters) -> Operation:
    defaults = {
        "spindle_mode": "constant_surface_speed",
        "cutting_speed_m_min": 100,
        "maximum_spindle_rpm": 8000,
        "feed_per_revolution_mm": 0.12,
    }
    defaults.update(parameters)
    return Operation(
        id="OP10", sequence=10, type=kind, name=kind, feature_ids=["RP-1"],
        tool=get_tool(tool_id), parameters=defaults, rationale=["test"], confidence=1,
        definition_id=kind,
    )


def context() -> TurningContext:
    return TurningContext(machine_snapshot_hash="a" * 64, stock_radius_mm=12)


def stepped_profile() -> RotationalProfile:
    return RotationalProfile(
        id="RP-1", axis_id="RA-1", side="outer", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=-30, radius=5),
            RotationalProfilePoint(z=-10, radius=5),
            RotationalProfilePoint(z=0, radius=10),
        ],
        confidence=1, review_state="accepted",
    )


def stepped_inner_profile() -> RotationalProfile:
    return RotationalProfile(
        id="RP-INNER-1", axis_id="RA-1", side="inner", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=-20, radius=6),
            RotationalProfilePoint(z=-8, radius=6),
            RotationalProfilePoint(z=0, radius=8),
        ],
        confidence=1, review_state="accepted",
    )


def target_radius(z_value: float) -> float:
    if z_value <= -10:
        return 5
    return 5 + (z_value + 10) * 0.5


def test_facing_generates_diameter_mode_ir_with_safe_approach() -> None:
    program = TurningProvider().generate(
        operation("turn_facing", face_z_mm=0, center_overtravel_mm=0.2), context(),
    )

    commands = program.channels[0].commands
    assert program.coordinate_convention == "diameter-x_z"
    assert commands[4].type == "rapid_move"
    assert commands[4].axes == {"X": 28.0, "Z": 2.0}
    assert any(item.type == "feed_move" and item.axes.get("X") == -0.4 for item in commands)
    assert commands[-1].type == "spindle_stop"


def test_od_finishing_follows_accepted_profile_from_front_to_back() -> None:
    program = TurningProvider().generate(
        operation("turn_od_finishing", tool_id="TURN-OD-F", radial_allowance_mm=0),
        context(), stepped_profile(),
    )

    cutting = [item for item in program.channels[0].commands if item.type == "feed_move"]
    assert [item.axes["Z"] for item in cutting] == sorted(
        (item.axes["Z"] for item in cutting), reverse=True,
    )
    assert cutting[0].axes["X"] > 20
    assert cutting[-1].axes == pytest.approx({"X": 10.8, "Z": -30.0})
    assert all(item.parameters["position_role"] == "nose_center" for item in cutting)
    assert all(item.parameters["tool_nose_radius_mm"] == 0.4 for item in cutting)


def test_od_roughing_clips_each_pass_to_profile_without_overcut() -> None:
    allowance = 0.5
    program = TurningProvider().generate(
        operation("turn_od_roughing", radial_allowance_mm=allowance, depth_of_cut_mm=2),
        context(), stepped_profile(),
    )

    axial_cuts = [
        item for item in program.channels[0].commands
        if item.type == "feed_move" and set(item.axes) == {"X", "Z"}
    ]
    assert axial_cuts
    for item in axial_cuts:
        assert item.axes["X"] / 2 + 1e-7 >= target_radius(item.axes["Z"]) + allowance


def test_cutoff_requires_retention_and_emits_part_state_transition() -> None:
    program = TurningProvider().generate(
        operation("turn_cutoff", tool_id="TURN-CUTOFF-2", z_mm=-32, breakthrough_radius_mm=0.1),
        context(),
    )

    commands = program.channels[0].commands
    approach = next(item for item in commands if item.type == "rapid_move")
    cutoff = next(item for item in commands if item.type == "cutoff")
    assert "part_retention_confirmed" in approach.safety_requirements
    assert cutoff.parameters["part_state"] == "separated"


def test_inner_finishing_requires_confirmed_initial_bore_and_stays_inside_profile() -> None:
    bore_context = TurningContext(
        machine_snapshot_hash="b" * 64, stock_radius_mm=12,
        initial_bore_radius_mm=4, radial_clearance_mm=1,
    )
    program = TurningProvider().generate(
        operation("turn_id_finishing", tool_id="TURN-ID-F", radial_allowance_mm=0.1),
        bore_context, stepped_inner_profile(),
    )

    cutting = [item for item in program.channels[0].commands if item.type == "feed_move"]
    assert [item.axes["Z"] for item in cutting] == sorted(
        (item.axes["Z"] for item in cutting), reverse=True,
    )
    assert cutting[-1].axes == pytest.approx({"X": 11.4, "Z": -20.0})
    assert all(item.parameters["position_role"] == "nose_center" for item in cutting)
    assert all(item.parameters["tool_nose_radius_mm"] == 0.2 for item in cutting)


def test_inner_roughing_rejects_missing_initial_bore() -> None:
    with pytest.raises(ValueError, match="initial bore"):
        TurningProvider().generate(
            operation("turn_id_roughing", tool_id="TURN-ID-R", depth_of_cut_mm=0.5),
            context(), stepped_inner_profile(),
        )


def test_threading_remains_semantic_until_a_controller_post_is_available() -> None:
    program = TurningProvider().generate(
        operation(
            "turn_threading", tool_id="TURN-THREAD-60", start_z_mm=0, end_z_mm=-12,
            pitch_mm=1.5, major_diameter_mm=10, minor_diameter_mm=8.4, pass_count=6,
        ),
        context(),
    )

    threads = [item for item in program.channels[0].commands if item.type == "thread_cut"]
    assert len(threads) == 6
    assert [item.parameters["pass_index"] for item in threads] == [1, 2, 3, 4, 5, 6]
    assert threads[0].parameters["target_diameter_mm"] > threads[-1].parameters["target_diameter_mm"]
    assert threads[-1].parameters["target_diameter_mm"] == 8.4
    assert all(item.parameters["pitch_mm"] == 1.5 for item in threads)
    assert all(item.parameters["pass_count"] == 6 for item in threads)
    assert all("spindle_synchronization_required" in item.safety_requirements for item in threads)


@pytest.mark.parametrize(("kind", "tool_id", "expected"), [
    ("axial_drilling", "DRILL-6.0", "drill_cycle"),
    ("axial_tapping", "TAP-M6", "tap_cycle"),
])
def test_axial_hole_operations_emit_controller_neutral_cycles(kind: str, tool_id: str, expected: str) -> None:
    parameters = {"depth_mm": 12, "peck_depth_mm": 2}
    if kind == "axial_tapping":
        parameters = {"depth_mm": 12, "pitch_mm": 1}
    program = TurningProvider().generate(operation(kind, tool_id=tool_id, **parameters), context())

    cycle = next(item for item in program.channels[0].commands if item.type == expected)
    assert cycle.parameters["start_z_mm"] == 0
    assert cycle.parameters["end_z_mm"] == -12


def test_unimplemented_back_turning_is_rejected_instead_of_emitting_unsafe_ir() -> None:
    with pytest.raises(ValueError, match="does not support"):
        TurningProvider().generate(operation("back_turning", tool_id="TURN-ID-R"), context())
