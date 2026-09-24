from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Vec3(BaseModel):
    x: float
    y: float
    z: float


class Bounds(BaseModel):
    minimum: Vec3
    maximum: Vec3
    size: Vec3


class PlanarFeature(BaseModel):
    id: str
    source_face_index: int | None = None
    area: float
    center: Vec3
    normal: Vec3
    bounds: Bounds | None = None
    wire_count: int = 1
    adjacent_edge_count: int = 0
    rising_edge_count: int = 0
    falling_edge_count: int = 0
    level_edge_count: int = 0
    free_edge_count: int = 0


class PrismaticFeature(BaseModel):
    id: str
    kind: Literal["pocket", "slot"]
    source_face_id: str
    center: Vec3
    bounds: Bounds
    access_direction: Vec3
    length: float
    width: float
    depth: float
    open_sides: int = 0
    confidence: float = Field(default=0.7, ge=0, le=1)
    review_state: Literal["accepted", "review", "excluded"] = "review"
    review_reasons: list[str] = Field(default_factory=list)


class PlanarMachiningFeature(BaseModel):
    id: str
    kind: Literal["planar_surface"] = "planar_surface"
    source_face_ids: list[str] = Field(default_factory=list)
    center: Vec3
    bounds: Bounds
    access_direction: Vec3
    length: float = Field(ge=0)
    width: float = Field(ge=0)
    depth: float = Field(default=0.05, ge=0)
    confidence: float = Field(default=0.7, ge=0, le=1)
    review_state: Literal["accepted", "review", "excluded"] = "review"
    review_reasons: list[str] = Field(default_factory=list)


class InternalProfileFeature(BaseModel):
    id: str
    kind: Literal["internal_profile"] = "internal_profile"
    source_face_id: str
    source_face_index: int | None = None
    wire_index: int = Field(ge=1)
    center: Vec3
    bounds: Bounds
    access_direction: Vec3
    edge_count: int = 0
    perimeter: float = Field(default=0, ge=0)
    circular: bool = False
    paired_profile_id: str | None = None
    bottom_face_id: str | None = None
    end_type: Literal["through", "blind", "unknown"] = "unknown"
    machining_kind: Literal["through_profile", "blind_pocket", "engraving", "unknown"] = "unknown"
    depth: float = Field(default=0, ge=0)
    length: float = Field(default=0, ge=0)
    width: float = Field(default=0, ge=0)
    confidence: float = Field(default=0.6, ge=0, le=1)
    review_state: Literal["accepted", "review", "excluded"] = "review"
    review_reasons: list[str] = Field(default_factory=list)


class CylindricalFeature(BaseModel):
    id: str
    kind: Literal["hole", "boss", "cylinder"]
    radius: float
    diameter: float
    length: float
    center: Vec3
    axis: Vec3
    angular_span_degrees: float = Field(default=360, ge=0, le=360)
    source_face_ids: list[str] = Field(default_factory=list)
    segment_count: int = 1
    end_type: Literal["through", "blind", "unknown"] = "unknown"
    access_direction: Vec3 | None = None
    confidence: float = Field(default=0.7, ge=0, le=1)
    review_state: Literal["accepted", "review", "excluded"] = "review"
    review_reasons: list[str] = Field(default_factory=list)


class SolidCandidate(BaseModel):
    index: int = Field(ge=1)
    volume: float = Field(ge=0)
    surface_area: float = Field(ge=0)
    center: Vec3
    bounds: Bounds
    selected: bool = False


class RotationalSectionPoint(BaseModel):
    z: float
    radius: float = Field(ge=0)


class RotationalSectionCandidate(BaseModel):
    source_feature_id: str
    axis_origin: Vec3
    axis: Vec3
    plane_normal: Vec3
    outer_profile: list[RotationalSectionPoint]
    inner_profile: list[RotationalSectionPoint] = Field(default_factory=list)
    tolerance_mm: float = Field(gt=0)
    warnings: list[str] = Field(default_factory=list)


class GeometryAnalysis(BaseModel):
    schema_version: str
    source_file: str
    topology: dict[str, int]
    measurements: dict[str, float | Bounds]
    planar_features: list[PlanarFeature]
    cylindrical_features: list[CylindricalFeature]
    prismatic_features: list[PrismaticFeature] = Field(default_factory=list)
    planar_machining_features: list[PlanarMachiningFeature] = Field(default_factory=list)
    internal_profile_features: list[InternalProfileFeature] = Field(default_factory=list)
    solid_candidates: list[SolidCandidate] = Field(default_factory=list)
    rotational_sections: list[RotationalSectionCandidate] = Field(default_factory=list)
    rotational_profile_reviews: dict[str, Literal["accepted", "review", "excluded"]] = Field(default_factory=dict)
    visual_edges: list[list[Vec3]] = Field(default_factory=list)


