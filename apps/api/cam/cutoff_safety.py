"""Conservative rotating-stock/tool envelopes and retained-component geometry."""
import math
import FreeCAD as App
import Part
from side_hole_sweep import volume

TOL=.0001


def mapped(shape,mapping):
    matrix=App.Matrix()
    for i in range(3):
        for j in range(3):setattr(matrix,f'A{i+1}{j+1}',mapping['rotation'][i][j])
        setattr(matrix,f'A{i+1}4',mapping['translation'][i])
    result=shape.copy();result.transformShape(matrix);return result


def rotating_envelope(shape,origin):
    box=shape.BoundBox
    radius=max(math.hypot(x-origin[0],y-origin[1]) for x in (box.XMin,box.XMax) for y in (box.YMin,box.YMax))
    return Part.makeCylinder(radius,box.ZMax-box.ZMin,App.Vector(origin[0],origin[1],box.ZMin),App.Vector(0,0,1))


def check_tool(stock,payload):
    setup=payload['cutoff_safety'];mapping=setup['trajectory']['mapping'];g=setup['tool_geometry']
    points=setup['trajectory']['tip_points_machine_xyz'];end,start=points[-1],points[0]
    feed=start[0]-end[0]
    if feed<=0:raise ValueError('Cutoff entry must be outside its radial endpoint')
    if not all(math.isfinite(v) and v>0 for v in g.values()):raise ValueError('Invalid measured cutoff body')
    if g['cutting_reach_mm']>g['stickout_mm']:raise ValueError('Cutoff reach exceeds stickout')
    bodies=[]
    if g['stickout_mm']>g['cutting_reach_mm']:
        bodies.append(Part.makeBox(g['stickout_mm']-g['cutting_reach_mm']+feed,g['blade_height_mm'],g['cutting_width_mm'],
            App.Vector(end[0]+g['cutting_reach_mm'],end[1]-g['blade_height_mm']/2,end[2]-g['cutting_width_mm']/2)))
    bodies.append(Part.makeBox(g['holder_length_mm']+feed,g['holder_height_mm'],g['holder_width_mm'],
        App.Vector(end[0]+g['stickout_mm'],end[1]-g['holder_height_mm']/2,end[2]-g['holder_width_mm']/2)))
    body=bodies[0] if len(bodies)==1 else bodies[0].fuse(bodies[1])
    # The work rotates through all angles. Bounding cylinders are conservative;
    # never use one indexed hole orientation to approve rotating clearance.
    stock_envelope=rotating_envelope(mapped(stock,mapping),mapping['axis_origin'])
    stock_contact=volume(stock_envelope.common(body))
    if not setup.get('fixture_file'):
        return {'status':'failed' if stock_contact>TOL else 'not_run','non_cutting_stock_contact_mm3':stock_contact,
            'fixture_contact_mm3':None,'scope':'conservative_rotating_stock_only_fixture_unavailable',
            'excluded_motions':['tool_station_change','sub_spindle_approach']}
    fixture=Part.read(setup['fixture_file'])
    if fixture.isNull() or not fixture.isValid() or not fixture.Solids:raise ValueError('Fixture must contain valid closed solids')
    if setup['fixture_attachment']=='main_spindle':fixture=rotating_envelope(fixture,mapping['axis_origin'])
    # Includes outward radial retreat along the same feed path. Station changes
    # and sub-spindle approach are deliberately separate, unverified motions.
    cutting=Part.makeBox(feed+max(.001,g['cutting_reach_mm']),g['blade_height_mm'],g['cutting_width_mm'],
        App.Vector(end[0],end[1]-g['blade_height_mm']/2,end[2]-g['cutting_width_mm']/2))
    fixture_contact=volume(fixture.common(body.fuse(cutting)))
    return {'status':'failed' if max(stock_contact,fixture_contact)>TOL else 'passed',
        'non_cutting_stock_contact_mm3':stock_contact,'fixture_contact_mm3':fixture_contact,
        'scope':'conservative_rotating_stock_and_axial_fixture_radial_feed_retract',
        'excluded_motions':['tool_station_change','sub_spindle_approach']}


