"""Feed sweeps and conservative retract/move boxes against bound fixture solids."""
import math
import Part,FreeCAD as App
from side_hole_sweep import volume


def mapped(shape,mapping,pose):
    matrix=App.Matrix();r=mapping['rotation'];t=mapping['translation']
    for i in range(3):
        for j in range(3):setattr(matrix,f'A{i+1}{j+1}',r[i][j])
        setattr(matrix,f'A{i+1}4',t[i])
    result=shape.copy();result.transformShape(matrix)
    result.rotate(App.Vector(*mapping['axis_origin']),App.Vector(0,0,1),pose['index_rotation_deg'])
    return result


def fixture_contacts(setup,hole,cutter,body,geometry):
    fixture=Part.read(setup['fixture_file'])
    if fixture.isNull() or not fixture.isValid() or not fixture.Solids:raise ValueError('Fixture must contain valid closed solids')
    origin=setup['mapping']['axis_origin']
    if setup['fixture_attachment']=='main_spindle':
        # Enclose every possible spindle rotation, avoiding untested angles
        # between indexed stations. This may conservatively reject a clearance.
        box=fixture.BoundBox
        radius=max(math.hypot(x-origin[0],y-origin[1]) for x in (box.XMin,box.XMax) for y in (box.YMin,box.YMax))
        fixture=Part.makeCylinder(radius,box.ZMax-box.ZMin,App.Vector(origin[0],origin[1],box.ZMin),App.Vector(0,0,1))
    pose=next(p for p in setup['poses'] if p['feature_id']==hole['feature_id'])
    feed=mapped(cutter,setup['mapping'],pose)
    if body is None:return {'status':'not_run','feed_contact_mm3':volume(fixture.common(feed))}
    feed=feed.fuse(mapped(body,setup['mapping'],pose))
    contact=volume(fixture.common(feed))
    # All rapid poses are outside the bar. A box enclosing all configured
    # retract Y/Z stations protects every index transition and lateral move.
    points=[p['retract_machine_xyz'] for p in setup['poses']]
    radius=max(hole['tool_diameter_mm'],geometry['shank_diameter_mm'],geometry['holder_diameter_mm'])/2
    length=geometry['stickout_from_tip_mm']+geometry['holder_length_mm']
    xmin=min(p[0] for p in points);xmax=max(p[0] for p in points)+length
    ymin=min(p[1] for p in points)-radius;ymax=max(p[1] for p in points)+radius
    zmin=min(p[2] for p in points)-radius;zmax=max(p[2] for p in points)+radius
    rapid=Part.makeBox(xmax-xmin,ymax-ymin,zmax-zmin,App.Vector(xmin,ymin,zmin))
    # Include radial retract between feed entry and the common retract X.
    entry=pose['entry_machine_xyz'];retract=pose['retract_machine_xyz']
    retreat=Part.makeBox(abs(retract[0]-entry[0])+length,2*radius,2*radius,
        App.Vector(min(entry[0],retract[0]),entry[1]-radius,entry[2]-radius))
    rapid_contact=volume(fixture.common(rapid.fuse(retreat)))
    return {'status':'failed' if max(contact,rapid_contact)>.0001 else 'passed',
        'feed_contact_mm3':contact,'rapid_contact_mm3':rapid_contact,
        'scope':'exact_feed_and_conservative_rapid_rotating_fixture_envelope'}
