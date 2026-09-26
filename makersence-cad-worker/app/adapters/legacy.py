from __future__ import annotations
from typing import Any
import re
import cadquery as cq
from app.adapters.base import CadAdapter, AdapterError, num, pos
from app.models import AdapterResult, Part
from app.geometry.primitives import open_box, rounded_box, frame_rect


def _infer_circle(path_d:str):
    # Hub vectorizer emits deterministic circle-like arc paths. Use radius and first M point.
    m=re.search(r'M\s*([-+0-9.]+)\s+([-+0-9.]+).*?A\s*([-+0-9.]+)\s+([-+0-9.]+)',path_d or '',re.I)
    if not m:return None
    x,y,rx,ry=map(float,m.groups())
    if abs(rx-ry)>.15:return None
    return (x+rx,y,rx)


class LegacyAdapter(CadAdapter):
    id='LEGACY'

    def build(self,contract:dict[str,Any],context:dict[str,Any])->AdapterResult:
        fam=str(contract.get('family') or context.get('classification') or '').strip()
        if not fam:
            raise AdapterError('LEGACY_CAD requires an explicit legacy family; silhouette_plate default is disabled')
        if fam in {'silhouette_plate','rounded_plate','keychain_plate','nfc_keychain'}:
            return self._plate(contract,context,fam)
        if fam in {'open_box','container'}: return self._open_box(contract)
        if fam=='phone_stand': return self._phone_stand(contract)
        if fam in {'lidded_container','sculpted_lidded_container_v1'}: return self._lidded(contract)
        if fam=='primitive_recipe': return self._primitive(contract)
        raise AdapterError(f'legacy family not implemented: {fam}')

    def _plate(self,c,ctx,fam):
        w=pos(c.get('width_mm'),60);h=pos(c.get('height_mm'),38);t=pos(c.get('thickness_mm'),2.4,.8);r=max(0,num(c.get('corner_radius_mm'),0))
        body=rounded_box(w,h,t,r).val()
        svg=ctx.get('svg_artifact') or {}
        for p in svg.get('parts') or []:
            if p.get('role')=='through_hole':
                circ=_infer_circle(str(p.get('path_d') or ''))
                if circ:
                    x,y,rad=circ;cut=cq.Workplane('XY').center(x-w/2,y-h/2).circle(rad).extrude(t+.4).val()
                    try:body=body.cut(cut)
                    except Exception:pass
        parts=[Part('BODY','main_body',body,'#17191a')]
        for p in svg.get('parts') or []:
            if p.get('role') in {'emboss','player_ui'} and p.get('id')!='BODY':
                # Conservative compatibility geometry is available only through explicit LEGACY_CAD routing.
                aw=min(w*.4,24);ah=min(h*.25,10);eh=max(.2,num(p.get('height_mm'),.5))
                shape=cq.Workplane('XY').box(aw,ah,eh,centered=(True,True,False)).translate((0,0,t)).val()
                parts.append(Part(str(p.get('id') or 'ACCENT'),str(p.get('role') or 'emboss'),shape,str(p.get('color') or '#f1efe8'),False,True))
        for sp in c.get('separate_parts') or []:
            if sp.get('kind')=='rect_frame':
                sh=frame_rect(pos(sp.get('outer_width_mm'),20),pos(sp.get('outer_height_mm'),20),pos(sp.get('inner_width_mm'),16),pos(sp.get('inner_height_mm'),16),pos(sp.get('thickness_mm'),1.2,.8)).val()
                sh=sh.translate((num(sp.get('print_x_mm'),0),num(sp.get('print_y_mm'),0),0))
                parts.append(Part(str(sp.get('name') or 'FRAME'),str(sp.get('role') or 'frame'),sh,str(sp.get('color') or '#17191a')))
        return AdapterResult(self.id,parts,assembly_contract={'family':fam})

    def _open_box(self,c):
        return AdapterResult(self.id,[Part('BODY','main_body',open_box(pos(c.get('width_mm'),80),pos(c.get('depth_mm'),60),pos(c.get('height_mm'),35),pos(c.get('wall_mm'),2.4,.8),pos(c.get('base_thickness_mm'),2.4,.8),num(c.get('corner_radius_mm'),3)).val())],assembly_contract={'family':'open_box'})

    def _phone_stand(self,c):
        w=pos(c.get('width_mm'),70);d=pos(c.get('depth_mm'),70);bt=pos(c.get('base_thickness_mm'),5,.8);bh=pos(c.get('back_height_mm'),90);back_t=pos(c.get('back_thickness_mm'),5,.8);lh=pos(c.get('lip_height_mm'),12);lt=pos(c.get('lip_thickness_mm'),5,.8)
        base=cq.Workplane('XY').box(w,d,bt,centered=(True,True,False)).val()
        back=cq.Workplane('XY').box(w,back_t,bh,centered=(True,True,False)).translate((0,d/2-back_t/2,bt)).val()
        lip=cq.Workplane('XY').box(w,lt,lh,centered=(True,True,False)).translate((0,-d/2+lt/2,bt)).val()
        body=base.fuse(back).fuse(lip)
        return AdapterResult(self.id,[Part('BODY','phone_stand',body)],assembly_contract={'family':'phone_stand'})

    def _lidded(self,c):
        w=pos(c.get('width_mm'),100);d=pos(c.get('depth_mm') or c.get('height_mm'),90);h=pos(c.get('body_height_mm') or c.get('height_mm'),55);wall=pos(c.get('wall_mm'),2.4,.8);clr=pos(c.get('xy_clearance_mm'),.25,.1)
        body=open_box(w,d,h,wall,wall,num(c.get('corner_radius_mm'),4)).val()
        lid_t=pos(c.get('lid_thickness_mm'),2.4,.8);lid=rounded_box(w+2*wall+2*clr,d+2*wall+2*clr,lid_t,num(c.get('corner_radius_mm'),4)).val().translate((0,0,h+3))
        return AdapterResult(self.id,[Part('BODY','main_body',body),Part('LID','lid',lid)],assembly_contract={'family':'lidded_container','xy_clearance_mm':clr})

    def _primitive(self,c):
        parts=[]
        for i,p in enumerate(c.get('primitives') or []):
            kind=str(p.get('kind') or 'box')
            if kind=='box':
                sh=cq.Workplane('XY').box(pos(p.get('width_mm'),10),pos(p.get('depth_mm'),10),pos(p.get('height_mm'),5),centered=(True,True,False)).translate((num(p.get('x_mm'),0),num(p.get('y_mm'),0),num(p.get('z_mm'),0))).val()
            elif kind=='cylinder':
                sh=cq.Workplane('XY').circle(pos(p.get('diameter_mm'),8)/2).extrude(pos(p.get('height_mm'),5)).translate((num(p.get('x_mm'),0),num(p.get('y_mm'),0),num(p.get('z_mm'),0))).val()
            else: raise AdapterError(f'unsupported primitive: {kind}')
            parts.append(Part(str(p.get('name') or f'PART_{i+1}'),str(p.get('role') or 'primitive'),sh,str(p.get('color') or '#777777')))
        return AdapterResult(self.id,parts,assembly_contract={'family':'primitive_recipe'})
