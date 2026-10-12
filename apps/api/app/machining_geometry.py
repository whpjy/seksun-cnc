"""Geometry capabilities shared by the tool catalog, intake and coverage gates."""
from math import sqrt


def axial_hole(feature: dict, axis: dict | None, tolerance_mm: float = 0.05) -> bool:
    if not axis or axis.get("review_state") == "excluded" or feature.get("kind") != "hole" or feature.get("review_state") == "excluded":
        return False
    direction = axis.get("direction") or {}
    normal = feature.get("axis") or {}
    a = [float(direction.get(k, 0)) for k in "xyz"]
    b = [float(normal.get(k, 0)) for k in "xyz"]
    an, bn = sqrt(sum(x*x for x in a)), sqrt(sum(x*x for x in b))
    if min(an, bn) < 1e-9:
        return False
    a = [x/an for x in a]
    if abs(sum(x*y/bn for x, y in zip(a, b))) < 0.999:
        return False
    center, origin = feature.get("center") or {}, axis.get("origin") or {}
    delta = [float(center.get(k, 0))-float(origin.get(k, 0)) for k in "xyz"]
    z = sum(x*y for x, y in zip(delta, a))
    return sqrt(sum((x-z*y)**2 for x, y in zip(delta, a))) <= tolerance_mm


def job_axis(job: dict, rotational: dict | None = None) -> dict | None:
    profiles = (rotational or {}).get("profiles") or []
    profile_id = ((job.get("plan") or {}).get("stock") or {}).get("rotational_profile_id")
    profile = next((p for p in profiles if p.get("id") == profile_id), None)
    axes = (rotational or {}).get("axes") or []
    if profile:
        return next((a for a in axes if a.get("id") == profile.get("axis_id")), None)
    stock_axis = ((job.get("plan") or {}).get("stock") or {}).get("rotational_axis")
    return stock_axis or (axes[0] if axes else None)
