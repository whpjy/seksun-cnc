import pytest
from app.side_hole_readiness import side_hole_readiness


def evidence(**changes):
    return dict(initial_missing_target_mm3=0., target_contact_mm3=0.,
                remaining_cylindrical_segments_mm3=0., remaining_excess_stock_mm3=300.,
                non_cutting_clearance_status="passed", stages=[], **changes)


def report(result, draft=None, machine=None, source="initial_solid_blank"):
    return side_hole_readiness(result, draft or {"holes":[], "drill_geometry":{"point_angle_deg":118}},
                              machine or {"checks":[]}, {"stock_source":source})


@pytest.mark.parametrize("metric", ["initial_missing_target_mm3", "target_contact_mm3", "remaining_cylindrical_segments_mm3"])
def test_preexisting_damage_overcut_or_residual_blocks_even_if_other_checks_pass(metric):
    result=evidence(); result[metric]=1.
    assessment=report(result)
    assert assessment["status"]=="blocked"
    assert not assessment["can_accept"] and not assessment["production_ready"]
    assert any(c["status"]=="blocked" and c["action"] for c in assessment["checks"])


def test_safe_reference_never_promotes_to_manufacturing_acceptance():
    assessment=report(evidence())
    assert assessment["status"]=="review"
    assert not assessment["can_accept"]
    assert {c["code"] for c in assessment["checks"]} >= {"process_sequence", "whole_part_geometry", "drawing_quality"}


@pytest.mark.parametrize("value",[None,float("nan"),-1,float("inf")])
def test_missing_or_invalid_metrics_cannot_pass(value):
    result=evidence(); result["initial_missing_target_mm3"]=value
    check=next(c for c in report(result)["checks"] if c["code"]=="previous_target_damage")
    assert check["status"]=="review"


def test_hole_specific_conflict_and_residual_are_identified():
    result=evidence();result["stages"]=[dict(feature_id="HF-14",remaining_cylindrical_segment_mm3=2.,target_inside_cylindrical_segment_mm3=.5)]
    checks=report(result)["checks"]
    assert {c["code"] for c in checks if c.get("feature_id")=="HF-14"}=={"cad_region_conflict","hole_residual"}


def test_machine_and_body_failures_remain_blocking():
    result=evidence();result["non_cutting_clearance_status"]="failed"
    checks=report(result,machine={"checks":[dict(code="axis",status="failed",detail="Missing axis")]})["checks"]
    assert next(c for c in checks if c["code"]=="tool_body_clearance")["status"]=="blocked"
    assert next(c for c in checks if c["code"]=="machine_axis")["status"]=="blocked"


def test_turning_envelope_requires_continuous_solid_evidence():
    assessment=report(evidence(),source="accepted_turning_envelope")
    assert "continuous_solid" in {c["code"] for c in assessment["checks"]}
