import pytest

from app.catalogs import get_tool
from app.models import Operation
from app.rotational_features import RotationalProfile, RotationalProfilePoint
from app.turning_simulation import simulate_turning_stock
from cam.providers.turning import TurningContext, TurningProvider


def operation(kind: str, tool_id: str = "TURN-OD-R", **parameters) -> Operation:
    defaults = {
        "spindle_mode": "constant_surface_speed", "cutting_speed_m_min": 100,
        "maximum_spindle_rpm": 8000, "feed_per_revolution_mm": 0.12,
    }
    defaults.update(parameters)
    return Operation(
        id="OP10", sequence=10, type=kind, name=kind, feature_ids=["RP-1"],
        tool=get_tool(tool_id), parameters=defaults, rationale=["test"], confidence=1,
        definition_id=kind,
    )


def stepped_profile() -> RotationalProfile:
    return RotationalProfile(
        id="RP-1", axis_id="RA-1", side="outer", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=-30, radius=5),
            RotationalProfilePoint(z=-10, radius=5),
            RotationalProfilePoint(z=0, radius=10),
        ], confidence=1, review_state="accepted",
    )


def stepped_inner_profile() -> RotationalProfile:
    return RotationalProfile(
        id="RP-INNER-1", axis_id="RA-1", side="inner", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=-20, radius=6),
            RotationalProfilePoint(z=-8, radius=6),
            RotationalProfilePoint(z=0, radius=8),
        ], confidence=1, review_state="accepted",
    )


def sample_at(result, z_value: float):
    return min(result.samples, key=lambda item: abs(item.z - z_value))


def test_simulates_external_profile_removal_in_zr_space() -> None:
    context = TurningContext(machine_snapshot_hash="d" * 64, stock_radius_mm=12)
    program = TurningProvider().generate(
        operation("turn_od_finishing", tool_id="TURN-OD-F", radial_allowance_mm=0),
        context, stepped_profile(),
    )

    result = simulate_turning_stock(
        program, stock_radius_mm=12, z_min_mm=-30, z_max_mm=0, resolution_mm=0.5,
    )

    assert sample_at(result, 0).outer_radius == 10
    assert sample_at(result, -20).outer_radius == pytest.approx(5, abs=0.01)
    assert result.approximation == "mixed_centerline_and_nose_circle"
    assert result.metrics.removed_volume_mm3 > 0
    assert result.metrics.remaining_volume_mm3 < result.metrics.initial_volume_mm3


def test_positive_z_roughing_does_not_assign_narrow_cut_to_protected_shoulder() -> None:
    profile = RotationalProfile(
        id="RP-1", axis_id="RA-1", side="outer", extraction_method="exact_section",
        points=[
            RotationalProfilePoint(z=1.15, radius=10.05),
            RotationalProfilePoint(z=1.216667, radius=2),
            RotationalProfilePoint(z=3.05, radius=0.5),
        ],
        confidence=1, review_state="accepted",
    )
    program = TurningProvider().generate(
        operation(
            "turn_od_roughing", tool_id="TURN-OD-L-R",
            radial_allowance_mm=0.3, depth_of_cut_mm=1,
        ),
        TurningContext(
            machine_snapshot_hash="d" * 64, stock_radius_mm=11.25,
            cut_direction="positive_z",
        ),
        profile,
    )

    result = simulate_turning_stock(
        program, stock_radius_mm=11.25,
        z_min_mm=1.15, z_max_mm=3.05, resolution_mm=0.1,
    )

    assert sample_at(result, 1.15).outer_radius == 11.25
    assert sample_at(result, 1.25).outer_radius < 11.25