def clusters(shape):
    solids=list(shape.Solids)
    if len(solids)>512:raise ValueError('Too many stock fragments for retained-component verification')
    parents=list(range(len(solids)))
    def root(i):
        while parents[i]!=i:i=parents[i]
        return i
    def boxes_overlap(x,y):
        return not any(getattr(x,f'{k}Max')<getattr(y,f'{k}Min')-1e-7 or getattr(y,f'{k}Max')<getattr(x,f'{k}Min')-1e-7 for k in 'XYZ')
    def shared_face(a,b):
        # OCC solid/solid Common may discard a zero-volume shared face. Check
        # the faces themselves; distance zero alone would join point/edge touches.
        for f in a.Faces:
            for g in b.Faces:
                if boxes_overlap(f.BoundBox,g.BoundBox):
                    contact=f.common(g)
                    if not contact.isNull() and contact.isValid() and contact.Area>1e-5:return True
        return False
    for i,a in enumerate(solids):
        for j in range(i+1,len(solids)):
            if root(i)==root(j):continue
            x,y=a.BoundBox,solids[j].BoundBox
            if not boxes_overlap(x,y):continue
            common=a.common(solids[j])
            # A point/edge touch is insufficient for a load-bearing component.
            if (not common.isNull() and (volume(common)>TOL or common.Area>1e-5)) or shared_face(a,solids[j]):parents[root(j)]=root(i)
    groups={}
    for i,s in enumerate(solids):groups.setdefault(root(i),[]).append(s)
    return [Part.makeCompound(parts) for parts in groups.values()]


def check_retention(result,target,payload):
    safety=payload['cutoff_safety'];cfg=safety['configuration'];mapping=safety['trajectory']['mapping']
    groups=clusters(result);target_contacts=[volume(g.common(target)) for g in groups]
    selected=[i for i,v in enumerate(target_contacts) if v>TOL]
    issues=[]
    if len(selected)!=1:issues.append('成品几何分布在多个分离材料块中，无法认定由同一夹持保持')
    row={'component_count':len(groups),'target_component_count':len(selected),'issues':issues,
        'geometry_status':'failed','force_confirmation_status':'review','transfer_motion_status':'not_run',
        'scope':'connected_material_and_declared_annular_grip_not_dynamic_transfer',
        'state_transitions':[]}
    if len(selected)!=1:return row
    part=mapped(groups[selected[0]],mapping);origin=mapping['axis_origin'];z=payload['z_mm'];width=payload['cutting_width_mm']
    low,high=cfg['grip_z_min_mm'],cfg['grip_z_max_mm']
    if high-low<cfg['minimum_grip_length_mm']:issues.append('声明夹持长度小于要求的最小夹持长度')
    if cfg['retained_side']=='+Z':
        if low<z+width/2-1e-7:issues.append('夹持区未完全位于切断后的成品侧')
        bar_side=any(mapped(g,mapping).BoundBox.ZMax<origin[2]+z-width/2+1e-6 for i,g in enumerate(groups) if i!=selected[0])
    else:
        if high>z-width/2+1e-7:issues.append('夹持区未完全位于切断后的成品侧')
        bar_side=any(mapped(g,mapping).BoundBox.ZMin>origin[2]+z+width/2-1e-6 for i,g in enumerate(groups) if i!=selected[0])
    if not bar_side:issues.append('材料范围内没有可辨识的棒料侧，无法证明主轴到背轴的完整分离')
    r=cfg['grip_diameter_mm']/2;thickness=min(.05,r/10)
    pos=App.Vector(origin[0],origin[1],origin[2]+low)
    band=Part.makeCylinder(r,high-low,pos).cut(Part.makeCylinder(r-thickness,high-low,pos))
    fraction=volume(part.common(band))/volume(band)
    if fraction<.95:issues.append('成品侧实际材料未覆盖声明夹持圆柱面，需核对夹持直径和范围')
    row.update(geometry_status='failed' if issues else 'passed',grip_material_fraction=fraction,bar_side_present=bar_side,
        target_component_index=selected[0],target_component_volume_mm3=groups[selected[0]].Volume)
    confirmed=cfg['clamp_confirmed'] and cfg['clamp_force_n']>=cfg['required_clamp_force_n']
    row['force_confirmation_status']='failed' if cfg['clamp_force_n']<cfg['required_clamp_force_n'] else 'passed' if confirmed else 'review'
    if not confirmed:issues.append('夹紧力及其要求尚未确认，或声明夹紧力低于要求')
    if row['geometry_status']=='passed' and confirmed:
        row['state_transitions']=[{'sequence':i+1,'state':state,'status':'proposed'} for i,state in enumerate(
            ('main_spindle_held','dual_spindle_clamped','phase_synchronized','part_separated','sub_spindle_held'))]
    row['issues'].append('背轴接近、夹紧及转接运动尚未完成实体碰撞验证；静态夹持检查不能接受完整转接')
    return row
