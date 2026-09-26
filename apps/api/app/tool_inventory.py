"""Machine-scoped records of physical tools, separate from catalog templates."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .models import Tool


class ToolInventoryInput(BaseModel):
    inventory_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    catalog_tool_id: str = Field(default="", max_length=64)
    custom_name: str = Field(default="", max_length=120)
    custom_kind: str = Field(default="", max_length=64)
    station: str = Field(default="", max_length=64)
    measured_diameter_mm: float | None = Field(default=None, gt=0)
    measured_cutting_width_mm: float | None = Field(default=None, gt=0)
    measured_stickout_mm: float | None = Field(default=None, gt=0)
    measured_holder_diameter_mm: float | None = Field(default=None, gt=0)
    measured_nose_radius_mm: float | None = Field(default=None, gt=0)
    groove_profile: Literal["rectangular", "full_radius"] | None = None
    axial_contouring_supported: bool = False
    capability_verified_by: str = Field(default="", max_length=120)
    capability_verification_reference: str = Field(default="", max_length=240)
    notes: str = Field(default="", max_length=500)
    active: bool = True

    @model_validator(mode="after")
    def trim_text(self) -> "ToolInventoryInput":
        self.station = self.station.strip()
        self.notes = self.notes.strip()
        self.custom_name = self.custom_name.strip()
        self.custom_kind = self.custom_kind.strip()
        self.capability_verified_by = self.capability_verified_by.strip()
        self.capability_verification_reference = self.capability_verification_reference.strip()
        if not self.catalog_tool_id and (not self.custom_name or not self.custom_kind):
            raise ValueError("custom physical tools require a name and tool kind")
        if self.axial_contouring_supported:
            if self.custom_kind and self.custom_kind != "grooving":
                raise ValueError("axial contouring capability is only supported for grooving tools")
            if self.groove_profile != "full_radius" or self.measured_nose_radius_mm is None:
                raise ValueError("axial contour grooving requires measured full-radius insert geometry")
            if not self.capability_verified_by or not self.capability_verification_reference:
                raise ValueError("axial contour grooving requires verifier and evidence reference")
            if self.measured_cutting_width_mm is None or abs(
                2 * self.measured_nose_radius_mm - self.measured_cutting_width_mm
            ) > max(0.02, self.measured_cutting_width_mm * 0.05):
                raise ValueError("full-radius insert radius must equal half the measured cutting width")
        return self


class ToolInventoryRecord(ToolInventoryInput):
    machine_instance_id: str
    catalog_tool_name: str
    tool_kind: str
    verification_state: Literal["recorded", "capability_verified"] = "recorded"
    recorded_at: datetime
    updated_at: datetime


def record_physical_tool(
    machine_instance_id: str,
    source: ToolInventoryInput,
    catalog_tool: Tool | None,
    previous: ToolInventoryRecord | None = None,
) -> ToolInventoryRecord:
    if catalog_tool is not None and source.catalog_tool_id != catalog_tool.id:
        raise ValueError("catalog tool identity mismatch")
    if source.catalog_tool_id and catalog_tool is None:
        raise ValueError("catalog tool is missing")
    if catalog_tool is not None and source.measured_cutting_width_mm is not None and catalog_tool.cutting_width_mm is not None:
        tolerance = max(0.05, catalog_tool.cutting_width_mm * 0.05)
        if abs(source.measured_cutting_width_mm-catalog_tool.cutting_width_mm) > tolerance:
            raise ValueError("measured cutting width conflicts with the catalog template; register a custom physical tool")
    if catalog_tool is None and source.custom_kind == "grooving" and source.measured_cutting_width_mm is None:
        raise ValueError("custom grooving tools require a measured cutting width")
    if previous and (
        previous.inventory_id != source.inventory_id
        or previous.machine_instance_id != machine_instance_id
    ):
        raise ValueError("physical tool identity cannot change")
    now = datetime.now(UTC)
    return ToolInventoryRecord(
        **source.model_dump(),
        machine_instance_id=machine_instance_id,
        catalog_tool_name=catalog_tool.name if catalog_tool else source.custom_name,
        tool_kind=catalog_tool.kind if catalog_tool else source.custom_kind,
        verification_state=(
            "capability_verified" if source.axial_contouring_supported else "recorded"
        ),
        recorded_at=previous.recorded_at if previous else now,
        updated_at=now,
    )


def physical_tool_fit_for_groove(record: ToolInventoryRecord, groove_width_mm: float) -> bool:
    """Width-only screening, never an engineering approval or NC release."""
    return bool(
        record.active
        and record.tool_kind == "grooving"
        and record.measured_cutting_width_mm is not None
        and record.measured_cutting_width_mm <= groove_width_mm + 1e-9
    )


def bind_verified_inventory_tool(record: ToolInventoryRecord, required: Tool) -> Tool:
    """Materialize a trial tool only when measured inventory satisfies its envelope contract."""
    if not record.active:
        raise ValueError("physical tool inventory record is inactive")
    if record.tool_kind != required.kind:
        raise ValueError("physical tool kind does not match the candidate tool")
    if required.cutting_width_mm is not None:
        if record.measured_cutting_width_mm is None:
            raise ValueError("physical tool is missing measured cutting width")
        tolerance = max(0.02, required.cutting_width_mm * 0.05)
        if abs(record.measured_cutting_width_mm - required.cutting_width_mm) > tolerance:
            raise ValueError("measured cutting width does not match the candidate envelope")
    if required.groove_profile == "full_radius":
        if record.verification_state != "capability_verified":
            raise ValueError("full-radius contour tool capability has not been verified")
        if record.groove_profile != "full_radius" or not record.axial_contouring_supported:
            raise ValueError("inventory tool is not verified for full-radius axial contouring")
        if required.nose_radius_mm is None or record.measured_nose_radius_mm is None:
            raise ValueError("full-radius contour tool is missing measured tip radius")
        tolerance = max(0.01, required.nose_radius_mm * 0.05)
        if abs(record.measured_nose_radius_mm - required.nose_radius_mm) > tolerance:
            raise ValueError("measured tip radius does not match the candidate compensation")
    bound = required.model_copy(deep=True)
    bound.id = f"INV-{record.inventory_id}"
    bound.name = record.catalog_tool_name
    bound.catalog_match = True
    bound.inventory_id = record.inventory_id
    if record.measured_diameter_mm is not None:
        bound.diameter_mm = record.measured_diameter_mm
    if record.measured_cutting_width_mm is not None:
        bound.cutting_width_mm = record.measured_cutting_width_mm
    if record.measured_nose_radius_mm is not None:
        bound.nose_radius_mm = record.measured_nose_radius_mm
    if record.measured_stickout_mm is not None:
        bound.stickout_mm = record.measured_stickout_mm
    if record.measured_holder_diameter_mm is not None:
        bound.holder_diameter_mm = record.measured_holder_diameter_mm
    return bound


def inventory_binding_evidence_request(
    required: Tool,
    records: list[ToolInventoryRecord],
) -> dict[str, object]:
    """Explain exactly what physical evidence is needed to bind a candidate tool."""
    compatible: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    for record in records:
        try:
            bind_verified_inventory_tool(record, required)
        except ValueError as error:
            rejected.append({
                "inventory_id": record.inventory_id,
                "name": record.catalog_tool_name,
                "reason": str(error),
            })
        else:
            compatible.append({
                "inventory_id": record.inventory_id,
                "name": record.catalog_tool_name,
                "station": record.station,
                "verification_state": record.verification_state,
                "verification_reference": record.capability_verification_reference,
            })

    fields: list[dict[str, object]] = [
        {
            "field": "inventory_id",
            "label": "现场刀具编号",
            "required": True,
            "source": "machine_inventory",
        },
        {
            "field": "measured_cutting_width_mm",
            "label": "实测刃宽（mm）",
            "required": required.cutting_width_mm is not None,
            "expected_value": required.cutting_width_mm,
            "source": "physical_measurement",
        },
    ]
    if required.groove_profile == "full_radius":
        fields.extend([
            {
                "field": "measured_nose_radius_mm",
                "label": "实测刀尖半径（mm）",
                "required": True,
                "expected_value": required.nose_radius_mm,
                "source": "physical_measurement",
            },
            {
                "field": "groove_profile",
                "label": "刀片轮廓",
                "required": True,
                "expected_value": "full_radius",
                "source": "physical_inspection",
            },
            {
                "field": "axial_contouring_supported",
                "label": "是否验证支持轴向轮廓加工",
                "required": True,
                "expected_value": True,
                "source": "capability_verification",
            },
            {
                "field": "capability_verified_by",
                "label": "能力检验人",
                "required": True,
                "source": "human_confirmation",
            },
            {
                "field": "capability_verification_reference",
                "label": "检验记录或证明编号",
                "required": True,
                "source": "inspection_record",
            },
        ])
    return {
        "status": "ready_to_bind" if compatible else "user_evidence_required",
        "required_tool": {
            "tool_id": required.id,
            "name": required.name,
            "kind": required.kind,
            "cutting_width_mm": required.cutting_width_mm,
            "nose_radius_mm": required.nose_radius_mm,
            "groove_profile": required.groove_profile,
            "axial_contouring_supported": required.axial_contouring_supported,
        },
        "compatible_inventory": compatible,
        "rejected_inventory": rejected[:20],
        "required_evidence": [item for item in fields if item["required"]],
        "agent_instruction": (
            "Bind one compatible inventory record."
            if compatible
            else "Ask the user for the listed physical evidence; never infer measurement or verifier values."
        ),
        "next_action": (
            "bind_l32_candidate_inventory_tool"
            if compatible else "request_user_tool_measurement"
        ),
    }
