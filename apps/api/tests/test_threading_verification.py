from app.catalogs import get_tool
from app.models import Operation
from app.threading_verification import verify_threading_cycle
from app.turning_simulation import simulate_turning_stock
from cam.providers.turning import TurningContext, TurningProvider


def reviewed_thread_operation() -> Operation:
    return Operation(
        id="OP35-T1", sequence=35, type="turn_threading", name="thread",
        feature_ids=["RP-1", "TPF-1", "D-1"], tool=get_tool("TURN-THREAD-60"),
        parameters={
            "spindle_mode": "constant_surface_speed",
            "cutting_speed_m_min": 35,
            "maximum_spindle_rpm": 8000,
            "feed_per_revolution_mm": 0.04,
            "start_z_mm": 0,
            "end_z_mm": -10,
            "pitch_mm": 1.5,
            "major_diameter_mm": 10,
            "minor_diameter_mm": 8.4,
            "thread_depth_mm": 0.8,
            "pass_count": 6,
            "relief_strategy": "groove",
            "relief_width_mm": 1.0,
        },
        rationale=["test"], confidence=1, definition_id="turn_threading",
    )


def test_verifies_multi_pass_thread_geometry_relief_and_simulated_root() -> None:
    operation = reviewed_thread_operation()
    context = TurningContext(machine_snapshot_hash="3" * 64, stock_radius_mm=6)
    program = TurningProvider().generate(operation, context)
    simulation = simulate_turning_stock(
        program, stock_radius_mm=6, z_min_mm=-12, z_max_mm=2, resolution_mm=0.1,
    )

    result = verify_threading_cycle(operation, program, simulation)

    assert result.status == "passed"
    assert result.metrics.emitted_pass_count == 6
    assert result.metrics.radial_depth_mm == 0.8
    assert all(item.status == "passed" for item in result.checks)


def test_rejects_unreviewed_relief_strategy() -> None:
    operation = reviewed_thread_operation()
    operation.parameters["relief_strategy"] = "unreviewed"
    context = TurningContext(machine_snapshot_hash="4" * 64, stock_radius_mm=6)
    program = TurningProvider().generate(operation, context)
    simulation = simulate_turning_stock(
        program, stock_radius_mm=6, z_min_mm=-12, z_max_mm=2, resolution_mm=0.1,
    )

    result = verify_threading_cycle(operation, program, simulation)

    assert result.status == "failed"
    assert next(item for item in result.checks if item.id == "relief_space").status == "failed"
