import os,time
os.environ['WORKER_TOKEN']='test-token'
from fastapi.testclient import TestClient
from app.main import app

client=TestClient(app)
H={'Authorization':'Bearer test-token'}

def test_health():
    r=client.get('/health',headers=H);assert r.status_code==200
    j=r.json();assert j['adapters']['INTERFACE_LOCKED_CAD']=='ready'

def test_legacy_generate_contract():
    payload={
      'module':'system_selftest','appearance_hash':'abc','idempotency_key':'test-legacy-1',
      'cad_contract':{'family':'silhouette_plate','width_mm':60,'height_mm':38,'thickness_mm':2.4,'wall_mm':2.4,'separate_parts':[{'name':'TEST_FRAME','role':'photo_frame','kind':'rect_frame','outer_width_mm':20,'outer_height_mm':20,'inner_width_mm':16,'inner_height_mm':16,'thickness_mm':1.2,'print_x_mm':42}]},
      'svg_artifact':{'parts':[{'id':'BODY','role':'silhouette','color':'#17191a'},{'id':'HOLE','role':'through_hole','path_d':'M 4.4 7 A 2.6 2.6 0 1 0 9.6 7 A 2.6 2.6 0 1 0 4.4 7 Z','color':'#17191a'},{'id':'ACCENT','role':'emboss','color':'#f1efe8','height_mm':.6}]}
    }
    r=client.post('/v1/generate',json=payload,headers=H);assert r.status_code==200;jid=r.json()['job_id']
    job=None
    for _ in range(40):
        job=client.get('/v1/jobs/'+jid,headers=H).json()
        if job['status'] in ('completed','failed'):break
        time.sleep(.1)
    assert job['status']=='completed',job
    assert job['validation']['brep_valid'] is True
    assert {'stl','step','3mf','glb','png','json'} <= {a['type'] for a in job['artifacts']}

def test_reference_upload_then_interface_generate(tmp_path):
    import cadquery as cq
    from pathlib import Path
    import tempfile
    ref=cq.Workplane('XY').rect(36,16).extrude(7).union(cq.Workplane('XY').box(10,5,3,centered=(True,True,False)).translate((0,5,7)))
    with tempfile.NamedTemporaryFile(suffix='.stl',delete=False) as f:
        cq.exporters.export(ref,f.name,tolerance=.04);data=Path(f.name).read_bytes()
    u=client.post('/v1/reference-assets?format=stl',content=data,headers={**H,'Content-Type':'application/octet-stream'})
    assert u.status_code==200,u.text
    aid=u.json()['asset_id']
    payload={'idempotency_key':'test-iface-api-1','cad_contract':{'adapter':'INTERFACE_LOCKED_CAD','generation_strategy':'INTERFACE_LOCKED_CAD','reference_asset_id':aid,'interface_axis':'Z','interface_plane_mm':1,'protected_band_mm':2,'redesign_offset_mm':3.5,'wall_mm':2.4,'body_length_mm':10}}
    r=client.post('/v1/generate',json=payload,headers=H);assert r.status_code==200
    jid=r.json()['job_id'];job=None
    for _ in range(60):
        job=client.get('/v1/jobs/'+jid,headers=H).json()
        if job['status'] in ('completed','failed'):break
        time.sleep(.1)
    assert job['status']=='completed',job
    assert job['adapter']=='INTERFACE_LOCKED_CAD'
    assert job['validation']['protected_interface_hash']
    assert job['validation']['watertight'] is True
