from __future__ import annotations

from math import ceil, hypot
from typing import Literal

from pydantic import BaseModel, Field

from .models import Vec3


class SideMillMove(BaseModel):
    kind: Literal["rapid", "feed"]
    point: Vec3
    layer: int = Field(ge=1)


class SideMillDraft(BaseModel):
    schema_version: str = "1.0.0"
    source_face_number: int
    analysis_face_id: str
    mode: Literal["side_face", "exterior_clear"] = "side_face"
    access_sign: Literal[-1, 1]
    required_module: str = "U30B"
    reference_only: bool = True
    nc_generated: bool = False
    coordinate_frame: str = "source_step_xyz_mm"
    tool_diameter_mm: float
    tool_catalog_match: bool = False
    provisional_flute_length_mm: float = 3.0
    silhouette_clearance_mm: float = 0.0
    stock_radius_mm: float
    face_y_mm: float
    layer_count: int
    raster_count_per_layer: int
    moves: list[SideMillMove]


def _distance_to_segment(x: float, z: float, a: tuple[float,float], b: tuple[float,float]) -> float:
    dx, dz = b[0]-a[0], b[1]-a[1]
    length_squared = dx*dx+dz*dz
    if length_squared < 1e-16:
        return hypot(x-a[0], z-a[1])
    t = max(0.0, min(1.0, ((x-a[0])*dx+(z-a[1])*dz)/length_squared))
    return hypot(x-a[0]-t*dx, z-a[1]-t*dz)


def _inside_polygon(x: float, z: float, vertices: list[tuple[float,float]]) -> bool:
    inside = False
    for a, b in zip(vertices, vertices[1:]+vertices[:1]):
        if (a[1] > z) != (b[1] > z):
            crossing = a[0] + (z-a[1])*(b[0]-a[0])/(b[1]-a[1])
            if x < crossing:
                inside = not inside
    return inside


def _safe_center(x: float, z: float, vertices: list[tuple[float,float]], radius: float) -> bool:
    if not _inside_polygon(x, z, vertices):
        return False
    return min(
        _distance_to_segment(x, z, a, b)
        for a, b in zip(vertices, vertices[1:]+vertices[:1])
    ) >= radius + 0.02


def _grid(low: float, high: float, maximum_step: float) -> list[float]:
    if high < low:
        return []
    count = max(1, ceil((high-low)/maximum_step))
    return [low+(high-low)*i/count for i in range(count+1)]


def build_l32_side_mill_draft(
    side_face: dict[str, object],
    *,
    stock_radius_mm: float,
    tool_diameter_mm: float = 0.5,
    axial_step_mm: float = 0.3,
    stepover_mm: float = 0.35,
    scan_step_mm: float = 0.025,
    clearance_mm: float = 0.5,
) -> SideMillDraft:
    """Raster a genuine CAD side face, conservatively inset for cutter radius.

    This only supplies candidate centers. An OCC tool sweep and holder/fixture
    check must pass before these centers can become an executable operation.
    """
    if min(stock_radius_mm, tool_diameter_mm, axial_step_mm, stepover_mm, scan_step_mm, clearance_mm) <= 0:
        raise ValueError("side-milling geometry and increments must be positive")
    normal_y = float(side_face["normal_y"])
    if abs(normal_y) < 0.999999 or int(side_face["wire_count"]) != 1:
        raise ValueError("side face must be a single radial ±Y planar outline")
    sign = 1 if normal_y > 0 else -1
    face_y = float(side_face["center_xyz_mm"][1])
    if face_y*sign <= 0 or stock_radius_mm <= abs(face_y):
        raise ValueError("stock must extend outside the radial side face")
    vertices = [tuple(map(float, point)) for point in side_face["points_xz_mm"]]
    if len(vertices) < 4:
        raise ValueError("side face has no usable outer wire")
    if hypot(vertices[0][0]-vertices[-1][0], vertices[0][1]-vertices[-1][1]) < 1e-7:
        vertices.pop()
    radius = tool_diameter_mm/2
    xs, zs = [p[0] for p in vertices], [p[1] for p in vertices]
    rows: list[tuple[float,float,float]] = []
    for z in _grid(min(zs)+radius+0.02, max(zs)-radius-0.02, stepover_mm):
        run_start = None
        previous_x = None
        for x in _grid(min(xs)+radius+0.02, max(xs)-radius-0.02, scan_step_mm):
            if _safe_center(x,z,vertices,radius):
                if run_start is None:
                    run_start = x
                previous_x = x
            elif run_start is not None:
                rows.append((z,run_start,previous_x))
                run_start = previous_x = None
        if run_start is not None:
            rows.append((z,run_start,previous_x))
    if not rows:
        raise ValueError("no cutter-center region remains after side-face inset")
    depth = stock_radius_mm-abs(face_y)
    layers = max(1,ceil(depth/axial_step_mm))
    safe_y = sign*(stock_radius_mm+clearance_mm)

    def point(x: float,y: float,z: float) -> Vec3:
        return Vec3(x=round(x,6),y=round(y,6),z=round(z,6))

    moves: list[SideMillMove] = []
    for layer in range(1,layers+1):
        y = sign*(stock_radius_mm-min(layer*axial_step_mm,depth))
        for z,x_start,x_end in rows:
            moves.extend([
                SideMillMove(kind="rapid",point=point(x_start,safe_y,z),layer=layer),
                SideMillMove(kind="feed",point=point(x_start,y,z),layer=layer),
                SideMillMove(kind="feed",point=point(x_end,y,z),layer=layer),
                SideMillMove(kind="feed",point=point(x_end,safe_y,z),layer=layer),
            ])
    return SideMillDraft(
        source_face_number=int(side_face["face_number"]),
        analysis_face_id=str(side_face["analysis_face_id"]),
        access_sign=sign,
        tool_diameter_mm=tool_diameter_mm,
        stock_radius_mm=stock_radius_mm,
        face_y_mm=face_y,
        layer_count=layers,
        raster_count_per_layer=len(rows),
        moves=moves,
    )


