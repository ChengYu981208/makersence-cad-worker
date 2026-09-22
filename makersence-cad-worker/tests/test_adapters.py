from pathlib import Path
import tempfile
import cadquery as cq
from app.assets import put_asset
from app.adapters.interface_locked import InterfaceLockedAdapter
from app.adapters.interface_mechanism import InterfaceMechanismAdapter
from app.adapters.mechanism import MechanismAdapter
from app.exporters import export_result


def reference_asset():
    # Synthetic functional clip profile: base + raised nose; enough to verify section preservation.
    base=cq.Workplane('XY').rect(42,18).extrude(8)
    nose=cq.Workplane('XY').box(12,6,4,centered=(True,True,False)).translate((0,6,8))
    shape=base.union(nose)
    with tempfile.NamedTemporaryFile(suffix='.stl',delete=False) as f:
        cq.exporters.export(shape,f.name,tolerance=.04)
        data=Path(f.name).read_bytes()
    return put_asset(data,'stl')


def test_interface_locked_preserves_contract_and_exports(tmp_path):
    a=reference_asset()
    c={'reference_asset_id':a.asset_id,'interface_axis':'Z','interface_plane_mm':1.0,'protected_band_mm':2.0,'redesign_offset_mm':4.0,'wall_mm':2.4,'body_length_mm':12}
    r=InterfaceLockedAdapter().build(c,{})
    assert r.protected_interface_hash
    assert r.assembly_contract['reference_asset_id']==a.asset_id
    assert len(r.parts)==1 and r.parts[0].shape.isValid()
    arts,val=export_result(r,tmp_path/'out')
    assert val['brep_valid'] is True
    assert val['watertight'] is True
    assert {'stl','step','3mf','glb','png','json'} <= {x['type'] for x in arts}


def test_mechanism_hinged_box():
    r=MechanismAdapter().build({'mechanism_type':'hinged_box','width_mm':70,'depth_mm':50,'height_mm':28,'joint_clearance_mm':.35},{})
    assert {p.name for p in r.parts}=={'BODY','LID','HINGE_PIN'}
    assert r.assembly_contract['joint']=='rotary_hinge'
    assert all(p.shape.isValid() for p in r.parts)


def test_mechanism_sliding_box():
    r=MechanismAdapter().build({'mechanism_type':'sliding_lid_box','width_mm':70,'depth_mm':90,'height_mm':35,'joint_clearance_mm':.3},{})
    assert {p.name for p in r.parts}=={'BODY','SLIDING_LID'}
    assert r.assembly_contract['joint']=='linear_slider'


def test_interface_mechanism_composes_reference_and_motion():
    a=reference_asset()
    c={
      'reference_asset_id':a.asset_id,'interface_axis':'Z','interface_plane_mm':1,'protected_band_mm':2,'redesign_offset_mm':3.5,'body_length_mm':10,
      'mechanism':{'mechanism_type':'pivot_arm','base_width_mm':45,'base_depth_mm':24,'base_thickness_mm':4,'arm_length_mm':42,'arm_width_mm':10,'arm_thickness_mm':4,'pivot_diameter_mm':5,'joint_clearance_mm':.35},
      'mechanism_translate_z_mm':14
    }
    r=InterfaceMechanismAdapter().build(c,{})
    assert r.protected_interface_hash
    assert r.assembly_contract['strategy']=='protected_external_interface_plus_explicit_internal_mechanism'
    assert any(p.role=='moving_arm' for p in r.parts)
    assert all(p.shape.isValid() for p in r.parts)
