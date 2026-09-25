import os, sys, json, uuid, pathlib, time, struct, zipfile, math, hashlib, html, threading, subprocess, re, gc, ctypes
import xml.etree.ElementTree as ET
from collections import Counter
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

import cadquery as cq
from cadquery import exporters
from PIL import Image, ImageDraw, ImageFilter
from svgpathtools import parse_path
from shapely.geometry import Polygon, MultiPolygon, GeometryCollection, Point, box as shapely_box
from shapely.ops import unary_union
from shapely.affinity import translate as geom_translate
from shapely.validation import explain_validity

TOKEN=os.environ.get("WORKER_TOKEN","")
PORT=int(os.environ.get("PORT","8000"))
ROOT=pathlib.Path("/tmp/makersence_jobs")
ROOT.mkdir(parents=True,exist_ok=True)
JOBS={}
SLICE_JOBS={}
BAMBU_BIN=os.environ.get("BAMBU_BIN","")
BAMBU_VERSION=os.environ.get("BAMBU_VERSION","2.8.2.61")
BAMBU_HOME=pathlib.Path("/tmp/bambu-home")
(BAMBU_HOME/".config"/"BambuStudio").mkdir(parents=True,exist_ok=True)
BUILD_MM=[180.0,180.0,180.0]
MIN_FEATURE_DEFAULT=0.8
TEXT_MIN_STROKE_DEFAULT=0.55
TEXT_LINE_WIDTH_DEFAULT=0.42
TEXT_LATIN_GAP_DEFAULT=0.50
TEXT_CJK_GAP_DEFAULT=0.55
TEXT_CJK_INTERNAL_GAP_DEFAULT=0.46
MAX_COLORS_DEFAULT=4
HEAVY_JOB_SEMAPHORE=threading.Semaphore(max(1,int(os.environ.get("MAKERSENCE_HEAVY_CONCURRENCY","1"))))

def current_rss_mb():
    try:
        txt=pathlib.Path("/proc/self/status").read_text()
        m=re.search(r"^VmRSS:\s+(\d+)\s+kB",txt,re.M)
        return round(int(m.group(1))/1024.0,1) if m else 0.0
    except Exception:return 0.0

def resource_policy(req):
    c=req.get("cad_contract") or {}
    p=c.get("resource_policy") or {}
    family=str(c.get("family") or "")
    if family=="universal_cad_recipe":
        # Railway shadow workers currently have ~1 GB RAM. Keep enough headroom
        # for OCCT/VTK/Python allocations so one CAD job cannot starve /health.
        soft=max(520.0,min(700.0,f(p.get("memory_soft_limit_mb"),620)))
        hard=max(680.0,min(820.0,f(p.get("memory_hard_limit_mb"),760)))
        if hard<=soft+48:hard=min(820.0,soft+64.0)
    else:
        soft=max(640.0,min(900.0,f(p.get("memory_soft_limit_mb"),840)))
        hard=max(800.0,min(968.0,f(p.get("memory_hard_limit_mb"),960)))
        if hard<=soft+48:hard=min(968.0,soft+64.0)
    return {
      "soft_mb":soft,
      "hard_mb":hard,
      "max_curve_control_points":max(24,min(120,int(f(p.get("max_curve_control_points"),72)))),
      "render_mesh_tolerance_mm":max(.06,min(.18,f(p.get("render_mesh_tolerance_mm"),.10)))
    }

def effective_mesh_tolerance(c):
    requested=max(.02,min(.08,f((c or {}).get("mesh_tolerance_mm"),.06)))
    family=str((c or {}).get("family") or "")
    # A1 mini 0.4 mm: 0.05 mm chord tolerance is already substantially finer
    # than printable XY detail. Keeping complex organic surfaces below this
    # multiplies tessellation RAM without improving the printed contour.
    if family=="sculpted_lidded_container":return max(.05,requested)
    if family=="universal_cad_recipe":return max(.08,requested)
    return requested

def effective_validation_mesh_tolerance(c):
    export_tol=effective_mesh_tolerance(c)
    family=str((c or {}).get("family") or "")
    # Intermediate QA mesh may be coarser because the exported 3MF is audited
    # again at export_tol. BREP validity, clearances and collisions remain CAD-based.
    if family=="sculpted_lidded_container":return max(.08,export_tol)
    if family=="universal_cad_recipe":return max(.12,export_tol)
    return export_tol

def _malloc_trim():
    try:
        libc=ctypes.CDLL("libc.so.6")
        fn=getattr(libc,"malloc_trim",None)
        if fn is not None:
            fn.argtypes=[ctypes.c_size_t];fn.restype=ctypes.c_int
            return bool(fn(0))
    except Exception:
        pass
    return False

def clear_shape_tessellation(shape):
    try:
        from OCP.BRepTools import BRepTools
        cleaner=getattr(BRepTools,"Clean_s",None) or getattr(BRepTools,"Clean",None)
        if cleaner is None:return False
        obj=shape.val().wrapped if hasattr(shape,"val") else getattr(shape,"wrapped",shape)
        cleaner(obj)
        return True
    except Exception:
        return False

def release_candidate_memory(shape=None):
    if shape is not None:
        try:clear_shape_tessellation(shape)
        except Exception:pass
    gc.collect()
    if current_rss_mb()>760:_malloc_trim()

def release_process_memory(parts=None):
    cache=False
    if parts:
        try:cache=clear_tessellation_cache(parts)
        except Exception:cache=False
    gc.collect()
    trimmed=_malloc_trim()
    return {"cache_cleaned":bool(cache),"malloc_trim":bool(trimmed),"rss_mb":current_rss_mb()}

def memory_guard(req,stage,hard=True):
    pol=resource_policy(req);rss=current_rss_mb()
    if rss>=pol["soft_mb"]:
        gc.collect();_malloc_trim();rss=current_rss_mb()
        print("memory guard",stage,"rss_mb",rss,"soft",pol["soft_mb"],"hard",pol["hard_mb"],flush=True)
    if hard and rss>=pol["hard_mb"]:
        raise MemoryError("CAD_RESOURCE_BUDGET_EXCEEDED:"+stage+":rss_mb="+str(rss))
    return rss

def clear_tessellation_cache(parts):
    """Release OCCT triangulation caches between validation and export.
    BREP geometry is preserved and will be tessellated again only when needed."""
    try:
        from OCP.BRepTools import BRepTools
        cleaner=getattr(BRepTools,"Clean_s",None) or getattr(BRepTools,"Clean",None)
        if cleaner is None:return False
        for p in parts:
            try:cleaner(p["shape"].val().wrapped)
            except Exception:pass
        gc.collect()
        return True
    except Exception:
        gc.collect()
        return False

def f(v,d=0.0):
    try:return float(v)
    except:return float(d)

def color_hex(v):
    s=str(v or "#000000").strip().lower()
    if len(s)==4 and s.startswith("#"): s="#"+s[1]*2+s[2]*2+s[3]*2
    return s if len(s)==7 and s.startswith("#") else "#000000"

def hex_rgba(s):
    s=color_hex(s)
    return [int(s[1:3],16)/255.0,int(s[3:5],16)/255.0,int(s[5:7],16)/255.0,1.0]

def rounded_box(w,d,h,r):
    r=max(0.0,min(float(r or 0),min(w,d)/2-0.01))
    s=cq.Workplane("XY").box(w,d,h,centered=(True,True,False))
    if r>0:
        try:s=s.edges("|Z").fillet(r)
        except Exception:pass
    return s

def open_box(p):
    w=f(p.get("width_mm"),80); d=f(p.get("depth_mm"),60); h=f(p.get("height_mm"),35); wall=f(p.get("wall_mm"),2.4); r=f(p.get("corner_radius_mm"),4)
    outer=rounded_box(w,d,h,r)
    inner=rounded_box(max(1,w-2*wall),max(1,d-2*wall),max(1,h-wall),max(0,r-wall)).translate((0,0,wall))
    return outer.cut(inner)

def phone_stand(p):
    width=f(p.get("width_mm"),75); depth=f(p.get("depth_mm"),80); base=f(p.get("base_thickness_mm"),5)
    back_h=f(p.get("back_height_mm"),90); back_t=f(p.get("back_thickness_mm"),5); lip_h=f(p.get("lip_height_mm"),12); lip_t=f(p.get("lip_thickness_mm"),5)
    s=cq.Workplane("XY").box(width,depth,base,centered=(True,True,False))
    back=cq.Workplane("XY").box(width,back_t,back_h,centered=(True,True,False)).translate((0,depth/2-back_t/2,base))
    lip=cq.Workplane("XY").box(width,lip_t,lip_h,centered=(True,True,False)).translate((0,-depth/2+lip_t/2,base))
    return s.union(back).union(lip)

def primitive_recipe(recipe):
    shape=None
    for op in recipe.get("primitives") or []:
        typ=op.get("type"); mode=op.get("op","add"); x=f(op.get("x_mm")); y=f(op.get("y_mm")); z=f(op.get("z_mm"))
        if typ=="box":
            obj=rounded_box(f(op["width_mm"]),f(op["depth_mm"]),f(op["height_mm"]),f(op.get("radius_mm"))).translate((x,y,z))
        elif typ=="cylinder":
            obj=cq.Workplane("XY").circle(f(op["diameter_mm"])/2).extrude(f(op["height_mm"])).translate((x,y,z))
        else: raise ValueError("unsupported primitive: "+str(typ))
        if shape is None:
            if mode=="cut": raise ValueError("first primitive cannot be cut")
            shape=obj
        else: shape=shape.cut(obj) if mode=="cut" else shape.union(obj)
    if shape is None: raise ValueError("empty recipe")
    return shape

def sample_svg_part(part,svg,tol_mm=0.08):
    vb=svg.get("view_box") or [0,0,1,1]; box=svg.get("design_box_mm") or [vb[2],vb[3]]
    vx,vy,vw,vh=[f(x) for x in vb]; dw,dh=[f(x) for x in box]
    if vw<=0 or vh<=0 or dw<=0 or dh<=0: raise ValueError("invalid SVG view/design box")
    sx,sy=dw/vw,dh/vh
    p=parse_path(str(part.get("path_d") or ""))
    subs=p.continuous_subpaths()
    polys=[]
    for sub in subs:
        if len(sub)==0: continue
        pts=[]
        for seg in sub:
            try: ln=max(float(seg.length(error=1e-4))*max(abs(sx),abs(sy)),tol_mm)
            except Exception: ln=tol_mm
            steps=max(2,min(1024,int(math.ceil(ln/tol_mm))))
            for i in range(steps):
                q=seg.point(i/steps)
                x=(q.real-vx)*sx-dw/2
                y=dh/2-(q.imag-vy)*sy
                if not pts or abs(x-pts[-1][0])>1e-7 or abs(y-pts[-1][1])>1e-7: pts.append((x,y))
        q=sub[-1].point(1.0); end=((q.real-vx)*sx-dw/2,dh/2-(q.imag-vy)*sy)
        if not pts or end!=pts[-1]:pts.append(end)
        if len(pts)<3:continue
        if pts[0]!=pts[-1]:pts.append(pts[0])
        poly=Polygon(pts)
        if not poly.is_valid:
            raise ValueError("SVG_SELF_INTERSECTION:"+json.dumps([{"part":part.get("id"),"reason":explain_validity(poly)}],ensure_ascii=False))
        if poly.area<=1e-8:continue
        polys.append(poly)
    if not polys:raise ValueError("SVG part has no closed printable area: "+str(part.get("id")))
    geom=None
    for poly in polys: geom=poly if geom is None else geom.symmetric_difference(poly)
    if geom.is_empty:raise ValueError("SVG part became empty: "+str(part.get("id")))
    return geom

def geom_to_shape(geom,z,height):
    geoms=[]
    if isinstance(geom,Polygon):geoms=[geom]
    elif isinstance(geom,MultiPolygon):geoms=list(geom.geoms)
    elif isinstance(geom,GeometryCollection):geoms=[g for g in geom.geoms if isinstance(g,Polygon)]
    else:raise ValueError("unsupported SVG polygon geometry")
    result=None
    for poly in geoms:
        ext=list(poly.exterior.coords)[:-1]
        wp=cq.Workplane("XY").workplane(offset=z).polyline(ext).close().extrude(height)
        for ring in poly.interiors:
            pts=list(ring.coords)[:-1]
            cutter=cq.Workplane("XY").workplane(offset=z-0.02).polyline(pts).close().extrude(height+0.04)
            wp=wp.cut(cutter)
        result=wp if result is None else result.union(wp)
    if result is None:raise ValueError("empty polygon extrusion")
    return result

def shape_volume(shape):
    try:return float(shape.val().Volume())
    except:return 0.0

def mesh_of(shape,tol=0.10):
    solid=shape.val()
    tol=max(.005,float(tol))
    angular=max(.035,min(.08,tol*2.0))
    verts,tris=solid.tessellate(tol,angular)
    vv=[(float(v.x),float(v.y),float(v.z)) for v in verts]
    tt=[(int(t[0]),int(t[1]),int(t[2])) for t in tris]
    return vv,tt

def weld_mesh_data(verts,tris,eps=1e-5):
    eps=max(1e-7,float(eps));key_to_new={};remap=[];out_v=[]
    for x,y,z in verts:
        key=(round(x/eps),round(y/eps),round(z/eps))
        idx=key_to_new.get(key)
        if idx is None:
            idx=len(out_v);key_to_new[key]=idx;out_v.append((float(x),float(y),float(z)))
        remap.append(idx)
    out_t=[];seen=set()
    for a,b,c in tris:
        a,b,c=remap[int(a)],remap[int(b)],remap[int(c)]
        if len({a,b,c})<3:continue
        ax,ay,az=out_v[a];bx,by,bz=out_v[b];cx,cy,cz=out_v[c]
        ux,uy,uz=bx-ax,by-ay,bz-az;vx,vy,vz=cx-ax,cy-ay,cz-az
        nx=uy*vz-uz*vy;ny=uz*vx-ux*vz;nz=ux*vy-uy*vx
        if nx*nx+ny*ny+nz*nz<=1e-16:continue
        key=tuple(sorted((a,b,c)))
        if key in seen:continue
        seen.add(key);out_t.append((a,b,c))
    return out_v,out_t

def mesh_edge_stats(verts,tris):
    edges=Counter()
    for a,b,c in tris:
        for u,v in ((a,b),(b,c),(c,a)):
            if u>v:u,v=v,u
            edges[(u,v)]+=1
    return {"open_edges":sum(1 for n in edges.values() if n==1),"nonmanifold_edges":sum(1 for n in edges.values() if n>2)}

def clean_mesh_data(raw_v,raw_t,tol=.04):
    max_eps=max(1e-5,min(5e-3,float(tol)*.10))
    candidates=sorted(set([1e-5,2e-5,5e-5,1e-4,2e-4,5e-4,1e-3,2e-3,max_eps]))
    candidates=[x for x in candidates if x<=max_eps+1e-12]
    best_score=None;best_eps=candidates[0]
    for eps in candidates:
        v,t=weld_mesh_data(raw_v,raw_t,eps);s=mesh_edge_stats(v,t)
        if s["open_edges"]==0 and s["nonmanifold_edges"]==0:
            return {"vertices":v,"triangles":t,"weld_eps_mm":eps,**s}
        # Keep only the scalar winning epsilon while searching. Retaining a
        # previous full vertex/triangle mesh doubles peak RAM on organic CAD.
        score=(1000000 if s["nonmanifold_edges"] else 0)+s["nonmanifold_edges"]*10000+s["open_edges"]
        if best_score is None or score<best_score:best_score=score;best_eps=eps
        del v,t,s
        if current_rss_mb()>700:gc.collect()
    v,t=weld_mesh_data(raw_v,raw_t,best_eps);s=mesh_edge_stats(v,t)
    return {"vertices":v,"triangles":t,"weld_eps_mm":best_eps,**s}

def shape_xy_footprint(shape,tol=.035):
    verts,tris=mesh_of(shape,tol);polys=[]
    for a,b,c in tris:
        pts=[(verts[int(i)][0],verts[int(i)][1]) for i in (a,b,c)]
        try:
            p=Polygon(pts)
            if p.is_valid and p.area>1e-8:polys.append(p)
        except Exception:pass
    if not polys:return None
    g=unary_union(polys)
    if not g.is_valid:g=g.buffer(0)
    return g

def glyph_class(ch):
    if not ch:return "other"
    o=ord(ch)
    if (0x3400<=o<=0x9fff) or (0xf900<=o<=0xfaff):return "cjk"
    if ch.isdigit():return "digit"
    if ch.isspace():return "space"
    if ch.isascii() and ch.isalpha():return "latin"
    return "symbol"

def script_tracking(script):
    s=str(script or "latin").lower()
    if s=="cjk":return TEXT_CJK_GAP_DEFAULT
    if s=="mixed":return .50
    return TEXT_LATIN_GAP_DEFAULT

def pair_tracking(base,left_cls,right_cls):
    gap=float(base)
    if left_cls=="cjk" or right_cls=="cjk":gap=max(gap,TEXT_CJK_GAP_DEFAULT)
    if left_cls=="digit" and right_cls=="digit":gap=max(gap,TEXT_LATIN_GAP_DEFAULT)
    if left_cls=="symbol" or right_cls=="symbol":gap=max(gap,.50)
    return gap

def safe_numeric_display_geometry(txt,size,tracking,line_width=TEXT_LINE_WIDTH_DEFAULT,min_stroke=TEXT_MIN_STROKE_DEFAULT):
    """Deterministic printable glyphs for numeric UI readouts such as 03:42.
    Avoids font/BRep instability for the simplest possible display text."""
    text=str(txt or "")
    if not re.fullmatch(r"\d{1,2}:\d{2}",text):raise ValueError("numeric_display_pattern")
    h=max(2.8,float(size));stroke=max(float(min_stroke)+.10,float(line_width)+.12,h*.17)
    stroke=min(stroke,h*.23);w=max(stroke*2.4,h*.56);gap=max(float(tracking),float(line_width)+.08)
    segs={
      "0":"ab cdef".replace(" ",""),"1":"bc","2":"abdeg","3":"abcdg","4":"bcfg",
      "5":"acdfg","6":"acdefg","7":"abc","8":"abcdefg","9":"abcdfg"
    }
    def rect(x0,y0,x1,y1):return shapely_box(float(x0),float(y0),float(x1),float(y1))
    def digit_geom(ch):
        s=stroke;H=h;W=w;mid=H/2.0
        pieces={
          "a":rect(s*.45,H-s,W-s*.45,H),
          "g":rect(s*.45,mid-s/2,W-s*.45,mid+s/2),
          "d":rect(s*.45,0,W-s*.45,s),
          "f":rect(0,mid-s*.15,s,H-s*.45),
          "b":rect(W-s,mid-s*.15,W,H-s*.45),
          "e":rect(0,s*.45,s,mid+s*.15),
          "c":rect(W-s,s*.45,W,mid+s*.15)
        }
        return unary_union([pieces[k] for k in segs[ch]])
    placed=[];cursor=0.0
    for ch in text:
        if ch==":":
            dot=max(stroke,0.68)
            g=unary_union([rect(0,h*.63-dot/2,dot,h*.63+dot/2),rect(0,h*.30-dot/2,dot,h*.30+dot/2)])
            adv=dot
        else:
            g=digit_geom(ch);adv=w
        minx,miny,maxx,maxy=g.bounds
        g=geom_translate(g,xoff=cursor-minx,yoff=0)
        placed.append({"char":ch,"class":glyph_class(ch),"geom":g})
        cursor+=(maxx-minx)+gap
    if not placed:raise ValueError("numeric_display_empty")
    geom=unary_union([p["geom"] for p in placed])
    minx,miny,maxx,maxy=geom.bounds
    xcenter=(minx+maxx)/2.0;ycenter=(miny+maxy)/2.0
    geom=geom_translate(geom,xoff=-xcenter,yoff=-ycenter)
    for p in placed:p["geom"]=geom_translate(p["geom"],xoff=-xcenter,yoff=-ycenter)
    clearances=[float(placed[i]["geom"].distance(placed[i+1]["geom"])) for i in range(len(placed)-1)]
    min_clear=min(clearances) if clearances else 999.0
    stroke_checks=[{"char":p["char"],"ok":bool(geometric_feature_check(p["geom"],min_stroke).get("ok"))} for p in placed]
    return {"geom":geom,"glyphs":placed,"min_clearance_mm":min_clear,"glyph_count":len(placed),
      "slicer_line_width_mm":float(line_width),"slicer_gap_margin_mm":min_clear-float(line_width),
      "slicer_no_merge_ok":bool(min_clear+1e-6>=float(line_width)),
      "glyph_stroke_ok":all(x["ok"] for x in stroke_checks),"glyph_stroke_checks":stroke_checks,
      "internal_gap_ok":True,"internal_gap_mm":999.0,"internal_gap_checks":[],
      "vector_profile":"makersence_numeric_display_v1"}

def polygon_parts(g):
    if isinstance(g,Polygon):return [g]
    if isinstance(g,MultiPolygon):return [p for p in g.geoms if not p.is_empty]
    if isinstance(g,GeometryCollection):return [p for p in g.geoms if isinstance(p,Polygon) and not p.is_empty]
    return []

def significant_counter_count(g,area_floor):
    total=0
    for p in polygon_parts(g):
        for ring in p.interiors:
            try:
                hole=Polygon(ring)
                if not hole.is_empty and hole.area>=area_floor:total+=1
            except Exception:pass
    return total

def glyph_internal_clearance(g,required,expected_counters=0):
    req=max(0.0,float(required or 0))
    if req<=1e-6:return {"ok":True,"min_gap_mm":999.0,"narrow_holes":0,"lost_counters":0,"component_gap_mm":999.0}
    parts=polygon_parts(g);component_gap=999.0
    # Detached radicals are valid CJK topology. Record their spacing, but do not
    # force a full nozzle-width gap; doing so would distort characters such as 們.
    for i in range(len(parts)):
        for j in range(i+1,len(parts)):
            d=float(parts[i].distance(parts[j]))
            if d>1e-7:component_gap=min(component_gap,d)
    area_floor=max(.02,req*req*.15);actual_counters=significant_counter_count(g,area_floor)
    lost=max(0,int(expected_counters or 0)-actual_counters)
    # Enclosed counters/channels are the critical anti-merge gate.
    narrow=0
    for p in parts:
        for ring in p.interiors:
            try:
                hole=Polygon(ring)
                if hole.is_empty or hole.area<area_floor:continue
                if hole.buffer(-req/2.0,join_style=1,resolution=8).is_empty:narrow+=1
            except Exception:narrow+=1
    ok=narrow==0 and lost==0
    return {"ok":ok,"min_gap_mm":req if ok else 0.0,"narrow_holes":narrow,"lost_counters":lost,
            "counter_count":actual_counters,"component_gap_mm":round(component_gap,3) if component_gap<900 else 999.0}

def spaced_text_geometry(txt,size,h,font,kind,boost,tracking,script,line_width=TEXT_LINE_WIDTH_DEFAULT,min_stroke=TEXT_MIN_STROKE_DEFAULT,min_internal_gap=0.0):
    placed=[];cursor=0.0;prev_cls=None;prev_max=None;internal_reports=[];glyph_strokes=[]
    space_advance=max(.9,float(size)*.34)
    for ch in str(txt):
        cls=glyph_class(ch)
        if cls=="space":
            cursor=(prev_max if prev_max is not None else cursor)+space_advance
            prev_max=cursor;prev_cls="space"
            continue
        raw=cq.Workplane("XY").text(ch,float(size),float(h),font=str(font),kind=str(kind),halign="center",valign="center",combine=True,clean=True)
        if shape_volume(raw)<=.0001:
            release_candidate_memory(raw)
            raise ValueError("missing_or_zero_glyph:"+repr(ch))
        gg=shape_xy_footprint(raw,.06)
        release_candidate_memory(raw);del raw
        if gg is None or gg.is_empty:raise ValueError("empty_glyph_footprint:"+repr(ch))
        raw_gg=gg
        counter_req=min_internal_gap if cls=="cjk" else 0.0
        expected_counters=significant_counter_count(raw_gg,max(.02,float(counter_req or 0)**2*.15)) if counter_req>0 else 0
        if boost>0:
            gg=gg.buffer(float(boost),join_style=1,resolution=8)
            if gg.is_empty:raise ValueError("glyph_bolden_empty:"+repr(ch))
        stroke=geometric_feature_check(gg,min_stroke);glyph_strokes.append({"char":ch,"ok":bool(stroke.get("ok"))})
        internal=glyph_internal_clearance(gg,counter_req,expected_counters);internal_reports.append({"char":ch,**internal})
        minx,miny,maxx,maxy=gg.bounds
        if prev_max is None:
            xoff=-minx
        else:
            gap=pair_tracking(tracking,prev_cls,cls)
            xoff=(prev_max+gap)-minx
        gg=geom_translate(gg,xoff=xoff,yoff=0)
        _,_,gx1,_=gg.bounds
        placed.append({"char":ch,"class":cls,"geom":gg})
        prev_max=gx1;prev_cls=cls
    if not placed:raise ValueError("text_has_no_printable_glyphs")
    geom=unary_union([g["geom"] for g in placed])
    if geom.is_empty:raise ValueError("text_union_empty")
    clearances=[]
    for i in range(len(placed)-1):
        d=float(placed[i]["geom"].distance(placed[i+1]["geom"]))
        clearances.append(d)
    minx,miny,maxx,maxy=geom.bounds
    geom=geom_translate(geom,xoff=float(0-(minx+maxx)/2),yoff=float(0-(miny+maxy)/2))
    for p in placed:
        p["geom"]=geom_translate(p["geom"],xoff=float(0-(minx+maxx)/2),yoff=float(0-(miny+maxy)/2))
    min_clearance=min(clearances) if clearances else 999.0
    slicer_margin=min_clearance-float(line_width) if min_clearance<900 else 999.0
    finite_internal=[x["min_gap_mm"] for x in internal_reports if x["min_gap_mm"]<900]
    internal_min=min(finite_internal) if finite_internal else 999.0
    return {"geom":geom,"glyphs":placed,"min_clearance_mm":min_clearance,"glyph_count":len(placed),
            "slicer_line_width_mm":float(line_width),"slicer_gap_margin_mm":slicer_margin,
            "slicer_no_merge_ok":bool(slicer_margin>=-1e-6),
            "glyph_stroke_ok":all(x["ok"] for x in glyph_strokes),"glyph_stroke_checks":glyph_strokes,
            "internal_gap_ok":all(x["ok"] for x in internal_reports),"internal_gap_mm":internal_min,
            "internal_gap_checks":internal_reports}

