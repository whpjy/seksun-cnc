from math import hypot

import pytest

from app.rotational_features import RotationalProfile, RotationalProfilePoint
from app.turning_compensation import compensate_profile_for_nose


def profile(side: str, points: list[tuple[float, float]]) -> RotationalProfile:
    return RotationalProfile(
        id="RP-COMP", axis_id="RA-1", side=side,
        extraction_method="exact_section",
        points=[RotationalProfilePoint(z=z_value, radius=radius) for z_value, radius in points],
        confidence=1, review_state="accepted",
    )


def test_outer_cylinder_offsets_nose_center_radially_outward() -> None:
    compensated = compensate_profile_for_nose(
        profile("outer", [(-20, 5), (0, 5)]), nose_radius_mm=0.4,
    )

    assert [(item.z, item.radius) for item in compensated] == [(-20, 5.4), (0, 5.4)]


def test_inner_cylinder_offsets_nose_center_into_bore_and_preserves_allowance() -> None:
    compensated = compensate_profile_for_nose(
        profile("inner", [(-20, 6), (0, 6)]),
        nose_radius_mm=0.2, allowance_mm=0.1,
    )

    assert [(item.z, item.radius) for item in compensated] == pytest.approx([
        (-20, 5.7), (0, 5.7),
    ])


def test_taper_offset_remains_one_nose_radius_from_target_line() -> None:
    nose_radius = 0.4
    original = [(-10, 5), (0, 10)]
    compensated = compensate_profile_for_nose(
        profile("outer", original), nose_radius_mm=nose_radius,
    )
    dz = original[1][0] - original[0][0]
    dr = original[1][1] - original[0][1]
    line_length = hypot(dz, dr)

    for point in compensated:
        distance = abs(dr * point.z - dz * point.radius + dz * original[0][1] - dr * original[0][0]) / line_length
        assert distance == pytest.approx(nose_radius)


def test_sharp_outer_transition_uses_offset_line_intersection_instead_of_inside_chord() -> None:
    nose_radius = 0.2
    corner = (-1.9, 10.05)
    compensated = compensate_profile_for_nose(
        profile("outer", [(-2.1, 10.25), corner, (-1.833333, 2.0)]),
        nose_radius_mm=nose_radius,
    )

    compensated_corner = compensated[1]
    assert hypot(
        compensated_corner.z - corner[0], compensated_corner.radius - corner[1],
    ) > nose_radius


def test_compensation_rejects_inner_path_crossing_centerline() -> None:
    with pytest.raises(ValueError, match="centerline"):
        compensate_profile_for_nose(
            profile("inner", [(-5, 0.1), (0, 0.1)]), nose_radius_mm=0.2,
        )
