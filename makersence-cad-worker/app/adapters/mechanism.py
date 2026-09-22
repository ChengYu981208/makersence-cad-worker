from __future__ import annotations
from typing import Any
import cadquery as cq
from app.adapters.base import CadAdapter, AdapterError, num, pos, require
from app.models import AdapterResult, Part
from app.geometry.primitives import open_box, rounded_box, box, ring, cylinder


class MechanismAdapter(CadAdapter):
    id='MECHANISM_CAD'

    def build(self, contract: dict[str,Any], context: dict[str,Any]) -> AdapterResult:
        kind=str(contract.get('mechanism_type') or 'hinged_box').lower()
        if kind in {'hinged_box','hinge_box','print_in_place_hinge'}:
            return self._hinged_box(contract,print_in_place=kind=='print_in_place_hinge')
        if kind in {'sliding_lid_box','slider_box','sliding_box'}:
            return self._sliding_box(contract)
        if kind in {'pivot_arm','hinge_stand'}:
            return self._pivot_arm(contract)
        raise AdapterError(f'unsupported MECHANISM_CAD mechanism_type: {kind}')

    def _hinged_box(self,c:dict[str,Any],print_in_place:bool=False)->AdapterResult:
        w=pos(c.get('width_mm'),80);d=pos(c.get('depth_mm'),55);h=pos(c.get('height_mm'),32)
        wall=pos(c.get('wall_mm'),2.4,.8);floor=pos(c.get('floor_mm'),2.4,.8);clr=pos(c.get('joint_clearance_mm'),.35,.15)
        barrel_d=pos(c.get('hinge_barrel_diameter_mm'),6,2.0);pin_d=max(.8,barrel_d-2*clr-1.0)
        body=open_box(w,d,h,wall,floor,num(c.get('corner_radius_mm'),3)).val()
        lid_t=pos(c.get('lid_thickness_mm'),2.4,.8)
        lid=cq.Workplane('XY').rect(w,d).extrude(lid_t).translate((0,0,h+clr)).val()
        y=-d/2-barrel_d*.15
        # Alternating hinge knuckles: body gets two, lid gets one center knuckle.
        seg=w/5
        def barrel(x0,length,z0):
            return cq.Workplane('YZ').center(y,z0).circle(barrel_d/2).circle(pin_d/2).extrude(length,both=True).translate((x0,0,0)).val()
        b1=barrel(-w*.32,seg*.55,h-.2);b2=barrel(w*.32,seg*.55,h-.2);lm=barrel(0,seg*.65,h-.2)
        try: body=body.fuse(b1).fuse(b2)
        except Exception: pass
        try: lid=lid.fuse(lm)
        except Exception: pass
        parts=[Part('BODY','main_body',body,'#2a2a2a'),Part('LID','lid',lid,'#3a3a3a')]
        if not print_in_place:
            pin=cq.Workplane('YZ').center(y,h-.2).circle(pin_d/2).extrude(w*.92,both=True).val()
            parts.append(Part('HINGE_PIN','hinge_pin',pin,'#555555'))
        return AdapterResult(self.id,parts,assembly_contract={
            'mechanism_type':'hinged_box','joint':'rotary_hinge','joint_clearance_mm':clr,
            'barrel_diameter_mm':barrel_d,'pin_diameter_mm':pin_d,'serviceable':not print_in_place
        },diagnostics={'print_in_place':print_in_place})

    def _sliding_box(self,c:dict[str,Any])->AdapterResult:
        w=pos(c.get('width_mm'),72);d=pos(c.get('depth_mm'),100);h=pos(c.get('height_mm'),38)
        wall=pos(c.get('wall_mm'),2.4,.8);clr=pos(c.get('joint_clearance_mm'),.3,.15);rail=pos(c.get('rail_height_mm'),2.0,.8)
        body=open_box(w,d,h,wall,wall,num(c.get('corner_radius_mm'),3)).val()
        # Rails on inner long sides, explicitly parameterized.
        rail_len=d-wall*2
        rx=w/2-wall*1.15
        rshape=cq.Workplane('XY').box(wall,rail_len,rail,centered=(True,True,False)).translate((rx,0,h-rail)).val()
        try: body=body.fuse(rshape).fuse(rshape.mirror('YZ'))
        except Exception: pass
        lid_w=max(2,w-2*wall-2*clr);lid_d=max(2,d-2*wall);lid_t=pos(c.get('lid_thickness_mm'),2.0,.8)
        lid=cq.Workplane('XY').rect(lid_w,lid_d).extrude(lid_t).translate((0,0,h-rail+clr)).val()
        return AdapterResult(self.id,[Part('BODY','main_body',body,'#2a2a2a'),Part('SLIDING_LID','sliding_lid',lid,'#3a3a3a')],assembly_contract={
            'mechanism_type':'sliding_lid_box','joint':'linear_slider','joint_clearance_mm':clr,'rail_height_mm':rail
        })

    def _pivot_arm(self,c:dict[str,Any])->AdapterResult:
        base_w=pos(c.get('base_width_mm'),70);base_d=pos(c.get('base_depth_mm'),32);base_t=pos(c.get('base_thickness_mm'),5,.8)
        arm_l=pos(c.get('arm_length_mm'),65);arm_w=pos(c.get('arm_width_mm'),12);arm_t=pos(c.get('arm_thickness_mm'),5,.8)
        pivot_d=pos(c.get('pivot_diameter_mm'),6,2);clr=pos(c.get('joint_clearance_mm'),.35,.15)
        base=cq.Workplane('XY').box(base_w,base_d,base_t,centered=(True,True,False)).val()
        boss=cq.Workplane('XZ').center(0,base_t+pivot_d/2).circle((pivot_d+4)/2).circle((pivot_d+2*clr)/2).extrude(arm_w*.7,both=True).val()
        try:base=base.fuse(boss)
        except Exception:pass
        arm=cq.Workplane('XY').box(arm_l,arm_w,arm_t,centered=(False,True,False)).translate((-arm_l*.08,0,base_t+pivot_d/2-arm_t/2)).val()
        hole=cq.Workplane('XZ').center(0,base_t+pivot_d/2).circle((pivot_d+2*clr)/2).extrude(arm_w,both=True).val()
        try:arm=arm.cut(hole)
        except Exception:pass
        pin=cq.Workplane('XZ').center(0,base_t+pivot_d/2).circle(pivot_d/2).extrude(arm_w*1.3,both=True).val()
        return AdapterResult(self.id,[Part('BASE','interface_carrier',base),Part('ARM','moving_arm',arm),Part('PIVOT_PIN','pivot_pin',pin)],assembly_contract={
            'mechanism_type':'pivot_arm','joint':'rotary_hinge','pivot_diameter_mm':pivot_d,'joint_clearance_mm':clr
        })
