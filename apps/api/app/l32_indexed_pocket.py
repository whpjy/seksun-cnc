from __future__ import annotations

from math import ceil, pi
from typing import Literal

from pydantic import BaseModel, Field

from .models import Bounds, GeometryAnalysis, PrismaticFeature, Vec3


class PocketMove(BaseModel):
    kind: Literal["rapid", "feed"]
    point: Vec3
    layer: int = Field(ge=0)


class IndexedPocketDraft(BaseModel):
    schema_version: str = "1.0.0"
    feature_id: str
    source_face_id: str
    source_face_index: int
    floor_bounds: Bounds
    access_direction: Vec3
    pocket_depth_mm: float
    required_module: str = "U151B"
    reference_only: bool = True
    nc_generated: bool = False
    bound_machine_has_required_module: bool | None = None
    coordinate_frame: str = "source_step_xyz_mm"
    tool_diameter_mm: float
    provisional_flute_length_mm: float = 3.0
    tool_catalog_match: bool = False
    depth_step_mm: float
    stepover_mm: float
    depth_layers: int
    rectangular_target_volume_mm3: float
    minimum_uncut_corner_volume_mm3: float
    moves: list[PocketMove]


class PocketSweepStage(BaseModel):
    layer: int = Field(ge=1)
    uncut_region_mm3: float = Field(ge=0)


class IndexedPocketSweepCheck(BaseModel):
    schema_version: str = "1.0.0"
    feature_id: str
    reference_only: bool = True
    nc_generated: bool = False
    bound_machine_has_required_module: bool | None = None
    pocket_region_status: Literal["overcut", "residual", "within_geometric_tolerance"]
    target_material_inside_pocket_region_mm3: float = Field(ge=0)
    summed_target_contact_mm3: float = Field(ge=0)
    remaining_pocket_region_mm3: float = Field(ge=0)
    removed_pocket_region_mm3: float = Field(ge=0)
    stages: list[PocketSweepStage]


def _axis_index(vector: Vec3) -> tuple[int, int]:
    coordinates = (vector.x, vector.y, vector.z)
    axis = max(range(3), key=lambda index: abs(coordinates[index]))
    if abs(coordinates[axis]) < 0.999999 or any(
        abs(value) > 0.000001 for index, value in enumerate(coordinates) if index != axis
    ):
        raise ValueError("indexed pocket draft requires a cardinal access direction")
    return axis, 1 if coordinates[axis] > 0 else -1


def _positions(low: float, high: float, maximum_step: float) -> list[float]:
    if high < low - 1e-8:
        raise ValueError("cutter diameter exceeds pocket cross-section")
    if abs(high - low) < 1e-8:
        return [(low + high) / 2]
    count = max(1, ceil((high - low) / maximum_step))
    return [low + (high - low) * index / count for index in range(count + 1)]


def build_indexed_back_pocket_draft(
    analysis: GeometryAnalysis,
    feature: PrismaticFeature,
    *,
    tool_diameter_mm: float = 1.0,
    depth_step_mm: float = 0.3,
    stepover_mm: float = 0.35,
    clearance_mm: float = 0.5,
) -> IndexedPocketDraft:
    """Generate a geometric centerline draft, not machine coordinates or NC.

    The source planar face must be an axis-aligned rectangle. Its area/bounds
    check avoids treating an arbitrary prismatic feature's bounding box as a
    machinable pocket. The swept cutter leaves finite-radius corner stock.
    """
    if feature.kind != "pocket" or feature.review_state != "accepted":
        raise ValueError("pocket feature must be accepted")
    if feature.depth <= 0:
        raise ValueError("pocket depth must be positive")
    if min(tool_diameter_mm, depth_step_mm, stepover_mm, clearance_mm) <= 0:
        raise ValueError("tool and cutting increments must be positive")
    axis, sign = _axis_index(feature.access_direction)
    face = next((item for item in analysis.planar_features if item.id == feature.source_face_id), None)
    if face is None or face.bounds is None or face.source_face_index is None:
        raise ValueError("accepted pocket has no exact source planar face")
    face_axis, face_sign = _axis_index(face.normal)
    if face_axis != axis or face_sign != sign or face.wire_count != 1:
        raise ValueError("source face orientation or wire topology is not a simple pocket floor")
    minimum = (face.bounds.minimum.x, face.bounds.minimum.y, face.bounds.minimum.z)
    maximum = (face.bounds.maximum.x, face.bounds.maximum.y, face.bounds.maximum.z)
    cross = [index for index in range(3) if index != axis]
    lengths = [maximum[index] - minimum[index] for index in cross]
    rectangle_area = lengths[0] * lengths[1]
    if (
        maximum[axis] - minimum[axis] > 0.01
        or rectangle_area <= 0
        or abs(face.area - rectangle_area) > max(0.0001, rectangle_area * 0.0001)
        or face.adjacent_edge_count != 4
    ):
        raise ValueError("source face is not an exact axis-aligned rectangle")
    radius = tool_diameter_mm / 2
    if any(length < tool_diameter_mm - 1e-8 for length in lengths):
        raise ValueError("cutter diameter exceeds pocket width")
    # The floor face lies at the end of the cut. The access direction points
    # out of the part, toward the back spindle and away from the floor.
    floor = (minimum[axis] + maximum[axis]) / 2
    entrance = floor + sign * feature.depth
    safe = entrance + sign * clearance_mm
    layers = max(1, ceil(feature.depth / depth_step_mm))
    short, long = sorted(cross, key=lambda index: maximum[index] - minimum[index])
    short_positions = _positions(minimum[short] + radius, maximum[short] - radius, stepover_mm)
    long_low, long_high = minimum[long] + radius, maximum[long] - radius

    def point(axial: float, short_value: float, long_value: float) -> Vec3:
        coordinates = [0.0, 0.0, 0.0]
        coordinates[axis] = axial
        coordinates[short] = short_value
        coordinates[long] = long_value
        return Vec3(x=round(coordinates[0], 6), y=round(coordinates[1], 6), z=round(coordinates[2], 6))

    moves: list[PocketMove] = []
    for layer in range(1, layers + 1):
        axial = entrance - sign * min(layer * depth_step_mm, feature.depth)
        first = short_positions[0]
        moves.append(PocketMove(kind="rapid", point=point(safe, first, long_low), layer=layer))
        moves.append(PocketMove(kind="feed", point=point(axial, first, long_low), layer=layer))
        for index, short_value in enumerate(short_positions):
            start_long = long_low if index % 2 == 0 else long_high
            end_long = long_high if index % 2 == 0 else long_low
            if index:
                moves.append(PocketMove(kind="feed", point=point(axial, short_value, start_long), layer=layer))
            moves.append(PocketMove(kind="feed", point=point(axial, short_value, end_long), layer=layer))
        last_long = long_high if (len(short_positions) - 1) % 2 == 0 else long_low
        moves.append(PocketMove(kind="feed", point=point(safe, short_positions[-1], last_long), layer=layer))

    return IndexedPocketDraft(
        feature_id=feature.id,
        source_face_id=face.id,
        source_face_index=face.source_face_index,
        floor_bounds=face.bounds,
        access_direction=feature.access_direction,
        pocket_depth_mm=feature.depth,
        tool_diameter_mm=tool_diameter_mm,
        depth_step_mm=depth_step_mm,
        stepover_mm=stepover_mm,
        depth_layers=layers,
        rectangular_target_volume_mm3=round(rectangle_area * feature.depth, 6),
        minimum_uncut_corner_volume_mm3=round((4 - pi) * radius ** 2 * feature.depth, 6),
        moves=moves,
    )
