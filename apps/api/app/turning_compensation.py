from __future__ import annotations

from math import hypot

from .rotational_features import RotationalProfile, RotationalProfilePoint


def _segment_normal(
    left: RotationalProfilePoint,
    right: RotationalProfilePoint,
    side: str,
) -> tuple[float, float]:
    dz = right.z - left.z
    dr = right.radius - left.radius
    length = hypot(dz, dr)
    if length <= 1e-12:
        return (0, 0)
    normal_z, normal_r = -dr / length, dz / length
    desired_radial_sign = 1 if side == "outer" else -1
    if normal_r * desired_radial_sign < 0:
        normal_z, normal_r = -normal_z, -normal_r
    return normal_z, normal_r


def compensate_profile_for_nose(
    profile: RotationalProfile,
    *,
    nose_radius_mm: float,
    allowance_mm: float = 0,
) -> list[RotationalProfilePoint]:
    """Offset a Z-R profile to an explicit circular insert-nose center path.

    This is a geometric draft offset. Insert quadrant/orientation reachability is
    checked separately before this path can become production-capable.
    """
    if nose_radius_mm < 0 or allowance_mm < 0:
        raise ValueError("nose radius and allowance must be non-negative")
    points = sorted(profile.points, key=lambda item: (item.z, item.radius))
    material_offset = allowance_mm if profile.side == "outer" else -allowance_mm
    if nose_radius_mm <= 1e-12:
        return [
            RotationalProfilePoint(z=point.z, radius=point.radius + material_offset)
            for point in points
        ]

    segment_normals = [
        _segment_normal(left, right, profile.side)
        for left, right in zip(points, points[1:])
    ]
    compensated: list[RotationalProfilePoint] = []
    for index, point in enumerate(points):
        adjacent = []
        if index > 0:
            adjacent.append(segment_normals[index - 1])
        if index < len(segment_normals):
            adjacent.append(segment_normals[index])
        normal_z = sum(item[0] for item in adjacent)
        normal_r = sum(item[1] for item in adjacent)
        normal_length = hypot(normal_z, normal_r)
        if normal_length <= 1e-12:
            normal_z, normal_r = (0, 1 if profile.side == "outer" else -1)
        else:
            normal_z /= normal_length
            normal_r /= normal_length
        radius = point.radius + material_offset + nose_radius_mm * normal_r
        if radius < 0:
            raise ValueError("nose compensation crosses the rotational centerline")
        compensated.append(RotationalProfilePoint(
            z=point.z + nose_radius_mm * normal_z,
            radius=radius,
        ))
    return compensated
