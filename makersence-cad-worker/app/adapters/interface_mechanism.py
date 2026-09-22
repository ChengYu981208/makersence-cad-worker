from __future__ import annotations
from typing import Any
from app.adapters.base import CadAdapter, num
from app.models import AdapterResult, Part
from app.adapters.interface_locked import InterfaceLockedAdapter
from app.adapters.mechanism import MechanismAdapter


class InterfaceMechanismAdapter(CadAdapter):
    id='INTERFACE_MECHANISM_CAD'

    def build(self, contract: dict[str,Any], context: dict[str,Any]) -> AdapterResult:
        interface_contract=dict(contract)
        interface_contract.update(contract.get('interface') or {})
        mech_contract=dict(contract.get('mechanism') or {})
        if not mech_contract:
            mech_contract={k:v for k,v in contract.items() if k.startswith('mechanism_') or k in {
                'mechanism_type','joint_clearance_mm','base_width_mm','base_depth_mm','base_thickness_mm','arm_length_mm','arm_width_mm','arm_thickness_mm','pivot_diameter_mm'
            }}
            if 'mechanism_type' not in mech_contract:
                mech_contract['mechanism_type']='pivot_arm'
        ires=InterfaceLockedAdapter().build(interface_contract,context)
        mres=MechanismAdapter().build(mech_contract,context)
        dx=num(contract.get('mechanism_translate_x_mm'),0);dy=num(contract.get('mechanism_translate_y_mm'),0);dz=num(contract.get('mechanism_translate_z_mm'),0)
        mech_parts=[]
        for p in mres.parts:
            shape=p.shape.translate((dx,dy,dz)) if any(abs(v)>1e-9 for v in (dx,dy,dz)) else p.shape
            mech_parts.append(Part(p.name,p.role,shape,p.color,p.physical_separate,p.editable_separate,{**p.metadata,'mechanism':True}))
        interface_body=ires.parts[0]
        carrier_name=str(contract.get('mechanism_carrier_part') or 'BASE')
        carrier=next((p for p in mech_parts if p.name==carrier_name),None)
        parts=[]
        if carrier is not None and contract.get('fuse_carrier_to_interface',True):
            try:
                fused=interface_body.shape.fuse(carrier.shape)
                parts.append(Part('BODY','interface_mechanism_body',fused,interface_body.color,True,True,{'protected_interface':True,'mechanism_carrier_fused':carrier.name}))
                mech_parts=[p for p in mech_parts if p is not carrier]
            except Exception:
                parts.append(interface_body);parts.append(carrier);mech_parts=[p for p in mech_parts if p is not carrier]
        else:
            parts.append(interface_body)
        parts.extend(mech_parts)
        return AdapterResult(
            adapter=self.id,parts=parts,protected_interface_hash=ires.protected_interface_hash,
            assembly_contract={
                'interface':ires.assembly_contract,
                'mechanism':mres.assembly_contract,
                'strategy':'protected_external_interface_plus_explicit_internal_mechanism',
                'mechanism_translate_mm':[dx,dy,dz]
            },
            diagnostics={'interface':ires.diagnostics,'mechanism':mres.diagnostics},
            warnings=[*ires.warnings,*mres.warnings]
        )