def build_l32_exterior_clear_draft(
    side_face: dict[str, object],
    *,
    stock_radius_mm: float,
    access_sign: Literal[-1, 1],
    tool_diameter_mm: float = 0.5,
    radial_step_mm: float = 0.4,
    stepover_mm: float = 0.35,
    scan_step_mm: float = 0.025,
    clearance_mm: float = 0.5,
    silhouette_clearance_mm: float = 0.18,
) -> SideMillDraft:
    """Clear round-bar stock only outside an exact rear X/Z silhouette.

    The cutter is inset from the front shoulder and offset outward from the
    target boundary. The default silhouette margin is only a starting point;
    the exact OCC sweep is mandatory because the side face is not the whole
    three-dimensional projection.
    """
    if silhouette_clearance_mm < 0 or access_sign not in (-1,1) or min(
        stock_radius_mm,tool_diameter_mm,radial_step_mm,stepover_mm,scan_step_mm,clearance_mm,
    ) <= 0:
        raise ValueError("invalid exterior clearing direction or cutting increments")
    if int(side_face["wire_count"]) != 1:
        raise ValueError("exterior clearing needs one exact outer face wire")
    vertices = [tuple(map(float,point)) for point in side_face["points_xz_mm"]]
    if hypot(vertices[0][0]-vertices[-1][0],vertices[0][1]-vertices[-1][1]) < 1e-7:
        vertices.pop()
    if len(vertices) < 3:
        raise ValueError("exterior clearing face has no usable polygon")
    radius = tool_diameter_mm/2
    x_min = min(point[0] for point in vertices)-radius-0.05
    x_max = max(point[0] for point in vertices)-radius-0.02
    rows: list[tuple[float,float,float]] = []
    edges = list(zip(vertices,vertices[1:]+vertices[:1]))
    for z in _grid(-stock_radius_mm-radius,stock_radius_mm+radius,stepover_mm):
        run_start = None
        previous_x = None
        for x in _grid(x_min,x_max,scan_step_mm):
            outside = not _inside_polygon(x,z,vertices)
            distant = min(_distance_to_segment(x,z,a,b) for a,b in edges) >= radius+silhouette_clearance_mm
            if outside and distant:
                if run_start is None:
                    run_start = x
                previous_x = x
            elif run_start is not None:
                rows.append((z,run_start,previous_x))
                run_start = previous_x = None
        if run_start is not None:
            rows.append((z,run_start,previous_x))
    if not rows:
        raise ValueError("no exterior cutter-center region remains")
    layers = max(1,ceil(stock_radius_mm/radial_step_mm))
    safe_y = access_sign*(stock_radius_mm+clearance_mm)
    moves: list[SideMillMove] = []
    for layer in range(1,layers+1):
        y = access_sign*(stock_radius_mm-min(layer*radial_step_mm,stock_radius_mm))
        for z,x_start,x_end in rows:
            def point(x_value: float,y_value: float) -> Vec3:
                return Vec3(x=round(x_value,6),y=round(y_value,6),z=round(z,6))
            moves.extend([
                SideMillMove(kind="rapid",point=point(x_start,safe_y),layer=layer),
                SideMillMove(kind="feed",point=point(x_start,y),layer=layer),
                SideMillMove(kind="feed",point=point(x_end,y),layer=layer),
                SideMillMove(kind="feed",point=point(x_end,safe_y),layer=layer),
            ])
    return SideMillDraft(
        source_face_number=int(side_face["face_number"]),
        analysis_face_id=str(side_face["analysis_face_id"]),
        mode="exterior_clear",
        access_sign=access_sign,
        silhouette_clearance_mm=silhouette_clearance_mm,
        tool_diameter_mm=tool_diameter_mm,
        stock_radius_mm=stock_radius_mm,
        face_y_mm=0,
        layer_count=layers,
        raster_count_per_layer=len(rows),
        moves=moves,
    )