def test_simulates_inner_profile_without_crossing_outer_stock() -> None:
    context = TurningContext(
        machine_snapshot_hash="e" * 64, stock_radius_mm=12,
        initial_bore_radius_mm=4, radial_clearance_mm=1,
    )
    program = TurningProvider().generate(
        operation("turn_id_finishing", tool_id="TURN-ID-F", radial_allowance_mm=0),
        context, stepped_inner_profile(),
    )

    result = simulate_turning_stock(
        program, stock_radius_mm=12, initial_bore_radius_mm=4,
        z_min_mm=-20, z_max_mm=0, resolution_mm=0.5,
    )

    assert sample_at(result, 0).inner_radius == 8
    assert sample_at(result, -15).inner_radius == pytest.approx(6, abs=0.01)
    assert result.approximation == "mixed_centerline_and_nose_circle"
    assert all(item.inner_radius <= item.outer_radius for item in result.samples)


def test_facing_removes_material_in_front_of_finished_face() -> None:
    context = TurningContext(machine_snapshot_hash="f" * 64, stock_radius_mm=12)
    program = TurningProvider().generate(
        operation("turn_facing", face_z_mm=0, center_overtravel_mm=0.2), context,
    )

    result = simulate_turning_stock(
        program, stock_radius_mm=12, z_min_mm=-10, z_max_mm=2, resolution_mm=0.25,
    )

    assert sample_at(result, 1).outer_radius == 0
    assert sample_at(result, -1).outer_radius == 12


def test_axial_drilling_simulates_full_diameter_and_drill_tip() -> None:
    context = TurningContext(machine_snapshot_hash="a" * 64, stock_radius_mm=12)
    program = TurningProvider().generate(
        operation(
            "axial_drilling", tool_id="DRILL-10.0", start_z_mm=0,
            depth_mm=23, peck_depth_mm=2, drill_tip_length_mm=3,
        ),
        context, stepped_inner_profile(),
    )

    result = simulate_turning_stock(
        program, stock_radius_mm=12, z_min_mm=-24, z_max_mm=0, resolution_mm=0.25,
    )

    assert sample_at(result, -10).inner_radius == pytest.approx(5, abs=0.01)
    assert sample_at(result, -21.5).inner_radius == pytest.approx(2.5, abs=0.15)
    assert sample_at(result, -23).inner_radius == pytest.approx(0, abs=0.01)
    assert result.metrics.removed_volume_mm3 > 0


def test_cutoff_width_removes_a_band_instead_of_one_zero_width_sample() -> None:
    context = TurningContext(machine_snapshot_hash="1" * 64, stock_radius_mm=12)
    program = TurningProvider().generate(
        operation("turn_cutoff", tool_id="TURN-CUTOFF-2", z_mm=-5, breakthrough_radius_mm=0.1),
        context,
    )

    result = simulate_turning_stock(
        program, stock_radius_mm=12, z_min_mm=-10, z_max_mm=0, resolution_mm=0.25,
    )

    assert sample_at(result, -5).outer_radius == 0.1
    assert sample_at(result, -4.25).outer_radius == 0.1
    assert sample_at(result, -3.5).outer_radius == 12


def test_threading_simulates_each_pass_to_the_reviewed_root_envelope() -> None:
    context = TurningContext(machine_snapshot_hash="2" * 64, stock_radius_mm=6)
    program = TurningProvider().generate(
        operation(
            "turn_threading", tool_id="TURN-THREAD-60",
            start_z_mm=0, end_z_mm=-10, pitch_mm=1.5,
            major_diameter_mm=10, minor_diameter_mm=8.4,
            thread_depth_mm=0.8, pass_count=6,
            relief_strategy="groove", relief_width_mm=1.0,
        ),
        context,
    )

    result = simulate_turning_stock(
        program, stock_radius_mm=6, z_min_mm=-12, z_max_mm=2, resolution_mm=0.1,
    )

    assert result.approximation == "thread_root_envelope"
    assert sample_at(result, -5).outer_radius == pytest.approx(4.2)
    assert sample_at(result, -11).outer_radius == 6
    assert result.metrics.removed_volume_mm3 > 0
