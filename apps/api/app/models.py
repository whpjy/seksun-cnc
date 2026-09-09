from __future__ import annotations

from typing import Literal

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


class GeometryAnalysis(BaseModel):
    schema_version: str
    source_file: str
    topology: dict[str, int]
    measurements: dict[str, float | Bounds]
    planar_features: list[PlanarFeature]
    cylindrical_features: list[CylindricalFeature]
    prismatic_features: list[PrismaticFeature] = Field(default_factory=list)
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


class Setup(BaseModel):
    id: str
    name: str
    work_axis: Vec3
    datum_feature_id: str | None
    fixture: str
    operations: list[Operation]


class ProcessPlan(BaseModel):
    schema_version: str = "0.8.0"
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


class JobResponse(BaseModel):
    id: str
    status: Literal["processing", "completed", "failed"]
    filename: str
    created_at: str
    material: str
    machine: str
    analysis: GeometryAnalysis | None = None
    plan: ProcessPlan | None = None
    model_url: str | None = None
    error: str | None = None


class FeatureReviewRequest(BaseModel):
    review_state: Literal["accepted", "review", "excluded"]


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


class OperationUpdateRequest(BaseModel):
    name: str | None = None
    feature_ids: list[str] | None = None
    tool_id: str | None = None
    parameters: dict[str, OperationParameterValue] | None = None
    enabled: bool | None = None


class OperationReorderRequest(BaseModel):
    operation_ids: list[str]
