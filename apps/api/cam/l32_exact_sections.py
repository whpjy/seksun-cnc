"""Inspect exact STEP sections normal to the L32 spindle axis.

Run through the existing FreeCAD engine with CNC_CAM_ARGUMENTS_JSON set to
``[source_step, x0, x1, ...]``. This is an extraction tool, not CAM output.
"""

from __future__ import annotations

import json
import os

import FreeCAD as App
import Part


def section_at_x(solid, x):
    plane = Part.makePlane(100, 100, App.Vector(x, 0, 0), App.Vector(1, 0, 0))
    center = plane.CenterOfMass
    plane.translate(App.Vector(0, -center.y, -center.z))
    section = solid.section(plane)
    loops = []
    for edges in Part.sortEdges(section.Edges):
        wire = Part.Wire(edges)
        if not wire.isClosed():
            continue
        points = wire.discretize(Deflection=0.01)
        if len(points) < 3:
            continue
        yz = [[round(point.y, 6), round(point.z, 6)] for point in points]
        bounds = wire.BoundBox
        try:
            area = Part.Face(wire).Area
        except Exception:
            area = 0.0
        loops.append({
            "area_mm2": round(area, 6),
            "bounds_yz_mm": [
                round(bounds.YMin, 6), round(bounds.YMax, 6),
                round(bounds.ZMin, 6), round(bounds.ZMax, 6),
            ],
            "points_yz_mm": yz,
        })
    return {"x_mm": x, "loops": loops, "edge_count": len(section.Edges)}


def broad_side_faces(solid):
    """Return exact planar X/Z outlines visible from the radial ±Y directions."""
    faces = []
    for index, face in enumerate(solid.Faces, 1):
        if type(face.Surface).__name__ != "Plane":
            continue
        u0, u1, v0, v1 = face.ParameterRange
        normal = face.normalAt((u0+u1)/2, (v0+v1)/2)
        if abs(normal.y) < 0.999 or face.BoundBox.XLength < 1 or face.Area < 1:
            continue
        points = face.OuterWire.discretize(Deflection=0.01)
        faces.append({
            "face_number": index,
            "area_mm2": round(face.Area, 6),
            "center_xyz_mm": [round(face.CenterOfMass.x, 6), round(face.CenterOfMass.y, 6), round(face.CenterOfMass.z, 6)],
            "normal_y": round(normal.y, 6),
            "bounds_xz_mm": [round(face.BoundBox.XMin, 6), round(face.BoundBox.XMax, 6), round(face.BoundBox.ZMin, 6), round(face.BoundBox.ZMax, 6)],
            "wire_count": len(face.Wires),
            "points_xz_mm": [[round(p.x, 6), round(p.z, 6)] for p in points],
        })
    return faces


def main():
    args = json.loads(os.environ["CNC_CAM_ARGUMENTS_JSON"])
    if not args:
        raise ValueError("expected source STEP")
    shape = Part.read(str(args[0]))
    solids = list(shape.Solids)
    if len(solids) != 1:
        raise ValueError(f"expected one target solid, got {len(solids)}")
    solid = solids[0]
    sections = [section_at_x(solid, float(value)) for value in args[1:]]
    print("CNC_EXACT_SECTIONS " + json.dumps({
        "source": str(args[0]),
        "solid_valid": solid.isValid(),
        "solid_volume_mm3": round(solid.Volume, 6),
        "sections": sections,
        "broad_side_faces": broad_side_faces(solid),
    }, separators=(",", ":")), flush=True)


main()
