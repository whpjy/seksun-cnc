"""Cut a kerf from the inherited BRep; never rebuild drilled material."""
from math import isfinite
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent) if '__file__' in globals() else '/app/cam')
import Part
from side_hole_sweep import vector,volume


def cutoff(stock,target,axis,center_z,width,stock_radius):
    if not all(isfinite(x) for x in (center_z,width,stock_radius)) or min(width,stock_radius)<=0:
        raise ValueError('Invalid cutoff kerf dimensions')
    direction=vector(axis['direction'])
    if direction.Length<1e-9:raise ValueError('Invalid cutoff axis')
    direction.normalize()
    cutter=Part.makeCylinder(stock_radius,width,vector(axis['origin'])+direction*(center_z-width/2),direction)
    contact=volume(target.copy().common(cutter))
    if contact>.0001:raise ValueError('Cutoff kerf intersects the STEP target')
    result=stock.copy().cut(cutter)
    if result.isNull() or not result.isValid() or result.Volume>stock.Volume+.0001:
        raise ValueError('Invalid continuous cutoff material')
    return result,{'target_contact_mm3':contact,'removed_volume_mm3':stock.Volume-result.Volume,
        'remaining_stock_volume_mm3':result.Volume,'source':'inherited_brep','can_accept':False,'production_ready':False}


if __name__=='__main__':
    import os,json
    from pathlib import Path
    import MeshPart
    source,payload,output=json.loads(os.environ['CNC_CAM_ARGUMENTS_JSON']);p=json.loads(payload)
    target=Part.read(source)
    if not target.isValid() or len(target.Solids)!=1:raise ValueError('Cutoff requires one valid target solid')
    stock=Part.read(p['input_stock_brep'])
    if stock.isNull() or not stock.isValid() or not stock.Solids:raise ValueError('Inherited cutoff stock is invalid')
    result,report=cutoff(stock,target,p['axis'],p['z_mm'],p['cutting_width_mm'],p['stock_radius_mm'])
    report.update(initial_stock_volume_mm3=stock.Volume,target_protection_status='passed',remaining_stock_valid=True,
        material_removal_status='passed' if report['removed_volume_mm3']>.0001 else 'failed')
    root=Path(output);root.mkdir(parents=True,exist_ok=True)
    result.exportBrep(str(root/'remaining-stock.brep'))
    MeshPart.meshFromShape(Shape=result,LinearDeflection=.06,AngularDeflection=.35,Relative=False).write(str(root/'remaining-stock.stl'))
    (root/'result.json').write_text(json.dumps(report),encoding='utf-8')
    print('CNC_CONTINUOUS_CUTOFF '+json.dumps(report),flush=True)
