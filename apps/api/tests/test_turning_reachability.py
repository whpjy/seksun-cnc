from app.catalogs import get_tool
from app.models import Operation
from app.rotational_features import RotationalProfile, RotationalProfilePoint
from app.turning_reachability import assess_turning_reachability
from cam.providers.turning import TurningContext


def operation(tool_id: str) -> Operation:
    operation_type = "turn_id_finishing" if tool_id.startswith("TURN-ID") else "turn_od_finishing"
    return Operation(
        id="OP-REACH", sequence=10, type=operation_type, name="reachability",
        feature_ids=["RP-REACH"], tool=get_tool(tool_id), parameters={},
        rationale=["test"], confidence=1,
    )


def profile(side: str, points: list[tuple[float, float]]) -> RotationalProfile:
    return RotationalProfile(
        id="RP-REACH", axis_id="RA-1", side=side,
        extraction_method="exact_section",
        points=[RotationalProfilePoint(z=z_value, radius=radius) for z_value, radius in points],
        confidence=1, review_state="accepted",
    )


def context(**updates) -> TurningContext:
    values = {"machine_snapshot_hash": "8" * 64, "stock_radius_mm": 12}
    values.update(updates)
    return TurningContext(**values)


def test_external_front_turning_profile_is_reachable() -> None:
    result = assess_turning_reachability(
        operation("TURN-OD-F"),
        profile("outer", [(-30, 5), (-10, 5), (0, 10)]),
        context(),
    )

    assert result.status == "passed"
    assert not result.blocking_reasons
    assert all(item.status == "passed" for item in result.checks)


def test_positive_z_outer_turning_requires_left_hand_tool() -> None:
    front_form = profile("outer", [(1.15, 10), (1.25, 2), (3, 0.5)])

    accepted = assess_turning_reachability(
        operation("TURN-OD-L-MICRO-F"),
        front_form,
        context(cut_direction="positive_z"),
    )
    rejected = assess_turning_reachability(
        operation("TURN-OD-F"),
        front_form,
        context(cut_direction="positive_z"),
    )

    assert accepted.status == "passed"
    assert rejected.status == "failed"
    check = next(item for item in rejected.checks if item.id == "cut_direction_tool_hand")
    assert check.status == "failed"


def test_front_chamfer_may_increase_to_body_diameter_before_longitudinal_cut() -> None:
    result = assess_turning_reachability(
        operation("TURN-OD-F"),
        profile("outer", [(-20, 5), (-2, 10), (0, 8)]),
        context(),
    )

    assert result.status == "passed"
    assert next(item for item in result.checks if item.id == "profile_undercut").status == "passed"


def test_hidden_external_undercut_is_blocked_for_standard_longitudinal_tool() -> None:
    result = assess_turning_reachability(
        operation("TURN-OD-F"),
        profile("outer", [(-20, 9), (-10, 5), (0, 10)]),
        context(),
    )

    assert result.status == "failed"
    assert next(item for item in result.checks if item.id == "profile_undercut").status == "failed"


def test_dedicated_external_groove_is_not_treated_as_longitudinal_undercut() -> None:
    result = assess_turning_reachability(
        operation("TURN-OD-F"),
        profile("outer", [
            (-3, 5), (-2, 5), (-2, 3), (-1, 3), (-1, 5), (0, 5),
        ]),
        context(),
    )

    assert result.status == "passed"
    assert next(item for item in result.checks if item.id == "profile_undercut").status == "passed"


def test_wrong_tool_side_is_blocked() -> None:
    result = assess_turning_reachability(
        operation("TURN-ID-F"),
        profile("outer", [(-20, 5), (0, 10)]),
        context(),
    )

    assert result.status == "failed"
    assert next(item for item in result.checks if item.id == "tool_side_compatibility").status == "failed"


def test_boring_bar_requires_entry_clearance_and_axial_reach() -> None:
    inner = profile("inner", [(-30, 6), (0, 8)])
    result = assess_turning_reachability(
        operation("TURN-ID-F"), inner,
        context(initial_bore_radius_mm=4.1, radial_clearance_mm=1),
    )

    checks = {item.id: item for item in result.checks}
    assert result.status == "failed"
    assert checks["boring_bar_entry"].status == "failed"
    assert checks["boring_bar_axial_reach"].status == "failed"


def test_boring_bar_passes_when_bore_and_stickout_are_sufficient() -> None:
    result = assess_turning_reachability(
        operation("TURN-ID-F"),
        profile("inner", [(-20, 6), (0, 8)]),
        context(initial_bore_radius_mm=4.5, radial_clearance_mm=1),
    )

    assert result.status == "passed"


def test_axial_drill_uses_drill_envelope_instead_of_boring_bar_checks() -> None:
    drilling = operation("DRILL-10.0")
    drilling.type = "axial_drilling"
    drilling.parameters = {"depth_mm": 20}
    result = assess_turning_reachability(
        drilling,
        profile("inner", [(-20, 6), (0, 8)]),
        context(),
    )

    checks = {item.id: item for item in result.checks}
    assert result.status == "passed"
    assert checks["tool_side_compatibility"].status == "passed"
    assert checks["drill_profile_clearance"].status == "passed"
    assert checks["drill_axial_reach"].status == "passed"
    assert "boring_bar_entry" not in checks


def test_grooving_tool_does_not_require_turning_insert_nose_radius() -> None:
    grooving = operation("TURN-GROOVE-0.8")
    grooving.type = "turn_grooving"
    grooving.parameters = {"groove_side": "external", "groove_width_mm": 0.9}

    result = assess_turning_reachability(
        grooving,
        profile("outer", [(-1, 2), (0, 1), (1, 2)]),
        context(),
    )

    assert result.status == "passed"
    assert next(item for item in result.checks if item.id == "nose_radius").status == "passed"