def topology(shape,tol=0.10):
    raw_v,raw_t=mesh_of(shape,tol)
    cleaned=clean_mesh_data(raw_v,raw_t,tol)
    verts,tris=cleaned["vertices"],cleaned["triangles"]
    removed=max(0,len(raw_t)-len(tris))
    return {"vertices":len(raw_v),"welded_vertices":len(verts),"triangles":len(tris),"mesh_cleanup_removed_triangles":removed,"mesh_weld_eps_mm":cleaned["weld_eps_mm"],"degenerate_triangles":0,"open_edges":cleaned["open_edges"],"nonmanifold_edges":cleaned["nonmanifold_edges"]}

def mesh_validation_summaries(parts,tol=.06):
    topo=[];bounds=[];h=hashlib.sha256()
    for p in parts:
        raw_v,raw_t=mesh_of(p["shape"],tol)
        cleaned=clean_mesh_data(raw_v,raw_t,tol)
        v,t=cleaned["vertices"],cleaned["triangles"]
        if v:
            mn=[float("inf")]*3;mx=[float("-inf")]*3
            for q in v:
                for i in range(3):mn[i]=min(mn[i],q[i]);mx[i]=max(mx[i],q[i])
        else:
            mn=[0,0,0];mx=[0,0,0]
        dm=[mx[i]-mn[i] for i in range(3)]
        topo.append({"vertices":len(raw_v),"welded_vertices":len(v),"triangles":len(t),"mesh_cleanup_removed_triangles":max(0,len(raw_t)-len(t)),"mesh_weld_eps_mm":cleaned["weld_eps_mm"],"degenerate_triangles":0,"open_edges":cleaned["open_edges"],"nonmanifold_edges":cleaned["nonmanifold_edges"]})
        bounds.append({"part":p["name"],"min":[round(x,3) for x in mn],"max":[round(x,3) for x in mx],"dimensions":[round(x,3) for x in dm]})
        h.update(str(p.get("name","")).encode());h.update(color_hex(p.get("color")).encode())
        for x,y,z in v:h.update(("%.6f,%.6f,%.6f;"%(x,y,z)).encode())
        for a,b,c0 in t:h.update(("%d,%d,%d;"%(a,b,c0)).encode())
        del raw_v,raw_t,v,t,cleaned
        if current_rss_mb()>700:gc.collect()
    return topo,bounds,h.hexdigest()

def aggregate_bbox(parts):
    boxes=[p["shape"].val().BoundingBox() for p in parts]
    return [min(b.xmin for b in boxes),min(b.ymin for b in boxes),min(b.zmin for b in boxes),max(b.xmax for b in boxes),max(b.ymax for b in boxes),max(b.zmax for b in boxes)]

def dims_from_bbox(bb):return [round(bb[3]-bb[0],3),round(bb[4]-bb[1],3),round(bb[5]-bb[2],3)]

def build_svg_plate(req):
    svg=req.get("svg_artifact") or {}; contract=req.get("cad_contract") or {}; parts=svg.get("parts") or []
    manifest={str(x.get("name") or ""):x for x in (contract.get("part_manifest") or [])}
    if not parts:raise ValueError("svg_artifact.parts missing")
    thickness=max(MIN_FEATURE_DEFAULT,f(contract.get("thickness_mm"),2.4))
    geoms=[]; invalid=[]
    for part in parts:
        geom=sample_svg_part(part,svg)
        if not geom.is_valid: invalid.append({"part":part.get("id"),"reason":explain_validity(geom)})
        geoms.append((part,geom))
    if invalid:
        raise ValueError("SVG_SELF_INTERSECTION:"+json.dumps(invalid,ensure_ascii=False))
    silhouettes=[g for p,g in geoms if p.get("role")=="silhouette"]
    if not silhouettes:raise ValueError("no silhouette SVG part")
    silhouette=unary_union(silhouettes)
    base=geom_to_shape(silhouette,0,thickness)
    hole_tools=[]; out_parts=[]
    for p,g in geoms:
        role=p.get("role")
        if role=="through_hole":
            tool=geom_to_shape(g,-1,thickness+2+f(contract.get("hole_cut_extra_mm"),0))
            hole_tools.append((p,tool));base=base.cut(tool)
        elif role in ("pocket","engrave"):
            dep=min(thickness-.01,max(.05,f(p.get("height_mm"),.5)))
            tool=geom_to_shape(g,thickness-dep,dep+.05);base=base.cut(tool)
        elif role=="inlay":
            dep=min(thickness-.01,max(.05,f(p.get("height_mm"),.5)))
            tool=geom_to_shape(g,thickness-dep,dep+.02);base=base.cut(tool)
            ins=geom_to_shape(g,thickness-dep,dep)
            nm=str(p.get("id") or "inlay");pm=manifest.get(nm,{})
            out_parts.append({"name":nm,"role":str(pm.get("role") or "inlay"),"physical_separate":bool(pm.get("physical_separate",False)),"editable_separate":bool(pm.get("editable_separate",True)),"color":color_hex(p.get("color")),"shape":ins,"geom":g,"min_feature_mm":p.get("min_feature_mm")})
        elif role=="emboss":
            h=max(.05,f(p.get("height_mm"),.5)); z=thickness+f(p.get("z_mm"),0)
            emb=geom_to_shape(g,z,h)
            nm=str(p.get("id") or "emboss");pm=manifest.get(nm,{})
            out_parts.append({"name":nm,"role":str(pm.get("role") or "emboss"),"physical_separate":bool(pm.get("physical_separate",False)),"editable_separate":bool(pm.get("editable_separate",True)),"color":color_hex(p.get("color")),"shape":emb,"geom":g,"min_feature_mm":p.get("min_feature_mm")})
    for hd in contract.get("holes") or []:
        x=f(hd.get("x_mm"));y=f(hd.get("y_mm"));dia=f(hd.get("diameter_mm"),4);through=bool(hd.get("through",True))
        if through:
            extra=f(contract.get("hole_cut_extra_mm"),0)
            tool=cq.Workplane("XY").workplane(offset=-1-extra).center(x,y).circle(dia/2).extrude(thickness+2+2*extra)
            base=base.cut(tool);hole_tools.append(({"id":hd.get("name") or "cad_hole"},tool))
    for pk in contract.get("pockets") or []:
        x=f(pk.get("x_mm"));y=f(pk.get("y_mm"));dia=f(pk.get("diameter_mm"),0);pw=f(pk.get("width_mm"),0);ph=f(pk.get("height_mm"),0)
        dep=min(thickness-.01,max(.05,f(pk.get("depth_mm"),1)));shape=str(pk.get("shape") or ("rect" if pw>0 and ph>0 else "circle")).lower()
        side=str(pk.get("side") or "front").lower()
        z0=-.05 if side=="back" else thickness-dep
        if shape=="rect" and pw>0 and ph>0:
            tool=cq.Workplane("XY").workplane(offset=z0).center(x,y).rect(pw,ph).extrude(dep+.05)
            base=base.cut(tool)
        elif dia>0:
            tool=cq.Workplane("XY").workplane(offset=z0).center(x,y).circle(dia/2).extrude(dep+.05)
            base=base.cut(tool)
    base_color=next((color_hex(p.get("color")) for p,g in geoms if p.get("role")=="silhouette"),"#000000")
    bpm=manifest.get("BODY",{})
    result=[{"name":"BODY","role":str(bpm.get("role") or "main_body"),"physical_separate":True,"editable_separate":True,"color":base_color,"shape":base,"geom":silhouette,"min_feature_mm":contract.get("wall_mm") or thickness}]
    result.extend(out_parts)
    # Typography V2: construct each glyph independently, bolden each glyph before
    # placement, then enforce script-aware clearance. This prevents boldening from
    # welding Chinese/Latin/digit neighbours into one unreadable mass.
    for tf in contract.get("text_features") or []:
        name=str(tf.get("name") or "TEXT");txt=str(tf.get("text") or "").strip()
        if not txt:raise ValueError("empty text feature: "+name)
        print("text build start",name,"chars",len(txt),"rss_mb",current_rss_mb(),flush=True)
        requested_size=max(2.5,f(tf.get("size_mm"),5));min_size=max(2.5,min(requested_size,f(tf.get("min_size_mm"),requested_size)))
        h=max(.2,f(tf.get("height_mm"),.4));x=f(tf.get("cad_x_mm"));y=f(tf.get("cad_y_mm"));z=f(tf.get("z_mm"),0)
        script=str(tf.get("script") or "latin").lower()
        # Typography is a manufacturing exception to the structural 0.8 mm floor:
        # 0.55 mm strokes are allowed for 0.4 mm PLA text, while body/walls remain >=0.8 mm.
        min_stroke=max(TEXT_MIN_STROKE_DEFAULT,f(tf.get("min_stroke_mm"),TEXT_MIN_STROKE_DEFAULT))
        line_width=max(.35,min(.60,f(tf.get("slicer_line_width_mm"),TEXT_LINE_WIDTH_DEFAULT)))
        default_clear=TEXT_CJK_GAP_DEFAULT if script=="cjk" else (.50 if script=="mixed" else TEXT_LATIN_GAP_DEFAULT)
        min_clear=max(line_width,f(tf.get("min_glyph_clearance_mm"),default_clear))
        min_internal=max(line_width,f(tf.get("min_internal_gap_mm"),TEXT_CJK_INTERNAL_GAP_DEFAULT)) if script in ("cjk","mixed") else 0.0
        tracking=max(line_width,min(.90,f(tf.get("tracking_mm"),script_tracking(script))))
        max_w=max(1.0,f(tf.get("max_width_mm"),999));max_h=max(1.0,f(tf.get("max_height_mm"),999));max_bolden=max(0.0,min(.40,f(tf.get("max_bolden_mm"),.24)))
        preferred_bolden=max(0.0,min(max_bolden,f(tf.get("preferred_bolden_mm"),.06)))
        candidates=tf.get("font_candidates") or [tf.get("font") or "DejaVu Sans"];requested_kind=str(tf.get("font_kind") or "bold")
        # Dense CJK starts from regular outlines. Bold is fallback only when regular
        # cannot meet the 0.55 mm solid-stroke gate without closing internal voids.
        kind_candidates=["regular","bold"] if script in ("cjk","mixed") else [requested_kind]
        chosen=None;errors=[]
        # Numeric UI readouts are basic geometry, not a typography stress test.
        # Build a deterministic printable vector first; only fall back to installed fonts
        # when the content is not a supported numeric display.
        if re.fullmatch(r"\d{1,2}:\d{2}",txt):
            try:
                built=safe_numeric_display_geometry(txt,requested_size,tracking,line_width=line_width,min_stroke=min_stroke)
                gg=geom_translate(built["geom"],xoff=x,yoff=y)
                minx,miny,maxx,maxy=gg.bounds;bw=maxx-minx;bh=maxy-miny
                stroke=geometric_feature_check(gg,min_stroke)
                clearance_ok=built["min_clearance_mm"]+1e-6>=min_clear
                slicer_no_merge_ok=bool(built.get("slicer_no_merge_ok")) and f(built.get("slicer_gap_margin_mm"),-1)>=-1e-6
                internal_ok=bool(built.get("internal_gap_ok")) and bool(built.get("glyph_stroke_ok"))
                layout_ok=bw<=max_w+1e-6 and bh<=max_h+1e-6
                if layout_ok and clearance_ok and slicer_no_merge_ok and internal_ok and stroke["ok"]:
                    shp=geom_to_shape(gg,thickness+z,h)
                    tt=topology(shp,f(contract.get("mesh_tolerance_mm"),.04))
                    text_mesh_ok=int(tt.get("open_edges",0))==0 and int(tt.get("nonmanifold_edges",0))==0 and int(tt.get("degenerate_triangles",0))==0
                    if text_mesh_ok:
                        chosen={"shape":shp,"geom":gg,"font":"MakerSence Numeric Safe","font_kind":"vector","size_mm":requested_size,"bolden_mm":0.0,"tracking_mm":tracking,
                            "glyph_clearance_mm":round(float(built["min_clearance_mm"]),3),"glyph_count":built["glyph_count"],
                            "internal_gap_mm":999.0,"internal_gap_ok":True,"internal_gap_checks":[],
                            "slicer_line_width_mm":round(float(built["slicer_line_width_mm"]),3),"slicer_gap_margin_mm":round(float(built["slicer_gap_margin_mm"]),3),
                            "slicer_no_merge_ok":True,"mesh_topology":tt,
                            "bounds_mm":[round(bw,3),round(bh,3)],"stroke_check":stroke,"layout_ok":True,"clearance_ok":True,
                            "vector_profile":built.get("vector_profile")}
                    else:errors.append("numeric_safe:mesh_open="+str(tt.get("open_edges",0))+",nonmanifold="+str(tt.get("nonmanifold_edges",0)))
                else:errors.append("numeric_safe:layout_clearance_or_stroke")
            except Exception as ex:
                errors.append("numeric_safe:"+str(ex))
        if script in ("cjk","mixed"):
            boosts=sorted(set(round(v,3) for v in [0,preferred_bolden,.06,.10,max_bolden] if v<=max_bolden+1e-9))
        else:
            boosts=sorted(set(round(v,3) for v in [0,preferred_bolden,min(max_bolden,preferred_bolden+.08),max_bolden] if v<=max_bolden+1e-9))
        sizes=[]
        if not chosen:
            # Bounded search: typography fallback must never become an unbounded CAD workload.
            # Try the requested size first, then a few meaningful reductions down to min_size.
            span=max(0.0,requested_size-min_size)
            sizes=sorted(set(round(max(min_size,requested_size-frac*span),2) for frac in [0,.33,.66,1.0]),reverse=True)
        for size_try in sizes:
            for font in candidates:
                for kind_try in kind_candidates:
                    try:
                        for boost in boosts:
                            built=spaced_text_geometry(txt,size_try,h,font,kind_try,boost,tracking,script,line_width=line_width,min_stroke=min_stroke,min_internal_gap=min_internal)
                            gg=geom_translate(built["geom"],xoff=x,yoff=y)
                            if gg.is_empty:continue
                            minx,miny,maxx,maxy=gg.bounds;bw=maxx-minx;bh=maxy-miny
                            stroke=geometric_feature_check(gg,min_stroke)
                            clearance_ok=built["min_clearance_mm"]+1e-6>=min_clear
                            slicer_no_merge_ok=bool(built.get("slicer_no_merge_ok")) and f(built.get("slicer_gap_margin_mm"),-1)>=-1e-6
                            internal_ok=bool(built.get("internal_gap_ok")) and bool(built.get("glyph_stroke_ok"))
                            layout_ok=bw<=max_w+1e-6 and bh<=max_h+1e-6
                            if layout_ok and clearance_ok and slicer_no_merge_ok and internal_ok and stroke["ok"]:
                                shp=geom_to_shape(gg,thickness+z,h)
                                # A glyph can pass 2D readability yet still tessellate into
                                # an open 3D mesh (observed on some mono digits such as 01:18).
                                # Reject that candidate here and try the next boost/font before
                                # the expensive full export/QA pipeline.
                                tt=topology(shp,f(contract.get("mesh_tolerance_mm"),.04))
                                text_mesh_ok=int(tt.get("open_edges",0))==0 and int(tt.get("nonmanifold_edges",0))==0 and int(tt.get("degenerate_triangles",0))==0
                                if not text_mesh_ok:
                                    errors.append(str(font)+"/"+kind_try+"@"+str(size_try)+"+"+str(boost)+":mesh_open="+str(tt.get("open_edges",0))+",nonmanifold="+str(tt.get("nonmanifold_edges",0)))
                                    release_candidate_memory(shp);del shp,tt
                                    continue
                                chosen={"shape":shp,"geom":gg,"font":str(font),"font_kind":kind_try,"size_mm":size_try,"bolden_mm":boost,"tracking_mm":tracking,
                                    "glyph_clearance_mm":round(float(built["min_clearance_mm"]),3),"glyph_count":built["glyph_count"],
                                    "internal_gap_mm":round(float(built.get("internal_gap_mm",999)),3),"internal_gap_ok":internal_ok,"internal_gap_checks":built.get("internal_gap_checks",[]),
                                    "slicer_line_width_mm":round(float(built["slicer_line_width_mm"]),3),"slicer_gap_margin_mm":round(float(built["slicer_gap_margin_mm"]),3),
                                    "slicer_no_merge_ok":slicer_no_merge_ok,"mesh_topology":tt,
                                    "bounds_mm":[round(bw,3),round(bh,3)],"stroke_check":stroke,"layout_ok":True,"clearance_ok":True}
                                break
                        if chosen:break
                        errors.append(str(font)+"/"+kind_try+"@"+str(size_try)+":layout_clearance_internal_or_stroke")
                    except Exception as ex:
                        errors.append(str(font)+"/"+kind_try+"@"+str(size_try)+":"+str(ex))
                if chosen:break
            if chosen:break
        if not chosen:
            raise ValueError("TEXT_FIT_CLEARANCE_OR_STROKE_FAILED:"+name+":"+json.dumps(errors[-18:],ensure_ascii=False))
        pm=manifest.get(name,{})
        text_meta={"text":txt,"font_role":tf.get("font_role"),"font_used":chosen["font"],"font_kind":chosen["font_kind"],
            "requested_size_mm":requested_size,"size_mm":chosen["size_mm"],"min_size_mm":min_size,
            "auto_bolden_mm":chosen["bolden_mm"],"tracking_mm":chosen["tracking_mm"],"glyph_count":chosen["glyph_count"],
            "glyph_clearance_mm":chosen["glyph_clearance_mm"],"min_glyph_clearance_mm":min_clear,
            "internal_gap_mm":chosen["internal_gap_mm"],"min_internal_gap_mm":min_internal,"internal_gap_ok":bool(chosen["internal_gap_ok"]),"internal_gap_checks":chosen["internal_gap_checks"],
            "slicer_line_width_mm":chosen["slicer_line_width_mm"],"slicer_gap_margin_mm":chosen["slicer_gap_margin_mm"],
            "slicer_no_merge_ok":bool(chosen["slicer_no_merge_ok"]),"wall_generator":str(tf.get("wall_generator") or "arachne"),
            "bounds_mm":chosen["bounds_mm"],"max_bounds_mm":[max_w,max_h],"min_stroke_mm":min_stroke,"script":script,
            "min_stroke_ok":bool(chosen["stroke_check"]["ok"]),"glyph_clearance_ok":bool(chosen["clearance_ok"]),
            "layout_bounds_ok":bool(chosen["layout_ok"]),"text_mesh_topology":chosen.get("mesh_topology",{}),"text_mesh_topology_ok":True,
            "text_readability_ok":bool(chosen["stroke_check"]["ok"] and chosen["clearance_ok"] and chosen["internal_gap_ok"] and chosen["slicer_no_merge_ok"] and chosen["layout_ok"])}
        result.append({"name":name,"role":str(pm.get("role") or tf.get("role") or "custom_text"),"physical_separate":bool(pm.get("physical_separate",False)),"editable_separate":bool(pm.get("editable_separate",True)),"color":color_hex(tf.get("color") or "#ffffff"),"shape":chosen["shape"],"geom":chosen["geom"],"min_feature_mm":min_stroke,"text_meta":text_meta})
        print("text build PASS",name,"font",chosen["font"],"size",chosen["size_mm"],"rss_mb",current_rss_mb(),flush=True)
    # True detachable / separately printed CAD parts. Print transform and assembly transform are distinct:
    # 3MF/STL/GLB use print placement; product render reassembles the exact same formal meshes.
    for sp in contract.get("separate_parts") or []:
        kind=str(sp.get("kind") or "");name=str(sp.get("name") or "PART");role=str(sp.get("role") or "part")
        h=max(MIN_FEATURE_DEFAULT,f(sp.get("thickness_mm"),1.2));px=f(sp.get("print_x_mm"));py=f(sp.get("print_y_mm"))
        shape=None
        if kind=="rect_frame":
            ow=f(sp.get("outer_width_mm"),32);oh=f(sp.get("outer_height_mm"),32);iw=f(sp.get("inner_width_mm"),30);ih=f(sp.get("inner_height_mm"),30)
            if not (ow>iw>0 and oh>ih>0):raise ValueError("invalid rect_frame dimensions: "+name)
            outer=cq.Workplane("XY").rect(ow,oh).extrude(h)
            inner=cq.Workplane("XY").workplane(offset=-.05).rect(iw,ih).extrude(h+.1)
            shape=outer.cut(inner).translate((px,py,0))
        elif kind=="disc":
            dia=f(sp.get("diameter_mm"),25)
            if dia<=0:raise ValueError("invalid disc diameter: "+name)
            shape=cq.Workplane("XY").circle(dia/2).extrude(h).translate((px,py,0))
        else:
            raise ValueError("unsupported separate part: "+kind)
        at=sp.get("assembly_translate") or [0,0,0]
        if not isinstance(at,list) or len(at)!=3:at=[0,0,0]
        result.append({"name":name,"role":role,"physical_separate":True,"editable_separate":True,"color":color_hex(sp.get("color") or base_color),"shape":shape,"geom":None,"min_feature_mm":f(sp.get("min_feature_mm"),MIN_FEATURE_DEFAULT),"assembly_translate":[f(at[0]),f(at[1]),f(at[2])]})
    return result,geoms,hole_tools,invalid

def _loft_ellipse(width,depth,height,bottom_scale=.72,top_scale=.78):
    width=max(8.0,f(width,80));depth=max(8.0,f(depth,width));height=max(4.0,f(height,40))
    wp=cq.Workplane("XY").ellipse(width*bottom_scale/2,depth*bottom_scale/2)
    wp=wp.workplane(offset=height*.30).ellipse(width*.49,depth*.49)
    wp=wp.workplane(offset=height*.42).ellipse(width*.50,depth*.50)
    wp=wp.workplane(offset=height*.28).ellipse(width*top_scale/2,depth*top_scale/2)
    return wp.loft(combine=True)

def _flute_shape(shape,width,depth,height,count=8,strength=.055):
    count=max(6,min(12,int(count or 8)));rr=min(width,depth)*strength
    if rr<=.6:return shape
    out=shape
    for i in range(count):
        a=2*math.pi*i/count
        cx=math.cos(a)*width*.48;cy=math.sin(a)*depth*.48
        tool=cq.Workplane("XY").center(cx,cy).circle(rr).extrude(height+2)
        try:out=out.cut(tool)
        except Exception:pass
    return out

def _clip_ellipse_envelope(shape,width,depth,height):
    limit=cq.Workplane("XY").ellipse(max(.1,width/2),max(.1,depth/2)).extrude(max(.1,height))
    return shape.intersect(limit)

def _seat_on_z0(shape):
    # Seating is a BREP bounds operation, not a mesh operation. Avoid an
    # unnecessary full tessellation before formal mesh QA on organic solids.
    try:
        bb=shape.val().BoundingBox()
        z0=float(bb.zmin)
        return shape.translate((0,0,-z0)) if abs(z0)>1e-6 else shape
    except Exception:
        return shape

def _lobed_profile_points(width,depth,lobes=8,amp=.07,samples=72,scale=1.0,phase=0.0,max_points=72):
    width=max(8.0,f(width,80));depth=max(8.0,f(depth,width));lobes=max(4,min(16,int(lobes or 8)))
    amp=max(0.0,min(.16,float(amp)));max_points=max(24,min(120,int(max_points or 72)));samples=max(24,min(max_points,int(samples or max_points)))
    pts=[]
    denom=1.0+amp
    for i in range(samples):
        a=2*math.pi*i/samples
        mod=(1.0+amp*math.cos(lobes*a+phase))/denom
        pts.append((math.cos(a)*width*.5*scale*mod,math.sin(a)*depth*.5*scale*mod))
    return pts

_LOBED_BBOX_COMP_CACHE={}
def _lobed_bbox_compensation(lobes,amp,phase,samples,max_points):
    key=(int(lobes),round(float(amp),6),round(float(phase),6),int(samples),int(max_points))
    hit=_LOBED_BBOX_COMP_CACHE.get(key)
    if hit:return hit
    nominal=100.0
    pts=_lobed_profile_points(nominal,nominal,lobes,amp,samples,1.0,phase,max_points)
    wire=cq.Workplane("XY").spline(pts,periodic=True,makeWire=True).val()
    bb=wire.BoundingBox();xlen=max(.001,float(bb.xmax-bb.xmin));ylen=max(.001,float(bb.ymax-bb.ymin))
    comp=(nominal/xlen,nominal/ylen)
    _LOBED_BBOX_COMP_CACHE[key]=comp
    return comp