class Tool(BaseModel):
    id: str
    name: str
    kind: str
    diameter_mm: float
    flute_count: int = 2
    max_rpm: int = 10000
    catalog_match: bool = True
    flute_length_mm: float = 20
    stickout_mm: float = 35
    holder_diameter_mm: float = 32
    nose_radius_mm: float | None = Field(default=None, ge=0)
    cutting_width_mm: float | None = Field(default=None, gt=0)
    insert_shape: str | None = None
    hand: Literal["left", "right", "neutral"] | None = None
    orientation_code: int | None = Field(default=None, ge=1, le=9)


class MaterialProfile(BaseModel):
    id: str
    name: str
    milling_speed_m_min: float
    drilling_speed_m_min: float
    mill_feed_per_tooth_mm: float
    drill_feed_per_rev_mm: float


class MachineProfile(BaseModel):
    id: str
    name: str
    axes: int
    travel_mm: list[float]
    max_spindle_rpm: int
    max_feed_mm_min: float
    max_tool_diameter_mm: float
    postprocessor: str | None = None


class FixtureComponent(BaseModel):
    id: str
    name: str
    bounds: Bounds
    setup_id: str | None = None
    work_axis: Vec3 | None = None
    kind: Literal["fixture", "sacrificial", "machine"] = "fixture"


class SafetyConfiguration(BaseModel):
    clearance_mm: float = Field(default=3, ge=0.5, le=50)
    vise_grip_height_mm: float = Field(default=1.5, ge=0.5, le=50)
    fixture_strategy: Literal["vise", "sacrificial_plate"] = "vise"
    support_thickness_mm: float = Field(default=3.0, ge=0.5, le=50)
    fixture_components: list[FixtureComponent] = Field(default_factory=list)


class Operation(BaseModel):
    id: str
    sequence: int
    type: str
    name: str
    feature_ids: list[str]
    tool: Tool
    parameters: dict[str, float | int | str | bool]
    rationale: list[str]
    confidence: float = Field(ge=0, le=1)
    status: Literal["proposed", "approved", "warning"] = "proposed"
    definition_id: str | None = None
    definition_version: int = 1
    source: Literal["manual", "automatic", "template", "recommendation"] = "automatic"
    enabled: bool = True
    generation_state: Literal["dirty", "generating", "generated", "failed"] = "dirty"
    manufacturing_code: str | None = None
    channel_id: Literal["main", "sub"] | None = None
    spindle_id: Literal["main", "sub"] | None = None
    workpiece_side: Literal["front", "back"] | None = None
    synchronization_group: str | None = None


class Setup(BaseModel):
    id: str
    name: str
    work_axis: Vec3
    datum_feature_id: str | None
    fixture: str
    operations: list[Operation]
    machine_id: str | None = None
    machine_name: str | None = None


class CoverageTarget(BaseModel):
    id: str
    kind: Literal["hole", "pocket", "slot", "surface", "outer_profile", "internal_profile"]
    label: str
    state: Literal["covered", "uncovered", "review", "unresolved"]
    required_operation_types: list[str] = Field(default_factory=list)
    covered_by: list[str] = Field(default_factory=list)
    source_feature_ids: list[str] = Field(default_factory=list)


class ManufacturingCoverage(BaseModel):
    schema_version: str = "1.0.0"
    status: Literal["complete", "review", "incomplete"]
    score: float = Field(ge=0, le=1)
    target_count: int = 0
    covered_count: int = 0
    unresolved_count: int = 0
    review_count: int = 0
    production_ready: bool = False
    targets: list[CoverageTarget] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    capability_gaps: list[str] = Field(default_factory=list)


class ProcessKnowledgeAssessment(BaseModel):
    schema_version: str = "1.0.0"
    catalog_version: str
    catalog_process_count: int
    status: Literal["complete", "review", "incomplete"]
    production_ready: bool = False
    route_process_codes: list[str] = Field(default_factory=list)
    operation_count: int = 0
    mapped_operation_count: int = 0
    executable_operation_count: int = 0
    unmapped_operation_ids: list[str] = Field(default_factory=list)
    nonvalidated_operation_ids: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    required_engineer_decisions: list[str] = Field(default_factory=list)


