"""OCC side-hole subtraction from initial stock or an accepted turning envelope.

The cutter is a cylindrical reference sweep, not a validated physical drill.
Only a new sandbox stock is written; target and process acceptance are untouched.
"""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, '/app/cam')
import FreeCAD as App
import Part
import MeshPart


def difference_volume(target, stock):
    # Keep unavailable raw Boolean diagnostics unknown, never interpret as zero.
    from l32_target_protection import missing_volume
    try:
        return missing_volume(target,stock)
    except ValueError:
        return None


def volume(shape):
    if shape.isNull():
        return 0.
    if not shape.isValid():
        raise ValueError('invalid Boolean volume')
    return shape.Volume


def vector(value):
    return App.Vector(*[float(value[k]) for k in "xyz"])


def build_stock(payload):
    axis = vector(payload["axis"]["direction"])
    axis.normalize()
    origin = vector(payload["axis"]["origin"])
    samples = payload.get("samples")
    if not samples:
        low, high = payload["z_range_mm"]
        return Part.makeCylinder(payload["stock_diameter_mm"]/2, high-low, origin+axis*low, axis)
    # Keep each sampled interval's larger outer and smaller inner radius. This
    # preserves possible residual stock instead of inventing finished geometry.
    intervals = []
    for a, b in zip(samples, samples[1:]):
        height = b["z"]-a["z"]
        outer = max(a["outer_radius"], b["outer_radius"])
        inner = min(a["inner_radius"], b["inner_radius"])
        if height <= 0 or inner < 0 or outer < inner:
            raise ValueError("invalid accepted material envelope")
        if outer-inner < 1e-8:
            continue
        if intervals and intervals[-1][2:] == (outer, inner) and intervals[-1][1] == a["z"]:
            previous = intervals[-1]
            intervals[-1] = (previous[0], b["z"], outer, inner)
        else:
            intervals.append((a["z"], b["z"], outer, inner))
    # Identical adjacent annuli form the same solid when merged. Reducing
    # hundreds of coincident faces keeps exact Boolean protection tractable.
    pieces = []
    for low, high, outer, inner in intervals:
        height = high-low
        start = origin+axis*low
        region = Part.makeCylinder(outer,height,start,axis)
        if inner > 1e-8:
            region = region.cut(Part.makeCylinder(inner,height,start,axis))
        pieces.append(region)
    if not pieces:
        raise ValueError("accepted envelope has no material")
    return Part.makeCompound(pieces)