def _lobed_loft(width,depth,sections,lobes=8,amp=.07,phase=0.0,max_points=72):
    sections=sorted(sections,key=lambda x:x[0])
    if len(sections)<2:raise ValueError("lobed loft needs >=2 sections")
    samples=max(36,min(int(max_points or 72),int(max(36,lobes*5))))
    # Periodic splines can overshoot control points, and even lobe counts such as
    # 10 can place a trough on one Cartesian axis. Compensate using the actual
    # spline wire bounds so width/depth remain contract dimensions.
    _,max_sc,max_aa=max(sections,key=lambda x:f(x[1],1))
    cx,cy=_lobed_bbox_compensation(lobes,amp*f(max_aa,1),phase,samples,max_points)
    width=f(width)*cx;depth=f(depth)*cy
    z0,sc0,a0=sections[0]
    wp=cq.Workplane("XY").workplane(offset=f(z0)).spline(_lobed_profile_points(width,depth,lobes,amp*f(a0,1),samples,f(sc0,1),phase,max_points),periodic=True,makeWire=True)
    last=f(z0)
    for z,sc,aa in sections[1:]:
        z=f(z);wp=wp.workplane(offset=z-last).spline(_lobed_profile_points(width,depth,lobes,amp*f(aa,1),samples,f(sc,1),phase,max_points),periodic=True,makeWire=True);last=z
    return wp.loft(combine=True,ruled=False)

def sculpted_lidded_container(c):
    p=c.get("family_parameters") or {}
    w=f(c.get("width_mm"),128);d=f(c.get("height_mm"),123);body_h=f(p.get("body_height_mm"),62)
    floor=f(p.get("floor_mm"),3.2);lobes=int(f(p.get("lobe_count"),8))
    ow=f(p.get("opening_width_mm"),w*.66);od=f(p.get("opening_depth_mm"),d*.66)
    lw=f(p.get("lid_width_mm"),w*.80);ld=f(p.get("lid_depth_mm"),d*.80);lh=f(p.get("lid_height_mm"),13)
    lid_shell=max(2.2,f(p.get("lid_shell_mm"),2.8));lid_skirt=max(2.2,f(p.get("lid_skirt_mm"),2.8));lid_eng=max(3.0,f(p.get("lid_engagement_depth_mm"),4.0))
    sw=f(p.get("stem_width_mm"),min(w,d)*.14);sd=f(p.get("stem_depth_mm"),sw*.82);sh=f(p.get("stem_height_mm"),9)
    stem_joint_d=max(5.0,f(p.get("stem_joint_diameter_mm"),6.4));stem_eng=max(3.0,f(p.get("stem_engagement_mm"),4.0));stem_clear=max(.15,f(p.get("stem_joint_clearance_mm"),.22))
    clear=max(.20,f(p.get("lid_radial_clearance_mm"),f(c.get("xy_clearance_mm"),.25)));wall=max(2.4,f(c.get("wall_mm"),2.8))
    theme=str(p.get("theme_id") or "")
    pumpkin=theme in ("halloween_pumpkin","pumpkin_autumn")
    amp=.075 if pumpkin else .055
    rp=c.get("resource_policy") or {}
    max_curve_points=max(24,min(120,int(f(rp.get("max_curve_control_points"),72))))
    # Organic gourd body: broad equator, compressed poles, true periodic B-spline lobes.
    body=_lobed_loft(w,d,[(0,.74,.55),(body_h*.16,.92,.90),(body_h*.45,1.0,1.0),(body_h*.71,.97,1.0),(body_h*.90,.86,.82),(body_h,.76,.55)],lobes,amp,max_points=max_curve_points)
    cavity=cq.Workplane("XY").workplane(offset=floor).ellipse(ow/2,od/2).extrude(body_h-floor+1.0)
    body=body.cut(cavity)
    # Lid: same lobe phase as body. The seam lands at the pumpkin shoulder and the skirt enters the body opening.
    cap=_lobed_loft(lw,ld,[(lid_eng,1.0,.95),(lid_eng+lh*.40,.96,.90),(lid_eng+lh*.72,.82,.75),(lid_eng+lh,.60,.40)],lobes,amp,max_points=max_curve_points)
    inner=_lobed_loft(max(20,lw-2*lid_shell),max(20,ld-2*lid_shell),[(lid_eng-.05,.98,.85),(lid_eng+lh*.38,.94,.80),(lid_eng+max(1.0,lh-lid_shell),.65,.45)],lobes,max(.035,amp*.8),max_points=max_curve_points)
    lid=cap.cut(inner)
    skirt_ow=max(20,ow-2*clear);skirt_od=max(20,od-2*clear)
    if skirt_ow<=2*lid_skirt+2 or skirt_od<=2*lid_skirt+2:raise ValueError("lid skirt too thick for opening")
    skirt_outer=cq.Workplane("XY").ellipse(skirt_ow/2,skirt_od/2).extrude(lid_eng)
    skirt_inner=cq.Workplane("XY").workplane(offset=-.05).ellipse((skirt_ow-2*lid_skirt)/2,(skirt_od-2*lid_skirt)/2).extrude(lid_eng+.1)
    lid=lid.union(skirt_outer.cut(skirt_inner))
    # Reinforced socket under the stem so the visible top shell is not weakened by the detachable stem.
    boss_h=stem_eng+max(2.2,lid_shell*.9);boss_d=stem_joint_d+max(5.0,lid_shell*2.0)
    boss_z=lid_eng+lh-boss_h
    lid=lid.union(cq.Workplane("XY").workplane(offset=boss_z).circle(boss_d/2).extrude(boss_h))
    socket_d=stem_joint_d+2*stem_clear
    socket_z=lid_eng+lh-stem_eng-.15
    lid=lid.cut(cq.Workplane("XY").workplane(offset=socket_z).circle(socket_d/2).extrude(stem_eng+.30))
    # Stem: printable peg + gently leaning organic grip. The peg gives a real locating/adhesive joint.
    peg=cq.Workplane("XY").circle(stem_joint_d/2).extrude(stem_eng)
    visible=(cq.Workplane("XY").workplane(offset=stem_eng).ellipse(sw*.48,sd*.48)
             .workplane(offset=sh*.45).center(sw*.035,0).ellipse(sw*.38,sd*.38)
             .workplane(offset=sh*.35).center(sw*.055,0).ellipse(sw*.29,sd*.29)
             .workplane(offset=sh*.20).center(sw*.05,0).ellipse(sw*.19,sd*.19).loft(combine=True,ruled=False))
    stem=peg.union(visible)
    body=_seat_on_z0(body);lid=_seat_on_z0(lid);stem=_seat_on_z0(stem)
    body_color=color_hex(p.get("body_color") or "#d77a2b");lid_color=color_hex(p.get("lid_color") or body_color);stem_color=color_hex(p.get("stem_color") or "#4f6a39")
    return [
      {"name":"BODY","role":"main_body","physical_separate":True,"editable_separate":True,"color":body_color,"shape":body,"geom":None,"min_feature_mm":wall,"assembly_translate":[0,0,0]},
      {"name":"LID","role":"back_cover","physical_separate":True,"editable_separate":True,"color":lid_color,"shape":lid,"geom":None,"min_feature_mm":min(lid_shell,lid_skirt),"assembly_translate":[0,0,body_h-lid_eng]},
      {"name":"STEM","role":"insert","physical_separate":True,"editable_separate":True,"color":stem_color,"shape":stem,"geom":None,"min_feature_mm":max(.8,min(stem_joint_d,sw,sd)*.45),"assembly_translate":[0,0,body_h+lh-stem_eng]}
    ]

def _u_signed_area(loop):
    pts=list(loop or [])
    if len(pts)<3:return 0.0
    return sum(f(pts[i][0])*f(pts[(i+1)%len(pts)][1])-f(pts[(i+1)%len(pts)][0])*f(pts[i][1]) for i in range(len(pts)))/2.0

def _u_resample_loop(loop,count=56):
    pts=[[f(p[0]),f(p[1])] for p in (loop or []) if isinstance(p,(list,tuple)) and len(p)>=2]
    if len(pts)<3:raise ValueError("UNIVERSAL_RECIPE_LOOP_TOO_SMALL")
    if math.hypot(pts[0][0]-pts[-1][0],pts[0][1]-pts[-1][1])<1e-6:pts=pts[:-1]
    if len(pts)<3:raise ValueError("UNIVERSAL_RECIPE_LOOP_TOO_SMALL")
    if _u_signed_area(pts)<0:pts=list(reversed(pts))
    cx=sum(p[0] for p in pts)/len(pts);cy=sum(p[1] for p in pts)/len(pts)
    start=max(range(len(pts)),key=lambda i:(pts[i][0]-cx,-abs(pts[i][1]-cy)))
    pts=pts[start:]+pts[:start]
    closed=pts+[pts[0]];lens=[0.0]
    for i in range(len(pts)):
        lens.append(lens[-1]+math.hypot(closed[i+1][0]-closed[i][0],closed[i+1][1]-closed[i][1]))
    total=lens[-1]
    if total<1e-5:raise ValueError("UNIVERSAL_RECIPE_ZERO_PERIMETER")
    out=[]
    for k in range(max(24,min(96,int(count)))):
        target=total*k/max(24,min(96,int(count)))
        j=0
        while j<len(pts)-1 and lens[j+1]<target:j+=1
        seg=max(1e-9,lens[j+1]-lens[j]);t=(target-lens[j])/seg
        out.append([closed[j][0]+(closed[j+1][0]-closed[j][0])*t,closed[j][1]+(closed[j+1][1]-closed[j][1])*t])
    return out

def _u_plane(axis,at):
    axis=str(axis or "Z").upper()
    if axis=="X":return cq.Plane(origin=(f(at),0,0),xDir=(0,1,0),normal=(1,0,0))
    if axis=="Y":return cq.Plane(origin=(0,f(at),0),xDir=(1,0,0),normal=(0,1,0))
    return cq.Plane(origin=(0,0,f(at)),xDir=(1,0,0),normal=(0,0,1))

def _u_loft_from_loops(axis,sections,loop_index=0):
    sections=sorted(list(sections or []),key=lambda s:f(s.get("at_mm")))
    if len(sections)<3:raise ValueError("UNIVERSAL_RECIPE_NEEDS_3_SECTIONS")
    first=sections[0];loops=first.get("profile_loops") or []
    if loop_index>=len(loops):raise ValueError("UNIVERSAL_RECIPE_LOOP_CORRESPONDENCE")
    pts=_u_resample_loop(loops[loop_index])
    wp=cq.Workplane(_u_plane(axis,f(first.get("at_mm")))).spline(pts,periodic=True,makeWire=True)
    last=f(first.get("at_mm"))
    for sec in sections[1:]:
        ls=sec.get("profile_loops") or []
        if loop_index>=len(ls):raise ValueError("UNIVERSAL_RECIPE_LOOP_CORRESPONDENCE")
        at=f(sec.get("at_mm"));pts=_u_resample_loop(ls[loop_index])
        wp=wp.workplane(offset=at-last).spline(pts,periodic=True,makeWire=True);last=at
    return wp.loft(combine=True,ruled=False)

def _u_section_shape(node):
    axis=str(node.get("axis") or "Z").upper();sections=list(node.get("sections") or [])
    if len(sections)<3:raise ValueError("UNIVERSAL_RECIPE_NEEDS_3_SECTIONS")
    ordered=[]
    for s in sorted(sections,key=lambda q:f(q.get("at_mm"))):
        loops=sorted(list(s.get("profile_loops") or []),key=lambda x:abs(_u_signed_area(x)),reverse=True)
        ordered.append({**s,"profile_loops":loops})
    # Reconstruction samples sit slightly inside the measured mesh envelope.
    # Extend the nearest measured profile to the source min/max so dimension
    # fidelity is explicit instead of silently reconstructing only the middle.
    src_min=node.get("source_axis_min_mm");src_max=node.get("source_axis_max_mm")
    if src_min is not None and f(src_min)<f(ordered[0].get("at_mm"))-1e-5:
        ordered.insert(0,{**ordered[0],"at_mm":f(src_min),"fraction":0.0,"synthetic_boundary":True})
    if src_max is not None and f(src_max)>f(ordered[-1].get("at_mm"))+1e-5:
        ordered.append({**ordered[-1],"at_mm":f(src_max),"fraction":1.0,"synthetic_boundary":True})
    counts=[len(s.get("profile_loops") or []) for s in ordered]
    if not counts or any(n not in (1,2) for n in counts):raise ValueError("UNIVERSAL_RECIPE_LOOP_COUNT_UNSUPPORTED:"+str(counts))
    outer=_u_loft_from_loops(axis,ordered,0)
    inner_idx=[i for i,n in enumerate(counts) if n==2]
    if inner_idx:
        if len(inner_idx)<3 or max(inner_idx)-min(inner_idx)+1!=len(inner_idx):
            raise ValueError("UNIVERSAL_RECIPE_CAVITY_TOPOLOGY_UNSUPPORTED:"+str(counts))
        inner_sections=[ordered[i] for i in inner_idx]
        open_min=inner_idx[0]==0;open_max=inner_idx[-1]==len(ordered)-1
        if not (open_min or open_max):
            raise ValueError("UNIVERSAL_RECIPE_ENCLOSED_VOID_REQUIRES_INTENT")
        # If the cavity reaches an outer boundary, extend its cutter slightly
        # beyond that boundary to guarantee a genuinely open mouth after Boolean.
        margin=max(.8,f(node.get("boundary_cut_margin_mm"),1.0))
        if open_min:
            inner_sections=[{**inner_sections[0],"at_mm":f(inner_sections[0].get("at_mm"))-margin,"synthetic_cutter_extension":True}]+inner_sections
        if open_max:
            inner_sections=inner_sections+[{**inner_sections[-1],"at_mm":f(inner_sections[-1].get("at_mm"))+margin,"synthetic_cutter_extension":True}]
        inner=_u_loft_from_loops(axis,inner_sections,1)
        try:
            if shape_volume(inner)>=shape_volume(outer)*.98:raise ValueError("UNIVERSAL_RECIPE_INNER_INVALID")
            outer=outer.cut(inner)
        except ValueError:raise
        except Exception as ex:raise ValueError("UNIVERSAL_RECIPE_HOLLOW_CUT_FAILED:"+str(ex))
    return outer

def _u_prismatic_shape(node):
    axis=str(node.get("axis") or "Z").upper();outline=node.get("outline") or [];t=f(node.get("thickness_mm"))
    if t<=0 or len(outline)<3:raise ValueError("UNIVERSAL_RECIPE_PRISM_INVALID")
    pts=[[f(p[0]),f(p[1])] for p in outline if isinstance(p,(list,tuple)) and len(p)>=2]
    plane=_u_plane(axis,0)
    return cq.Workplane(plane).polyline(pts).close().extrude(t)

def _u_axisymmetric_shape(node):
    axis=str(node.get("axis") or "Z").upper()
    rows=sorted(list(node.get("sections") or []),key=lambda q:f(q.get("at_mm")))
    if len(rows)<5:raise ValueError("UNIVERSAL_AXISYMMETRIC_NEEDS_5_SECTIONS")
    outer=[(f(r.get("at_mm")),f(r.get("outer_radius_mm"))) for r in rows]
    if any(rad<=.05 for _,rad in outer):raise ValueError("UNIVERSAL_AXISYMMETRIC_OUTER_RADIUS_INVALID")
    src_min=f(node.get("source_axis_min_mm"),outer[0][0]);src_max=f(node.get("source_axis_max_mm"),outer[-1][0])
    if src_min<outer[0][0]-1e-5:outer=[(src_min,outer[0][1])]+outer
    if src_max>outer[-1][0]+1e-5:outer=outer+[(src_max,outer[-1][1])]
    inner=[(f(r.get("at_mm")),f(r.get("inner_radius_mm"))) for r in rows if r.get("inner_radius_mm") is not None and f(r.get("inner_radius_mm"))>.05]
    if not inner:
        conceptual=[(src_min,0.0)]+outer+[(src_max,0.0)]
    else:
        inner.sort(key=lambda x:x[0]);open_min=bool(node.get("open_inner_min"));open_max=bool(node.get("open_inner_max"))
        if open_min and inner[0][0]>src_min+1e-5:inner=[(src_min,inner[0][1])]+inner
        if open_max and inner[-1][0]<src_max-1e-5:inner=inner+[(src_max,inner[-1][1])]
        if open_min and open_max:
            conceptual=outer+list(reversed(inner))
        elif open_max:
            first_inner=inner[0][0];before=[a for a,_ in outer if a<first_inner-1e-6]
            apex=(max(before)+first_inner)/2.0 if before else src_min
            conceptual=[(src_min,0.0)]+outer+list(reversed(inner))+[(apex,0.0)]
        elif open_min:
            last_inner=inner[-1][0];after=[a for a,_ in outer if a>last_inner+1e-6]
            apex=(last_inner+min(after))/2.0 if after else src_max
            conceptual=outer+[(src_max,0.0),(apex,0.0)]+list(reversed(inner))
        else:
            raise ValueError("UNIVERSAL_AXISYMMETRIC_ENCLOSED_VOID_REQUIRES_INTENT")
    pts=[]
    for axial,radial in conceptual:
        q=(radial,axial) if axis=="Z" else (axial,radial)
        if not pts or abs(q[0]-pts[-1][0])>1e-7 or abs(q[1]-pts[-1][1])>1e-7:pts.append(q)
    if len(pts)<4:raise ValueError("UNIVERSAL_AXISYMMETRIC_PROFILE_INVALID")
    if axis=="Z":plane="XZ";axis_end=(0,1)
    elif axis=="X":plane="XY";axis_end=(1,0)
    elif axis=="Y":plane="YZ";axis_end=(1,0)
    else:raise ValueError("UNIVERSAL_AXISYMMETRIC_AXIS_INVALID")
    try:
        shp=cq.Workplane(plane).polyline(pts).close().revolve(360,(0,0),axis_end)
        if shape_volume(shp)<=.001:raise ValueError("zero_volume")
        return shp
    except Exception as ex:
        raise ValueError("UNIVERSAL_AXISYMMETRIC_REVOLVE_FAILED:"+str(ex))

def _u_bounds(shape):
    bb=shape.val().BoundingBox()
    return [bb.xmin,bb.ymin,bb.zmin],[bb.xmax,bb.ymax,bb.zmax],[bb.xlen,bb.ylen,bb.zlen]

def _u_normalize_origin(shape):
    mn,mx,dims=_u_bounds(shape)
    return shape.translate((-mn[0],-mn[1],-mn[2]))

def _u_apply_openings(shape,nodes):
    mn,mx,dims=_u_bounds(shape)
    for node in nodes:
        if str(node.get("operation") or "").upper()!="CUT":continue
        typ=str(node.get("type") or "")
        if not bool(node.get("through")):continue
        if typ!="circular_hole":raise ValueError("UNIVERSAL_RECIPE_REQUIRED_OPENING_UNSUPPORTED:"+typ)
        d=f(node.get("diameter_mm"));center=node.get("center_2d_mm") or []
        if d<=0 or len(center)<2:raise ValueError("UNIVERSAL_RECIPE_HOLE_INVALID")
        axis=str(node.get("axis") or "Z").upper();margin=3.0
        if axis=="Z":
            tool=cq.Workplane("XY").workplane(offset=mn[2]-margin).center(f(center[0]),f(center[1])).circle(d/2).extrude(dims[2]+2*margin)
        elif axis=="X":
            tool=cq.Workplane(_u_plane("X",mn[0]-margin)).center(f(center[0]),f(center[1])).circle(d/2).extrude(dims[0]+2*margin)
        elif axis=="Y":
            tool=cq.Workplane(_u_plane("Y",mn[1]-margin)).center(f(center[0]),f(center[1])).circle(d/2).extrude(dims[1]+2*margin)
        else:raise ValueError("UNIVERSAL_RECIPE_HOLE_AXIS")
        shape=shape.cut(tool)
    return shape

def universal_cad_recipe(c):
    plan=c.get("universal_recipe") or c.get("recipe") or {}
    if str(plan.get("status") or "")!="READY":raise ValueError("UNIVERSAL_RECIPE_NOT_READY:"+str(plan.get("status")))
    evidence=plan.get("geometry_evidence") or {}
    if int(f(evidence.get("source_part_count"),1))!=1:raise ValueError("UNIVERSAL_RECIPE_MULTIPART_EXECUTOR_NOT_READY")
    if plan.get("hard_blockers"):raise ValueError("UNIVERSAL_RECIPE_BLOCKED:"+",".join(str(x) for x in plan.get("hard_blockers")))
    graph=plan.get("feature_graph") or {};nodes=list(graph.get("nodes") or [])
    mode=str(plan.get("reconstruction_mode") or "")
    if mode=="AXISYMMETRIC_REVOLVE_RECONSTRUCTION":
        profile=next((x for x in nodes if x.get("type")=="axisymmetric_profile"),None)
        if not profile:raise ValueError("UNIVERSAL_RECIPE_AXISYMMETRIC_PROFILE_MISSING")
        shape=_u_axisymmetric_shape(profile)
    elif mode=="SECTION_LOFT_RECONSTRUCTION":
        stack=next((x for x in nodes if x.get("type")=="section_stack"),None)
        if not stack:raise ValueError("UNIVERSAL_RECIPE_SECTION_STACK_MISSING")
        shape=_u_section_shape(stack)
    elif mode=="PLANAR_PRISMATIC_RECONSTRUCTION":
        profile=next((x for x in nodes if x.get("type")=="measured_outline"),None)
        if not profile:raise ValueError("UNIVERSAL_RECIPE_PROFILE_MISSING")
        shape=_u_prismatic_shape(profile)
    else:
        raise ValueError("UNIVERSAL_RECIPE_MODE_UNSUPPORTED:"+mode)
    shape=_u_apply_openings(shape,nodes)
    shape=_u_normalize_origin(shape)
    if shape_volume(shape)<=1e-3:raise ValueError("UNIVERSAL_RECIPE_ZERO_VOLUME")
    return [{"name":"BODY","role":"main_body","physical_separate":True,"editable_separate":True,"color":color_hex(c.get("body_color") or "#6d7480"),"shape":shape,"geom":None,"min_feature_mm":max(.8,f(c.get("wall_mm"),.8))}]

def static_functional_utensil_vessel(c):
    kind=str(c.get("functional_type") or "").strip().lower()
    if kind!="scoop_vessel":raise ValueError("STATIC_FUNCTIONAL_TYPE_UNSUPPORTED:"+kind)
    outer_d=max(40.0,f(c.get("outer_diameter_mm"),76))
    cup_h=max(18.0,f(c.get("cup_height_mm"),40))
    wall=max(1.6,f(c.get("wall_mm"),2.6))
    floor=max(1.6,f(c.get("floor_mm"),2.8))
    handle_len=max(35.0,f(c.get("handle_length_mm"),82))
    handle_w=max(12.0,f(c.get("handle_width_mm"),22))
    handle_t=max(3.0,f(c.get("handle_thickness_mm"),7))
    overlap=max(3.0,f(c.get("handle_overlap_mm"),12))
    if outer_d<=wall*2+8:raise ValueError("STATIC_VESSEL_WALL_INVALID")
    if cup_h<=floor+8:raise ValueError("STATIC_VESSEL_FLOOR_INVALID")
    inner_d=outer_d-2*wall
    overall=outer_d+handle_len-overlap

    outer=cq.Workplane("XY").circle(outer_d/2).extrude(cup_h)
    handle_start=outer_d/2-overlap
    handle_center=handle_start+handle_len/2
    handle=rounded_box(handle_w,handle_len,handle_t,min(3.0,handle_w*.18)).translate((0,handle_center,0))
    neck_w=min(outer_d*.72,max(handle_w*1.55,outer_d*.42))
    neck_d=max(overlap*1.8,16.0)
    neck_h=max(handle_t+1.6,min(cup_h*.30,handle_t*1.55))
    neck_y=outer_d/2-overlap*.55
    neck=rounded_box(neck_w,neck_d,neck_h,min(4.0,neck_w*.16)).translate((0,neck_y,0))
    body=outer.union(handle).union(neck)
    cavity=cq.Workplane("XY").workplane(offset=floor).circle(inner_d/2).extrude(cup_h-floor+.6)
    body=body.cut(cavity)
    if body.val().isNull() or not body.val().isValid():raise ValueError("STATIC_VESSEL_BREP_INVALID")

    parts=[{"name":"BODY","role":"functional_body","physical_separate":True,"editable_separate":True,
            "color":color_hex(c.get("body_color") or "#b89165"),"shape":body,"geom":None,"min_feature_mm":wall}]
    if bool(c.get("paw_relief",False)):
        relief_h=max(.6,min(1.2,f(c.get("paw_relief_height_mm"),.8)))
        relief_y=handle_start+handle_len*.67
        accent=cq.Workplane("XY").ellipse(min(5.4,handle_w*.24),min(4.2,handle_w*.19)).extrude(relief_h)
        toe_r=max(1.25,min(2.0,handle_w*.075))
        for dx,dy in [(-handle_w*.24,4.2),(-handle_w*.08,5.8),(handle_w*.08,5.8),(handle_w*.24,4.2)]:
            accent=accent.union(cq.Workplane("XY").center(dx,dy).circle(toe_r).extrude(relief_h))
        accent=accent.translate((0,relief_y,handle_t))
        if accent.val().isValid():
            parts.append({"name":"PAW_RELIEF","role":"semantic_relief","physical_separate":False,"editable_separate":True,
                          "color":color_hex(c.get("relief_color") or "#f2dfc4"),"shape":accent,"geom":None,"min_feature_mm":.8})

    capacity=math.pi*(inner_d/2)**2*max(0,cup_h-floor)/1000
    target=f(c.get("target_capacity_ml"),0)
    if target>0:
        err=abs(capacity-target)/target*100
        if err>8:raise ValueError("STATIC_CAPACITY_MISMATCH:"+str(round(capacity,1))+":"+str(round(target,1)))
    return parts