class ManufacturingRouteStep(BaseModel):
    sequence: int
    process_code: str
    name: str
    phase: Literal[
        "incoming", "blank", "roughing", "stabilization", "semi_finishing",
        "finishing", "special", "surface_treatment", "inspection", "release",
    ]
    selection: Literal["required", "conditional"]
    execution_mode: Literal["cam", "external", "manual", "inspection"]
    execution_state: Literal["partially_executable", "reference_only"]
    cam_operation_ids: list[str] = Field(default_factory=list)
    source_operation_ids: list[str] = Field(default_factory=list)
    source_feature_ids: list[str] = Field(default_factory=list)
    prerequisite_codes: list[str] = Field(default_factory=list)
    reason: str
    confidence: float = Field(ge=0, le=1)
    blocking_missing_information: list[str] = Field(default_factory=list)


class ManufacturingRouteProposal(BaseModel):
    schema_version: str = "1.0.0"
    catalog_version: str
    status: Literal["complete", "review", "incomplete"]
    part_family: Literal["prismatic", "rotational", "freeform", "sheet_forming", "mixed"]
    planning_basis: list[str] = Field(default_factory=list)
    steps: list[ManufacturingRouteStep] = Field(default_factory=list)
    capability_gaps: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    alternative_process_codes: list[str] = Field(default_factory=list)


class ThreadSpecification(BaseModel):
    designation: str
    standard: Literal["UNF", "UNC", "UNEF", "BSPP"]
    side: Literal["external", "internal", "unknown"] = "unknown"
    nominal_size: str
    major_diameter_mm: float = Field(gt=0)
    threads_per_inch: float = Field(gt=0)
    pitch_mm: float = Field(gt=0)
    form_angle_degrees: float = Field(gt=0)
    class_fit: str | None = None
    handedness: Literal["right", "left", "unknown"] = "right"


class ManufacturingRequirement(BaseModel):
    id: str
    type: str
    subtype: str | None = None
    nominal: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    tolerance_upper: float | None = None
    tolerance_lower: float | None = None
    unit: str = "mm"
    quantity: int = Field(default=1, ge=1)
    parameter: str | None = None
    cad_feature_ids: list[str] = Field(default_factory=list)
    mapping_status: Literal["matched", "ambiguous", "unmapped", "not_applicable"]
    verification_status: str
    confidence: float = Field(default=0, ge=0, le=1)
    raw_text: str | None = None
    thread: ThreadSpecification | None = None
    source: dict[str, Any] = Field(default_factory=dict)


class ManufacturingRequirements(BaseModel):
    schema_version: str = "1.0.0"
    source_system: str
    source_schema_version: str | None = None
    drawing_number: str | None = None
    revision: str | None = None
    status: Literal["complete", "review", "incomplete"]
    requirements: list[ManufacturingRequirement] = Field(default_factory=list)
    unresolved_requirement_ids: list[str] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


class ManufacturingRequirementsImportRequest(BaseModel):
    source_system: str = "seksun-meas"
    specification: dict[str, Any]


class ThreadBindingConfirmRequest(BaseModel):
    thread_feature_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)


class GrooveBindingConfirmRequest(BaseModel):
    groove_feature_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1, max_length=100)
    requirement_kind: Literal["external_groove", "internal_groove", "seal_groove"]
    confirmed_groove_width_mm: float = Field(gt=0, le=50)
    confirmed_groove_depth_mm: float | None = Field(default=None, gt=0, le=50)
    confirmed_bottom_diameter_mm: float = Field(gt=0, le=100)
    raw_text: str | None = Field(default=None, max_length=500)
    reviewer: str = Field(min_length=1, max_length=100)


class ThreadOperationReviewRequest(BaseModel):
    start_z_mm: float
    end_z_mm: float
    thread_depth_mm: float = Field(gt=0)
    pass_count: int = Field(ge=1, le=20)
    relief_strategy: Literal["groove", "runout", "thread_to_end"]
    relief_width_mm: float = Field(default=0, ge=0)
    tool_insert_id: str = Field(min_length=1, max_length=64)
    controller_cycle_id: str = Field(min_length=1, max_length=64)
    reviewer: str = Field(min_length=1, max_length=100)


class GroovingOperationReviewRequest(BaseModel):
    confirmed_groove_width_mm: float = Field(gt=0, le=50)
    confirmed_final_diameter_mm: float = Field(gt=0, le=100)
    peck_depth_mm: float = Field(gt=0, le=10)
    groove_tool_id: str = Field(min_length=1, max_length=64)
    groove_tool_inventory_id: str = Field(min_length=1, max_length=64)
    confirmed_stickout_mm: float | None = Field(default=None, gt=0, le=200)
    assembly_clearance_mm: float = Field(default=0.2, ge=0, le=5)
    profile_form_confirmed: bool = False
    reviewer: str = Field(min_length=1, max_length=100)


