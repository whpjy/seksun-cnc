import json
import subprocess
import pytest
from app.engines import resolve_executable,run_freecad_adapter


def test_straight_outer_wall_is_present_at_inner_cone_vertices(tmp_path):
    analyzer=resolve_executable("occt-analyzer") or resolve_executable("/opt/seksun-cnc/bin/occt-analyzer")
    if not analyzer or not resolve_executable("FreeCADCmd"):pytest.skip("requires native OCCT and FreeCAD")
    source=tmp_path/"bored-cylinder.step"
    script=tmp_path/"shape.py"
    script.write_text('''
import FreeCAD as App
import Part
outer=Part.makeCylinder(10,20)
bore=Part.makeCylinder(4,5).fuse(Part.makeCone(4,8,10,App.Vector(0,0,5)))
bore=bore.fuse(Part.makeCylinder(8,5,App.Vector(0,0,15)))
outer.cut(bore).exportStep(SOURCE)
'''.replace("SOURCE",repr(str(source))),encoding="utf-8")
    run_freecad_adapter("FreeCADCmd",script,[],timeout_seconds=90)
    analysis=tmp_path/"analysis.json"
    subprocess.run([analyzer,str(source),str(analysis),str(tmp_path/"model.stl")],check=True,capture_output=True)
    section=json.loads(analysis.read_text())["rotational_sections"][0]
    assert len(section["outer_profile"])>=2
    assert all(p["radius"]==pytest.approx(10,abs=.01) for p in section["outer_profile"])
    assert any(p["radius"]==pytest.approx(4,abs=.01) for p in section["inner_profile"])
    assert any(p["radius"]==pytest.approx(8,abs=.01) for p in section["inner_profile"])


@pytest.mark.parametrize("first_radius,second_radius", [(8, 4), (4, 8)])
def test_square_bore_shoulder_preserves_both_radii_at_same_station(tmp_path, first_radius, second_radius):
    analyzer=resolve_executable("occt-analyzer") or resolve_executable("/opt/seksun-cnc/bin/occt-analyzer")
    if not analyzer or not resolve_executable("FreeCADCmd"):pytest.skip("requires native OCCT and FreeCAD")
    source=tmp_path/"stepped-bore.step"
    script=tmp_path/"shape.py"
    script.write_text(f'''
import FreeCAD as App
import Part
outer=Part.makeCylinder(10,20)
bore=Part.makeCylinder({first_radius},7).fuse(Part.makeCylinder({second_radius},13,App.Vector(0,0,7)))
outer.cut(bore).exportStep({str(source)!r})
''',encoding="utf-8")
    run_freecad_adapter("FreeCADCmd",script,[],timeout_seconds=90)
    analysis=tmp_path/"analysis.json"
    subprocess.run([analyzer,str(source),str(analysis),str(tmp_path/"model.stl")],check=True,capture_output=True)
    section=json.loads(analysis.read_text())["rotational_sections"][0]
    origin=section["axis_origin"]["z"]
    points=section["inner_profile"]
    shoulder=[p for p in points if abs(p["z"]+origin-7)<.01]
    assert [p["radius"] for p in shoulder]==pytest.approx([first_radius,second_radius],abs=.01)
    # Every nonvertical segment must remain cylindrical, never a fabricated cone.
    assert all(abs(a["radius"]-b["radius"])<.01 for a,b in zip(points,points[1:]) if abs(a["z"]-b["z"])>.01)