def build_cad_family(req):
    c=req.get("cad_contract") or req.get("recipe") or {}
    family=str(c.get("family") or "rounded_plate")
    if family=="universal_cad_recipe":return universal_cad_recipe(c),[],[],[]
    if family=="static_functional_utensil_vessel":return static_functional_utensil_vessel(c),[],[],[]
    if family=="sculpted_lidded_container":return sculpted_lidded_container(c),[],[],[]
    if family=="open_box":base=open_box(c)
    elif family=="phone_stand":base=phone_stand(c)
    elif family=="primitive_recipe":base=primitive_recipe(c)
    else:
        w=f(c.get("width_mm"),80);h=f(c.get("height_mm"),50);t=max(.8,f(c.get("thickness_mm"),3));r=max(0,f(c.get("corner_radius_mm"),4))
        base=rounded_box(w,h,t,r)
    return [{"name":"BODY","role":"body","color":"#000000","shape":base,"geom":None,"min_feature_mm":c.get("wall_mm") or c.get("thickness_mm")}],[],[],[]

def build(req):
    c=req.get("cad_contract") or req.get("recipe") or {}
    family=str(c.get("family") or "")
    if req.get("svg_artifact",{}).get("parts") and family in ("silhouette_plate","rounded_plate","keychain_plate","nfc_keychain"):
        return build_svg_plate(req)
    return build_cad_family(req)

def _part_shape(part,assembled=False):
    s=part["shape"]
    if assembled:
        at=part.get("assembly_translate") or [0,0,0]
        if isinstance(at,list) and len(at)==3 and any(abs(f(v))>1e-9 for v in at):
            s=s.translate((f(at[0]),f(at[1]),f(at[2])))
    return s

def _parts_for_bbox(parts,assembled=False):
    return [{**p,"shape":_part_shape(p,assembled)} for p in parts]

def pair_overlap(parts,assembled=False):
    collisions=[]
    for i in range(len(parts)):
        for j in range(i+1,len(parts)):
            try:
                a=_part_shape(parts[i],assembled);b=_part_shape(parts[j],assembled)
                vol=shape_volume(a.intersect(b))
            except Exception:vol=0
            if vol>0.001:collisions.append({"a":parts[i]["name"],"b":parts[j]["name"],"volume_mm3":round(vol,4)})
    return collisions

def hole_penetration_ok(base,hole_tools):
    for meta,tool in hole_tools:
        try:
            if shape_volume(base.intersect(tool))>0.001:return False
        except Exception:return False
    return True

def geometric_feature_check(g,min_feature):
    if g is None:return {"ok":True,"lost_area_mm2":0.0}
    try:
        radius=max(0.001,float(min_feature)/2.0)
        eroded=g.buffer(-radius,join_style=2)
        if eroded.is_empty:return {"ok":False,"lost_area_mm2":round(float(g.area),6)}
        recovered=eroded.buffer(radius,join_style=2)
        lost=float(g.difference(recovered).area)
        tolerance=max(0.002,float(g.area)*0.0005)
        return {"ok":lost<=tolerance,"lost_area_mm2":round(lost,6)}
    except Exception:
        return {"ok":False,"lost_area_mm2":None}

def feature_estimate(part,min_feature):
    explicit=part.get("min_feature_mm")
    g=part.get("geom")
    check=geometric_feature_check(g,min_feature)
    if explicit is not None:
        estimate=f(explicit,0)
    elif g is not None:
        minx,miny,maxx,maxy=g.bounds
        estimate=min(maxx-minx,maxy-miny)
    else:
        estimate=f(part.get("min_feature_mm"),999)
    return {"estimate_mm":estimate,"geometry_ok":check["ok"],"lost_area_mm2":check["lost_area_mm2"]}

def geometry_fingerprint(parts,tol=.10):
    h=hashlib.sha256()
    for p in parts:
        h.update(str(p.get("name","")).encode());h.update(color_hex(p.get("color")).encode())
        verts,tris=mesh_of(p["shape"],tol)
        for x,y,z in verts:h.update(("%.6f,%.6f,%.6f;"%(x,y,z)).encode())
        for a,b,c in tris:h.update(("%d,%d,%d;"%(a,b,c)).encode())
    return h.hexdigest()

def validate_parts(req,parts,svg_geoms,hole_tools,invalid):
    c=req.get("cad_contract") or {}; profile=(req.get("parameters") or {}).get("manufacturing_profile") or {}
    min_feature=f(profile.get("min_feature_mm"),MIN_FEATURE_DEFAULT); max_colors=int(f(profile.get("max_colors"),MAX_COLORS_DEFAULT))
    build=profile.get("build_volume_mm") or BUILD_MM
    if not isinstance(build,list) or len(build)!=3:build=BUILD_MM
    build=[f(x,BUILD_MM[i]) for i,x in enumerate(build)]
    nozzle=f(profile.get("nozzle_mm"),.4);nozzle_ok=abs(nozzle-.4)<=1e-6

    family=str(c.get("family") or "")
    assembled_family=family in ("sculpted_lidded_container",)
    valid=all(bool(p["shape"].val().isValid()) for p in parts)
    print_tol=effective_validation_mesh_tolerance(c)
    topo,printable_part_bounds,fingerprint=mesh_validation_summaries(parts,print_tol)
    open_edges=sum(t["open_edges"] for t in topo); nonmanifold=sum(t["nonmanifold_edges"] for t in topo)
    degenerate=sum(t.get("degenerate_triangles",0) for t in topo)
    volumes=[{"part":p["name"],"volume_mm3":round(shape_volume(p["shape"]),4)} for p in parts]
    zero_volume=any(x["volume_mm3"]<=.001 for x in volumes)
    collisions=pair_overlap(parts,assembled=assembled_family)
    printable_parts=_parts_for_bbox(parts,False)
    bb=aggregate_bbox(printable_parts);dims=dims_from_bbox(bb)
    assembled_bb=aggregate_bbox(_parts_for_bbox(parts,True)) if assembled_family else bb
    assembled_dims=dims_from_bbox(assembled_bb)
    pd=c.get("product_dimensions_mm") or [c.get("width_mm"),c.get("height_mm"),c.get("thickness_mm")]
    if family=="universal_cad_recipe":
        up=c.get("universal_recipe") or c.get("recipe") or {};ud=(up.get("geometry_evidence") or {}).get("dimensions_mm") or []
        product_dims=[f(ud[i],assembled_dims[i]) for i in range(3)] if isinstance(ud,list) and len(ud)==3 else list(assembled_dims)
    else:
        product_dims=[f(pd[i],assembled_dims[i]) for i in range(3)] if isinstance(pd,list) and len(pd)==3 else list(assembled_dims)
    occ_tol=.01
    part_by_name={str(p.get("name") or ""):p for p in parts}
    bed_required_bounds=[x for x in printable_part_bounds if (part_by_name.get(str(x.get("part") or ""),{}).get("physical_separate") is True)]
    if not bed_required_bounds:
        bed_required_bounds=printable_part_bounds[:1]
    first_layer_ok=all(x["min"][2]>=-occ_tol and x["min"][2]<=.05 for x in bed_required_bounds)
    fit=all((x["dimensions"][0]<=build[0]+occ_tol and x["dimensions"][1]<=build[1]+occ_tol and x["dimensions"][2]<=build[2]+occ_tol) for x in printable_part_bounds)
    colors=sorted(set(color_hex(p.get("color")) for p in parts));ams_ok=len(colors)<=max_colors

    feats=[]
    for p in parts:
        tm=p.get("text_meta")
        required=max(TEXT_MIN_STROKE_DEFAULT,f((tm or {}).get("min_stroke_mm"),TEXT_MIN_STROKE_DEFAULT)) if tm else min_feature
        fc=feature_estimate(p,required)
        feats.append({"part":p["name"],"feature_class":"text" if tm else "structural","required_mm":round(required,3),"estimate_mm":round(fc["estimate_mm"],3),"geometry_ok":fc["geometry_ok"],"lost_area_mm2":fc["lost_area_mm2"]})
    min_ok=all(x["estimate_mm"]>=x["required_mm"]-1e-6 and x["geometry_ok"] for x in feats)
    typography_checks=[]
    for p in parts:
        tm=p.get("text_meta")
        if not tm:continue
        text_required=max(TEXT_MIN_STROKE_DEFAULT,f(tm.get("min_stroke_mm"),TEXT_MIN_STROKE_DEFAULT))
        fc=feature_estimate(p,text_required)
        min_stroke_ok=bool(tm.get("min_stroke_ok")) and fc["geometry_ok"] and fc["estimate_mm"]>=text_required-1e-6
        glyph_clearance_ok=bool(tm.get("glyph_clearance_ok")) and f(tm.get("glyph_clearance_mm"),999)+1e-6>=f(tm.get("min_glyph_clearance_mm"),TEXT_LATIN_GAP_DEFAULT)
        internal_clearance_ok=bool(tm.get("internal_gap_ok",True)) and (str(tm.get("script") or "").lower() not in ("cjk","mixed") or f(tm.get("internal_gap_mm"),999)+1e-6>=f(tm.get("min_internal_gap_mm"),TEXT_CJK_INTERNAL_GAP_DEFAULT))
        slicer_no_merge_ok=bool(tm.get("slicer_no_merge_ok")) and f(tm.get("slicer_gap_margin_mm"),-1)>=-1e-6
        layout_bounds_ok=bool(tm.get("layout_bounds_ok"))
        readability_ok=bool(tm.get("text_readability_ok")) and min_stroke_ok and glyph_clearance_ok and internal_clearance_ok and slicer_no_merge_ok and layout_bounds_ok
        typography_checks.append({
            "part":p["name"],"text":tm.get("text"),"script":tm.get("script"),"font_role":tm.get("font_role"),"font_used":tm.get("font_used"),
            "requested_size_mm":tm.get("requested_size_mm"),"size_mm":tm.get("size_mm"),"auto_bolden_mm":tm.get("auto_bolden_mm"),
            "tracking_mm":tm.get("tracking_mm"),"glyph_count":tm.get("glyph_count"),"glyph_clearance_mm":tm.get("glyph_clearance_mm"),
            "min_glyph_clearance_mm":tm.get("min_glyph_clearance_mm"),"internal_gap_mm":tm.get("internal_gap_mm"),"min_internal_gap_mm":tm.get("min_internal_gap_mm"),"internal_gap_checks":tm.get("internal_gap_checks"),
            "slicer_line_width_mm":tm.get("slicer_line_width_mm"),"slicer_gap_margin_mm":tm.get("slicer_gap_margin_mm"),"wall_generator":tm.get("wall_generator"),
            "bounds_mm":tm.get("bounds_mm"),"max_bounds_mm":tm.get("max_bounds_mm"),
            "min_stroke_mm":text_required,"stroke_geometry_ok":fc["geometry_ok"],"lost_area_mm2":fc["lost_area_mm2"],
            "min_stroke_ok":min_stroke_ok,"glyph_clearance_ok":glyph_clearance_ok,"internal_clearance_ok":internal_clearance_ok,"slicer_no_merge_ok":slicer_no_merge_ok,"layout_bounds_ok":layout_bounds_ok,
            "text_readability_ok":readability_ok,"ok":bool(tm.get("font_used")) and readability_ok
        })
    typography_ok=all(x["ok"] for x in typography_checks) if typography_checks else True
    text_min_stroke_ok=all(x["min_stroke_ok"] for x in typography_checks) if typography_checks else True
    glyph_clearance_ok=all(x["glyph_clearance_ok"] for x in typography_checks) if typography_checks else True
    text_internal_clearance_ok=all(x["internal_clearance_ok"] for x in typography_checks) if typography_checks else True
    text_slicer_no_merge_ok=all(x["slicer_no_merge_ok"] for x in typography_checks) if typography_checks else True
    text_layout_bounds_ok=all(x["layout_bounds_ok"] for x in typography_checks) if typography_checks else True
    text_readability_ok=all(x["text_readability_ok"] for x in typography_checks) if typography_checks else True
    if not typography_ok:min_ok=False

    structural_fields=["wall_mm","thickness_mm","base_thickness_mm","back_thickness_mm","lip_thickness_mm"]
    structural_checks=[]
    for k in structural_fields:
        if c.get(k) is not None:
            val=f(c.get(k));structural_checks.append({"field":k,"value_mm":round(val,3),"required_mm":min_feature,"ok":val+1e-9>=min_feature})
    structural_min_ok=all(x["ok"] for x in structural_checks) if structural_checks else True
    if not structural_min_ok:min_ok=False

    smooth_required=str(c.get("curve_quality") or "").lower().startswith("high")
    smooth_checks=[]
    for sp in (req.get("svg_artifact") or {}).get("parts") or []:
        d=str(sp.get("path_d") or "")
        line_cmds=sum(d.count(ch) for ch in ("L","l"))
        curve_cmds=sum(d.count(ch) for ch in ("A","a","C","c","Q","q","S","s"))
        suspicious=(line_cmds>36 and curve_cmds==0)
        smooth_checks.append({"part":sp.get("id"),"line_commands":line_cmds,"curve_commands":curve_cmds,"suspected_stair_step_trace":suspicious})
    mesh_tol=effective_mesh_tolerance(c)
    smooth_vector_ok=(not smooth_required) or (mesh_tol<=.05 and not any(x["suspected_stair_step_trace"] for x in smooth_checks))

    silhouettes=[g for p,g in svg_geoms if p.get("role")=="silhouette"]
    outer=unary_union(silhouettes) if silhouettes else None
    hole_wall_checks=[];feature_containment=[]
    if outer is not None:
        safe_outer=outer.buffer(1e-6)
        for p,g in svg_geoms:
            role=p.get("role")
            if role!="silhouette":
                inside=bool(safe_outer.covers(g))
                feature_containment.append({"part":p.get("id"),"role":role,"inside_body":inside})
            if role=="through_hole":
                inside=bool(safe_outer.covers(g))
                remaining=float(g.distance(outer.boundary)) if inside else 0.0
                hole_wall_checks.append({"part":p.get("id"),"remaining_wall_mm":round(remaining,3),"required_mm":min_feature,"inside_body":inside,"ok":inside and remaining+1e-6>=min_feature})
        if hole_wall_checks and not all(x["ok"] for x in hole_wall_checks):min_ok=False
    feature_containment_ok=all(x["inside_body"] for x in feature_containment) if feature_containment else True

    pocket_checks=[]
    thickness=f(c.get("thickness_mm"),f(c.get("wall_mm"),min_feature));strict_intent=bool(c.get("enforce_part_intent",False))
    for pk in c.get("pockets") or []:
        dia=f(pk.get("diameter_mm"),0);pw=f(pk.get("width_mm"),0);ph=f(pk.get("height_mm"),0);dep=f(pk.get("depth_mm"),0);x=f(pk.get("x_mm"));y=f(pk.get("y_mm"))
        required=f(pk.get("required_wall_mm"),min_feature);shape=str(pk.get("shape") or ("rect" if pw>0 and ph>0 else "circle")).lower()
        purpose=str(pk.get("purpose") or "").strip();declared_through=bool(pk.get("through",False))
        pg=None
        if shape=="rect" and pw>0 and ph>0:pg=shapely_box(x-pw/2,y-ph/2,x+pw/2,y+ph/2)
        elif dia>0:pg=Point(x,y).buffer(dia/2,resolution=64)
        if pg is not None:
            inside=bool(outer.buffer(1e-6).covers(pg)) if outer is not None else True
            remaining=float(pg.distance(outer.boundary)) if outer is not None and inside else (999.0 if outer is None else 0.0)
            floor=thickness-dep
            intent_ok=(not strict_intent) or (bool(purpose) and declared_through is False)
            ok=inside and remaining+1e-6>=required and floor+1e-6>=min_feature and dep>0 and intent_ok
            pocket_checks.append({"name":pk.get("name") or "pocket","shape":shape,"diameter_mm":round(dia,3) if dia>0 else None,"width_mm":round(pw,3) if pw>0 else None,"height_mm":round(ph,3) if ph>0 else None,"depth_mm":round(dep,3),"remaining_wall_mm":round(remaining,3),"floor_mm":round(floor,3),"required_wall_mm":required,"required_floor_mm":min_feature,"inside_body":inside,"purpose":purpose or None,"declared_through":declared_through,"intent_ok":intent_ok,"ok":ok})
    pocket_ok=all(x["ok"] for x in pocket_checks) if pocket_checks else True
    if not pocket_ok:min_ok=False

    clearances=c.get("clearances") or []
    target=f(profile.get("xy_clearance_mm"),.25)
    clearance_checks=[]
    for x in clearances:
        actual=f(x.get("actual_mm"),f(c.get("xy_clearance_mm"),target));required=f(x.get("required_mm"),target)
        clearance_checks.append({"name":x.get("name") or "clearance","actual_mm":actual,"required_mm":required,"ok":actual+1e-9>=required})
    if not clearance_checks:
        actual=f(c.get("xy_clearance_mm"),target);clearance_checks=[{"name":"xy_clearance","actual_mm":actual,"required_mm":target,"ok":actual+1e-9>=target}]
    clearance_ok=all(x["ok"] for x in clearance_checks)

    svg_driven=family in ("silhouette_plate","rounded_plate","keychain_plate","nfc_keychain")
    box=(req.get("svg_artifact") or {}).get("design_box_mm") or []
    contract_svg_match=True
    if svg_driven and len(box)==2:
        contract_svg_match=abs(f(c.get("width_mm"),box[0])-f(box[0]))<=.05 and abs(f(c.get("height_mm"),box[1])-f(box[1]))<=.05
    contract_dimensions_ok=True;expected_dimensions_mm=None;contract_measured_dimensions_mm=None
    if family in ("open_box","phone_stand"):
        expected_dimensions_mm=[f(c.get("width_mm"),dims[0]),f(c.get("depth_mm"),dims[1]),f(c.get("height_mm"),dims[2])]
        contract_measured_dimensions_mm=list(dims)
        contract_dimensions_ok=all(abs(dims[i]-expected_dimensions_mm[i])<=.05 for i in range(3))
    elif family=="static_functional_utensil_vessel":
        outer_d=f(c.get("outer_diameter_mm"),dims[0]);overall=outer_d+f(c.get("handle_length_mm"),80)-f(c.get("handle_overlap_mm"),12);cup_h=f(c.get("cup_height_mm"),dims[2])
        expected_dimensions_mm=[outer_d,overall,cup_h];contract_measured_dimensions_mm=list(dims)
        contract_dimensions_ok=all(abs(dims[i]-expected_dimensions_mm[i])<=.45 for i in range(3))
    elif family=="sculpted_lidded_container":
        expected_dimensions_mm=[f(product_dims[i],assembled_dims[i]) for i in range(3)]
        # OCCT BREP BoundingBox can be inflated by curve tolerances on periodic
        # organic splines. The manufactured contour is the tessellated/exported
        # surface, so use the already-audited validation mesh bounds plus the
        # formal assembly transforms here. Final 3MF receives a second export audit.
        mn=[float("inf")]*3;mx=[float("-inf")]*3;seen=False
        for b in printable_part_bounds:
            p=part_by_name.get(str(b.get("part") or "")) or {}
            at=p.get("assembly_translate") or [0,0,0]
            tr=[f(at[i]) if isinstance(at,list) and len(at)==3 else 0.0 for i in range(3)]
            for i in range(3):
                mn[i]=min(mn[i],f((b.get("min") or [0,0,0])[i])+tr[i])
                mx[i]=max(mx[i],f((b.get("max") or [0,0,0])[i])+tr[i])
            seen=True
        contract_measured_dimensions_mm=[round(mx[i]-mn[i],6) for i in range(3)] if seen else list(assembled_dims)
        contract_dimensions_ok=all(abs(contract_measured_dimensions_mm[i]-expected_dimensions_mm[i])<=.35 for i in range(3))
    universal_fidelity=None
    if family=="universal_cad_recipe":
        plan=c.get("universal_recipe") or c.get("recipe") or {}
        evidence=plan.get("geometry_evidence") or {}
        src_dims=evidence.get("dimensions_mm") or []
        if not isinstance(src_dims,list) or len(src_dims)!=3 or any(f(x)<=0 for x in src_dims):
            contract_dimensions_ok=False
            universal_fidelity={"status":"FAIL","reason":"missing_source_dimensions"}
        else:
            expected_dimensions_mm=[f(x) for x in src_dims]
            contract_measured_dimensions_mm=[round(x,6) for x in dims]
            tolerances=[max(.60,expected_dimensions_mm[i]*.015) for i in range(3)]
            errors=[abs(contract_measured_dimensions_mm[i]-expected_dimensions_mm[i]) for i in range(3)]
            contract_dimensions_ok=all(errors[i]<=tolerances[i]+1e-9 for i in range(3))
            universal_fidelity={
              "status":"PASS" if contract_dimensions_ok else "FAIL",
              "mode":"overall_dimension_gate_v1",
              "source_dimensions_mm":[round(x,3) for x in expected_dimensions_mm],
              "rebuilt_dimensions_mm":[round(x,3) for x in contract_measured_dimensions_mm],
              "absolute_error_mm":[round(x,3) for x in errors],
              "tolerance_mm":[round(x,3) for x in tolerances]
            }

    self_intersection=bool(invalid) or not valid
    hole_ok=hole_penetration_ok(parts[0]["shape"],hole_tools)
    feature_intents={str(x.get("name") or ""):x for x in (c.get("feature_intents") or [])}
    through_intent_checks=[]
    for p,g in svg_geoms:
        if p.get("role")=="through_hole":
            meta=feature_intents.get(str(p.get("id") or "")) or {}
            intent_ok=(not strict_intent) or (bool(str(meta.get("purpose") or "").strip()) and meta.get("through") is True and str(meta.get("kind") or "")=="through_hole")
            through_intent_checks.append({"name":p.get("id"),"purpose":meta.get("purpose"),"declared_through":meta.get("through"),"ok":intent_ok})
    unintended_through_cut_free=all(x.get("ok") is True for x in through_intent_checks) and all((x.get("declared_through") is False and x.get("floor_mm",0)+1e-6>=x.get("required_floor_mm",min_feature)) for x in pocket_checks)
    appearance_hash_match=(req.get("appearance_lock") or {}).get("appearance_hash")==req.get("appearance_hash") and bool(req.get("appearance_hash"))
    known_support_free=family in ("silhouette_plate","rounded_plate","keychain_plate","nfc_keychain","open_box","phone_stand","sculpted_lidded_container","static_functional_utensil_vessel")
    support_deferred=family=="universal_cad_recipe"
    support_required=not known_support_free
    island_free=feature_containment_ok
    mesh_ok=open_edges==0 and nonmanifold==0 and degenerate==0
    expected_parts=c.get("expected_parts") or []
    actual_parts=[{"name":str(p.get("name") or ""),"role":str(p.get("role") or "part"),"physical_separate":bool(p.get("physical_separate",False)),"editable_separate":bool(p.get("editable_separate",True))} for p in parts]
    actual_roles=set(x["role"] for x in actual_parts)
    actual_names=set(x["name"] for x in actual_parts)
    manifest={str(x.get("name") or ""):x for x in (c.get("part_manifest") or []) if not bool(x.get("virtual",False))}
    unexpected_parts=[];intent_issues=[]
    if strict_intent:
        for ap in actual_parts:
            pm=manifest.get(ap["name"])
            if pm is None:
                unexpected_parts.append(ap["name"]);continue
            fn=str(pm.get("function") or "").strip();strategy=str(pm.get("geometry_strategy") or "").strip()
            if not fn or not strategy or any(k in (fn+" "+strategy).lower() for k in ("placeholder","dummy","arbitrary","random")):
                intent_issues.append({"name":ap["name"],"function":fn or None,"geometry_strategy":strategy or None})
    part_intent_ok=(not strict_intent) or (not unexpected_parts and not intent_issues)
    orphan_geometry_free=(not strict_intent) or (not unexpected_parts)
    missing_expected=[]
    for ep in expected_parts:
        en=str(ep.get("name") or "");er=str(ep.get("role") or "");need_sep=bool(ep.get("physical_separate",False))
        cand=next((x for x in actual_parts if (en and x["name"]==en) or (not en and er and x["role"]==er)),None)
        if cand is None and er:cand=next((x for x in actual_parts if x["role"]==er),None)
        if cand is not None and (not need_sep or cand.get("physical_separate") is True):continue
        missing_expected.append({"name":en,"role":er,"physical_separate":need_sep})
    assembly_parts_ok=len(missing_expected)==0
    repair_needed=(not valid) or (not mesh_ok) or zero_volume or self_intersection or bool(collisions) or (not assembly_parts_ok) or (not part_intent_ok) or (not orphan_geometry_free) or (not unintended_through_cut_free)

    checks={
      "brep_valid":valid and not zero_volume,"watertight":valid and mesh_ok and not zero_volume,"open_edges":open_edges,"nonmanifold_edges":nonmanifold,"degenerate_triangles":degenerate,
      "zero_volume":zero_volume,"volume_checks":volumes,"geometry_fingerprint":fingerprint,
      "self_intersection":self_intersection,"self_intersection_details":invalid,
      "part_overlap":bool(collisions),"overlaps":collisions,
      "hole_penetration":hole_ok,"hole_wall_checks":hole_wall_checks,"pocket_checks":pocket_checks,
      "through_intent_checks":through_intent_checks,"unintended_through_cut_free":unintended_through_cut_free,
      "part_intent_ok":part_intent_ok,"orphan_geometry_free":orphan_geometry_free,"unexpected_parts":unexpected_parts,"part_intent_issues":intent_issues,
      "smooth_vector_ok":smooth_vector_ok,"smooth_vector_required":smooth_required,"mesh_tolerance_mm":mesh_tol,"smoothness_checks":smooth_checks,
      "typography_ok":typography_ok,"text_min_stroke_ok":text_min_stroke_ok,"glyph_clearance_ok":glyph_clearance_ok,"text_internal_clearance_ok":text_internal_clearance_ok,"text_slicer_no_merge_ok":text_slicer_no_merge_ok,"text_layout_bounds_ok":text_layout_bounds_ok,"text_readability_ok":text_readability_ok,"typography_checks":typography_checks,
      "min_feature_ok":min_ok and feature_containment_ok,"min_feature_mm":min_feature,"feature_checks":feats,"structural_checks":structural_checks,"structural_min_ok":structural_min_ok,"feature_containment":feature_containment,"feature_containment_ok":feature_containment_ok,
      "clearance_ok":clearance_ok,"clearance_checks":clearance_checks,
      "assembly_interference":bool(collisions),"assembly_parts_ok":assembly_parts_ok,"expected_parts":expected_parts,"actual_parts":actual_parts,"missing_expected_parts":missing_expected,"repair_needed":repair_needed,"first_layer_contact_ok":first_layer_ok,"island_free":island_free,
      "support_required":support_required,"support_profile_ok":known_support_free or support_deferred,"support_profile_mode":"defer_to_bambu_slice" if support_deferred else ("known_support_free" if known_support_free else "required"),
      "a1_mini_fit":fit,"build_volume_mm":build,"dimensions_mm":dims,"product_dimensions_mm":[round(x,3) for x in product_dims],"bbox_mm":[round(x,3) for x in bb],"printable_part_bounds":printable_part_bounds,
      "ams_colors_ok":ams_ok,"ams_color_count":len(colors),"ams_colors":colors,
      "nozzle_mm":nozzle,"nozzle_profile_ok":nozzle_ok,"real_slicer_verified":False,"slice_status":"NOT_RUN",
      "contract_svg_match":contract_svg_match,"contract_dimensions_ok":contract_dimensions_ok,"expected_dimensions_mm":expected_dimensions_mm,"contract_measured_dimensions_mm":contract_measured_dimensions_mm,
      "universal_fidelity":universal_fidelity,
      "appearance_hash_match":appearance_hash_match
    }
    return checks

