import math
import pytest
from app.side_hole_draft import build_side_hole_draft

AXIS = {"direction": dict(x=0,y=1,z=0), "origin": dict(x=0,y=-22.231021,z=0)}


def hole(x=8.1, z=6, identifier="HF-14", **changes):
    return {"id":identifier, "kind":"hole", "review_state":"accepted", "diameter":1.5,
            "length":1., "center":dict(x=x,y=-22.344974,z=z), "axis":dict(x=0,y=0,z=1),
            "angular_span_degrees":360, **changes}


def test_typical_four_holes_keep_offsets_and_choose_near_exterior():
    features=[hole(x,z,f"HF-{14+i}") for i,(x,z) in enumerate([(8.1,6),(8.1,-6),(-8.1,6),(-8.1,-6)])]
    draft=build_side_hole_draft(features,AXIS,24.2)
    assert len(draft["holes"])==4
    assert draft["can_accept"] is False and not draft["nc_generated"]
    for feature, record in zip(features,draft["holes"]):
        assert record["axis_offset_mm"] == pytest.approx(8.1)
        assert record["axial_position_mm"] == pytest.approx(-.113953)
        assert record["requires_transverse_positioning"]
        sign=1 if feature["center"]["z"]>0 else -1
        assert record["entry"]["z"]*sign>6.5
        assert record["end"]["z"]==pytest.approx(sign*5.5)
        assert record["entry"]["x"]==feature["center"]["x"]
        assert math.hypot(record["entry"]["x"],record["entry"]["z"])==pytest.approx(13.85)
        assert record["moves"][0]["point"]==record["moves"][-1]["point"]


def test_centered_radial_hole_does_not_require_offset_positioning():
    record=build_side_hole_draft([hole(x=0)],AXIS,24.2)["holes"][0]
    assert record["axis_offset_mm"]==0 and not record["requires_transverse_positioning"]


@pytest.mark.parametrize("feature", [hole(review_state="review"),hole(angular_span_degrees=180),
    hole(kind="boss"),hole(axis=dict(x=0,y=1,z=0)),hole(axis=dict(x=0,y=.2,z=1)),hole(x=20),hole(length=0)])
def test_non_hole_partial_cylinder_or_invalid_access_is_rejected(feature):
    with pytest.raises(ValueError): build_side_hole_draft([feature],AXIS,24.2)


@pytest.mark.parametrize("diameter", [0,-1,float("nan"),2])
def test_bad_or_oversized_tool_is_rejected(diameter):
    with pytest.raises(ValueError): build_side_hole_draft([hole()],AXIS,24.2,tool_diameter_mm=diameter)
