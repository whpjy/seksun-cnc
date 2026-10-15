"""Conservative straight axial motion of a confirmed cylindrical jaw envelope."""
import Part,FreeCAD as App
from side_hole_sweep import volume
from cutoff_safety import mapped,clusters,rotating_envelope
TOL=.0001

def tube(inner,outer,low,high,origin):
    base=App.Vector(origin[0],origin[1],origin[2]+low)
    return Part.makeCylinder(outer,high-low,base).cut(Part.makeCylinder(inner,high-low,base))

def check_transfer(stock,result,target,payload):
    safety=payload['cutoff_safety'];cfg=safety['configuration'];m=cfg.get('sub_spindle_motion')
    if not m:return {'status':'not_run','detail':'尚未配置背轴移动包络与X2/Z2坐标行程'}
    if not m['envelope_confirmed']:return {'status':'review','detail':'背轴圆柱包络尚未确认能覆盖实际移动部件'}
    issues=[];mapping=safety['trajectory']['mapping'];origin=mapping['axis_origin'];sign=1 if cfg['retained_side']=='+Z' else -1
    low,high=cfg['grip_z_min_mm'],cfg['grip_z_max_mm'];length=high-low
    if m['body_length_mm']<length:issues.append('背轴包络长度不足以覆盖夹持区')
    if m['open_bore_diameter_mm']<=cfg['grip_diameter_mm']:issues.append('张开内径必须大于夹持直径')
    positions=[(low+high)/2,(low+high)/2+sign*m['approach_distance_mm'],(low+high)/2+sign*m['withdrawal_distance_mm']]
    for pos in positions:
        command=m['z2_origin_mm']+m['z2_sign']*pos
        if not m['z2_limits'][0]<=command<=m['z2_limits'][1]:issues.append('背轴Z2运动超出声明行程')
    if not m['x2_limits'][0]<=m['x2_position_mm']<=m['x2_limits'][1]:issues.append('背轴X2位置超出声明行程')
    # X2 is a declared controller coordinate at coaxial alignment, not inferred
    # from the source model. Its machine datum must be part of the confirmation.
    part_groups=clusters(result);bearing=[g for g in part_groups if volume(g.common(target))>TOL]
    if len(bearing)!=1:return {'status':'failed','detail':'成品侧连通材料不唯一，不能计算背轴取件运动'}
    material=mapped(stock,mapping);part=mapped(bearing[0],mapping)
    fixture=Part.read(safety['fixture_file']) if safety.get('fixture_file') else None
    if fixture is None:return {'status':'not_run','detail':'缺少固定夹具实体，背轴运动不能通过验证'}
    if not fixture.isValid() or not fixture.Solids:raise ValueError('Invalid stationary fixture')
    if safety['fixture_attachment']=='main_spindle':fixture=rotating_envelope(fixture,origin)
    inner=m['open_bore_diameter_mm']/2;outer=m['envelope_outer_diameter_mm']/2
    body_low=low if sign==1 else high-m['body_length_mm'];body_high=body_low+m['body_length_mm']
    approach=tube(inner,outer,min(body_low,body_low+sign*m['approach_distance_mm']),max(body_high,body_high+sign*m['approach_distance_mm']),origin)
    jaw=tube(cfg['grip_diameter_mm']/2,inner,low,high,origin)
    final_body=tube(inner,outer,body_low,body_high,origin).fuse(jaw)
    # Use the bound round-bar radius as a proven upper bound throughout rotation.
    box=material.BoundBox
    rotating_stock=Part.makeCylinder(payload['stock_radius_mm'],box.ZLength,App.Vector(origin[0],origin[1],box.ZMin))
    approach_stock=volume(approach.common(rotating_stock))
    approach_fixture=volume(approach.common(fixture))
    clamp_stock=volume(jaw.common(material))
    clamp_fixture=volume(final_body.common(fixture))
    holding=safety.get('main_holding') or {}
    free_rotation=rotating_stock
    if holding.get('clamp_confirmed'):
        hlow,hhigh=holding['grip_z_min_mm'],holding['grip_z_max_mm']
        held=Part.makeCylinder(payload['stock_radius_mm'],hhigh-hlow,App.Vector(origin[0],origin[1],origin[2]+hlow))
        free_rotation=free_rotation.cut(held)
    phase_fixture=volume(free_rotation.common(fixture))
    distance=sign*m['withdrawal_distance_mm']
    withdraw_body=tube(inner,outer,min(body_low,body_low+distance),max(body_high,body_high+distance),origin).fuse(
        tube(cfg['grip_diameter_mm']/2,inner,min(low,low+distance),max(high,high+distance),origin))
    p=part.BoundBox
    withdraw_part=Part.makeBox(p.XLength,p.YLength,p.ZLength+abs(distance),App.Vector(p.XMin,p.YMin,min(p.ZMin,p.ZMin+distance)))
    remaining=[g for g in part_groups if g not in bearing]
    # The main bar stays in place. The product is excluded from stationary stock.
    bar=mapped(Part.makeCompound(remaining),mapping) if remaining else None
    withdrawal_stock=volume(withdraw_body.fuse(withdraw_part).common(bar)) if bar else 0
    withdrawal_fixture=volume(withdraw_body.fuse(withdraw_part).common(fixture))
    # Cutoff blade is at its configured radial retract position during take-away.
    tip=safety['trajectory']['tip_points_machine_xyz'][0];tool=safety['tool_geometry']
    parked=Part.makeBox(tool['stickout_mm']+tool['holder_length_mm'],max(tool['blade_height_mm'],tool['holder_height_mm']),tool['holder_width_mm'],
        App.Vector(tip[0],tip[1]-max(tool['blade_height_mm'],tool['holder_height_mm'])/2,tip[2]-tool['holder_width_mm']/2))
    withdrawal_tool=volume(withdraw_body.fuse(withdraw_part).common(parked))
    stages=[{'state':'approach','stock_contact_mm3':approach_stock,'fixture_contact_mm3':approach_fixture},
        {'state':'clamp','stock_contact_mm3':clamp_stock,'fixture_contact_mm3':clamp_fixture},
        {'state':'phase_synchronized','fixture_contact_mm3':phase_fixture},
        {'state':'withdrawal','stock_contact_mm3':withdrawal_stock,'fixture_contact_mm3':withdrawal_fixture,'tool_contact_mm3':withdrawal_tool}]
    if any(v>TOL for s in stages for k,v in s.items() if k.endswith('_mm3')):issues.append('背轴接近、夹紧或取件包络与材料/夹具/退刀后的刀具发生接触')
    return {'status':'failed' if issues else 'passed','issues':list(dict.fromkeys(issues)),'stages':stages,
        'scope':'confirmed_conservative_coaxial_cylindrical_envelope_and_declared_X2_Z2_not_controller_execution'}