def tessellated_parts(parts,tol=.06,assembled=False):
    out=[]
    for p in parts:
        raw_v,raw_t=mesh_of(p["shape"],tol)
        cleaned=clean_mesh_data(raw_v,raw_t,tol)
        v,t=cleaned["vertices"],cleaned["triangles"]
        at=p.get("assembly_translate") or [0,0,0]
        if assembled and isinstance(at,list) and len(at)==3:
            dx,dy,dz=f(at[0]),f(at[1]),f(at[2]);v=[(x+dx,y+dy,z+dz) for x,y,z in v]
        out.append({"name":p["name"],"role":p.get("role","part"),"physical_separate":p.get("physical_separate",False),"editable_separate":p.get("editable_separate",True),"assembly_translate":at,"color":p["color"],"vertices":v,"triangles":t,"mesh_weld_eps_mm":cleaned["weld_eps_mm"],"mesh_open_edges":cleaned["open_edges"],"mesh_nonmanifold_edges":cleaned["nonmanifold_edges"]})
    return out

def tessellated_dimensions(parts,tol=.06,assembled=False):
    mn=[float("inf")]*3;mx=[float("-inf")]*3;seen=False
    for p in parts:
        v,_=mesh_of(_part_shape(p,assembled),tol)
        for q in v:
            seen=True
            for i in range(3):mn[i]=min(mn[i],q[i]);mx[i]=max(mx[i],q[i])
        del v
    return [round(mx[i]-mn[i],6) for i in range(3)] if seen else [0,0,0]

def write_glb(parts,path,tol=.06,assembled=False):
    # Stream positions and indices to temporary files to avoid all_v/all_t/binary duplication.
    pos_tmp=pathlib.Path(str(path)+".pos.tmp");idx_tmp=pathlib.Path(str(path)+".idx.tmp")
    vcount=tcount=0;mn=[float("inf")]*3;mx=[float("-inf")]*3
    with open(pos_tmp,"wb",buffering=1024*1024) as pf,open(idx_tmp,"wb",buffering=1024*1024) as inf:
        for p in parts:
            raw_v,raw_t=mesh_of(p["shape"],tol);cleaned=clean_mesh_data(raw_v,raw_t,tol);v,t=cleaned["vertices"],cleaned["triangles"]
            at=p.get("assembly_translate") or [0,0,0]
            dx,dy,dz=(f(at[0]),f(at[1]),f(at[2])) if assembled and isinstance(at,list) and len(at)==3 else (0.0,0.0,0.0)
            base=vcount
            for x,y,z in v:
                q=(x+dx,y+dy,z+dz);pf.write(struct.pack("<fff",*q));vcount+=1
                for i in range(3):mn[i]=min(mn[i],q[i]);mx[i]=max(mx[i],q[i])
            for a,b,c0 in t:inf.write(struct.pack("<III",a+base,b+base,c0+base));tcount+=1
            del raw_v,raw_t,v,t,cleaned;gc.collect()
    if not vcount:raise ValueError("glb: no mesh vertices")
    pos_len=pos_tmp.stat().st_size;idx_len=idx_tmp.stat().st_size;idx_off=pos_len;binary_len=pos_len+idx_len
    doc={"asset":{"version":"2.0","generator":"MakerSence CAD Worker V2 streaming"},"scene":0,"scenes":[{"nodes":[0]}],"nodes":[{"mesh":0}],
      "meshes":[{"primitives":[{"attributes":{"POSITION":0},"indices":1}]}],"buffers":[{"byteLength":binary_len}],
      "bufferViews":[{"buffer":0,"byteOffset":0,"byteLength":pos_len,"target":34962},{"buffer":0,"byteOffset":idx_off,"byteLength":idx_len,"target":34963}],
      "accessors":[{"bufferView":0,"componentType":5126,"count":vcount,"type":"VEC3","min":mn,"max":mx},{"bufferView":1,"componentType":5125,"count":tcount*3,"type":"SCALAR"}]}
    jb=json.dumps(doc,separators=(",",":")).encode()
    while len(jb)%4:jb+=b" "
    total=12+8+len(jb)+8+binary_len
    with open(path,"wb",buffering=1024*1024) as out:
        out.write(struct.pack("<III",0x46546C67,2,total));out.write(struct.pack("<II",len(jb),0x4E4F534A));out.write(jb);out.write(struct.pack("<II",binary_len,0x004E4942))
        for tmp in (pos_tmp,idx_tmp):
            with open(tmp,"rb") as src:
                while True:
                    b=src.read(1024*1024)
                    if not b:break
                    out.write(b)
    for tmp in (pos_tmp,idx_tmp):
        try:tmp.unlink()
        except Exception:pass

def write_3mf(parts,path,tol=.04,assembled=False,placements=None,plate_index=1,plate_name=None):
    # Stream formal 3MF XML to disk one part at a time. This preserves editable
    # child objects while avoiding a full-model mesh list + giant XML string in RAM.
    colors=[];color_idx={}
    placements=placements or {}
    export_mn=[float("inf")]*3;export_mx=[float("-inf")]*3;export_seen=False
    for p in parts:
        c=color_hex(p.get("color"))
        if c not in color_idx:color_idx[c]=len(colors);colors.append(c)
    ns='http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
    child_ids=list(range(2,2+len(parts)));parent_id=2+len(parts)
    model_tmp=pathlib.Path(str(path)+".model.tmp")
    with open(model_tmp,"w",encoding="utf-8",buffering=1024*1024) as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>')
        fh.write('<model unit="millimeter" xml:lang="en-US" xmlns="'+ns+'"><resources><basematerials id="1">')
        for i,c in enumerate(colors):fh.write('<base name="Color '+str(i+1)+'" displaycolor="'+c.upper()+'FF"/>')
        fh.write('</basematerials>')
        for oi,p in zip(child_ids,parts):
            raw_v,raw_t=mesh_of(p["shape"],tol);cleaned=clean_mesh_data(raw_v,raw_t,tol);v,t=cleaned["vertices"],cleaned["triangles"]
            raw_name=str(p.get("name") or "PART");nm=html.escape(raw_name,quote=True);ci=color_idx[color_hex(p.get("color"))]
            at=p.get("assembly_translate") or [0,0,0]
            placement=placements.get(raw_name) if isinstance(placements,dict) else None
            rotate90=bool((placement or {}).get("rotated_90"))
            if placement:
                mnx=mny=mnz=float("inf")
                for x0,y0,z0 in v:
                    rx,ry=(-y0,x0) if rotate90 else (x0,y0)
                    mnx=min(mnx,rx);mny=min(mny,ry);mnz=min(mnz,z0)
                dx=f((placement or {}).get("x_mm"),0)-mnx
                dy=f((placement or {}).get("y_mm"),0)-mny
                dz=-mnz
            elif assembled and isinstance(at,list) and len(at)==3:
                dx,dy,dz=f(at[0]),f(at[1]),f(at[2])
            else:
                dx=dy=dz=0.0
            fh.write('<object id="'+str(oi)+'" type="model" name="'+nm+'"><mesh><vertices>')
            for x,y,z in v:
                rx,ry=(-y,x) if rotate90 else (x,y)
                xx,yy,zz=rx+dx,ry+dy,z+dz
                export_seen=True
                export_mn[0]=min(export_mn[0],xx);export_mn[1]=min(export_mn[1],yy);export_mn[2]=min(export_mn[2],zz)
                export_mx[0]=max(export_mx[0],xx);export_mx[1]=max(export_mx[1],yy);export_mx[2]=max(export_mx[2],zz)
                fh.write('<vertex x="'+str(round(xx,6))+'" y="'+str(round(yy,6))+'" z="'+str(round(zz,6))+'"/>')
            fh.write('</vertices><triangles>')
            for a,b,c0 in t:fh.write('<triangle v1="'+str(a)+'" v2="'+str(b)+'" v3="'+str(c0)+'" pid="1" p1="'+str(ci)+'"/>')
            fh.write('</triangles></mesh></object>')
            del raw_v,raw_t,v,t,cleaned;gc.collect()
        if placements:
            fh.write('</resources><build>')
            for oi in child_ids:fh.write('<item objectid="'+str(oi)+'"/>')
            fh.write('</build></model>')
        else:
            fh.write('<object id="'+str(parent_id)+'" type="model" name="MakerSense Product"><components>')
            for oi in child_ids:fh.write('<component objectid="'+str(oi)+'"/>')
            fh.write('</components></object></resources><build><item objectid="'+str(parent_id)+'"/></build></model>')
    ct=b'''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'''
    rel=b'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'''
    obj_settings=['<?xml version="1.0" encoding="UTF-8"?><config>']
    if not placements:obj_settings.append('<object id="'+str(parent_id)+'"><metadata key="name" value="MakerSense Product"/>')
    for oi,p in zip(child_ids,parts):
        ci=color_idx[color_hex(p.get("color"))];nm=html.escape(str(p.get("name") or "PART"),quote=True);role=html.escape(str(p.get("role") or "part"),quote=True)
        at=" ".join(str(round(f(v),6)) for v in (p.get("assembly_translate") or [0,0,0]))
        if placements:
            obj_settings.append('<object id="'+str(oi)+'"><metadata key="name" value="'+nm+'"/><metadata key="extruder" value="'+str(ci+1)+'"/><metadata key="MakerSenseRole" value="'+role+'"/><metadata key="MakerSensePhysicalSeparate" value="1"/></object>')
        else:
            obj_settings.append('<part id="'+str(oi)+'" subtype="normal_part"><metadata key="name" value="'+nm+'"/><metadata key="extruder" value="'+str(ci+1)+'"/><metadata key="MakerSenseRole" value="'+role+'"/><metadata key="MakerSensePhysicalSeparate" value="'+("1" if p.get("physical_separate") else "0")+'"/><metadata key="MakerSenseAssemblyTranslate" value="'+html.escape(at,quote=True)+'"/></part>')
    if not placements:obj_settings.append('</object>')
    obj_settings.append('</config>')
    project_settings=json.dumps({"filament_colour":colors,"filament_type":["PLA"]*len(colors),"wall_generator":"arachne","min_bead_width":"40%","line_width":"0.42","outer_wall_line_width":"0.42","inner_wall_line_width":"0.45","top_surface_line_width":"0.42","detect_thin_wall":"1","MakerSense":"true","MakerSense3MF":"native_parts_v1","MakerSenseArachneMinWallWidthPercent":"40","MakerSenseTypography":"bambu_04_slicer_safe_v3","MakerSenseTextMinStroke":"0.55","MakerSenseTextCjkMinGap":"0.55","MakerSenseTextCjkInternalGap":"0.46","MakerSenseTextLatinMinGap":"0.50"},ensure_ascii=False)
    if placements:
        plate_objects=[{"object_id":oi,"name":p.get("name"),"role":p.get("role","part"),"extruder":color_idx[color_hex(p.get("color"))]+1} for oi,p in zip(child_ids,parts)]
    else:
        plate_objects=[{"object_id":parent_id,"name":"MakerSense Product","parts":[{"part_id":oi,"name":p.get("name"),"role":p.get("role","part"),"extruder":color_idx[color_hex(p.get("color"))]+1} for oi,p in zip(child_ids,parts)]}]
    plate=json.dumps({"plate_index":int(plate_index or 1),"name":plate_name or ("Plate "+str(int(plate_index or 1))),"objects":plate_objects,"filaments":list(range(1,len(colors)+1))},ensure_ascii=False)
    with zipfile.ZipFile(path,"w",zipfile.ZIP_DEFLATED,allowZip64=True) as z:
        z.writestr("[Content_Types].xml",ct);z.writestr("_rels/.rels",rel);z.write(model_tmp,"3D/3dmodel.model")
        z.writestr("Metadata/model_settings.config","".join(obj_settings).encode("utf-8"));z.writestr("Metadata/project_settings.config",project_settings.encode("utf-8"));z.writestr("Metadata/plate_1.json",plate.encode("utf-8"))
    try:model_tmp.unlink()
    except Exception:pass
    dims=[export_mx[i]-export_mn[i] for i in range(3)] if export_seen else [0,0,0]
    return {"dimensions_mm":[round(x,3) for x in dims],"bounds_mm":{"min":[round(x,3) for x in export_mn] if export_seen else [0,0,0],"max":[round(x,3) for x in export_mx] if export_seen else [0,0,0]},"part_names":[str(p.get("name") or "PART") for p in parts],"plate_index":int(plate_index or 1)}

def _vsub(a,b):return (a[0]-b[0],a[1]-b[1],a[2]-b[2])
def _vdot(a,b):return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]
def _vcross(a,b):return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def _vnorm(a):
    l=math.sqrt(max(1e-18,_vdot(a,a)))
    return (a[0]/l,a[1]/l,a[2]/l)
def _rgb(s):
    s=color_hex(s);return (int(s[1:3],16),int(s[3:5],16),int(s[5:7],16))
def _mix(a,b,t):return tuple(int(round(a[i]*(1-t)+b[i]*t)) for i in range(3))
def _quad_point(q,u,v):
    a=(q[0][0]*(1-u)+q[1][0]*u,q[0][1]*(1-u)+q[1][1]*u)
    b=(q[3][0]*(1-u)+q[2][0]*u,q[3][1]*(1-u)+q[2][1]*u)
    return (a[0]*(1-v)+b[0]*v,a[1]*(1-v)+b[1]*v)

def render_product_png(req,parts,svg_geoms,path,validation,camera_view="perspective",meshes=None):
    render_tol=resource_policy(req)["render_mesh_tolerance_mm"]
    if meshes is None:meshes=tessellated_parts(parts,render_tol,assembled=True)
    allv=[v for m in meshes for v in m["vertices"]]
    if not allv:raise ValueError("render: no mesh vertices")
    final_w,final_h=1400,1050;ss=2;W,H=final_w*ss,final_h*ss
    intent=((req.get("parameters") or {}).get("manufacturing_profile") or {}).get("research_dna",{}).get("product_intent") or {}
    warm=str(intent.get("archetype") or "")=="music_photo_nfc_keepsake"
    top=(244,239,226) if warm else (236,238,235);bot=(210,205,191) if warm else (197,203,198)
    img=Image.new("RGB",(W,H),top);d=ImageDraw.Draw(img)
    for y in range(H):
        t=y/max(1,H-1);d.line([(0,y),(W,y)],fill=_mix(top,bot,t))
    # Fixed camera contract over the same formal assembled meshes used for export.
    camera_key=str(camera_view or "perspective").strip().lower()
    camera_vectors={"front":(0.0,0.0,1.0),"back":(0.0,0.0,-1.0),"side":(1.0,0.0,0.0),"perspective":(0.62,-0.70,0.78)}
    if camera_key not in camera_vectors:raise ValueError("unsupported camera view: "+camera_key)
    view=_vnorm(camera_vectors[camera_key])
    up_hint=(0.0,1.0,0.0) if abs(_vdot(view,(0.0,0.0,1.0)))>.92 else (0.0,0.0,1.0)
    right=_vnorm(_vcross(up_hint,view));up=_vnorm(_vcross(view,right))
    mn=[min(v[i] for v in allv) for i in range(3)];mx=[max(v[i] for v in allv) for i in range(3)]
    center=tuple((mn[i]+mx[i])/2 for i in range(3))
    def rawproj(p):
        q=_vsub(p,center);return (_vdot(q,right),_vdot(q,up),_vdot(q,view))
    pp=[rawproj(v) for v in allv];ux=[p[0] for p in pp];uy=[p[1] for p in pp]
    ur=max(1e-6,max(ux)-min(ux));vr=max(1e-6,max(uy)-min(uy))
    scale=min(W*.66/ur,H*.58/vr);cx=W*.53;cy=H*.48
    def proj(p):
        u,v,z=rawproj(p);return (cx+u*scale,cy-v*scale,z)
    scr=[proj(v) for v in allv];sx=[p[0] for p in scr];sy=[p[1] for p in scr]
    bbox_w=max(sx)-min(sx);bbox_h=max(sy)-min(sy);coverage=(bbox_w*bbox_h)/max(1,W*H)
    off_canvas=min(sx)<0 or max(sx)>W or min(sy)<0 or max(sy)>H
    min_coverage=.01 if camera_key=="side" else .08
    camera_ok=(not off_canvas) and coverage>=min_coverage and coverage<=.72
    # Soft floor shadow.
    shadow=Image.new("RGBA",(W,H),(0,0,0,0));sd=ImageDraw.Draw(shadow)
    bx=(min(sx)+max(sx))/2;by=max(sy)+H*.035;bw=(max(sx)-min(sx))*.78;bh=max(H*.035,(max(sy)-min(sy))*.10)
    sd.ellipse((bx-bw/2,by-bh/2,bx+bw/2,by+bh/2),fill=(25,22,18,95 if warm else 78))
    shadow=shadow.filter(ImageFilter.GaussianBlur(max(12,int(28*ss))));img=Image.alpha_composite(img.convert("RGBA"),shadow).convert("RGB")
    d=ImageDraw.Draw(img)
    light=_vnorm((-0.55,-0.35,1.0))
    def draw_mesh_set(selected):
        faces=[]
        for m in selected:
            base=_rgb(m["color"])
            for tri in m["triangles"]:
                a,b,c=[m["vertices"][i] for i in tri]
                n=_vnorm(_vcross(_vsub(b,a),_vsub(c,a)))
                facing=_vdot(n,view)
                if facing<=.015:continue
                pa,pb,pc=proj(a),proj(b),proj(c);depth=(pa[2]+pb[2]+pc[2])/3
                lam=max(0,_vdot(n,light));spec=max(0,facing)**10
                br=.58+.34*lam
                col=tuple(max(0,min(255,int(base[i]*br+34*spec))) for i in range(3))
                faces.append((depth,[pa[:2],pb[:2],pc[:2]],col))
        faces.sort(key=lambda x:x[0])
        for _,poly,col in faces:d.polygon(poly,fill=col)
    body=[m for m in meshes if str(m.get("role"))=="body" or str(m.get("name"))=="BODY"]
    raised=[m for m in meshes if m not in body]
    draw_mesh_set(body or meshes)
    # Product-context insert: a sample photo is rendered only when the Product Intent calls for it.
    # It is presentation content, not an extra printable solid.
    if warm and camera_key in ("front","perspective"):
        photo=None;photo_part=None
        for p,g in svg_geoms:
            if str(p.get("id") or "").upper() in ("PHOTO_WINDOW","PHOTO_RECESS"):
                photo=g;photo_part=p;break
        if photo is not None and not photo.is_empty:
            minx,miny,maxx,maxy=photo.bounds
            thickness=f((req.get("cad_contract") or {}).get("thickness_mm"),3)
            z=thickness+.025
            q=[proj((minx,miny,z))[:2],proj((maxx,miny,z))[:2],proj((maxx,maxy,z))[:2],proj((minx,maxy,z))[:2]]
            # Warm generic keepsake photo: sky + sun + mountains, clipped by constructing bands inside the quad.
            bands=[((235,178,126),(244,205,156)),((219,151,116),(235,178,126)),((148,126,113),(201,142,113))]
            for bi,(c0,c1) in enumerate(bands):
                v0=bi/len(bands);v1=(bi+1)/len(bands)
                steps=9
                for j in range(steps):
                    a0=v0+(v1-v0)*j/steps;a1=v0+(v1-v0)*(j+1)/steps
                    col=_mix(c0,c1,(j+.5)/steps)
                    d.polygon([_quad_point(q,0,a0),_quad_point(q,1,a0),_quad_point(q,1,a1),_quad_point(q,0,a1)],fill=col)
            sun=_quad_point(q,.73,.31);sr=max(5,int(min(abs(q[1][0]-q[0][0]),abs(q[3][1]-q[0][1]))*.075))
            d.ellipse((sun[0]-sr,sun[1]-sr,sun[0]+sr,sun[1]+sr),fill=(250,225,176))
            mountain=[_quad_point(q,0,.73),_quad_point(q,.28,.47),_quad_point(q,.47,.70),_quad_point(q,.67,.52),_quad_point(q,1,.76),_quad_point(q,1,1),_quad_point(q,0,1)]
            d.polygon(mountain,fill=(75,73,66))
            # faint glass highlight
            d.line([_quad_point(q,.08,.06),_quad_point(q,.88,.06)],fill=(255,255,255),width=max(2,ss*2))
    if raised:draw_mesh_set(raised)
    # Ground/contact sheen under product.
    d=ImageDraw.Draw(img,"RGBA")
    d.line([(W*.18,H*.80),(W*.86,H*.80)],fill=(255,255,255,45),width=max(1,ss))
    # Downsample for anti-aliasing.
    img=img.resize((final_w,final_h),Image.Resampling.LANCZOS)
    img.save(str(path),"PNG",optimize=True)
    assembled=any(any(abs(f(v))>1e-9 for v in (p.get("assembly_translate") or [0,0,0])) for p in parts)
    return {
      "status":"PASS" if camera_ok else "FAIL","mode":"deterministic_product_context",
      "geometry_source":"formal_3mf_parts_assembled" if assembled else "formal_cad_mesh",
      "view":camera_key,"camera_vector":[round(f(x),5) for x in view],
      "intent_archetype":intent.get("archetype"),"mock_content":["photo_insert"] if warm and camera_key in ("front","perspective") else [],
      "camera_ok":camera_ok,"off_canvas":off_canvas,"projected_coverage":round(coverage,4),"minimum_coverage":min_coverage,
      "note":"Mock content is presentation-only; product geometry is rendered from the same formal parts exported to 3MF."
    }

