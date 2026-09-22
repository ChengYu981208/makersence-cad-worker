from __future__ import annotations
from pathlib import Path
from typing import Any
import io, zipfile, xml.etree.ElementTree as ET
import numpy as np
import trimesh
import cadquery as cq
from shapely.geometry import Polygon, MultiPoint
from app.assets import get_asset

NS = {'m':'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}


def _parse_transform(text: str | None) -> np.ndarray:
    M = np.eye(4)
    if not text:
        return M
    vals = [float(x) for x in text.split()]
    if len(vals) != 12:
        return M
    # 3MF transform is 3x4 row-major; convert to 4x4.
    M[:3,:4] = np.array(vals, dtype=float).reshape(3,4)
    return M


def _load_3mf_basic(path: Path) -> trimesh.Trimesh:
    data = path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if n.lower().endswith('.model')]
        if not names:
            raise ValueError('3MF has no .model')
        name = '3D/3dmodel.model' if '3D/3dmodel.model' in names else names[0]
        root = ET.fromstring(z.read(name))
        objects = {}
        comps = {}
        for obj in root.findall('.//m:resources/m:object', NS):
            oid = obj.get('id')
            mesh = obj.find('m:mesh', NS)
            if mesh is not None:
                vs=[];fs=[]
                for v in mesh.findall('m:vertices/m:vertex', NS):
                    vs.append([float(v.get('x','0')),float(v.get('y','0')),float(v.get('z','0'))])
                for t in mesh.findall('m:triangles/m:triangle', NS):
                    fs.append([int(t.get('v1','0')),int(t.get('v2','0')),int(t.get('v3','0'))])
                if vs and fs:
                    objects[oid]=trimesh.Trimesh(vertices=np.array(vs),faces=np.array(fs),process=False)
            cs = obj.find('m:components', NS)
            if cs is not None:
                comps[oid]=[(c.get('objectid'),_parse_transform(c.get('transform'))) for c in cs.findall('m:component',NS)]
        def resolve(oid, T=None, stack=None):
            T=np.eye(4) if T is None else T;stack=set() if stack is None else stack
            if oid in stack: return []
            if oid in objects:
                m=objects[oid].copy();m.apply_transform(T);return [m]
            out=[]
            for child,ct in comps.get(oid,[]): out.extend(resolve(child,T@ct,stack|{oid}))
            return out
        meshes=[]
        build=root.findall('.//m:build/m:item',NS)
        if build:
            for item in build:
                meshes.extend(resolve(item.get('objectid'),_parse_transform(item.get('transform'))))
        else:
            for oid in objects: meshes.extend(resolve(oid))
        if not meshes: raise ValueError('3MF contains no mesh')
        return trimesh.util.concatenate(meshes)


def load_reference_mesh(asset_id: str) -> trimesh.Trimesh:
    a = get_asset(asset_id)
    if a.format == 'stl':
        m=trimesh.load(a.path,force='mesh',process=False)
    elif a.format == '3mf':
        m=_load_3mf_basic(a.path)
    else:
        raise ValueError('mesh section extraction requires STL/3MF reference')
    if not isinstance(m,trimesh.Trimesh):
        m=trimesh.util.concatenate(tuple(m.geometry.values()))
    return m


def mesh_summary(asset_id: str) -> dict[str, Any]:
    m=load_reference_mesh(asset_id)
    return {
        'asset_id':asset_id,
        'vertices':int(len(m.vertices)),
        'triangles':int(len(m.faces)),
        'bounds_mm':{'min':m.bounds[0].round(4).tolist(),'max':m.bounds[1].round(4).tolist(),'dimensions':m.extents.round(4).tolist()},
        'watertight':bool(m.is_watertight)
    }


def _axis(axis: str):
    axis=axis.upper()
    if axis=='X': return np.array([1.,0.,0.]), (1,2), 0
    if axis=='Y': return np.array([0.,1.,0.]), (0,2), 1
    return np.array([0.,0.,1.]), (0,1), 2


def section_polygon(mesh: trimesh.Trimesh, axis: str='Z', plane_mm: float|None=None, buffer_mm: float=0.0) -> Polygon:
    normal, dims, ai = _axis(axis)
    if plane_mm is None:
        plane_mm=float((mesh.bounds[0,ai]+mesh.bounds[1,ai])/2)
    origin=np.zeros(3);origin[ai]=plane_mm
    sec=mesh.section(plane_origin=origin,plane_normal=normal)
    poly=None
    if sec is not None:
        try:
            planar,_=sec.to_2D()
            polys=list(planar.polygons_full)
            if polys: poly=max(polys,key=lambda p:p.area)
        except Exception:
            poly=None
    if poly is None or poly.is_empty:
        # Robust fallback: use vertices in a narrow plane band and convex hull.
        tol=max(.25, float(np.max(mesh.extents))*.003)
        pts=mesh.vertices[np.abs(mesh.vertices[:,ai]-plane_mm)<=tol][:,list(dims)]
        if len(pts)<3:
            pts=mesh.vertices[:,list(dims)]
        poly=MultiPoint(pts).convex_hull
    if buffer_mm:
        poly=poly.buffer(buffer_mm,join_style=2)
    if poly.geom_type!='Polygon':
        poly=max(poly.geoms,key=lambda p:p.area)
    return poly


def polygon_points(poly: Polygon, max_points: int=160) -> list[tuple[float,float]]:
    coords=list(poly.exterior.coords)[:-1]
    if len(coords)>max_points:
        step=max(1,len(coords)//max_points)
        coords=coords[::step]
    return [(float(x),float(y)) for x,y in coords]


def wire_from_polygon(poly: Polygon, axis: str='Z', plane_mm: float=0.0) -> cq.Wire:
    pts=polygon_points(poly)
    if axis.upper()=='Z': vec=[cq.Vector(x,y,plane_mm) for x,y in pts]
    elif axis.upper()=='X': vec=[cq.Vector(plane_mm,x,y) for x,y in pts]
    else: vec=[cq.Vector(x,plane_mm,y) for x,y in pts]
    return cq.Wire.makePolygon(vec, close=True)


def extrude_polygon(poly: Polygon, axis: str='Z', start_mm: float=0.0, length_mm: float=2.0) -> cq.Shape:
    pts=polygon_points(poly)
    if axis.upper()=='Z':
        return cq.Workplane('XY').workplane(offset=start_mm).polyline(pts).close().extrude(length_mm).val()
    if axis.upper()=='X':
        # sketch in YZ on YZ plane, extrusion along X
        return cq.Workplane('YZ').workplane(offset=start_mm).polyline(pts).close().extrude(length_mm).val()
    return cq.Workplane('XZ').workplane(offset=start_mm).polyline(pts).close().extrude(length_mm).val()
