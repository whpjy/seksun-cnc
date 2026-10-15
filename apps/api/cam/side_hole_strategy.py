"""Independent alternatives plus a BRep-linked drill/flat-finish comparison."""
import sys,json,os
from pathlib import Path
sys.path.insert(0,'/app/cam')
from side_hole_sweep import check


def compare(source,payload,output):
    output=Path(output)
    common={k:v for k,v in payload.items() if k!='drafts'}
    rows=[];backed=None
    for name,draft in [('direct',payload['drafts']['direct']),('backoff',payload['drafts']['backoff']),('flat_finish',payload['drafts']['flat_finish'])]:
        initial={**common,'draft':draft}
        if name=='flat_finish':initial['input_stock_brep']=str(output/'backoff'/'remaining-stock.brep')
        try:
            if name=='flat_finish' and backed is None:raise ValueError('前序回退钻削没有有效实体，不能计算连续平底修整')
            native=check(source,initial,output/name)
        except (ValueError,RuntimeError) as error:
            rows.append({'id':name,'name':{'direct':'直接钻削','backoff':'回退钻深','flat_finish':'回退钻深后平底精加工'}[name],
                'target_contact_mm3':None,'remaining_hole_stock_mm3':None,'removed_volume_mm3':None,
                'geometry_status':'unavailable','detail':str(error),'can_accept':False})
            continue
        if name=='backoff':backed=native
        contact=native['target_contact_mm3']+(backed['target_contact_mm3'] if name=='flat_finish' else 0)
        residual=native['remaining_cylindrical_segments_mm3']
        rows.append({'id':name,'name':{'direct':'直接钻削','backoff':'回退钻深','flat_finish':'回退钻深后平底精加工'}[name],
            'target_contact_mm3':contact,'remaining_hole_stock_mm3':residual,
            'removed_volume_mm3':native['removed_volume_mm3']+(backed['removed_volume_mm3'] if name=='flat_finish' else 0),
            'geometry_status':'failed' if contact>.0001 or residual>.0001 else 'passed',
            'continued_from_strategy':'backoff' if name=='flat_finish' else None,
            'native':native,'can_accept':False})
    result={'strategies':rows,'scope':'nominal_cylindrical_segment_strategy_comparison','can_accept':False,'production_ready':False}
    if payload.get('cutoff'):
        result['cutoff_continuation']={'status':'not_run','detail':'前序平底修整未产生通过几何检查的连续实体，切断未计算','can_accept':False}
    if payload.get('cutoff') and rows[-1].get('geometry_status')=='passed':
        import Part,MeshPart
        from continuous_cutoff import cutoff
        kerf=payload['cutoff']
        stock=Part.read(str(output/'flat_finish'/'remaining-stock.brep'))
        target=Part.read(source).Solids[0]
        try:
            cut,evidence=cutoff(stock,target,payload['axis'],kerf['z_mm'],kerf['cutting_width_mm'],payload['stock_diameter_mm']/2)
            dest=output/'cutoff';dest.mkdir()
            cut.exportBrep(str(dest/'remaining-stock.brep'))
            MeshPart.meshFromShape(Shape=cut,LinearDeflection=.06,AngularDeflection=.35,Relative=False).write(str(dest/'remaining-stock.stl'))
            result['cutoff_continuation']={**evidence,'status':'passed','continued_from_strategy':'flat_finish'}
        except ValueError as error:
            result['cutoff_continuation']={'status':'failed','detail':str(error),'can_accept':False}
    return result


if __name__=='__main__':
    source,payload,output=json.loads(os.environ['CNC_CAM_ARGUMENTS_JSON'])
    print('CNC_SIDE_HOLE_STRATEGY '+json.dumps(compare(source,json.loads(payload),output)),flush=True)