def audit_print_plate_3mf(path,expected_names,build_volume):
    out={"ok":False,"name":path.name,"zip_ok":False,"object_count":0,"build_item_count":0,"component_count":0,
         "open_edges":0,"nonmanifold_edges":0,"degenerate_triangles":0,"object_names":[],"dimensions_mm":[0,0,0],"within_build_volume":False}
    if not path.exists() or path.stat().st_size<64:return out
    try:
        with zipfile.ZipFile(path,"r") as z:
            out["zip_ok"]=z.testzip() is None
            model_name=next((n for n in z.namelist() if n.lower().endswith(".model")),None)
            if not out["zip_ok"] or not model_name:return out
            mn=[float("inf")]*3;mx=[float("-inf")]*3;seen=False;cur=None
            with z.open(model_name) as fh:
                for ev,el in ET.iterparse(fh,events=("start","end")):
                    tag=str(el.tag).rsplit("}",1)[-1]
                    if ev=="start":
                        if tag=="object":
                            out["object_count"]+=1
                            cur={"name":str(el.attrib.get("name") or ""),"edges":Counter(),"degenerate":0,"has_mesh":False}
                            if cur["name"]:out["object_names"].append(cur["name"])
                        elif tag=="mesh" and cur is not None:cur["has_mesh"]=True
                        elif tag=="component":out["component_count"]+=1
                        elif tag=="item":out["build_item_count"]+=1
                        elif tag=="vertex" and cur is not None:
                            x=float(el.attrib["x"]);y=float(el.attrib["y"]);z0=float(el.attrib["z"]);seen=True
                            for i,q in enumerate((x,y,z0)):mn[i]=min(mn[i],q);mx[i]=max(mx[i],q)
                        elif tag=="triangle" and cur is not None:
                            a=int(el.attrib["v1"]);b=int(el.attrib["v2"]);c0=int(el.attrib["v3"])
                            if len({a,b,c0})<3:cur["degenerate"]+=1
                            else:
                                for u,v in ((a,b),(b,c0),(c0,a)):
                                    if u>v:u,v=v,u
                                    cur["edges"][(u,v)]+=1
                    elif tag=="object" and cur is not None:
                        if cur["has_mesh"]:
                            out["open_edges"]+=sum(1 for n in cur["edges"].values() if n==1)
                            out["nonmanifold_edges"]+=sum(1 for n in cur["edges"].values() if n>2)
                            out["degenerate_triangles"]+=cur["degenerate"]
                        cur=None
                    if ev=="end":el.clear()
            dims=[mx[i]-mn[i] for i in range(3)] if seen else [0,0,0]
            out["dimensions_mm"]=[round(x,3) for x in dims]
            bv=[float(x) for x in (build_volume or [180,180,180])[:3]]
            while len(bv)<3:bv.append(180.0)
            out["within_build_volume"]=bool(seen and all(mn[i]>=-0.051 and mx[i]<=bv[i]+0.051 for i in range(3)))
            expected=sorted(str(x) for x in (expected_names or []))
            actual=sorted(out["object_names"])
            out["expected_names"]=expected;out["actual_names"]=actual
            out["parts_match"]=actual==expected
            out["ok"]=bool(out["zip_ok"] and out["parts_match"] and out["object_count"]==len(expected) and
                           out["build_item_count"]==len(expected) and out["component_count"]==0 and
                           out["open_edges"]==0 and out["nonmanifold_edges"]==0 and out["degenerate_triangles"]==0 and
                           out["within_build_volume"])
    except Exception as e:out["error"]=str(e)
    return out

def export_print_plate_3mfs(req,parts,folder,tol):
    cad=req.get("cad_contract") or {};plan=cad.get("plate_plan") or {}
    plates=plan.get("plates") or []
    if not plates:
        return {"status":"NOT_PLANNED","ok":False,"reason":"cad_contract.plate_plan missing","artifacts":[],"reports":[]}
    by_name={str(p.get("name") or ""):p for p in parts}
    expected=set(by_name.keys());counts={name:0 for name in expected};reports=[];artifacts=[]
    reserve=plan.get("reservations") or {};margin=f(reserve.get("edge_margin_mm"),5)
    build_volume=plan.get("build_volume_mm") or (req.get("master_spec") or {}).get("build_volume_mm") or [180,180,180]
    for idx,plate in enumerate(plates,1):
        pidx=int(plate.get("index") or idx);assignments=plate.get("assignments") or []
        names=[];placements={}
        for a in assignments:
            anames=a.get("parts") or []
            for name in anames:
                name=str(name)
                if name not in by_name:continue
                counts[name]=counts.get(name,0)+1;names.append(name)
                placements[name]={"x_mm":f(a.get("x_mm"),0)+margin,"y_mm":f(a.get("y_mm"),0)+margin,
                                  "rotated_90":bool(a.get("rotated_90"))}
        selected=[by_name[n] for n in names if n in by_name]
        path=folder/("print_plate_"+str(pidx)+".3mf")
        if selected:
            write_meta=write_3mf(selected,path,tol,assembled=False,placements=placements,plate_index=pidx,plate_name=plate.get("name") or ("Plate "+str(pidx)))
            report=audit_print_plate_3mf(path,names,build_volume)
            report["write_meta"]=write_meta;report["planned_assignments"]=assignments
            reports.append(report)
            if report.get("ok"):
                artifacts.append({"type":"3mf","name":path.name,"url":"/v1/artifacts/"+folder.name+"/"+path.name})
        else:
            reports.append({"ok":False,"name":path.name,"error":"plate has no valid physical parts","planned_assignments":assignments})
    missing=sorted([n for n in expected if counts.get(n,0)==0])
    duplicates=sorted([n for n in expected if counts.get(n,0)>1])
    extras=sorted([n for n in counts if n not in expected and counts.get(n,0)>0])
    ok=bool(reports and all(r.get("ok") is True for r in reports) and not missing and not duplicates and not extras and len(artifacts)==len(plates))
    return {"status":"PASS" if ok else "FAIL","ok":ok,"plate_count":len(plates),"artifact_count":len(artifacts),
            "expected_parts":sorted(expected),"assignment_counts":counts,"missing_parts":missing,"duplicate_parts":duplicates,
            "extra_parts":extras,"build_volume_mm":build_volume,"edge_margin_mm":margin,"reports":reports,"artifacts":artifacts}

def export_all(req,parts,svg_geoms,folder,validation):
    family=str((req.get("cad_contract") or {}).get("family") or "")
    assembled_export=family in ("sculpted_lidded_container",)
    stl=folder/"model.stl";step=folder/"model.step";mf=folder/"model.3mf";glb=folder/"preview.glb";png=folder/"product_render_main.png"
    memory_guard(req,"export_brep_start",hard=True)
    shapes=[_part_shape(p,assembled_export).val() for p in parts]
    compound=cq.Compound.makeCompound(shapes)
    exporters.export(compound,str(stl),exportType="STL")
    release_process_memory(parts);memory_guard(req,"export_stl_done",hard=True)
    exporters.export(compound,str(step),exportType="STEP",opt={"write_pcurves":False})
    del compound,shapes
    release_process_memory(parts);memory_guard(req,"export_step_done",hard=True)
    mesh_tol=effective_mesh_tolerance(req.get("cad_contract") or {})
    write_3mf(parts,mf,mesh_tol,assembled=assembled_export)
    geometry_3mf_sha256=hashlib.sha256(mf.read_bytes()).hexdigest()
    release_process_memory(parts);memory_guard(req,"export_3mf_done",hard=True)
    plate_export=export_print_plate_3mfs(req,parts,folder,mesh_tol)
    validation["print_plate_plan"]=plate_export
    validation["print_plate_plan_ok"]=plate_export.get("ok") is True
    validation["print_plate_count"]=int(plate_export.get("plate_count") or 0)
    validation["print_plate_reports"]=plate_export.get("reports") or []
    release_process_memory(parts);memory_guard(req,"export_print_plates_done",hard=True)
    write_glb(parts,glb,mesh_tol,assembled=assembled_export)
    release_process_memory(parts);memory_guard(req,"export_glb_done",hard=True)
    artifacts=[{"type":"stl","name":"model.stl","url":"/v1/artifacts/"+folder.name+"/model.stl"},{"type":"step","name":"model.step","url":"/v1/artifacts/"+folder.name+"/model.step"},{"type":"3mf","name":"model.3mf","url":"/v1/artifacts/"+folder.name+"/model.3mf"},{"type":"glb","name":"preview.glb","url":"/v1/artifacts/"+folder.name+"/preview.glb"}]
    artifacts.extend(plate_export.get("artifacts") or [])
    render_views={};render_meshes=None
    render_files={
      "front":folder/"product_render_front.png",
      "back":folder/"product_render_back.png",
      "side":folder/"product_render_side.png",
      "perspective":folder/"product_render_perspective.png"
    }
    try:
        render_tol=resource_policy(req)["render_mesh_tolerance_mm"]
        render_meshes=tessellated_parts(parts,render_tol,assembled=True)
        for view_name,view_path in render_files.items():
            try:
                report=render_product_png(req,parts,svg_geoms,view_path,validation,camera_view=view_name,meshes=render_meshes)
                report["appearance_hash"]=req.get("appearance_hash")
                report["geometry_3mf_sha256"]=geometry_3mf_sha256
                render_views[view_name]=report
                if view_path.exists() and view_path.stat().st_size>1024:
                    artifacts.append({"type":"png","name":view_path.name,"url":"/v1/artifacts/"+folder.name+"/"+view_path.name,
                                      "view":view_name,"appearance_hash":req.get("appearance_hash"),"geometry_3mf_sha256":geometry_3mf_sha256})
                else:
                    render_views[view_name]={"status":"FAIL","view":view_name,"error":"render output missing or empty","appearance_hash":req.get("appearance_hash"),"geometry_3mf_sha256":geometry_3mf_sha256}
            except Exception as view_ex:
                render_views[view_name]={"status":"FAIL","view":view_name,"error":str(view_ex),"appearance_hash":req.get("appearance_hash"),"geometry_3mf_sha256":geometry_3mf_sha256}
        perspective=render_files["perspective"]
        if perspective.exists() and perspective.stat().st_size>1024:
            png.write_bytes(perspective.read_bytes())
            artifacts.append({"type":"png","name":"product_render_main.png","url":"/v1/artifacts/"+folder.name+"/product_render_main.png",
                              "view":"perspective_alias","appearance_hash":req.get("appearance_hash"),"geometry_3mf_sha256":geometry_3mf_sha256})
        validation["product_render_views"]={
          "status":"PASS" if all((render_views.get(v) or {}).get("status")=="PASS" for v in ("front","back","side","perspective")) else "FAIL",
          "required_views":["front","back","side","perspective"],
          "geometry_3mf_sha256":geometry_3mf_sha256,
          "appearance_hash":req.get("appearance_hash"),
          "views":render_views
        }
        validation["product_render_views_ok"]=validation["product_render_views"]["status"]=="PASS"
        validation["product_render"]=render_views.get("perspective") or {"status":"FAIL","error":"perspective render unavailable"}
    except Exception as ex:
        validation["product_render_views"]={"status":"FAIL","error":str(ex),"required_views":["front","back","side","perspective"],"geometry_3mf_sha256":geometry_3mf_sha256,"appearance_hash":req.get("appearance_hash"),"views":render_views}
        validation["product_render_views_ok"]=False
        validation["product_render"]={"status":"FAIL","error":str(ex)}
    finally:
        if render_meshes is not None:del render_meshes
    return artifacts

def audit_exports(folder,expected_parts,expected_dims):
    result={"ok":False,"files":{},"three_mf":{}}
    stl=folder/"model.stl";step=folder/"model.step";mf=folder/"model.3mf";glb=folder/"preview.glb"
    result["files"]["stl"]={"size":stl.stat().st_size if stl.exists() else 0,"ok":stl.exists() and stl.stat().st_size>84}
    step_head=step.read_bytes()[:256] if step.exists() else b""
    result["files"]["step"]={"size":step.stat().st_size if step.exists() else 0,"ok":b"ISO-10303-21" in step_head}
    glb_head=glb.read_bytes()[:12] if glb.exists() else b""
    glb_ok=len(glb_head)==12 and struct.unpack("<I",glb_head[:4])[0]==0x46546C67 and struct.unpack("<I",glb_head[4:8])[0]==2
    result["files"]["glb"]={"size":glb.stat().st_size if glb.exists() else 0,"ok":glb_ok}
    mf_ok=False
    if mf.exists():
        try:
            with zipfile.ZipFile(mf,"r") as z:
                bad=z.testzip();model_name=next((n for n in z.namelist() if n.lower().endswith(".model")),None)
                if bad is None and model_name:
                    object_count=item_count=component_count=0
                    mesh_reports=[];mf_open=mf_nonmanifold=mf_degenerate=0
                    mn=[float("inf")]*3;mx=[float("-inf")]*3;seen=False
                    cur=None
                    with z.open(model_name) as fh:
                        for ev,el in ET.iterparse(fh,events=("start","end")):
                            tag=str(el.tag).rsplit("}",1)[-1]
                            if ev=="start":
                                if tag=="object":
                                    object_count+=1;cur={"id":el.attrib.get("id"),"name":el.attrib.get("name"),"vertices":0,"triangles":0,"edges":Counter(),"degenerate":0,"has_mesh":False}
                                elif tag=="mesh" and cur is not None:cur["has_mesh"]=True
                                elif tag=="component":component_count+=1
                                elif tag=="item":item_count+=1
                                elif tag=="vertex" and cur is not None:
                                    x=float(el.attrib["x"]);y=float(el.attrib["y"]);z0=float(el.attrib["z"]);cur["vertices"]+=1;seen=True
                                    for i,q in enumerate((x,y,z0)):mn[i]=min(mn[i],q);mx[i]=max(mx[i],q)
                                elif tag=="triangle" and cur is not None:
                                    a=int(el.attrib["v1"]);b=int(el.attrib["v2"]);c0=int(el.attrib["v3"]);cur["triangles"]+=1
                                    if len({a,b,c0})<3:cur["degenerate"]+=1
                                    else:
                                        for u,v in ((a,b),(b,c0),(c0,a)):
                                            if u>v:u,v=v,u
                                            cur["edges"][(u,v)]+=1
                            elif tag=="object" and cur is not None:
                                if cur["has_mesh"]:
                                    oe=sum(1 for n in cur["edges"].values() if n==1);nm=sum(1 for n in cur["edges"].values() if n>2);deg=cur["degenerate"]
                                    mf_open+=oe;mf_nonmanifold+=nm;mf_degenerate+=deg
                                    mesh_reports.append({"object_id":cur["id"],"name":cur["name"],"vertices":cur["vertices"],"triangles":cur["triangles"],"open_edges":oe,"nonmanifold_edges":nm,"degenerate_triangles":deg})
                                cur=None;gc.collect()
                            if ev=="end":el.clear()
                    dims=[mx[i]-mn[i] for i in range(3)] if seen else [0,0,0]
                    dim_match=all(abs(float(dims[i])-float(expected_dims[i]))<=0.05 for i in range(3))
                    native_parts=(object_count==expected_parts+1 and item_count==1 and component_count==expected_parts)
                    legacy_parts=(object_count==expected_parts and item_count==expected_parts)
                    mesh_topology_ok=(mf_open==0 and mf_nonmanifold==0 and mf_degenerate==0)
                    mf_ok=(native_parts or legacy_parts) and dim_match and mesh_topology_ok
                    result["three_mf"]={"object_count":object_count,"build_item_count":item_count,"component_part_count":component_count,"native_bambu_parts":native_parts,"dimensions_mm":[round(x,3) for x in dims],"dimensions_match":dim_match,"zip_ok":True,"mesh_topology_ok":mesh_topology_ok,"open_edges":mf_open,"nonmanifold_edges":mf_nonmanifold,"degenerate_triangles":mf_degenerate,"mesh_reports":mesh_reports,"audit_mode":"streaming_iterparse"}
        except Exception as ex:
            result["three_mf"]={"zip_ok":False,"error":str(ex)}
    result["files"]["3mf"]={"size":mf.stat().st_size if mf.exists() else 0,"ok":mf_ok}
    result["ok"]=all(x.get("ok") is True for x in result["files"].values())
    return result

def _run_generate_job_inner(jid,req,folder):
    started=time.time()
    try:
        JOBS[jid].update({"stage":"building","updated_at":time.time()})
        rss_start=memory_guard(req,"build_start",hard=True)
        parts,svg_geoms,hole_tools,invalid=build(req)
        JOBS[jid].update({"stage":"validating","updated_at":time.time()})
        rss_built=memory_guard(req,"build_done",hard=True)
        validation=validate_parts(req,parts,svg_geoms,hole_tools,invalid)
        released_after_validation=release_process_memory(parts)
        rss_validated=memory_guard(req,"validation_done",hard=True)
        validation["tessellation_cache_released"]=bool(released_after_validation.get("cache_cleaned"))
        validation["malloc_trim_after_validation"]=bool(released_after_validation.get("malloc_trim"))
        family=str((req.get("cad_contract") or {}).get("family") or "")
        assembled_export=family in ("sculpted_lidded_container",)
        mesh_tol=effective_mesh_tolerance(req.get("cad_contract") or {})
        expected_export_dims=tessellated_dimensions(parts,mesh_tol,assembled=assembled_export)
        release_process_memory(parts);memory_guard(req,"dimension_check_done",hard=True)
        JOBS[jid].update({"stage":"exporting","updated_at":time.time()})
        artifacts=export_all(req,parts,svg_geoms,folder,validation)
        gc.collect();rss_exported=memory_guard(req,"all_exports_done",hard=True)
        export_audit=audit_exports(folder,len(parts),expected_export_dims)
        gc.collect();rss_audited=memory_guard(req,"artifact_audit_done",hard=True)
        validation["resource_usage"]={"mode":"quality_preserving_memory_budget","rss_mb":{"start":rss_start,"after_build":rss_built,"after_validation":rss_validated,"after_export":rss_exported,"after_audit":rss_audited},"policy":resource_policy(req)}
        validation["export_expected_dimensions_mm"]=expected_export_dims
        validation["export_audit"]=export_audit
        validation["preview_matches_export"]=export_audit["ok"]
        mf_top=(export_audit.get("three_mf") or {})
        validation["bambu_mesh_topology_ok"]=mf_top.get("mesh_topology_ok") is True
        validation["exported_3mf_open_edges"]=int(mf_top.get("open_edges") or 0)
        validation["exported_3mf_nonmanifold_edges"]=int(mf_top.get("nonmanifold_edges") or 0)
        validation["exported_3mf_degenerate_triangles"]=int(mf_top.get("degenerate_triangles") or 0)
        required=["brep_valid","watertight","hole_penetration","unintended_through_cut_free","part_intent_ok","orphan_geometry_free","smooth_vector_ok","typography_ok","text_min_stroke_ok","glyph_clearance_ok","text_internal_clearance_ok","text_slicer_no_merge_ok","text_layout_bounds_ok","text_readability_ok","min_feature_ok","structural_min_ok","feature_containment_ok","clearance_ok","assembly_parts_ok","first_layer_contact_ok","island_free","support_profile_ok","a1_mini_fit","ams_colors_ok","nozzle_profile_ok","contract_svg_match","contract_dimensions_ok","appearance_hash_match","preview_matches_export","bambu_mesh_topology_ok"]
        if (req.get("cad_contract") or {}).get("plate_plan"):required.append("print_plate_plan_ok")
        pass_core=all(validation.get(k) is True for k in required)
        pass_neg=validation.get("self_intersection") is False and validation.get("part_overlap") is False and validation.get("assembly_interference") is False and validation.get("repair_needed") is False and validation.get("zero_volume") is False and validation.get("open_edges")==0 and validation.get("nonmanifold_edges")==0 and validation.get("degenerate_triangles")==0
        validation["status"]="PASS" if pass_core and pass_neg else "FAIL"
        manifest={"module":req.get("module"),"module_version":req.get("module_version"),"appearance_hash":req.get("appearance_hash"),"appearance_lock":req.get("appearance_lock"),"svg_artifact":req.get("svg_artifact"),"cad_contract":req.get("cad_contract"),"printer_profile":req.get("printer_profile","bambu_a1_mini_04"),"validation":validation,"parts":[{"name":p["name"],"role":p["role"],"color":p["color"]} for p in parts],"artifacts":artifacts}
        (folder/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
        artifacts.append({"type":"json","name":"manifest.json","url":"/v1/artifacts/"+jid+"/manifest.json"})
        JOBS[jid].update({"status":"completed","stage":"completed","artifacts":artifacts,"validation":validation,"appearance_hash":req.get("appearance_hash"),"duration_ms":int((time.time()-started)*1000),"updated_at":time.time()})
    except Exception as ex:
        msg=str(ex);is_self=msg.startswith("SVG_SELF_INTERSECTION:")
        validation={"status":"FAIL","geometry_error":msg,"self_intersection":is_self,"appearance_hash_match":(req.get("appearance_lock") or {}).get("appearance_hash")==req.get("appearance_hash") and bool(req.get("appearance_hash"))}
        JOBS[jid].update({"status":"failed","stage":"failed","error":msg,"validation":validation,"artifacts":[],"appearance_hash":req.get("appearance_hash"),"duration_ms":int((time.time()-started)*1000),"updated_at":time.time()})
        print("async generate failed:",jid,repr(ex),flush=True)
    finally:
        gc.collect()

def _run_generate_job(jid,req,folder):
    with HEAVY_JOB_SEMAPHORE:
        memory_guard(req,"generate_start",hard=True)
        return _run_generate_job_inner(jid,req,folder)

def _job_log_tail(path,max_bytes=6000):
    try:
        data=path.read_bytes()
        return data[-max_bytes:].decode("utf-8","replace")
    except Exception:return ""

def _run_generate_job_isolated(jid,req,folder):
    started=time.time();request_path=folder/"isolated_request.json";result_path=folder/"isolated_result.json";log_path=folder/"isolated_child.log"
    with HEAVY_JOB_SEMAPHORE:
        try:
            request_path.write_text(json.dumps(req,ensure_ascii=False),encoding="utf-8")
            JOBS[jid].update({"stage":"isolated_starting","execution_mode":"isolated_subprocess","updated_at":time.time()})
            with log_path.open("wb") as log:
                proc=subprocess.Popen([sys.executable,str(pathlib.Path(__file__).resolve()),"--generate-child",jid,str(folder)],stdout=log,stderr=subprocess.STDOUT,env=os.environ.copy())
                JOBS[jid].update({"stage":"isolated_running","child_pid":int(proc.pid),"updated_at":time.time()})
                try:rc=proc.wait(timeout=max(60,int(f((req.get("cad_contract") or {}).get("job_timeout_seconds"),180))))
                except subprocess.TimeoutExpired:
                    proc.kill();proc.wait(timeout=10)
                    raise TimeoutError("UNIVERSAL_CAD_CHILD_TIMEOUT")
            if result_path.exists():
                child=json.loads(result_path.read_text(encoding="utf-8"))
                if isinstance(child,dict):
                    child["execution_mode"]="isolated_subprocess";child["child_exit_code"]=int(rc);child["child_log_tail"]=_job_log_tail(log_path,2500) if child.get("status")=="failed" else None
                    JOBS[jid].update(child)
                    JOBS[jid]["idempotency_key"]=req.get("idempotency_key") or JOBS[jid].get("idempotency_key")
                    JOBS[jid]["updated_at"]=time.time()
                    return
            raise RuntimeError("UNIVERSAL_CAD_CHILD_NO_RESULT:exit="+str(rc)+":"+_job_log_tail(log_path,2500))
        except Exception as ex:
            JOBS[jid].update({"status":"failed","stage":"failed","error":str(ex),"validation":{"status":"FAIL","geometry_error":str(ex)},"artifacts":[],"execution_mode":"isolated_subprocess","duration_ms":int((time.time()-started)*1000),"child_log_tail":_job_log_tail(log_path,2500),"updated_at":time.time()})
            print("isolated generate failed:",jid,repr(ex),flush=True)

def _generate_child_cli(jid,folder):
    folder=pathlib.Path(folder);req_path=folder/"isolated_request.json";result_path=folder/"isolated_result.json"
    req=json.loads(req_path.read_text(encoding="utf-8"))
    JOBS[jid]={"status":"processing","stage":"child_start","idempotency_key":req.get("idempotency_key"),"artifacts":[],"created_at":time.time(),"updated_at":time.time(),"appearance_hash":req.get("appearance_hash"),"execution_mode":"isolated_child"}
    _run_generate_job_inner(jid,req,folder)
    out=dict(JOBS.get(jid) or {});out["execution_mode"]="isolated_child"
    tmp=result_path.with_suffix(".tmp");tmp.write_text(json.dumps(out,ensure_ascii=False),encoding="utf-8");tmp.replace(result_path)
    return 0 if out.get("status")=="completed" else 2

def generate(req):
    key=req.get("idempotency_key") or str(uuid.uuid4())
    for jid,j in list(JOBS.items()):
        if j.get("idempotency_key")==key:return {"job_id":jid,"status":j["status"]}
    jid=str(uuid.uuid4());folder=ROOT/jid;folder.mkdir(parents=True,exist_ok=True)
    JOBS[jid]={"status":"processing","stage":"queued","idempotency_key":key,"artifacts":[],"created_at":time.time(),"updated_at":time.time(),"appearance_hash":req.get("appearance_hash")}
    family=str((req.get("cad_contract") or {}).get("family") or "")
    isolated=family in ("universal_cad_recipe","static_functional_utensil_vessel")
    target=_run_generate_job_isolated if isolated else _run_generate_job
    threading.Thread(target=target,args=(jid,req,folder),daemon=True,name=("makersence-iso-" if isolated else "makersence-")+jid[:8]).start()
    return {"job_id":jid,"status":"processing","execution_mode":"isolated_subprocess" if isolated else "in_process"}

def inspect_sliced_3mf(path):
    out={"zip_ok":False,"gcode_entries":[],"slicedata_entries":[],"toolpath_checked":False,"layer_markers":0,"gcode_bytes":0}
    if not path.exists() or path.stat().st_size<64:return out
    try:
        with zipfile.ZipFile(path,"r") as z:
            bad=z.testzip()
            out["zip_ok"]=bad is None
            names=z.namelist()
            gc=[n for n in names if n.lower().endswith(".gcode") or ".gcode." in n.lower()]
            sd=[n for n in names if ("slice" in n.lower() or "plate" in n.lower()) and n.lower().endswith((".config",".json",".xml"))]
            out["gcode_entries"]=gc
            out["slicedata_entries"]=sd[:80]
            chunks=[];total=0
            for name in gc[:8]:
                b=z.read(name);total+=len(b)
                if sum(len(x) for x in chunks)<8_000_000:chunks.append(b[:4_000_000])
            out["gcode_bytes"]=total
            txt=b"\n".join(chunks).decode("utf-8","ignore")
            out["layer_markers"]=len(re.findall(r"(?:^|\n)(?:;LAYER_CHANGE|; layer num/|;LAYER:)",txt,re.I))
            out["toolpath_checked"]=bool(out["zip_ok"] and gc and total>1000)
    except Exception as ex:
        out["error"]=str(ex)
    return out

def _run_slice_job_inner(jid,input_path,output_path,slicedata_dir):
    started=time.time()
    env=os.environ.copy();env["HOME"]=str(BAMBU_HOME)
    runtime_dir=pathlib.Path("/tmp/bambu-xdg-runtime")
    runtime_dir.mkdir(parents=True,exist_ok=True)
    try:runtime_dir.chmod(0o700)
    except Exception:pass
    env["XDG_RUNTIME_DIR"]=str(runtime_dir)
    env["XDG_SESSION_TYPE"]="x11"
    env["GDK_BACKEND"]="x11"
    env["LIBGL_ALWAYS_SOFTWARE"]="1"
    env["GALLIUM_DRIVER"]="llvmpipe"
    env["MESA_LOADER_DRIVER_OVERRIDE"]="llvmpipe"
    env["WEBKIT_DISABLE_DMABUF_RENDERER"]="1"
    env.pop("WAYLAND_DISPLAY",None)
    cmd=["xvfb-run","-a","-s","-screen 0 1024x768x24 +extension GLX +render -noreset",BAMBU_BIN,"--slice","0","--debug","2","--export-slicedata",str(slicedata_dir),"--export-3mf",str(output_path),str(input_path)]
    try:
        if not BAMBU_BIN or not pathlib.Path(BAMBU_BIN).exists():raise RuntimeError("Bambu Studio CLI unavailable")
        p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env,timeout=240)
        log=p.stdout.decode("utf-8","ignore")[-30000:]
        report=inspect_sliced_3mf(output_path)
        ok=p.returncode==0 and report.get("toolpath_checked") is True
        SLICE_JOBS[jid].update({
            "status":"completed" if ok else "failed",
            "returncode":p.returncode,
            "log_tail":log,
            "validation":{
                "status":"PASS" if ok else "FAIL",
                "real_slicer_verified":ok,
                "toolpath_checked":report.get("toolpath_checked",False),
                "engine":"Bambu Studio",
                "engine_version":BAMBU_VERSION,
                "slice_plate":0,
                "output_3mf_bytes":output_path.stat().st_size if output_path.exists() else 0,
                **report
            },
            "artifact":"/v1/slice-artifacts/"+jid+"/bambu_sliced.3mf" if output_path.exists() else None,
            "duration_ms":int((time.time()-started)*1000),
            "updated_at":time.time()
        })
    except Exception as ex:
        SLICE_JOBS[jid].update({"status":"failed","error":str(ex),"validation":{"status":"FAIL","real_slicer_verified":False,"engine":"Bambu Studio","engine_version":BAMBU_VERSION},"duration_ms":int((time.time()-started)*1000),"updated_at":time.time()})
        print("slice failed:",jid,repr(ex),flush=True)
    finally:
        gc.collect()

