from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

try:
    import manifold3d
    from manifold3d import Manifold, Mesh
except Exception:
    manifold3d=None
    Manifold=None
    Mesh=None

try:
    import lib3mf
    from lib3mf import get_wrapper
except Exception:
    lib3mf=None
    get_wrapper=None


def tooling_status()->dict[str,Any]:
    manifold_version=getattr(manifold3d,'__version__',None) if manifold3d else None
    lib3mf_version=None
    if get_wrapper:
        try:
            lib3mf_version='.'.join(str(x) for x in get_wrapper().GetLibraryVersion())
        except Exception:
            lib3mf_version='imported'
    return {
        'manifold':{
            'available':Manifold is not None and Mesh is not None,
            'version':manifold_version,
            'role':'robust mesh boolean/probe; never substitutes for B-rep STEP',
            'auto_repair_policy':'fail_closed_no_destructive_remesh'
        },
        'lib3mf':{
            'available':get_wrapper is not None,
            'version':lib3mf_version,
            'role':'standards-level 3MF read/write validation',
            'bambu_project_compatibility':'separate_validation_required'
        }
    }


def manifold_probe(mesh:trimesh.Trimesh)->dict[str,Any]:
    if Manifold is None or Mesh is None:
        return {'status':'UNAVAILABLE','available':False}
    try:
        verts=np.asarray(mesh.vertices,dtype=np.float32)
        faces=np.asarray(mesh.faces,dtype=np.uint32)
        src=Mesh(vert_properties=verts,tri_verts=faces)
        m=Manifold(src)
        status=str(m.status())
        out=m.to_mesh()
        out_v=np.asarray(out.vert_properties)
        out_f=np.asarray(out.tri_verts)
        valid=out_v.ndim==2 and len(out_v)>0 and out_f.ndim==2 and len(out_f)>0
        return {
            'status':'PASS' if valid else 'FAIL',
            'available':True,
            'manifold_status':status,
            'input_vertices':int(len(verts)),
            'input_triangles':int(len(faces)),
            'output_vertices':int(len(out_v)) if out_v.ndim else 0,
            'output_triangles':int(len(out_f)) if out_f.ndim else 0,
            'repair_applied':False,
            'policy':'Probe only. Do not auto-replace source geometry; any repair must preserve dimensions, openings, tolerances, part identity and be revalidated.'
        }
    except Exception as e:
        return {
            'status':'FAIL','available':True,'error':str(e)[:500],
            'repair_applied':False,
            'policy':'Fail closed; no coarse remesh or silent topology rewrite.'
        }


def validate_3mf_file(path:Path)->dict[str,Any]:
    if get_wrapper is None:
        return {'status':'UNAVAILABLE','available':False}
    try:
        wrapper=get_wrapper()
        model=wrapper.CreateModel()
        reader=model.QueryReader('3mf')
        try:
            reader.SetStrictModeActive(True)
        except Exception:
            pass
        reader.ReadFromFile(str(path))
        warnings=[]
        try:
            for i in range(reader.GetWarningCount()):
                code,msg=reader.GetWarning(i)
                warnings.append({'code':int(code),'message':str(msg)})
        except Exception:
            pass
        obj_it=model.GetObjects();object_count=0;mesh_count=0;component_count=0
        while obj_it.MoveNext():
            object_count+=1
            obj=obj_it.GetCurrentObject()
            try:
                if obj.IsMeshObject():mesh_count+=1
                elif obj.IsComponentsObject():component_count+=1
            except Exception:
                pass
        build_it=model.GetBuildItems();build_count=0
        while build_it.MoveNext():build_count+=1
        return {
            'status':'PASS',
            'available':True,
            'strict_reader':True,
            'object_count':object_count,
            'mesh_object_count':mesh_count,
            'components_object_count':component_count,
            'build_item_count':build_count,
            'warnings':warnings,
            'warning_count':len(warnings),
            'policy':'lib3mf PASS means standard 3MF readability only; Bambu project compatibility and slicing remain separate gates.'
        }
    except Exception as e:
        return {
            'status':'FAIL','available':True,'strict_reader':True,'error':str(e)[:800],
            'policy':'Standard 3MF validation failed; do not treat ZIP/XML presence as equivalent to lib3mf readability.'
        }


def validate_3mf_bytes(data:bytes)->dict[str,Any]:
    fd,name=tempfile.mkstemp(prefix='makersence-lib3mf-',suffix='.3mf')
    os.close(fd)
    p=Path(name)
    try:
        p.write_bytes(data)
        return validate_3mf_file(p)
    finally:
        try:p.unlink()
        except Exception:pass


def integration_selftest()->dict[str,Any]:
    status=tooling_status()
    checks={}
    evidence={}
    try:
        outer=Manifold.cube((20.0,20.0,6.0),True)
        inner=Manifold.cube((8.0,8.0,8.0),True)
        cut=outer-inner
        mesh=cut.to_mesh()
        tri_count=int(len(np.asarray(mesh.tri_verts)))
        checks['manifold_boolean']=tri_count>12
        evidence['manifold_boolean']={'triangles':tri_count,'status':str(cut.status())}
    except Exception as e:
        checks['manifold_boolean']=False
        evidence['manifold_boolean']={'error':str(e)}

    try:
        wrapper=get_wrapper()
        model=wrapper.CreateModel()
        obj=model.AddMeshObject()
        obj.SetName('SELFTEST_CUBE')
        pts=[(-5,-5,0),(5,-5,0),(5,5,0),(-5,5,0),(-5,-5,4),(5,-5,4),(5,5,4),(-5,5,4)]
        tris=[(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),(1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
        vertices=[]
        for x,y,z in pts:
            pos=lib3mf.Position();pos.Coordinates[0]=float(x);pos.Coordinates[1]=float(y);pos.Coordinates[2]=float(z);vertices.append(pos)
        triangles=[]
        for a,b,c in tris:
            t=lib3mf.Triangle();t.Indices[0]=a;t.Indices[1]=b;t.Indices[2]=c;triangles.append(t)
        obj.SetGeometry(vertices,triangles)
        model.AddBuildItem(obj,wrapper.GetIdentityTransform())
        fd,name=tempfile.mkstemp(prefix='makersence-lib3mf-selftest-',suffix='.3mf');os.close(fd);p=Path(name)
        try:
            writer=model.QueryWriter('3mf');writer.WriteToFile(str(p))
            rt=validate_3mf_file(p)
        finally:
            try:p.unlink()
            except Exception:pass
        checks['lib3mf_roundtrip']=rt.get('status')=='PASS' and rt.get('mesh_object_count')==1 and rt.get('build_item_count')==1
        evidence['lib3mf_roundtrip']=rt
    except Exception as e:
        checks['lib3mf_roundtrip']=False
        evidence['lib3mf_roundtrip']={'error':str(e)}

    return {
        'status':'PASS' if checks and all(checks.values()) else 'FAIL',
        'checks':checks,
        'tools':status,
        'evidence':evidence,
        'policy':'This selftest proves the libraries execute. It does not prove arbitrary damaged geometry is safe to auto-repair or that a standards-valid 3MF is Bambu-compatible.'
    }
