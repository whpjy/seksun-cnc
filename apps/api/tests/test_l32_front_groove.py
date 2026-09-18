from __future__ import annotations

from app.l32_front_groove import build_front_groove_geometry_draft
from app.rotational_features import RotationalProfile, RotationalProfilePoint, TurningProfileFeature


def _geometry():
    profile = RotationalProfile(
        id="RP-TEST",axis_id="RA-TEST",side="outer",extraction_method="exact_section",
        review_state="accepted",confidence=1,
        points=[RotationalProfilePoint(z=z,radius=r) for z,r in [
            (-1.0,1.8),(-0.8,1.8),(-0.7,1.5),(-0.5,0.8),
            (0.5,0.8),(0.7,1.5),(0.8,1.8),(1.0,1.8),
        ]],
    )
    feature = TurningProfileFeature(
        id="G1",profile_id=profile.id,kind="external_groove_candidate",
        z_start=-0.5,z_end=0.5,radius_start=0.8,radius_end=0.8,
        width_mm=1.0,depth_mm=1.0,source_point_indices=[3,4],confidence=1,
    )
    return profile,feature


def test_full_width_envelope_protects_rounded_shoulders_and_exposes_wrong_tool():
    profile,feature = _geometry()
    draft = build_front_groove_geometry_draft(
        profile,feature,stock_radius_mm=2.8,actual_planned_tool_width_mm=2.0,
        proposed_tool_width_mm=0.25,
    )
    assert draft.actual_tool_fits_floor is False
    assert draft.nc_generated is False
    assert draft.tool_catalog_match is False
    assert draft.strips[0].z_min_mm == -0.8
    assert draft.strips[-1].z_max_mm == 0.8
    assert any(strip.cut_to_radius_mm < 1.0 for strip in draft.strips)
    for strip in draft.strips:
        for index in range(101):
            z = strip.z_min_mm+(strip.z_max_mm-strip.z_min_mm)*index/100
            from app.l32_front_groove import _radius_at
            assert strip.cut_to_radius_mm >= _radius_at(profile,z)+0.019999


def test_proposed_tool_cannot_exceed_exact_floor_width():
    profile,feature = _geometry()
    import pytest
    with pytest.raises(ValueError,match="wider than the exact groove floor"):
        build_front_groove_geometry_draft(
            profile,feature,stock_radius_mm=2.8,actual_planned_tool_width_mm=2.0,
            proposed_tool_width_mm=1.1,
        )