def _run_slice_job(jid,input_path,output_path,slicedata_dir):
    with HEAVY_JOB_SEMAPHORE:
        return _run_slice_job_inner(jid,input_path,output_path,slicedata_dir)

def submit_slice(data,idempotency_key=None):
    if not isinstance(data,(bytes,bytearray)) or len(data)<64 or data[:2]!=b"PK":raise ValueError("input is not a zip-based 3MF")
    key=idempotency_key or hashlib.sha256(data).hexdigest()
    for jid,j in list(SLICE_JOBS.items()):
        if j.get("idempotency_key")==key:return {"job_id":jid,"status":j["status"]}
    jid=str(uuid.uuid4());folder=ROOT/("slice-"+jid);folder.mkdir(parents=True,exist_ok=True)
    inp=folder/"input.3mf";out=folder/"bambu_sliced.3mf";sd=folder/"slicedata";sd.mkdir(exist_ok=True);inp.write_bytes(data)
    SLICE_JOBS[jid]={"status":"processing","idempotency_key":key,"source_sha256":hashlib.sha256(data).hexdigest(),"created_at":time.time(),"updated_at":time.time(),"folder":str(folder)}
    threading.Thread(target=_run_slice_job,args=(jid,inp,out,sd),daemon=True,name="slice-"+jid[:8]).start()
    return {"job_id":jid,"status":"processing"}

def _dw_ptx(p,m):
    if not m:return p
    return [m[0][0]*p[0]+m[0][1]*p[1]+m[0][2]*p[2]+m[0][3],m[1][0]*p[0]+m[1][1]*p[1]+m[1][2]*p[2]+m[1][3],m[2][0]*p[0]+m[2][1]*p[1]+m[2][2]*p[2]+m[2][3]]
def _dw_bbox(vv):
    mn=[float('inf')]*3;mx=[float('-inf')]*3
    for p in vv:
        for i in range(3):mn[i]=min(mn[i],p[i]);mx[i]=max(mx[i],p[i])
    return {'min':mn,'max':mx,'dimensions':[max(0,mx[i]-mn[i]) for i in range(3)]}
def _dw_extract_mesh(z,part,world=None):
    path=str(part.get('source_model_path') or '').lstrip('/');oid=str(part.get('source_object_id') or '');vv=[];tt=[];inside=False;scale=1.0
    scales={'micron':.001,'millimeter':1.0,'centimeter':10.0,'inch':25.4,'meter':1000.0}
    with z.open(path) as fh:
        for ev,el in ET.iterparse(fh,events=('start','end')):
            tag=str(el.tag).rsplit('}',1)[-1]
            if ev=='start':
                if tag=='model':scale=scales.get(str(el.attrib.get('unit','millimeter')).lower(),1.0)
                elif tag=='object':inside=str(el.attrib.get('id') or '')==oid
                continue
            if inside and tag=='vertex':vv.append(_dw_ptx([f(el.attrib.get('x'))*scale,f(el.attrib.get('y'))*scale,f(el.attrib.get('z'))*scale],world))
            elif inside and tag=='triangle':
                try:tt.append([int(el.attrib.get('v1')),int(el.attrib.get('v2')),int(el.attrib.get('v3'))])
                except:pass
            elif tag=='object' and inside:break
            el.clear()
    return {'name':part.get('name') or 'Part','vertices':vv,'triangles':tt}
def _dw_edge_plane(a,b,axis,value,eps=1e-6):
    da=a[axis]-value;db=b[axis]-value
    if abs(da)<eps and abs(db)<eps:return None
    if (da>eps and db>eps) or (da<-eps and db<-eps):return None
    den=da-db
    if abs(den)<eps:return None
    t=da/den
    if t<-eps or t>1+eps:return None
    return [a[i]+(b[i]-a[i])*t for i in range(3)]
def _dw_to2(p,axis):return [p[1],p[2]] if axis==0 else ([p[0],p[2]] if axis==1 else [p[0],p[1]])
def _dw_slice(mesh,axis,value):
    vv=mesh['vertices'];out=[]
    for t in mesh['triangles']:
        ps=[vv[t[0]],vv[t[1]],vv[t[2]]];hits=[]
        for i,j in ((0,1),(1,2),(2,0)):
            q=_dw_edge_plane(ps[i],ps[j],axis,value)
            if q is not None and not any(math.dist(q,h)<1e-5 for h in hits):hits.append(q)
        if len(hits)>=2:
            best=(hits[0],hits[1]);bd=math.dist(*best)
            for i in range(len(hits)):
                for j in range(i+1,len(hits)):
                    d=math.dist(hits[i],hits[j])
                    if d>bd:best=(hits[i],hits[j]);bd=d
            if bd>.001:out.append([_dw_to2(best[0],axis),_dw_to2(best[1],axis)])
    return out
def _dw_loops(segs,tol=.07):
    nodes={};edges=[]
    def node(p):
        k=(round(p[0]/tol),round(p[1]/tol))
        if k not in nodes:nodes[k]={'p':p,'e':[]}
        return nodes[k]
    for s in segs:
        a=node(s[0]);b=node(s[1])
        if a is b:continue
        e={'a':a,'b':b,'u':False};a['e'].append(e);b['e'].append(e);edges.append(e)
    out=[]
    for e0 in edges:
        if e0['u']:continue
        start=e0['a'];prev=start;cur=e0['b'];e0['u']=True;pts=[start['p'],cur['p']];closed=False
        for _ in range(10000):
            if cur is start:closed=True;break
            choices=[e for e in cur['e'] if not e['u']]
            if not choices:break
            chosen=choices[0]
            if len(choices)>1:
                vin=[cur['p'][0]-prev['p'][0],cur['p'][1]-prev['p'][1]];vl=math.hypot(*vin) or 1;best=-2
                for e in choices:
                    nx=e['b'] if e['a'] is cur else e['a'];vo=[nx['p'][0]-cur['p'][0],nx['p'][1]-cur['p'][1]];ol=math.hypot(*vo) or 1;score=(vin[0]*vo[0]+vin[1]*vo[1])/(vl*ol)
                    if score>best:best=score;chosen=e
            chosen['u']=True;nx=chosen['b'] if chosen['a'] is cur else chosen['a'];prev,cur=cur,nx;pts.append(cur['p'])
        if closed and len(pts)>=4:out.append(pts[:-1])
    return out
def _dw_area(p):return sum(p[(i-1)%len(p)][0]*p[i][1]-p[i][0]*p[(i-1)%len(p)][1] for i in range(len(p)))/2 if p else 0
def _dw_per(p):return sum(math.dist(p[i],p[(i+1)%len(p)]) for i in range(len(p)))
def _dw_centroid(p):
    a=_dw_area(p)
    if abs(a)<1e-9:return [sum(x[0] for x in p)/len(p),sum(x[1] for x in p)/len(p)]
    x=y=0
    for i in range(len(p)):
        j=(i-1)%len(p);v=p[j][0]*p[i][1]-p[i][0]*p[j][1];x+=(p[j][0]+p[i][0])*v;y+=(p[j][1]+p[i][1])*v
    return [x/(6*a),y/(6*a)]
def _dw_inside(pt,p):
    c=False;j=len(p)-1
    for i in range(len(p)):
        a=p[i];b=p[j]
        if ((a[1]>pt[1])!=(b[1]>pt[1])) and pt[0]<(b[0]-a[0])*(pt[1]-a[1])/(b[1]-a[1]+1e-9)+a[0]:c=not c
        j=i
    return c
def _dw_section_holes(mesh,axis,value):
    info=[]
    for p in _dw_loops(_dw_slice(mesh,axis,value)):
        area=abs(_dw_area(p))
        if area<=.2:continue
        per=_dw_per(p);c=_dw_centroid(p);xs=[x[0] for x in p];ys=[x[1] for x in p];info.append({'p':p,'area':area,'per':per,'c':c,'bbox':[max(xs)-min(xs),max(ys)-min(ys)],'circ':4*math.pi*area/(per*per) if per else 0})
    info.sort(key=lambda x:-x['area']);return [x for i,x in enumerate(info) if sum(1 for y in info[:i] if _dw_inside(x['c'],y['p']))%2==1]
def _dw_openings(mesh):
    if len(mesh['triangles'])>160000:return []
    b=_dw_bbox(mesh['vertices']);samples=[];out=[]
    for axis in range(3):
        d=b['dimensions'][axis]
        if d<.8:continue
        for fr in (.22,.5,.78):samples.append((axis,fr,_dw_section_holes(mesh,axis,b['min'][axis]+d*fr)))
    for axis in range(3):
        groups=[]
        for ax,fr,holes in samples:
            if ax!=axis:continue
            for h in holes:
                g=None
                for q in groups:
                    bw=max(q['bbox'][0],h['bbox'][0],1);bh=max(q['bbox'][1],h['bbox'][1],1)
                    if math.dist(q['c'],h['c'])<=max(.5,.05*max(bw,bh)) and abs(q['bbox'][0]-h['bbox'][0])<=max(.5,bw*.12) and abs(q['bbox'][1]-h['bbox'][1])<=max(.5,bh*.12):g=q;break
                if g is None:g={'c':h['c'][:],'bbox':h['bbox'][:],'areas':[],'circ':[],'fr':[]};groups.append(g)
                n=len(g['fr']);g['c']=[(g['c'][0]*n+h['c'][0])/(n+1),(g['c'][1]*n+h['c'][1])/(n+1)];g['bbox']=[(g['bbox'][0]*n+h['bbox'][0])/(n+1),(g['bbox'][1]*n+h['bbox'][1])/(n+1)];g['areas'].append(h['area']);g['circ'].append(h['circ']);g['fr'].append(fr)
        for g in groups:
            count=len(set(round(x,2) for x in g['fr']))
            if count<2:continue
            aspect=max(g['bbox'])/max(.001,min(g['bbox']));circ=sum(g['circ'])/len(g['circ']);kind='circular_hole' if circ>.78 and aspect<1.25 else ('slot_or_elongated_hole' if aspect>1.45 else 'internal_opening');eq=2*math.sqrt((sum(g['areas'])/len(g['areas']))/math.pi)
            out.append({'axis':'XYZ'[axis],'kind':kind,'center_2d_mm':[round(x,2) for x in g['c']],'opening_size_mm':[round(x,2) for x in g['bbox']],'equivalent_diameter_mm':round(eq,2),'circularity':round(circ,3),'persistence':str(count)+'/3','confidence':'HIGH' if count==3 else 'MEDIUM','measurement_quality':'CALCULATED'})
    return out
def _dw_normal(a,b,c):
    u=[b[i]-a[i] for i in range(3)];v=[c[i]-a[i] for i in range(3)];n=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]];l=math.sqrt(sum(x*x for x in n))
    return [x/l for x in n] if l>1e-9 else [0,0,0]
def _dw_edges(mesh):
    vv=mesh['vertices'];tt=mesh['triangles']
    if len(tt)>160000:return {'front':[],'top':[],'right':[]},[],'skipped_complex_mesh'
    normals=[];emap={};b=_dw_bbox(vv)
    for i,t in enumerate(tt):
        normals.append(_dw_normal(vv[t[0]],vv[t[1]],vv[t[2]]))
        for u,v in ((t[0],t[1]),(t[1],t[2]),(t[2],t[0])):
            k=(u,v) if u<v else (v,u);emap.setdefault(k,[]).append(i)
    segs={'front':[],'top':[],'right':[]};groups={}
    def proj(p,v):return [p[0],p[2]] if v=='front' else ([p[1],p[2]] if v=='right' else [p[0],p[1]])
    def corner(mid):
        opts=[]
        for i in range(3):
            d=max(b['dimensions'][i],.001);opts.extend([(abs(mid[i]-b['min'][i])/d,i,'MIN'),(abs(b['max'][i]-mid[i])/d,i,'MAX')])
        opts.sort();a=opts[0];bb=next((x for x in opts if x[1]!=a[1]),None)
        if not bb or a[0]>.22 or bb[0]>.22:return None
        z=sorted([a,bb],key=lambda x:x[1]);free=next(i for i in range(3) if i not in (z[0][1],z[1][1]));return ('XYZ'[z[0][1]]+z[0][2]+'_'+'XYZ'[z[1][1]]+z[1][2],z,free)
    for (u,v),tris in emap.items():
        a=vv[u];bb=vv[v];elen=math.dist(a,bb)
        if elen<.04:continue
        angle=180
        if len(tris)==2:angle=math.degrees(math.acos(max(-1,min(1,sum(normals[tris[0]][i]*normals[tris[1]][i] for i in range(3))))))
        if len(tris)==1 or angle>=24:
            for view in segs:
                if len(segs[view])<1000:
                    p=proj(a,view);q=proj(bb,view);segs[view].append([round(p[0],3),round(p[1],3),round(q[0],3),round(q[1],3)])
        if len(tris)!=2:continue
        mid=[(a[i]+bb[i])/2 for i in range(3)];g=corner(mid);typ='chamfer' if 28<=angle<=72 else ('fillet' if 1.4<=angle<=19 else None)
        if not g or not typ:continue
        x=groups.setdefault((typ,g[0]),{'p':[],'l':[],'a':[],'g':g});x['p'].append(mid);x['l'].append(elen);x['a'].append(angle)
    out=[]
    for (typ,key),x in groups.items():
        count=len(x['p']);total=sum(x['l']);free=max(b['dimensions'][x['g'][2]],.1);mean=sum(x['a'])/count
        if typ=='chamfer' and (count<2 or total<min(1,free*.16)):continue
        if typ=='fillet' and (count<6 or total<min(1.2,free*.22)):continue
        center=[sum(p[i] for p in x['p'])/count for i in range(3)];vals=[]
        for p in x['p']:
            ds=[]
            for _,axis,side in x['g'][1]:ds.append(p[axis]-b['min'][axis] if side=='MIN' else b['max'][axis]-p[axis])
            vals.append(max([0]+ds))
        vals.sort();est=vals[min(len(vals)-1,round((len(vals)-1)*.9))] if vals else 0;est=round(est,2) if est>=.15 else None;conf='HIGH' if count>=14 and total>=free*.55 else 'MEDIUM'
        if typ=='chamfer':out.append({'kind':'chamfer','location':key,'center_mm':[round(q,2) for q in center],'angle_deg':round(mean,1),'size_mm':est,'callout':('C'+str(est)+' × '+str(round(mean))+'°') if est else ('倒角 · '+str(round(mean))+'°'),'confidence':conf,'measurement_quality':'ESTIMATED' if est else 'DETECTED'})
        else:out.append({'kind':'fillet','location':key,'center_mm':[round(q,2) for q in center],'radius_mm':est,'callout':('R'+str(est)) if est else '圓角','confidence':conf,'measurement_quality':'ESTIMATED' if est else 'DETECTED'})
    return segs,out[:32],'analyzed'
def _dw_hull(points):
    pts=sorted(set((round(p[0],3),round(p[1],3)) for p in points))
    if len(pts)<=2:return [list(x) for x in pts]
    def cr(o,a,b):return (a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0])
    lo=[]
    for p in pts:
        while len(lo)>=2 and cr(lo[-2],lo[-1],p)<=0:lo.pop()
        lo.append(p)
    hi=[]
    for p in reversed(pts):
        while len(hi)>=2 and cr(hi[-2],hi[-1],p)<=0:hi.pop()
        hi.append(p)
    return [list(x) for x in lo[:-1]+hi[:-1]][:240]
def _dw_view(mesh,view,segs):
    vv=mesh['vertices'];step=max(1,math.ceil(len(vv)/6000));pts=[]
    for i in range(0,len(vv),step):
        p=vv[i];pts.append([p[0],p[2]] if view=='front' else ([p[1],p[2]] if view=='right' else [p[0],p[1]]))
    return {'id':view,'label':{'front':'FRONT VIEW','top':'TOP VIEW','right':'RIGHT VIEW'}[view],'outline':_dw_hull(pts),'feature_segments':segs.get(view,[]),'projection':'orthographic'}
def _dw_compact_loop(loop,max_points=160):
    a=list(loop or [])
    if len(a)<=max_points:return [[round(p[0],3),round(p[1],3)] for p in a]
    step=max(1,math.ceil(len(a)/max_points))
    return [[round(a[i][0],3),round(a[i][1],3)] for i in range(0,len(a),step)][:max_points]

def _dw_mode_info(values):
    c={}
    for x in values:c[x]=c.get(x,0)+1
    if not c:return (None,0,0.0)
    k=max(c,key=c.get);return (k,c[k],c[k]/len(values))

def _dw_reconstruction(mesh,b):
    axes=['X','Y','Z'];max_dim=max(b['dimensions'] or [1]) or 1;probes=[];probe_fracs=(.12,.50,.88)
    tri_count=len(mesh.get('triangles') or [])
    for ai,axis in enumerate(axes):
        dim=float(b['dimensions'][ai] or 0)
        if dim<.8:continue
        counts=[];valid=0;point_count=0
        for fr in probe_fracs:
            loops=[x for x in _dw_loops(_dw_slice(mesh,ai,b['min'][ai]+dim*fr)) if len(x)>=6][:8]
            counts.append(len(loops))
            if loops:valid+=1
            point_count+=sum(len(x) for x in loops)
        dom,dom_count,ratio=_dw_mode_info(counts)
        changes=sum(1 for i in range(1,len(counts)) if counts[i]!=counts[i-1])
        avg=sum(counts)/len(counts) if counts else 99
        stability=max(0.0,min(1.0,ratio-(changes/max(1,len(counts)-1))*.18))
        score=valid*1000+stability*650+(dim/max_dim)*220-max(0,avg-2)*90-changes*45+min(point_count,300)*.05
        probes.append({'axis':axis,'axis_index':ai,'dimension_mm':round(dim,3),'probe_loop_counts':counts,'probe_topology_stability':round(stability,3),'score':round(score,2),'dominant_loop_count':dom})
    probes.sort(key=lambda x:x['score'],reverse=True)
    if not probes:return {'version':'reconstruction-sections-v2-remote','status':'insufficient','axis':None,'section_count':0,'topology_stability':0,'sections':[],'candidates':[]}
    best=probes[0];ai=best['axis_index'];dim=float(b['dimensions'][ai]);fracs=(.005,.02,.06,.18,.34,.50,.66,.82,.94,.98,.995)
    sections=[];counts=[];point_count=0
    for fr in fracs:
        at=b['min'][ai]+dim*fr
        loops=[x for x in _dw_loops(_dw_slice(mesh,ai,at)) if len(x)>=6][:8]
        if not loops:continue
        compact=[_dw_compact_loop(x) for x in loops]
        counts.append(len(compact));point_count+=sum(len(x) for x in compact)
        sections.append({'id':'RECON_'+best['axis']+'_'+str(round(fr*100)).zfill(2),'cut_axis':best['axis'],'at_mm':round(at,3),'fraction':fr,'profile_loops':compact,'view_axes':['Y','Z'] if ai==0 else (['X','Z'] if ai==1 else ['X','Y'])})
    dom,dom_count,ratio=_dw_mode_info(counts)
    changes=sum(1 for i in range(1,len(counts)) if counts[i]!=counts[i-1])
    avg=sum(counts)/len(counts) if counts else 99
    variance=(sum((x-avg)*(x-avg) for x in counts)/len(counts)) if counts else 999
    stability=max(0.0,min(1.0,ratio-(changes/max(1,len(counts)-1))*.18-min(.28,variance*.08)))
    ready=len(sections)>=3 and stability>=.34
    status='ready' if ready else ('review' if len(sections)>=3 else 'insufficient')
    return {'version':'reconstruction-sections-v2-remote','status':status,'axis':best['axis'],'section_count':len(sections),'topology_stability':round(stability,3),'dominant_loop_count':dom,'triangle_count':tri_count,'fractions':list(fracs),'candidates':[{k:v for k,v in x.items() if k!='axis_index'} for x in probes],'sections':sections}

