from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


AxisKind = Literal["linear", "rotary"]
OperationMode = Literal["guide_bushing", "guide_bushing_less"]
IssueSeverity = Literal["blocking", "warning"]


class AxisDefinition(BaseModel):
    id: str
    kind: AxisKind
    channel_id: str
    component: str
    availability: Literal["standard", "variant"] = "standard"
    variants: list[str] = Field(default_factory=list)
    maximum_rapid_mm_min: float | None = Field(default=None, gt=0)
    minimum_position: float | None = None
    maximum_position: float | None = None
    angular_range_degrees: list[float] | None = None
    continuous_interpolation: bool = False


class SpindleDefinition(BaseModel):
    id: str
    name: str
    channel_id: str
    role: Literal["work", "live_tool"]
    maximum_rpm: int = Field(gt=0)
    rated_rpm: int | None = Field(default=None, gt=0)
    motor_kw: list[float] = Field(default_factory=list)
    standard_indexing_increment_degrees: float | None = Field(default=None, gt=0)
    continuous_c_axis_option_id: str | None = None


class MachineModuleDefinition(BaseModel):
    id: str
    name: str
    station_group: Literal["gang", "opposed", "back"]
    compatible_variants: list[str]
    required_axes: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    fixed_tool_positions: int = Field(default=0, ge=0)
    live_tool_positions: int = Field(default=0, ge=0)
    notes: list[str] = Field(default_factory=list)


class MachineVariantDefinition(BaseModel):
    id: str
    model_code: str
    enabled_axes: list[str]
    minimum_tool_positions: int = Field(ge=0)
    maximum_tool_positions: int = Field(ge=0)


class MachineDefinition(BaseModel):
    schema_version: str = "1.0.0"
    id: str
    manufacturer: str
    family: str
    source_document: str
    source_revision: str
    controller_family: str
    standard_bar_diameter_mm: float = Field(gt=0)
    optional_bar_diameter_mm: float | None = Field(default=None, gt=0)
    maximum_length_per_chucking_mm: float = Field(gt=0)
    axes: list[AxisDefinition]
    spindles: list[SpindleDefinition]
    modules: list[MachineModuleDefinition]
    variants: list[MachineVariantDefinition]


class MachineInstance(BaseModel):
    schema_version: str = "1.0.0"
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    definition_id: str
    name: str
    serial_number: str | None = None
    manufacture_year: int | None = Field(default=None, ge=1990, le=2100)
    variant: str
    controller_revision: str | None = None
    operation_mode: OperationMode
    installed_modules: list[str] = Field(default_factory=list)
    enabled_options: list[str] = Field(default_factory=list)
    bar_diameter_mm: float = Field(default=32, gt=0)
    postprocessor_profile_id: str | None = None


class MachineConfigurationIssue(BaseModel):
    code: str
    severity: IssueSeverity
    field: str
    message: str


class MachineConfigurationValidation(BaseModel):
    valid: bool
    production_ready: bool
    enabled_axes: list[str]
    capabilities: list[str]
    issues: list[MachineConfigurationIssue] = Field(default_factory=list)


class MachineConfigurationSnapshot(BaseModel):
    schema_version: str = "1.0.0"
    instance: MachineInstance
    definition_revision: str
    configuration_hash: str
    captured_at: datetime
    validation: MachineConfigurationValidation


class MachineBindingRequest(BaseModel):
    machine_instance_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
