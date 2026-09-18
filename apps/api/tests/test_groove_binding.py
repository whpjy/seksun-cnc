from app.groove_binding import (
    DrawingGrooveRequirement,
    GrooveCandidateEvidence,
    assess_case_grooves,
    bind_groove_requirement,
)
from app.rotational_features import TurningProfileFeature


def candidate(identifier: str, side: str = "external", width: float = 2, depth: float = 1) -> GrooveCandidateEvidence:
    return GrooveCandidateEvidence(
        id=identifier,
        profile_id="PROFILE-1",
        side=side,
        z_start_mm=4,
        z_end_mm=4 + width,
        width_mm=width,
        depth_mm=depth,
        bottom_diameter_mm=8 if side == "external" else 12,
        confidence=0.7,
        review_state="review",
    )


def test_verified_dimensions_bind_a_unique_groove() -> None:
    requirement = DrawingGrooveRequirement(
        id="REQ-1", kind="external_groove", side="external",
        width_mm=2, bottom_diameter_mm=8, verification_status="verified_dimensions",
    )

    binding = bind_groove_requirement(
        requirement, [candidate("MATCH"), candidate("OTHER", width=3)],
    )

    assert binding.status == "matched"
    assert binding.matched_candidate_id == "MATCH"


def test_recognized_only_requirement_never_auto_binds() -> None:
    requirement = DrawingGrooveRequirement(
        id="REQ-1", kind="seal_groove", side="unknown",
    )

    binding = bind_groove_requirement(requirement, [candidate("A"), candidate("B")])

    assert binding.status == "ambiguous"
    assert binding.candidate_ids == ["A", "B"]


def test_side_mismatch_is_unmapped() -> None:
    requirement = DrawingGrooveRequirement(
        id="REQ-1", kind="internal_groove", side="internal",
    )

    assert bind_groove_requirement(requirement, [candidate("A")]).status == "unmapped"


def test_manifest_groove_requirement_is_traced_to_step_candidates() -> None:
    case = {
        "id": "PART-1",
        "drawing_files": ["PART-1.pdf"],
        "expected": {"required_features": ["outer_turn_profile", "external_groove"]},
    }
    feature = TurningProfileFeature(
        id="TPF-1", profile_id="PROFILE-1", kind="external_groove_candidate",
        z_start=2, z_end=4, radius_start=4, radius_end=4,
        width_mm=2, depth_mm=1, source_point_indices=[1, 2], confidence=0.7,
    )

    assessment = assess_case_grooves(case, [feature])

    assert assessment["status"] == "ambiguous"
    assert assessment["requirements"][0]["source"]["drawing_files"] == ["PART-1.pdf"]
    assert assessment["bindings"][0]["candidate_ids"] == ["TPF-1"]
