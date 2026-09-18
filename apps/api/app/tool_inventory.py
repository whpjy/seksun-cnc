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
    notes: str = Field(default="", max_length=500)
    active: bool = True

    @model_validator(mode="after")
    def trim_text(self) -> "ToolInventoryInput":
        self.station = self.station.strip()
        self.notes = self.notes.strip()
        self.custom_name = self.custom_name.strip()
        self.custom_kind = self.custom_kind.strip()
        if not self.catalog_tool_id and (not self.custom_name or not self.custom_kind):
            raise ValueError("custom physical tools require a name and tool kind")
        return self


class ToolInventoryRecord(ToolInventoryInput):
    machine_instance_id: str
    catalog_tool_name: str
    tool_kind: str
    verification_state: Literal["recorded"] = "recorded"
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