def _dw_drawing(z,part,world=None):
    if not part:return {'version':'cad-drawing-v1','status':'unavailable','reason':'no_part'}
    mesh=_dw_extract_mesh(z,part,world)
    if not mesh['vertices'] or not mesh['triangles']:return {'version':'cad-drawing-v1','status':'unavailable','reason':'no_mesh'}
    b=_dw_bbox(mesh['vertices']);openings=_dw_openings(mesh);segs,treatments,edge_status=_dw_edges(mesh);reconstruction=_dw_reconstruction(mesh,b);dims=[]
    for i,axis in enumerate('XYZ'):dims.append({'id':'OVERALL_'+axis,'type':'linear','axis':axis,'value_mm':round(b['dimensions'][i],3),'label':str(round(b['dimensions'][i],2)),'priority':'REQUIRED','quality':'MEASURED'})
    for i,h in enumerate(openings,1):
        view={'X':'right','Y':'front','Z':'top'}[h['axis']]
        if h['kind']=='circular_hole':dims.append({'id':'HOLE_'+str(i),'type':'diameter','feature':'hole','axis':h['axis'],'view':view,'value_mm':h['equivalent_diameter_mm'],'label':'Ø'+format(h['equivalent_diameter_mm'],'.2f'),'center_2d_mm':h['center_2d_mm'],'priority':'REQUIRED','confidence':h['confidence'],'quality':'CALCULATED'})
        else:dims.append({'id':'OPENING_'+str(i),'type':'opening','feature':h['kind'],'axis':h['axis'],'view':view,'size_mm':h['opening_size_mm'],'label':('槽 ' if h['kind']=='slot_or_elongated_hole' else '開口 ')+' × '.join(format(v,'.2f') for v in h['opening_size_mm']),'center_2d_mm':h['center_2d_mm'],'priority':'REQUIRED','confidence':h['confidence'],'quality':'CALCULATED'})
    for i,x in enumerate(treatments,1):dims.append({'id':('R_' if x['kind']=='fillet' else 'C_')+str(i),'type':x['kind'],'feature':x['kind'],'center_mm':x['center_mm'],'value_mm':x.get('radius_mm') or x.get('size_mm'),'label':x['callout'],'priority':'REQUIRED_IF_CONFIRMED','confidence':x['confidence'],'quality':x['measurement_quality']})
    sections=[]
    for i,h in enumerate(sorted(openings,key=lambda x:0 if x.get('confidence')=='HIGH' else 1)[:2]):
        L=chr(65+i);c=h.get('center_2d_mm') or [0,0]
        if h['axis']=='Z':cut,at,src='Y',c[1],'top'
        elif h['axis']=='Y':cut,at,src='Z',c[1],'front'
        else:cut,at,src='Z',c[1],'right'
        sections.append({'id':'SECTION_'+L+L,'label':'SECTION '+L+'-'+L,'cut_axis':cut,'at_mm':round(at,2),'through_feature':h['kind'],'feature_axis':h['axis'],'source_view':src,'reason':'內部孔槽/開口無法只靠外觀視圖完整表達','priority':'AUTO_REQUIRED','confidence':h.get('confidence','MEDIUM')})
    if not sections:
        ai=b['dimensions'].index(min(b['dimensions']));sections=[{'id':'SECTION_AA','label':'SECTION A-A','cut_axis':'XYZ'[ai],'at_mm':round((b['min'][ai]+b['max'][ai])/2,2),'through_feature':'body_midplane','source_view':'top' if ai==2 else 'front','reason':'自動中剖面，用於確認厚度與內部層次','priority':'AUTO_RECOMMENDED','confidence':'MEDIUM'}]
    for s in sections:
        ai='XYZ'.index(s['cut_axis']);s['profile_loops']=[[ [round(q[0],3),round(q[1],3)] for q in loop[:500] ] for loop in _dw_loops(_dw_slice(mesh,ai,float(s.get('at_mm') or 0)))[:32]];s['view_axes']=['Y','Z'] if ai==0 else (['X','Z'] if ai==1 else ['X','Y'])
    return {'version':'cad-drawing-v1','status':'ready','source_geometry':'3MF_MESH_REVERSE_ENGINEERING','primary_part':mesh['name'],'bounds_mm':{'min':[round(x,3) for x in b['min']],'max':[round(x,3) for x in b['max']],'dimensions':[round(x,3) for x in b['dimensions']]},'views':[_dw_view(mesh,v,segs) for v in ('front','top','right')],'sections':sections,'reconstruction':reconstruction,'details':[],'dimensions':dims,'features':{'holes':[x for x in openings if x['kind']=='circular_hole'],'slots':[x for x in openings if x['kind']=='slot_or_elongated_hole'],'other_openings':[x for x in openings if x['kind'] not in ('circular_hole','slot_or_elongated_hole')],'edge_treatments':treatments,'thickness_candidates':[]},'drawing_rules':{'standard':'ISO-like mechanical drawing','dimension_strategy':'minimal_complete_non_redundant','automatic_centerlines':True,'automatic_section_selection':True,'automatic_detail_views':True,'units':'mm'},'edge_analysis_status':edge_status,'confidence_note':'3MF 是三角網格，不含原始 CAD Feature Tree。外形尺寸為實測；孔槽由多截面幾何計算；圓角/倒角由網格法向與邊緣幾何反推並顯示信心等級。若補充 STEP/B-Rep，應以 B-Rep 作為精確特徵來源。'}

def analyze_remote_3mf_url(url,filename="makerworld-profile.3mf"):
    if not str(url or "").startswith("https://"):raise ValueError("remote 3MF URL must be https")
    folder=ROOT/("remote-"+str(uuid.uuid4()));folder.mkdir(parents=True,exist_ok=True);p=folder/"reference.3mf";total=0
    req=Request(str(url),headers={"User-Agent":"MakerSence/2.9","Accept":"application/octet-stream,*/*"})
    with urlopen(req,timeout=60) as r,open(p,"wb") as out:
        declared=int(r.headers.get("Content-Length") or 0)
        if declared>80_000_000:raise ValueError("3MF exceeds 80 MB worker limit")
        while True:
            chunk=r.read(1024*1024)
            if not chunk:break
            total+=len(chunk)
            if total>80_000_000:raise ValueError("3MF exceeds 80 MB worker limit")
            out.write(chunk)
    with zipfile.ZipFile(p,"r") as z:
        if z.testzip() is not None:raise ValueError("3MF ZIP damaged")
        names=z.namelist();models=[n for n in names if n.lower().endswith(".model")]
        if not models:raise ValueError("3MF has no .model geometry")
        main=next((n for n in models if n.lower()=="3d/3dmodel.model"),models[0])
        vc=tc=objects=0;parts=[];scales={"micron":.001,"millimeter":1.0,"centimeter":10.0,"inch":25.4,"meter":1000.0}
        def norm_path(v):
            return str(v or "").replace("\\","/").lstrip("/")
        def attr_path(el):
            for k,v in el.attrib.items():
                if k=="path" or k.endswith("}path"):return norm_path(v)
            return None
        def mat(v,unit_scale=1.0):
            try:a=[float(x) for x in str(v or "").split()]
            except:a=[]
            if len(a)!=12:return [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
            return [[a[0],a[3],a[6],a[9]*unit_scale],[a[1],a[4],a[7],a[10]*unit_scale],[a[2],a[5],a[8],a[11]*unit_scale],[0,0,0,1]]
        def mmul(a,b):
            return [[sum(a[i][k]*b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]
        def ptx(p,m):
            return [m[0][0]*p[0]+m[0][1]*p[1]+m[0][2]*p[2]+m[0][3],m[1][0]*p[0]+m[1][1]*p[1]+m[1][2]*p[2]+m[1][3],m[2][0]*p[0]+m[2][1]*p[1]+m[2][2]*p[2]+m[2][3]]
        def bunion(a,b):
            if b is None:return a
            if a is None:return list(b)
            return [min(a[0],b[0]),min(a[1],b[1]),min(a[2],b[2]),max(a[3],b[3]),max(a[4],b[4]),max(a[5],b[5])]
        def btx(b,m):
            if b is None:return None
            pts=[ptx([x,y,z],m) for x in (b[0],b[3]) for y in (b[1],b[4]) for z in (b[2],b[5])]
            return [min(q[0] for q in pts),min(q[1] for q in pts),min(q[2] for q in pts),max(q[0] for q in pts),max(q[1] for q in pts),max(q[2] for q in pts)]
        docs={}
        for name in models:
            key=norm_path(name);scale=1.0;cur=None;doc={"objects":{},"build":[]}
            try:
                with z.open(name) as fh:
                    for ev,el in ET.iterparse(fh,events=("start","end")):
                        tag=str(el.tag).rsplit("}",1)[-1]
                        if ev=="start":
                            if tag=="model":scale=scales.get(str(el.attrib.get("unit","millimeter")).lower(),1.0)
                            elif tag=="object":
                                objects+=1;cur={"id":str(el.attrib.get("id") or ""),"name":el.attrib.get("name"),"bbox":None,"ov":0,"ot":0,"components":[]}
                            elif tag=="component" and cur is not None:
                                cur["components"].append({"id":str(el.attrib.get("objectid") or ""),"path":attr_path(el) or key,"transform":mat(el.attrib.get("transform"),scale)})
                            elif tag=="item" and key==norm_path(main):
                                doc["build"].append({"id":str(el.attrib.get("objectid") or ""),"path":key,"transform":mat(el.attrib.get("transform"),scale)})
                            continue
                        if tag=="vertex" and cur is not None:
                            q=[f(el.attrib.get("x"))*scale,f(el.attrib.get("y"))*scale,f(el.attrib.get("z"))*scale];cur["ov"]+=1;vc+=1
                            cur["bbox"]=bunion(cur["bbox"],[q[0],q[1],q[2],q[0],q[1],q[2]])
                        elif tag=="triangle" and cur is not None:
                            cur["ot"]+=1;tc+=1
                        elif tag=="object" and cur is not None:
                            doc["objects"][cur["id"]]=cur;cur=None
                        el.clear()
            except Exception as ex:
                print("remote 3mf model parse skipped:",name,repr(ex),flush=True)
            docs[key]=doc
        if not vc or not tc:raise ValueError("3MF geometry has no measurable mesh")
        main_key=norm_path(main);main_doc=docs.get(main_key) or {};build=main_doc.get("build") or []
        memo={};part_world={}
        def obounds(path0,oid,stack=None):
            key=(norm_path(path0),str(oid));stack=set(stack or [])
            if key in memo:return memo[key]
            if key in stack:return None
            stack.add(key);o=(docs.get(key[0]) or {}).get("objects",{}).get(key[1])
            if not o:return None
            b=o.get("bbox")
            for c in o.get("components") or []:
                cb=obounds(c.get("path") or key[0],c.get("id"),stack)
                b=bunion(b,btx(cb,c.get("transform")) if cb is not None else None)
            memo[key]=b;return b
        def collect(path0,oid,world,root_index,stack=None):
            key=(norm_path(path0),str(oid));stack=set(stack or [])
            if key in stack:return
            stack.add(key);o=(docs.get(key[0]) or {}).get("objects",{}).get(key[1])
            if not o:return
            if o.get("bbox") is not None and o.get("ov") and o.get("ot"):
                part_world[(key[0],o["id"])]=world
                wb=btx(o["bbox"],world);dims0=[max(0,wb[i+3]-wb[i]) for i in range(3)]
                parts.append({"name":o.get("name") or ("Object "+str(o.get("id") or len(parts)+1)),"role":"unknown_part","dimensions_mm":[round(x,3) for x in dims0],"vertex_count":o["ov"],"triangle_count":o["ot"],"source_object_id":o["id"],"source_model_path":key[0],"root_build_index":root_index,"plate_index":None,"extruder":None,"subtype":None,"bambu_editable_part":True,"physical_detachable":None,"physical_detachability_status":"UNKNOWN","essential":True})
            for c in o.get("components") or []:
                collect(c.get("path") or key[0],c.get("id"),mmul(world,c.get("transform")),root_index,stack)
        total_bbox=None
        identity=mat(None)
        if build:
            for idx,it in enumerate(build,1):
                b=obounds(it["path"],it["id"]);total_bbox=bunion(total_bbox,btx(b,it["transform"]) if b is not None else None)
                collect(it["path"],it["id"],it["transform"],idx)
        else:
            referenced={(norm_path(c.get("path") or path0),str(c.get("id"))) for path0,d in docs.items() for o in d.get("objects",{}).values() for c in (o.get("components") or [])}
            roots=[(path0,oid) for path0,d in docs.items() for oid in d.get("objects",{}) if (path0,str(oid)) not in referenced]
            for idx,(path0,oid) in enumerate(roots,1):
                b=obounds(path0,oid);total_bbox=bunion(total_bbox,b);collect(path0,oid,identity,idx)
        if total_bbox is None:raise ValueError("3MF build graph has no measurable geometry")
        layout_dims=[max(0,total_bbox[i+3]-total_bbox[i]) for i in range(3)]
        largest=max(parts,key=lambda x:max(.001,x["dimensions_mm"][0])*max(.001,x["dimensions_mm"][1])*max(.001,x["dimensions_mm"][2])) if parts else None
        dims=list(largest["dimensions_mm"]) if largest else list(layout_dims)
        cad_drawing=_dw_drawing(z,largest,part_world.get((largest.get("source_model_path"),largest.get("source_object_id")))) if largest else {"version":"cad-drawing-v1","status":"unavailable","reason":"no_part"}
        part_max_dims=[max([p0["dimensions_mm"][i] for p0 in parts] or [0]) for i in range(3)]
        meta=[n for n in names if n.lower().startswith("metadata/")]
        plate_ids=set()
        for n0 in meta:
            m0=re.search(r"plate_(\d+)(?:\.|_)",n0,re.I)
            if m0:plate_ids.add(int(m0.group(1)))
        fits=all(all(float(v)<=180.0001 for v in p0["dimensions_mm"]) for p0 in parts)
        detachable=[x for x in parts if x.get("physical_detachable") is True]
        assembly={"part_count":len(parts),"bambu_editable_part_count":len(parts),"detachable_part_count":len(detachable),"detachable_parts":[{"name":x["name"],"dimensions_mm":x["dimensions_mm"]} for x in detachable],"roles":{"unknown_part":len(parts)},"multipart":len(parts)>1,"build_root_count":len(build),"plate_count":len(plate_ids),"build_layout_dimensions_mm":[round(x,3) for x in layout_dims],"largest_part_dimensions_mm":[round(x,3) for x in dims],"part_max_dimensions_mm":[round(x,3) for x in part_max_dims],"contact_graph":[],"preservation_policy":"preserve_editable_parts" if len(parts)>1 else "single_part","source_structure":"transform_aware_streaming_build_graph"}
        return {"format":"3mf","file_size_bytes":total,"model_entry":main,"unit":"millimeter","object_count":objects,"package_model_count":len(models),"build_item_count":len(build),"vertex_count":vc,"triangle_count":tc,"bounds_mm":{"min":[0,0,0],"max":[round(x,3) for x in dims],"dimensions":[round(x,3) for x in dims],"semantics":"largest_printable_part"},"build_layout_bounds_mm":{"min":[round(total_bbox[0],3),round(total_bbox[1],3),round(total_bbox[2],3)],"max":[round(total_bbox[3],3),round(total_bbox[4],3),round(total_bbox[5],3)],"dimensions":[round(x,3) for x in layout_dims]},"engineering_features":{"summary":{"part_count":len(parts),"analysis_mode":"remote_worker_transform_aware_streaming+drawing_v1"},"opening_candidates":((cad_drawing.get("features") or {}).get("holes") or [])+((cad_drawing.get("features") or {}).get("slots") or [])+((cad_drawing.get("features") or {}).get("other_openings") or []),"thickness_candidates":[],"edge_treatments":((cad_drawing.get("features") or {}).get("edge_treatments") or []),"contact_graph":[]},"cad_drawing":cad_drawing,"part_inventory":parts,"assembly_analysis":assembly,"a1_mini_fit":fits,"fit_margin_mm":[round(180-x,3) for x in part_max_dims],"metadata_entries":meta[:100],"bambu_project":{"detected":bool(meta),"plate_count":assembly["plate_count"],"project_settings":{},"raw_entries":meta[:80]},"measurement_quality":"MEASURED_REMOTE_WORKER_TRANSFORM_AWARE","caveats":["多分件 3MF 的 bounds_mm 代表最大可列印單件，不把不同列印位置的零件間距誤當成商品尺寸；build_layout_bounds_mm 另保留整體列印布局。","若作者文字提供組裝後標稱尺寸，Product Intent 應優先用該尺寸描述商品本體。"],"download_filename":filename}

def analyze_source_file_bytes(data,fmt):
    fmt=str(fmt or "").lower().lstrip(".")
    if fmt=="stp":fmt="step"
    if fmt not in ("stl","step"):raise ValueError("supported supplementary formats: STL, STEP")
    if not data or len(data)<16:raise ValueError("source file is empty")
    folder=ROOT/("source-"+str(uuid.uuid4()));folder.mkdir(parents=True,exist_ok=True)
    if fmt=="stl":
        lo=[float("inf")]*3;hi=[float("-inf")]*3;triangles=0
        binary=False
        if len(data)>=84:
            try:
                n=struct.unpack_from("<I",data,80)[0]
                binary=(84+n*50)<=len(data) and n<20_000_000
            except Exception:binary=False
        if binary:
            n=struct.unpack_from("<I",data,80)[0];off=84
            for _ in range(n):
                vals=struct.unpack_from("<12fH",data,off);off+=50;triangles+=1
                for k in (3,6,9):
                    q=vals[k:k+3]
                    for i in range(3):lo[i]=min(lo[i],q[i]);hi[i]=max(hi[i],q[i])
        else:
            for m in re.finditer(rb"(?:^|\n)\s*vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)",data):
                q=[float(m.group(1)),float(m.group(2)),float(m.group(3))]
                for i in range(3):lo[i]=min(lo[i],q[i]);hi[i]=max(hi[i],q[i])
                triangles+=1
            triangles//=3
        if triangles<=0 or any(not math.isfinite(x) for x in lo+hi):raise ValueError("STL geometry cannot be parsed")
        dims=[hi[i]-lo[i] for i in range(3)]
        return {"format":"STL","file_size_bytes":len(data),"triangle_count":triangles,"bounds_mm":{"min":[round(x,3) for x in lo],"max":[round(x,3) for x in hi],"dimensions":[round(x,3) for x in dims]},"a1_mini_fit":all(x<=180.0001 for x in dims),"analysis_quality":"MESH_BOUNDS"}
    p=folder/"source.step";p.write_bytes(data)
    wp=cq.importers.importStep(str(p));shapes=list(wp.vals())
    if not shapes:raise ValueError("STEP contains no importable shapes")
    lo=[float("inf")]*3;hi=[float("-inf")]*3;solids=faces=0
    for s in shapes:
        try:
            bb=s.BoundingBox()
            vals=[bb.xmin,bb.ymin,bb.zmin,bb.xmax,bb.ymax,bb.zmax]
            for i in range(3):lo[i]=min(lo[i],vals[i]);hi[i]=max(hi[i],vals[i+3])
        except Exception:pass
        try:solids+=len(s.Solids())
        except Exception:pass
        try:faces+=len(s.Faces())
        except Exception:pass
    if any(not math.isfinite(x) for x in lo+hi):raise ValueError("STEP bounds cannot be measured")
    dims=[hi[i]-lo[i] for i in range(3)]
    return {"format":"STEP","file_size_bytes":len(data),"shape_count":len(shapes),"solid_count":solids,"face_count":faces,"bounds_mm":{"min":[round(x,3) for x in lo],"max":[round(x,3) for x in hi],"dimensions":[round(x,3) for x in dims]},"a1_mini_fit":all(x<=180.0001 for x in dims),"analysis_quality":"CAD_BREP_BOUNDS"}

class Handler(BaseHTTPRequestHandler):
    server_version="MakerSenceCAD/2.10.5-universal-static"
    def log_message(self,fmt,*args):print(fmt%args,flush=True)
    def send_json(self,code,obj):
        data=json.dumps(obj,ensure_ascii=False).encode("utf-8")
        self.send_response(code);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
    def authorized(self):
        if not TOKEN:self.send_json(503,{"error":"WORKER_TOKEN missing"});return False
        if self.headers.get("Authorization")!="Bearer "+TOKEN:self.send_json(401,{"error":"unauthorized"});return False
        return True
    def do_GET(self):
        path=urlparse(self.path).path
        if path=="/health":return self.send_json(200,{"ok":True,"service":"makersence-cad-worker","version":"2.10.6-multiview-render","engine":"cadquery+svgpathtools+shapely+pillow","bambu_slicer":{"available":bool(BAMBU_BIN and pathlib.Path(BAMBU_BIN).exists()),"engine":"Bambu Studio","version":BAMBU_VERSION},"capabilities":["compact_step_brep","bambu_native_parts","detachable_parts","assembly_render","product_dimensions","open_edges_zero_gate","formal_mesh_render","multi_view_real_geometry_render_v1","artifact_reaudit","rectangular_blind_pockets","geometry_intent_gate","orphan_geometry_gate","unintended_through_cut_gate","welded_3mf_meshes","exported_3mf_topology_gate","true_font_outline_text","high_smooth_vector_mesh","multilingual_font_fallback","actual_text_stroke_gate","adaptive_cjk_regular_first","cjk_internal_clearance_gate","cjk_counter_preservation_gate","text_mesh_topology_candidate_gate","remote_3mf_stream_analyzer","remote_3mf_xml_iterparse","remote_3mf_transform_aware_bounds","remote_3mf_cad_drawing_v1","remote_3mf_reconstruction_sections_v2","auto_hole_slot_detection","auto_fillet_chamfer_candidates","auto_section_view_plan","multipart_dimension_semantics","supplementary_stl_step_analyzer","sculpted_lidded_container_v1","smooth_pumpkin_container_v2","generic_memory_budget_v1","streaming_3mf_glb_export","auto_text_boldening","typography_layout_bounds","script_aware_glyph_spacing","glyph_clearance_gate","text_readability_gate","bambu_04_text_profile","text_slicer_no_merge_gate","separate_structural_text_min_feature","arachne_text_project_settings","bambu_cli_real_slice","gcode_3mf_toolpath_gate","print_ready_plate_3mf","plate_part_coverage_gate","universal_cad_recipe_v2","section_loft_reconstruction_v1","section_loft_open_cavity_v2","axisymmetric_revolve_reconstruction_v1","isolated_universal_jobs_v1","STATIC_FUNCTIONAL_CAD","static_functional_utensil_vessel_v1","planar_prismatic_reconstruction_v1"],"profiles":["bambu_a1_mini_04"]})
        if path.startswith("/v1/jobs/"):
            if not self.authorized():return
            jid=path.split("/")[-1];j=JOBS.get(jid)
            return self.send_json(200,{"job_id":jid,**j}) if j else self.send_json(404,{"error":"job not found"})
        if path.startswith("/v1/slice-jobs/"):
            if not self.authorized():return
            jid=path.split("/")[-1];j=SLICE_JOBS.get(jid)
            return self.send_json(200,{"job_id":jid,**j}) if j else self.send_json(404,{"error":"slice job not found"})
        if path.startswith("/v1/slice-artifacts/"):
            if not self.authorized():return
            parts=path.strip("/").split("/")
            if len(parts)!=4:return self.send_json(404,{"error":"not found"})
            _,_,jid,name=parts
            if name!="bambu_sliced.3mf":return self.send_json(404,{"error":"not found"})
            j=SLICE_JOBS.get(jid);p=pathlib.Path(j.get("folder",""))/name if j else pathlib.Path("/__missing__")
            if not p.exists():return self.send_json(404,{"error":"not found"})
            data=p.read_bytes();self.send_response(200);self.send_header("Content-Type","model/3mf");self.send_header("Content-Length",str(len(data)));self.send_header("Content-Disposition",'attachment; filename="'+name+'"');self.end_headers();self.wfile.write(data);return
        if path.startswith("/v1/artifacts/"):
            if not self.authorized():return
            parts=path.strip("/").split("/")
            if len(parts)!=4:return self.send_json(404,{"error":"not found"})
            _,_,jid,name=parts;allowed={"model.stl","model.step","model.3mf","preview.glb","product_render_main.png","product_render_front.png","product_render_back.png","product_render_side.png","product_render_perspective.png","manifest.json"}
            plate_file=bool(re.fullmatch(r"print_plate_\d+\.3mf",name))
            if name not in allowed and not plate_file:return self.send_json(404,{"error":"not found"})
            p=ROOT/jid/name
            if not p.exists():return self.send_json(404,{"error":"not found"})
            typ="model/3mf" if plate_file else {"model.stl":"model/stl","model.step":"application/step","model.3mf":"model/3mf","preview.glb":"model/gltf-binary","product_render_main.png":"image/png","product_render_front.png":"image/png","product_render_back.png":"image/png","product_render_side.png":"image/png","product_render_perspective.png":"image/png","manifest.json":"application/json"}.get(name,"application/octet-stream")
            data=p.read_bytes();self.send_response(200);self.send_header("Content-Type",typ);self.send_header("Content-Length",str(len(data)));self.send_header("Content-Disposition",'attachment; filename="'+name+'"');self.end_headers();self.wfile.write(data);return
        self.send_json(404,{"error":"not found"})
    def do_POST(self):
        path=urlparse(self.path).path
        if not self.authorized():return
        try:
            n=int(self.headers.get("Content-Length","0"))
            if path=="/v1/analyze-source-file":
                if n<=0 or n>50_000_000:return self.send_json(400,{"error":"invalid source file size"})
                fmt=parse_qs(urlparse(self.path).query).get("format",[""])[0]
                data=self.rfile.read(n);out=analyze_source_file_bytes(data,fmt)
                return self.send_json(200,out)
            if path=="/v1/analyze-3mf-url":
                if n<=0 or n>200_000:return self.send_json(400,{"error":"invalid analyze request"})
                req=json.loads(self.rfile.read(n).decode("utf-8"))
                out=analyze_remote_3mf_url(req.get("url"),req.get("filename") or "makerworld-profile.3mf")
                return self.send_json(200,out)
            if path=="/v1/slice":
                if n<64 or n>32_000_000:return self.send_json(400,{"error":"invalid 3mf size"})
                data=self.rfile.read(n);out=submit_slice(data,self.headers.get("X-Idempotency-Key"));return self.send_json(202,out)
            if path!="/v1/generate":return self.send_json(404,{"error":"not found"})
            if n<=0 or n>4_000_000:return self.send_json(400,{"error":"invalid body"})
            req=json.loads(self.rfile.read(n).decode("utf-8"));out=generate(req);return self.send_json(200,out)
        except Exception as ex:
            print("worker POST failed:",path,repr(ex),flush=True);return self.send_json(422,{"error":str(ex)})

if __name__=="__main__":
    if len(sys.argv)>=4 and sys.argv[1]=="--generate-child":
        raise SystemExit(_generate_child_cli(sys.argv[2],sys.argv[3]))
    print("MakerSence CAD Worker 2.10.5-universal-static starting on",PORT,"Bambu Studio",BAMBU_VERSION,"available",bool(BAMBU_BIN and pathlib.Path(BAMBU_BIN).exists()),flush=True)
    ThreadingHTTPServer(("0.0.0.0",PORT),Handler).serve_forever()