def check(source_path, payload, output):
    solids = list(Part.read(source_path).Solids)
    if len(solids) != 1:
        raise ValueError("exact side-hole check requires one selected STEP solid")
    target = solids[0]
    stock = Part.read(payload['input_stock_brep']) if payload.get('input_stock_brep') else build_stock(payload)
    if not target.isValid() or not stock.isValid():
        raise ValueError("invalid target or initial material solid")
    initial_volume = stock.Volume
    initial_missing = difference_volume(target,stock)
    stages = []
    union = None
    regions = []
    noncut_target_contact = 0.
    noncut_stock_contact = 0.
    geometry = payload["draft"].get("drill_geometry") or {}
    for hole in payload["draft"]["holes"]:
        entry, end = vector(hole["entry"]), vector(hole["end"])
        direction = entry-end
        length = direction.Length
        if length <= 0:
            raise ValueError("empty drilling sweep")
        direction.normalize()
        cutter = Part.makeCylinder(hole["tool_diameter_mm"]/2,length,end,direction)
        tip_contact = 0.
        if "tip_end" in hole and hole.get('tip_length_mm',0)>1e-9:
            tip = Part.makeCone(0,hole["tool_diameter_mm"]/2,hole["tip_length_mm"],vector(hole["tip_end"]),direction)
            tip_contact = volume(target.copy().common(tip))
            cutter = cutter.fuse(tip)
        before = stock.Volume
        # Target contact is independent of whether a previous operation already
        # overcut the same region. Stock-only contact would hide repeated damage.
        contact = volume(target.copy().common(cutter))
        stock = stock.cut(cutter)
        if not stock.isValid() or stock.Volume > before+1e-5:
            raise ValueError("invalid material subtraction")
        body_target_contact = body_stock_contact = 0.
        body=None
        if geometry.get("holder_length_mm") is not None:
            tip_end = vector(hole["tip_end"])
            flute_length = geometry["flute_length_mm"]
            stickout = geometry["stickout_from_tip_mm"]
            bodies = []
            # Non-cutting volumes occupy the entire axial feed motion, but are
            # never subtracted from stock. Radial/rotary repositioning is absent.
            if stickout > flute_length:
                bodies.append(Part.makeCylinder(geometry["shank_diameter_mm"]/2,stickout-flute_length+length,
                    tip_end+direction*flute_length,direction))
            bodies.append(Part.makeCylinder(geometry["holder_diameter_mm"]/2,geometry["holder_length_mm"]+length,
                tip_end+direction*stickout,direction))
            body = bodies[0] if len(bodies)==1 else bodies[0].fuse(bodies[1])
            body_target_contact = volume(target.copy().common(body))
            body_stock_contact = volume(stock.common(body))
            noncut_target_contact += body_target_contact
            noncut_stock_contact += body_stock_contact
        stages.append({"feature_id":hole["feature_id"],"removed_volume_mm3":round(before-stock.Volume,6),
                       "target_contact_mm3":round(contact,6),"tip_target_contact_mm3":round(tip_contact,6),
                       "non_cutting_target_contact_mm3":round(body_target_contact,6),
                       "non_cutting_stock_contact_mm3":round(body_stock_contact,6),
                       "remaining_stock_volume_mm3":round(stock.Volume,6)})
        if payload.get('radial_setup'):
            from radial_fixture import fixture_contacts
            stages[-1]['fixture_clearance']=fixture_contacts(payload['radial_setup'],hole,cutter,body,geometry)
        if "cylindrical_segment_length_mm" in hole:
            nominal_end=end+direction*hole.get('full_diameter_extension_mm',geometry.get("full_diameter_extension_mm",0))
            region=Part.makeCylinder(hole["diameter_mm"]/2,hole["cylindrical_segment_length_mm"],nominal_end,direction)
            stages[-1]["target_inside_cylindrical_segment_mm3"]=round(volume(target.copy().common(region)),6)
            regions.append((len(stages)-1,region))
        union = cutter if union is None else union.fuse(cutter)
    contact = volume(target.copy().common(union))
    missing = difference_volume(target,stock)
    output = Path(output)
    output.mkdir(parents=True,exist_ok=True)
    stock.exportBrep(str(output/"remaining-stock.brep"))
    mesh=MeshPart.meshFromShape(Shape=stock,LinearDeflection=.06,AngularDeflection=.35,Relative=False)
    mesh.write(str(output/"remaining-stock.stl"))
    result={"executor":"exact_occ_side_hole_reference_sweep","initial_stock_volume_mm3":round(initial_volume,6),
            "remaining_stock_volume_mm3":round(stock.Volume,6),"removed_volume_mm3":round(initial_volume-stock.Volume,6),
            "target_contact_mm3":round(contact,6),"initial_missing_target_mm3":round(initial_missing,6) if initial_missing is not None else None,
            "final_missing_target_mm3":round(missing,6) if missing is not None else None,"new_missing_target_mm3":round(max(0.,missing-initial_missing),6) if missing is not None and initial_missing is not None else None,
            "remaining_excess_stock_mm3":difference_volume(stock,target),"remaining_stock_valid":True,"stages":stages,
            "non_cutting_target_contact_mm3":round(noncut_target_contact,6),
            "non_cutting_stock_contact_mm3":round(noncut_stock_contact,6),
            "non_cutting_contact_aggregation":"sum_of_per_hole_axial_feed_sweeps",
            "non_cutting_clearance_status":"not_run" if geometry.get("holder_length_mm") is None else "failed" if max(noncut_stock_contact,noncut_target_contact)>.0001 else "passed",
            "target_protection_status":"passed" if contact <= .0001 else "failed",
            "material_removal_status":"passed" if initial_volume-stock.Volume > .0001 else "failed"}
    region_union=None
    for index,region in regions:
        stages[index]["remaining_cylindrical_segment_mm3"]=round(volume(stock.common(region)),6)
        region_union=region if region_union is None else region_union.fuse(region)
    result["remaining_cylindrical_segments_mm3"]=round(volume(stock.common(region_union)),6) if region_union is not None else 0.
    if payload.get('radial_setup'):
        states=[s['fixture_clearance']['status'] for s in stages]
        result['fixture_clearance_status']='passed' if all(s=='passed' for s in states) else 'failed' if 'failed' in states else 'not_run'
    (output/"result.json").write_text(json.dumps(result),encoding="utf-8")
    return result


if __name__ == "__main__":
    source,payload,output=json.loads(os.environ["CNC_CAM_ARGUMENTS_JSON"])
    print("CNC_SIDE_HOLE_CHECK "+json.dumps(check(source,json.loads(payload),output)),flush=True)
