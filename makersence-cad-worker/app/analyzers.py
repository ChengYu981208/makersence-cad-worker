from __future__ import annotations
from pathlib import Path
from typing import Any
import io, tempfile, zipfile, xml.etree.ElementTree as ET
import numpy as np
import trimesh
import cadquery as cq
from app.assets import put_asset
from app.geometry.reference import mesh_summary, load_reference_mesh
from app.tooling import manifold_probe, validate_3mf_bytes


def analyze_bytes(data:bytes,fmt:str)->dict[str,Any]:
    fmt=fmt.lower().lstrip('.').replace('stp','step')
    if fmt in {'stl','3mf'}:
        a=put_asset(data,fmt);s=mesh_summary(a.asset_id)
        m=load_reference_mesh(a.asset_id)
        return {
            'format':fmt,'file_size_bytes':len(data),'vertex_count':int(len(m.vertices)),'triangle_count':int(len(m.faces)),
            'bounds_mm':s['bounds_mm'],'measurement_quality':'MEASURED_WORKER_V3','watertight':bool(m.is_watertight),
            'assembly_analysis':{'part_count':_count_parts(data,fmt),'multipart':_count_parts(data,fmt)>1},
            'engineering_features':{'opening_candidates':[],'thickness_candidates':[],'clearance_candidates':[]},
            'manifold_probe':manifold_probe(m),
            'lib3mf_validation':validate_3mf_bytes(data) if fmt=='3mf' else {'status':'NOT_APPLICABLE'},
            'reference_asset_id':a.asset_id
        }
    if fmt=='step':
        a=put_asset(data,fmt)
        shape=cq.importers.importStep(str(a.path));bb=shape.val().BoundingBox();dims=[bb.xlen,bb.ylen,bb.zlen]
        return {'format':'step','file_size_bytes':len(data),'bounds_mm':{'min':[bb.xmin,bb.ymin,bb.zmin],'max':[bb.xmax,bb.ymax,bb.zmax],'dimensions':dims},'measurement_quality':'MEASURED_BREP','assembly_analysis':{'part_count':1,'multipart':False},'engineering_features':{'opening_candidates':[],'thickness_candidates':[],'clearance_candidates':[]},'reference_asset_id':a.asset_id}
    raise ValueError('unsupported source format')


def _count_parts(data:bytes,fmt:str)->int:
    if fmt!='3mf':return 1
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names=[n for n in z.namelist() if n.lower().endswith('.model')]
            if not names:return 1
            root=ET.fromstring(z.read('3D/3dmodel.model' if '3D/3dmodel.model' in names else names[0]))
            ns={'m':'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
            return max(1,len(root.findall('.//m:resources/m:object',ns)))
    except Exception:return 1
