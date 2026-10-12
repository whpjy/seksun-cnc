from math import tan,radians
import json
import pytest
from pydantic import ValidationError
from app.side_hole_tool import SideDrillGeometry,prepare_drill_geometry,assess_side_hole_machine
from app.side_hole_draft import build_side_hole_draft
from test_side_hole_draft import AXIS,hole
from test_l32_operation_trial import job,snapshot


@pytest.mark.parametrize("offset",[.1,-.2])
def test_tip_and_extra_depth_follow_actual_inward_axis_without_mutating_draft(offset):
    draft=build_side_hole_draft([hole(z=6),hole(z=-6,identifier="HF-15")],AXIS,24.2)
    original=json.dumps(draft,sort_keys=True)
    result=prepare_drill_geometry(draft,SideDrillGeometry(point_angle_deg=118,full_diameter_extension_mm=offset,evidence="参考试算"))
    tip=.75/tan(radians(59))
    for before,after in zip(draft["holes"],result["holes"]):
        sign=before["outward_direction"]["z"]
        assert after["end"]["z"]==pytest.approx(before["end"]["z"]-sign*offset)
        assert after["tip_end"]["z"]==pytest.approx(before["end"]["z"]-sign*(offset+tip))
        assert after["tip_length_mm"]==pytest.approx(tip)
        assert after["moves"][1]["point"]==after["tip_end"]
    assert json.dumps(draft,sort_keys=True)==original


@pytest.mark.parametrize("changes",[{"point_angle_deg":0},{"point_angle_deg":float("nan")},{"evidence":" "},
    {"full_diameter_extension_mm":-21},{"flute_length_mm":5},
    {"flute_length_mm":10,"stickout_from_tip_mm":5,"shank_diameter_mm":2,"holder_diameter_mm":10,"holder_length_mm":10}])
def test_missing_or_invalid_geometry_is_rejected(changes):
    with pytest.raises(ValidationError): SideDrillGeometry(**{"point_angle_deg":118,"evidence":"资料",**changes})


def test_configured_axis_is_only_a_necessary_condition_not_a_machine_pass():
    source=job()
    machine=snapshot()
    source.machine_instance_id=machine.instance.id
    source.machine_configuration_hash=machine.configuration_hash
    draft=build_side_hole_draft([hole()],AXIS,24.2)
    result=assess_side_hole_machine(source,machine,draft)
    assert result["status"]=="failed" and not result["can_accept"]
    assert result["required_offset_axis"]=="Y1"
    assert next(c for c in result["checks"] if c["code"]=="radial_drilling_module")["status"]=="failed"
    machine.validation.capabilities.append("radial_drilling")
    result=assess_side_hole_machine(source,machine,draft)
    assert result["status"]=="review"
    assert next(c for c in result["checks"] if c["code"]=="station_and_travel")["status"]=="review"
    back=assess_side_hole_machine(source,machine,draft,"back")
    assert back["status"]=="failed" and back["required_offset_axis"]=="Y2"
