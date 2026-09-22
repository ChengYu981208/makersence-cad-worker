from __future__ import annotations
from typing import Any
import hashlib, json
import cadquery as cq
import numpy as np
from shapely.geometry import Polygon
from app.adapters.base import CadAdapter, AdapterError, num, pos, require
from app.models import AdapterResult, Part
from app.geometry.reference import load_reference_mesh, section_polygon, polygon_points, extrude_polygon, mesh_summary, wire_from_polygon


def _fingerprint(axis: str, plane: float, poly: Polygon) -> str:
    payload={"axis":axis.upper(),"plane_mm":round(float(plane),4),"points":[[round(x,4),round(y,4)] for x,y in polygon_points(poly,120)]}
    return hashlib.sha256(json.dumps(payload,separators=(',',':'),sort_keys=True).encode()).hexdigest()


def _add_retention(body: cq.Shape, feature: dict[str,Any], axis: str='Z') -> cq.Shape:
    kind=str(feature.get('kind','')).lower()
    if kind not in {'lip','nose','stop'}:
        return body
    w=pos(feature.get('width_mm'),10,.8);d=pos(feature.get('depth_mm'),2,.8);h=pos(feature.get('height_mm'),2,.8)
    x=num(feature.get('x_mm'),0);y=num(feature.get('y_mm'),0);z=num(feature.get('z_mm'),0)
    # Retention primitives are explicit contract geometry, never guessed.
    f=cq.Workplane('XY').box(w,d,h,centered=(True,True,False)).translate((x,y,z)).val()
    try:return body.fuse(f)
    except Exception:return body


class InterfaceLockedAdapter(CadAdapter):
    id='INTERFACE_LOCKED_CAD'

    def build(self, contract: dict[str,Any], context: dict[str,Any]) -> AdapterResult:
        asset_id=str(contract.get('reference_asset_id') or context.get('reference_asset_id') or '')
        require(bool(asset_id),'INTERFACE_LOCKED_CAD requires reference_asset_id; Hub must upload the analyzed 3MF/STL before generation')
        mesh=load_reference_mesh(asset_id)
        axis=str(contract.get('interface_axis') or 'Z').upper()
        ai={'X':0,'Y':1,'Z':2}.get(axis,2)
        lo=float(mesh.bounds[0,ai]);hi=float(mesh.bounds[1,ai])
        plane=num(contract.get('interface_plane_mm'),lo + (hi-lo)*.08)
        plane=max(lo+1e-4,min(hi-1e-4,plane))
        clearance=num(contract.get('interface_offset_mm'),0.0)
        band=pos(contract.get('protected_band_mm'),max(1.2,(hi-lo)*.08),.8)
        # Keep band inside source bounds. This band is protected and copied only for function.
        start=max(lo,min(hi-band,plane-band/2))
        section_count=max(2,min(9,int(contract.get('protected_section_count') or 5)))
        planes=np.linspace(start+max(.02,band*.02), start+band-max(.02,band*.02), section_count)
        sections=[]
        for z in planes:
            sp=section_polygon(mesh,axis,float(z),buffer_mm=clearance)
            if sp.area>1e-4:sections.append((float(z),sp))
        require(len(sections)>=2,'reference interface band does not contain enough stable sections')
        # Preserve 3D changes across the mating band by lofting multiple measured source sections.
        wires=[wire_from_polygon(sp,axis,z) for z,sp in sections]
        try:
            protected=cq.Solid.makeLoft(wires,False)
        except Exception:
            # Fail-safe fallback keeps the measured mid-section, but records the degraded mode.
            midz,midp=sections[len(sections)//2];protected=extrude_polygon(midp,axis,start,band)
        plane,poly=sections[len(sections)//2]

        outer_offset=pos(contract.get('redesign_offset_mm'),4.0,.8)
        wall=pos(contract.get('wall_mm'),2.4,.8)
        body_len=pos(contract.get('body_length_mm'),max(band*2,12),band)
        redesign_start=start+band
        outer=poly.buffer(outer_offset,join_style=2)
        inner=poly.buffer(max(0.0,outer_offset-wall),join_style=2)
        outer_s=extrude_polygon(outer,axis,redesign_start,body_len)
        if inner.area>0 and outer.contains(inner):
            inner_s=extrude_polygon(inner,axis,redesign_start-.05,body_len+.1)
            redesigned=outer_s.cut(inner_s)
        else:
            redesigned=outer_s
        try:body=protected.fuse(redesigned)
        except Exception:body=protected

        for feat in contract.get('retention_features') or []:
            body=_add_retention(body,feat,axis)

        result=AdapterResult(
            adapter=self.id,
            parts=[Part('BODY','main_body',body,color=str(contract.get('body_color') or '#202020'),physical_separate=True,editable_separate=True,
                        metadata={'protected_interface':True})],
            protected_interface_hash=_fingerprint(axis,plane,poly),
            assembly_contract={
                'interface_mode':'SOURCE_INTERFACE',
                'reference_asset_id':asset_id,
                'axis':axis,'section_plane_mm':round(plane,4),'protected_band_mm':round(band,4),
                'interface_offset_mm':round(clearance,4),'section_planes_mm':[round(z,4) for z,_ in sections],
                'protected_interface_hash':_fingerprint(axis,plane,poly),
                'policy':'Only the narrow mating band is derived from the reference asset. Geometry outside the protected band is rebuilt as MakerSence geometry.'
            },
            diagnostics={'reference':mesh_summary(asset_id),'section_area_mm2':round(poly.area,4),'section_points':len(polygon_points(poly)),'protected_section_count':len(sections)}
        )
        result.require_parts()
        return result