class BoringOperationReviewRequest(BaseModel):
    initial_bore_diameter_mm: float = Field(gt=0)
    confirmed_stickout_mm: float = Field(gt=0, le=200)
    assembly_clearance_mm: float = Field(default=0.2, ge=0, le=5)
    finishing_tool_id: str | None = Field(default=None, min_length=1, max_length=64)
    shoulder_strategy: Literal["not_required", "small_nose_tool"] = "not_required"
    boring_bar_inventory_id: str = Field(min_length=1, max_length=64)
    reviewer: str = Field(min_length=1, max_length=100)


class AxialDrillingOperationReviewRequest(BaseModel):
    drill_tool_id: str = Field(min_length=1, max_length=64)
    confirmed_stickout_mm: float = Field(gt=0, le=200)
    drill_point_angle_deg: float = Field(default=118, ge=90, le=150)
    peck_depth_mm: float = Field(gt=0, le=50)
    bottom_condition: Literal["through", "blind_tip_allowance_confirmed"]
    tip_overtravel_allowance_mm: float = Field(default=0, ge=0, le=50)
    chip_evacuation_strategy: Literal["standard_peck", "deep_hole_peck"] = "standard_peck"
    through_tool_coolant_confirmed: bool = False
    drill_inventory_id: str = Field(min_length=1, max_length=64)
    reviewer: str = Field(min_length=1, max_length=100)


class ProcessPlan(BaseModel):
    schema_version: str = "0.8.0"
    process_kind: Literal["subtractive", "sheet_forming"] = "subtractive"
    title: str
    material: str
    machine: str
    material_profile: MaterialProfile | None = None
    machine_profile: MachineProfile | None = None
    safety: SafetyConfiguration | None = None
    stock: dict[str, object]
    setups: list[Setup]
    warnings: list[str]
    assumptions: list[str]
    estimated_minutes: float
    automation_status: Literal["ready", "review", "unsupported"] = "ready"
    blocking_reasons: list[str] = Field(default_factory=list)
    coverage: ManufacturingCoverage | None = None
    manufacturing_requirements: ManufacturingRequirements | None = None
    manufacturing_route: ManufacturingRouteProposal | None = None
    knowledge_assessment: ProcessKnowledgeAssessment | None = None
    ai_planning: dict[str, Any] | None = None


class JobResponse(BaseModel):
    id: str
    status: Literal["processing", "completed", "failed"]
    filename: str
    created_at: str
    material: str
    machine: str
    device_id: str | None = None
    machine_instance_id: str | None = None
    machine_configuration_hash: str | None = None
    analysis: GeometryAnalysis | None = None
    plan: ProcessPlan | None = None
    model_url: str | None = None
    drawing_filename: str | None = None
    drawing_url: str | None = None
    measurement_job_id: str | None = None
    error: str | None = None


class JobHistoryItem(BaseModel):
    id: str
    status: Literal["processing", "completed", "failed"]
    filename: str
    created_at: str
    material: str
    machine: str
    process_kind: Literal["subtractive", "sheet_forming"] | None = None
    setup_count: int = 0
    operation_count: int = 0


class FeatureReviewRequest(BaseModel):
    review_state: Literal["accepted", "review", "excluded"]


class SolidSelectionRequest(BaseModel):
    solid_index: int = Field(ge=1)


class SafetyConfigurationRequest(BaseModel):
    clearance_mm: float = Field(ge=0.5, le=50)
    vise_grip_height_mm: float = Field(ge=0.5, le=50)
    support_thickness_mm: float = Field(default=3.0, ge=0.5, le=50)


OperationParameterValue = float | int | str | bool


class OperationCreateRequest(BaseModel):
    definition_id: str
    feature_ids: list[str] = Field(default_factory=list)
    tool_id: str | None = None
    name: str | None = None
    parameters: dict[str, OperationParameterValue] = Field(default_factory=dict)
    insert_after_operation_id: str | None = None


class OperationUpdateRequest(BaseModel):
    name: str | None = None
    feature_ids: list[str] | None = None
    tool_id: str | None = None
    parameters: dict[str, OperationParameterValue] | None = None
    enabled: bool | None = None


class OperationReorderRequest(BaseModel):
    operation_ids: list[str]
