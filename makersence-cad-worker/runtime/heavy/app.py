import os, sys, json, uuid, pathlib, time, struct, zipfile, math, hashlib, html, threading, subprocess, re, gc, ctypes, base64, zlib
import xml.etree.ElementTree as ET
from collections import Counter
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

import cadquery as cq
from cadquery import exporters
from PIL import Image, ImageDraw, ImageFilter
from svgpathtools import parse_path
from shapely.geometry import Polygon, MultiPolygon, GeometryCollection, Point, LineString, box as shapely_box
from shapely.ops import unary_union
from shapely.affinity import translate as geom_translate
from shapely.validation import explain_validity

TOKEN=os.environ.get("WORKER_TOKEN","")
PORT=int(os.environ.get("PORT","8000"))
ROOT=pathlib.Path("/tmp/makersence_jobs")
ROOT.mkdir(parents=True,exist_ok=True)
JOBS={}
SLICE_JOBS={}
MOTION_JOBS={}
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
        # Railway production workers have ~1 GB RAM. Keep a conservative cgroup
        # headroom, but do not reject healthy jobs solely because Python/OCCT RSS
        # retains shared/native pages above the old 760 MB self-imposed ceiling.
        soft=max(620.0,min(780.0,f(p.get("memory_soft_limit_mb"),700)))
        hard=max(820.0,min(920.0,f(p.get("memory_hard_limit_mb"),900)))
        if hard<=soft+64:hard=min(920.0,soft+96.0)
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
    # force a full nozzle-width gap; doing so would distort characters such as ��.
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
    return wp.loft(combine=True,ruled=bool(ruled))

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

UNIVERSAL_CAVITY_LOFT_POLICY="ruled_open_cavity_v1"

def _u_loft_from_loops(axis,sections,loop_index=0,ruled=False):
    sections=sorted(list(sections or []),key=lambda s:f(s.get("at_mm")))
    if len(sections)<3:raise ValueError("UNIVERSAL_RECIPE_NEEDS_3_SECTIONS")
    first=sections[0];loops=first.get("profile_loops") or []
    if loop_index>=len(loops):raise ValueError("UNIVERSAL_RECIPE_LOOP_CORRESPONDENCE")
    pts=_u_resample_loop(loops[loop_index])
    plane=_u_plane(axis,f(first.get("at_mm")))
    wp=cq.Workplane(plane).polyline(pts).close() if ruled else cq.Workplane(plane).spline(pts,periodic=True,makeWire=True)
    last=f(first.get("at_mm"))
    for sec in sections[1:]:
        ls=sec.get("profile_loops") or []
        if loop_index>=len(ls):raise ValueError("UNIVERSAL_RECIPE_LOOP_CORRESPONDENCE")
        at=f(sec.get("at_mm"));pts=_u_resample_loop(ls[loop_index])
        wp=wp.workplane(offset=at-last)
        wp=wp.polyline(pts).close() if ruled else wp.spline(pts,periodic=True,makeWire=True)
        last=at
    return wp.loft(combine=True,ruled=bool(ruled))

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
    inner_idx=[i for i,n in enumerate(counts) if n==2]
    cavity_mode=bool(inner_idx)
    outer=_u_loft_from_loops(axis,ordered,0,ruled=cavity_mode)
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
        inner=_u_loft_from_loops(axis,inner_sections,1,ruled=True)
        try:
            outer_before=shape_volume(outer);inner_volume=shape_volume(inner)
            if inner_volume<=1e-6 or inner_volume>=outer_before*.98:raise ValueError("UNIVERSAL_RECIPE_INNER_INVALID")
            cut=outer.cut(inner);outer_after=shape_volume(cut)
            if outer_after>=outer_before*.995:
                try:
                    direct=outer.val().cut(inner.val());cut=cq.Workplane("XY").newObject([direct]);outer_after=shape_volume(cut)
                except Exception:
                    pass
            if outer_after>=outer_before*.995:raise ValueError("UNIVERSAL_RECIPE_HOLLOW_CUT_NO_VOLUME_CHANGE")
            outer=cut
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

def _u_bind_dimension_constraints(nodes):
    out=[dict(x) for x in list(nodes or [])]
    by_id={str(x.get("id")):x for x in out if x.get("id")}
    bound=[]
    for c in out:
        if str(c.get("operation") or "").upper()!="CONSTRAIN_DIMENSION":continue
        state=str(c.get("execution_state") or "")
        if state=="SOURCE_GEOMETRY_PRESERVED":continue
        if state!="PARAMETRIC_MAPPING_RESOLVED":
            raise ValueError("UNIVERSAL_CAD_DIMENSION_MAPPING_UNRESOLVED:"+str(c.get("name") or c.get("id") or "unknown"))
        target=by_id.get(str(c.get("target_feature_id") or ""))
        if not target:raise ValueError("UNIVERSAL_CAD_DIMENSION_TARGET_MISSING:"+str(c.get("target_feature_id") or ""))
        param=str(c.get("target_parameter") or "")
        if param not in ("diameter_mm","depth_mm","residual_wall_mm"):
            raise ValueError("UNIVERSAL_CAD_DIMENSION_PARAMETER_UNSUPPORTED:"+param)
        value=f(c.get("value_mm"))
        if value<=0:raise ValueError("UNIVERSAL_CAD_DIMENSION_VALUE_INVALID:"+str(c.get("name") or c.get("id") or "unknown"))
        if param=="residual_wall_mm":
            target["required_residual_wall_mm"]=value
        else:
            target[param]=value
        target.setdefault("_bound_constraints",[]).append({
            "id":c.get("id"),"name":c.get("name"),"parameter":param,"value_mm":value,
            "tolerance_mm":f(c.get("tolerance_mm"),.05),"authority":c.get("authority")
        })
        bound.append({"constraint_id":c.get("id"),"target_feature_id":target.get("id"),"parameter":param,"value_mm":value})
    return out,bound

def _u_apply_openings(shape,nodes):
    mn,mx,dims=_u_bounds(shape)
    for node in nodes:
        if str(node.get("operation") or "").upper()!="CUT":continue
        typ=str(node.get("type") or "")
        d=f(node.get("diameter_mm"));center=node.get("center_2d_mm") or []
        axis=str(node.get("axis") or "Z").upper();margin=3.0
        if typ=="circular_hole" and bool(node.get("through")):
            if d<=0 or len(center)<2:raise ValueError("UNIVERSAL_RECIPE_HOLE_INVALID")
            if axis=="Z":
                tool=cq.Workplane("XY").workplane(offset=mn[2]-margin).center(f(center[0]),f(center[1])).circle(d/2).extrude(dims[2]+2*margin)
            elif axis=="X":
                tool=cq.Workplane(_u_plane("X",mn[0]-margin)).center(f(center[0]),f(center[1])).circle(d/2).extrude(dims[0]+2*margin)
            elif axis=="Y":
                tool=cq.Workplane(_u_plane("Y",mn[1]-margin)).center(f(center[0]),f(center[1])).circle(d/2).extrude(dims[1]+2*margin)
            else:raise ValueError("UNIVERSAL_RECIPE_HOLE_AXIS")
            shape=shape.cut(tool);continue
        if typ in ("blind_circular_pocket","blind_circular_cavity") and not bool(node.get("through")):
            depth=f(node.get("depth_mm"));fe=node.get("face_evidence") or {}
            required_wall=node.get("required_residual_wall_mm")
            if required_wall is not None:
                op=fe.get("opening_face") or {};opp=fe.get("opposite_exterior_face") or {}
                op_plane=op.get("plane_mm");opp_plane=opp.get("plane_mm")
                if op_plane is None or opp_plane is None:raise ValueError("UNIVERSAL_CAD_RESIDUAL_WALL_FACE_EVIDENCE_MISSING")
                full=abs(f(op_plane)-f(opp_plane));depth=full-f(required_wall)
            if d<=0 or depth<=0 or len(center)<2:raise ValueError("UNIVERSAL_RECIPE_BLIND_CAVITY_INVALID")
            opening_face=str(node.get("opening_face") or "").upper()
            if opening_face not in ("MIN","MAX"):
                op=(fe.get("opening_face") or {}).get("plane_mm")
                if op is not None:
                    ai={"X":0,"Y":1,"Z":2}.get(axis,2)
                    opening_face="MAX" if abs(f(op)-mx[ai])<=abs(f(op)-mn[ai]) else "MIN"
                else:raise ValueError("UNIVERSAL_RECIPE_BLIND_CAVITY_OPENING_FACE_MISSING")
            ai={"X":0,"Y":1,"Z":2}.get(axis)
            if ai is None:raise ValueError("UNIVERSAL_RECIPE_BLIND_CAVITY_AXIS")
            at=(mx[ai]+margin) if opening_face=="MAX" else (mn[ai]-margin)
            direction=-1 if opening_face=="MAX" else 1
            tool=cq.Workplane(_u_plane(axis,at)).center(f(center[0]),f(center[1])).circle(d/2).extrude(direction*(depth+margin))
            shape=shape.cut(tool);continue
        if bool(node.get("through")):raise ValueError("UNIVERSAL_RECIPE_REQUIRED_OPENING_UNSUPPORTED:"+typ)
    return shape

def _u_multipart_shapes(nodes,c):
    src=[x for x in nodes if x.get("type")=="source_part"]
    if len(src)<2:raise ValueError("UNIVERSAL_MULTIPART_NEEDS_PARTS")
    built=[]
    for i,node in enumerate(src):
        ge=node.get("geometry_evidence") or {};planar=ge.get("planar_prismatic") or {};mesh_brep=ge.get("mesh_brep") or {};rec=ge.get("reconstruction") or {};secs=list(rec.get("sections") or [])
        if str(planar.get("status") or "").lower()=="ready":
            shape=_u_planar_multiloop_shape(planar)
        elif str(mesh_brep.get("status") or "").lower()=="ready" and str(mesh_brep.get("strategy") or "")=="FACETED_MESH_BREP":
            shape=_u_faceted_mesh_brep_shape(mesh_brep)
        else:
            if str(ge.get("status") or "").lower()!="ready" or len(secs)<3:
                raise ValueError("UNIVERSAL_MULTIPART_PART_EVIDENCE_MISSING:"+str(node.get("name") or i+1))
            b=ge.get("bounds_mm") or {};mn=b.get("min") or [];mx=b.get("max") or []
            axis=str(rec.get("axis") or secs[0].get("cut_axis") or "Z").upper();ai="XYZ".find(axis)
            stack={"axis":axis,"sections":secs}
            if ai>=0 and len(mn)==3 and len(mx)==3:
                stack["source_axis_min_mm"]=f(mn[ai]);stack["source_axis_max_mm"]=f(mx[ai])
            shape=_u_section_shape(stack)
        if shape_volume(shape)<=1e-3:raise ValueError("UNIVERSAL_MULTIPART_ZERO_VOLUME:"+str(node.get("name") or i+1))
        pose_class=str(node.get("source_pose_class") or "UNRESOLVED")
        at=node.get("assembly_translate_mm") or [0,0,0]
        if not isinstance(at,list) or len(at)!=3:at=[0,0,0]
        built.append({"name":str(node.get("name") or ("PART_"+str(i+1))),"role":str(node.get("role") or "source_part"),"semantic_name":node.get("semantic_name"),"source_pose_class":pose_class,"assembly_pose_status":node.get("assembly_pose_status"),"assembly_parent":node.get("assembly_parent"),"assembly_translate":[f(at[0]),f(at[1]),f(at[2])],"mate_contract":node.get("mate_contract"),"physical_separate":bool(node.get("physical_detachable") is True or pose_class=="SEPARATE_PRINT_PART"),"editable_separate":True,"color":color_hex(c.get("body_color") or "#6d7480"),"shape":shape,"geom":None,"min_feature_mm":max(.8,f(c.get("wall_mm"),.8))})
    mn=[float("inf")]*3
    for p in built:
        b0=_u_bounds(p["shape"])[0]
        for j in range(3):mn[j]=min(mn[j],b0[j])
    shift=(-mn[0],-mn[1],-mn[2])
    for p in built:p["shape"]=p["shape"].translate(shift)
    return built

def universal_cad_recipe(c):
    plan=c.get("universal_recipe") or c.get("recipe") or {}
    if str(plan.get("status") or "")!="READY":raise ValueError("UNIVERSAL_RECIPE_NOT_READY:"+str(plan.get("status")))
    evidence=plan.get("geometry_evidence") or {}
    if plan.get("hard_blockers"):raise ValueError("UNIVERSAL_RECIPE_BLOCKED:"+",".join(str(x) for x in plan.get("hard_blockers")))
    graph=plan.get("feature_graph") or {};raw_nodes=list(graph.get("nodes") or [])
    nodes,bound_dimensions=_u_bind_dimension_constraints(raw_nodes)
    c["_cad_dimension_bindings"]=bound_dimensions
    mode=str(plan.get("reconstruction_mode") or "")
    if int(f(evidence.get("source_part_count"),1))!=1 and mode!="MULTIPART_RELATION_REBUILD":raise ValueError("UNIVERSAL_RECIPE_MULTIPART_EXECUTOR_NOT_READY")
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
    elif mode=="MULTIPART_RELATION_REBUILD":
        return _u_multipart_shapes(nodes,c)
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


BLENDER_BIN=os.environ.get("BLENDER_BIN","/usr/bin/blender")
BLENDER_BONE_SCRIPT=r'''
import bpy, json, sys
from mathutils.geometry import interpolate_bezier
args=sys.argv[sys.argv.index("--")+1:]
spec=json.load(open(args[0],encoding="utf-8"))
out_path,blend_path=args[1],args[2]
width,height,body_t=map(float,(spec["width_mm"],spec["height_mm"],spec["body_thickness_mm"]))
# Canonical horizontal bone: narrow central shaft, two rounded lobes at each end,
# no artificial top tab. Symmetry is deliberate so the silhouette reads before decoration.
anchors=[
(-.43,0),(-.48,.10),(-.50,.22),(-.48,.34),(-.43,.44),(-.36,.50),(-.29,.49),(-.24,.44),(-.23,.39),(-.25,.35),(-.20,.33),(-.10,.34),
(.10,.34),(.20,.33),(.25,.35),(.23,.39),(.24,.44),(.29,.49),(.36,.50),(.43,.44),(.48,.34),(.50,.22),(.48,.10),(.43,0),
(.48,-.10),(.50,-.22),(.48,-.34),(.43,-.44),(.36,-.50),(.29,-.49),(.24,-.44),(.23,-.39),(.25,-.35),(.20,-.33),(.10,-.34),
(-.10,-.34),(-.20,-.33),(-.25,-.35),(-.23,-.39),(-.24,-.44),(-.29,-.49),(-.36,-.50),(-.43,-.44),(-.48,-.34),(-.50,-.22),(-.48,-.10)
]
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
curve=bpy.data.curves.new("Bone_Sculptural_Contour","CURVE")
curve.dimensions="2D"
curve.fill_mode="BOTH"
curve.extrude=body_t/2
curve.resolution_u=40
spline=curve.splines.new("BEZIER")
spline.bezier_points.add(len(anchors)-1)
for point,(x,y) in zip(spline.bezier_points,anchors):
    point.co=(x*width,y*height,0)
    point.handle_left_type="AUTO"
    point.handle_right_type="AUTO"
spline.use_cyclic_u=True
obj=bpy.data.objects.new("Organic_Bone_Base",curve)
bpy.context.collection.objects.link(obj)
bpy.context.view_layer.update()
points=[]
for i in range(len(spline.bezier_points)):
    a=spline.bezier_points[i]
    b=spline.bezier_points[(i+1)%len(spline.bezier_points)]
    segment=interpolate_bezier(a.co,a.handle_right,b.handle_left,b.co,20)
    points.extend((float(v.x),float(v.y)) for v in segment[:-1])
if len(points)<120:raise RuntimeError("Blender contour is incomplete")
xmin=min(x for x,y in points);xmax=max(x for x,y in points)
ymin=min(y for x,y in points);ymax=max(y for x,y in points)
if xmax-xmin<=0 or ymax-ymin<=0:raise RuntimeError("Blender contour has no area")
sx=width/(xmax-xmin);sy=height/(ymax-ymin)
cx=(xmin+xmax)/2;cy=(ymin+ymax)/2
obj.scale=(sx,sy,1)
obj.location=(-cx*sx,-cy*sy,body_t/2)
normalized=[[round((x-cx)*sx,6),round((y-cy)*sy,6)] for x,y in points]
with open(out_path,"w",encoding="utf-8") as handle:
    json.dump({"engine":"Blender","blender_version":bpy.app.version_string,"profile":"canonical_bone_v4_refined","outline_points_mm":normalized,"dimensions_mm":[width,height,body_t]},handle)
bpy.ops.wm.save_as_mainfile(filepath=blend_path,check_existing=False)
'''
def hybrid_bone_tag(c,req):
    if not pathlib.Path(BLENDER_BIN).exists():raise ValueError("BLENDER_EXECUTOR_UNAVAILABLE")
    target=c.get("product_dimensions_mm") or []
    if not isinstance(target,list) or len(target)!=3:raise ValueError("HYBRID_TARGET_DIMENSIONS_REQUIRED")
    width,height,total_h=[f(x) for x in target]
    if not (38<=width<=175 and 30<=height<=175 and 2.8<=total_h<=12):raise ValueError("HYBRID_BONE_DIMENSIONS_UNSUPPORTED")
    relief=f(c.get("relief_height_mm"),.65)
    body_t=total_h-relief
    pocket_d=f(c.get("nfc_pocket_diameter_mm"),25.5)
    pocket_depth=f(c.get("nfc_pocket_depth_mm"),1.2)
    hole_d=f(c.get("keyring_hole_diameter_mm"),4.5)
    if relief<.55 or relief>1.2 or body_t-pocket_depth<.8 or pocket_d<18 or pocket_d>30 or hole_d<3.5 or hole_d>7:raise ValueError("HYBRID_BONE_FEATURE_DIMENSIONS_UNSUPPORTED")
    if pocket_d+4>height*.64*2:raise ValueError("HYBRID_BONE_NFC_POCKET_EXCEEDS_WAIST")
    if width<1.75*pocket_d:raise ValueError("HYBRID_BONE_NFC_POCKET_EXCEEDS_LENGTH")
    folder=pathlib.Path(req.get("_job_folder") or "")
    if not folder.is_dir():raise ValueError("HYBRID_JOB_FOLDER_MISSING")
    param_path=folder/"blender_input.json"
    profile_path=folder/"blender_profile.json"
    blend_path=folder/"source.blend"
    param_path.write_text(json.dumps({"width_mm":width,"height_mm":height,"body_thickness_mm":body_t}),encoding="utf-8")
    script_path=folder/"blender_profile.py"
    script_path.write_text(BLENDER_BONE_SCRIPT,encoding="utf-8")
    run=subprocess.run([BLENDER_BIN,"--background","--factory-startup","--python",str(script_path),"--",str(param_path),str(profile_path),str(blend_path)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=120)
    if run.returncode!=0 or not profile_path.exists() or not blend_path.exists():raise ValueError("BLENDER_PROFILE_FAILED:"+run.stdout[-1200:])
    print("hybrid blender complete",round(current_rss_mb(),1),flush=True)
    profile=json.loads(profile_path.read_text(encoding="utf-8"))
    points=profile.get("outline_points_mm") or []
    if len(points)<120 or any(not isinstance(p,list) or len(p)!=2 for p in points):raise ValueError("BLENDER_PROFILE_INVALID")
    outline=Polygon(points).buffer(0)
    if outline.geom_type!="Polygon" or outline.is_empty or not outline.is_valid:raise ValueError("HYBRID_SILHOUETTE_INVALID")
    pocket_edge_wall=max(1.2,min(1.8,body_t*.35))
    pocket_envelope=Point(0,0).buffer(pocket_d/2+pocket_edge_wall,resolution=64)
    if not outline.contains(pocket_envelope):raise ValueError("HYBRID_NFC_POCKET_OUTSIDE_PRODUCT_ENVELOPE")
    try:body=cq.Workplane("XY").moveTo(*points[0]).spline(points[1:]+[points[0]],includeCurrent=True).close().extrude(body_t)
    except Exception as ex:raise ValueError("BLENDER_TO_CAD_LOFT_FAILED:"+str(ex))
    print("hybrid body complete",round(current_rss_mb(),1),flush=True)
    if width<46 or height<42:raise ValueError("HYBRID_BONE_DETAIL_REQUIRES_AT_LEAST_46x42_MM")
    hole_x=-width*.39
    hole_y=height*.29
    pocket_x=pocket_y=0.0
    if math.hypot(hole_x-pocket_x,hole_y-pocket_y)-(hole_d+pocket_d)/2<2.0:raise ValueError("HYBRID_BONE_FEATURE_COLLISION")
    hole=cq.Workplane("XY").workplane(offset=-.1).center(hole_x,hole_y).circle(hole_d/2).extrude(body_t+.2)
    pocket=cq.Workplane("XY").workplane(offset=-.1).center(pocket_x,pocket_y).circle(pocket_d/2).extrude(pocket_depth+.1)
    body=body.cut(hole).cut(pocket)
    if body.val().isNull() or not body.val().isValid():raise ValueError("HYBRID_BONE_BREP_INVALID")
    if shape_volume(body.intersect(hole))>.001 or shape_volume(body.intersect(pocket))>.001:raise ValueError("HYBRID_BONE_OPENINGS_NOT_CUT")
    hb=hole.val().BoundingBox();pb=pocket.val().BoundingBox();bb=body.val().BoundingBox()
    req["_hybrid_measured_dims"]={"nfc_pocket_diameter_mm":round(float(pb.xmax-pb.xmin),4),"nfc_pocket_depth_mm":round(float(pb.zmax-max(0.0,bb.zmin)),4),"keyring_hole_diameter_mm":round(float(hb.xmax-hb.xmin),4),"residual_wall_mm":round(max(0.0,body_t-pocket_depth),4)}

    # Visual hierarchy: silhouette first, one paw as the primary accent, then only a few
    # restrained surface marks. No invented character face / muzzle / ears.
    # The primary paw reaches the declared relief height so the final Z dimension
    # matches the product contract; the border/detail stay subordinate.
    accent_h=relief
    detail_h=relief*.28
    outer=outline.buffer(-.55,join_style=1,resolution=10).simplify(.03,preserve_topology=True)
    inner=outline.buffer(-1.48,join_style=1,resolution=10).simplify(.03,preserve_topology=True)
    if outer.geom_type!="Polygon" or inner.geom_type!="Polygon" or outer.is_empty or inner.is_empty:raise ValueError("HYBRID_BORDER_PROFILE_INVALID")
    ring_outer=cq.Workplane("XY").polyline(list(outer.exterior.coords)[:-1]).close().extrude(relief*.24)
    ring_inner=cq.Workplane("XY").polyline(list(inner.exterior.coords)[:-1]).close().extrude(relief*.29)
    ring=ring_outer.cut(ring_inner)
    if ring.val().isNull() or not ring.val().isValid():raise ValueError("HYBRID_BORDER_BREP_INVALID")

    paw_cy=-height*.035
    paw_parts=[cq.Workplane("XY").center(0,paw_cy).ellipse(width*.090,height*.062).extrude(accent_h)]
    toe_r_x=max(.92,min(1.18,width*.023))
    toe_r_y=max(1.02,min(1.32,height*.026))
    for dx,dy in [(-3.55,3.15),(-1.18,4.02),(1.18,4.02),(3.55,3.15)]:
        paw_parts.append(cq.Workplane("XY").center(dx,paw_cy+dy).ellipse(toe_r_x,toe_r_y).extrude(accent_h))
    paw_compound=cq.Compound.makeCompound([x.val() for x in paw_parts])
    paw=cq.Workplane("XY").newObject([paw_compound])
    ring_t=ring.translate((0,0,body_t))
    paw_t=paw.translate((0,0,body_t))
    accent_compound=cq.Compound.makeCompound([ring_t.val(),paw_t.val()])
    accent=cq.Workplane("XY").newObject([accent_compound])
    if accent.val().isNull() or not accent.val().isValid():raise ValueError("HYBRID_ACCENT_INVALID")

    # A small three-stroke claw/fur cue sits beside the paw, inside the central
    # body mass. It is intentionally asymmetrical and separated from both the
    # perimeter border and the paw so it reads as secondary surface detail.
    detail_x=width*.165
    detail_y=0.0
    detail_solids=[]
    for dx,dy,ang in [(-1.55,-.10,-16),(0,.35,0),(1.55,-.10,16)]:
        mark=cq.Workplane("XY").ellipse(.46,1.20).extrude(detail_h).rotate((0,0,0),(0,0,1),ang).translate((detail_x+dx,detail_y+dy,body_t))
        detail_solids.append(mark.val())
    detail_compound=cq.Compound.makeCompound(detail_solids)
    detail=cq.Workplane("XY").newObject([detail_compound])
    if detail.val().isNull() or not detail.val().isValid():raise ValueError("HYBRID_PET_DETAIL_INVALID")
    print("hybrid refined detail complete",round(current_rss_mb(),1),flush=True)

    outline_poly=outline
    if outline_poly.geom_type!="Polygon" or outline_poly.is_empty or not outline_poly.is_valid:raise ValueError("HYBRID_SILHOUETTE_INVALID")
    area_ratio=float(outline_poly.area)/max(1e-9,width*height)
    hole_margin=max(0.0,float(outline_poly.boundary.distance(Point(hole_x,hole_y)))-hole_d/2)
    center_span=float(outline_poly.intersection(LineString([(0,-height),(0,height)])).length)
    left_span=float(outline_poly.intersection(LineString([(-width*.38,-height),(-width*.38,height)])).length)
    right_span=float(outline_poly.intersection(LineString([(width*.38,-height),(width*.38,height)])).length)
    end_span=(left_span+right_span)/2.0
    pbb=paw.val().BoundingBox();dbb=detail.val().BoundingBox()
    paw_width_ratio=float(pbb.xmax-pbb.xmin)/max(1e-9,width)
    paw_height_ratio=float(pbb.ymax-pbb.ymin)/max(1e-9,height)
    detail_width_ratio=float(dbb.xmax-dbb.xmin)/max(1e-9,width)
    detail_height_ratio=float(dbb.ymax-dbb.ymin)/max(1e-9,height)
    balance_ok=abs(float(outline_poly.centroid.x))<=width*.015 and abs(float(outline_poly.centroid.y))<=height*.015 and abs(left_span-right_span)<=max(.5,end_span*.04)
    center_ratio=center_span/max(1e-9,height)
    end_to_center=end_span/max(1e-9,center_span)
    canonical_ratio_ok=.58<=center_ratio<=.74 and 1.18<=end_to_center<=1.72
    req["_hybrid_visual_metrics"]={
      "profile":"commercial_bone_v4_refined",
      "silhouette_area_ratio":round(area_ratio,4),
      "center_vertical_span_mm":round(center_span,3),
      "end_vertical_span_mm":round(end_span,3),
      "keyring_edge_margin_mm":round(hole_margin,3),
      "paw_width_ratio":round(paw_width_ratio,4),
      "paw_height_ratio":round(paw_height_ratio,4),
      "surface_detail_width_ratio":round(detail_width_ratio,4),
      "surface_detail_height_ratio":round(detail_height_ratio,4),
      "canonical_bone_ratio_ok":bool(canonical_ratio_ok),
      "center_span_ratio":round(center_ratio,4),
      "end_to_center_ratio":round(end_to_center,4),
      "nfc_envelope_edge_wall_mm":round(pocket_edge_wall,3),
      "nfc_envelope_contained":True,
      "symmetry_balance_ok":bool(balance_ok),
      "semantic_scope_ok":True,
      "silhouette_profile_ok":bool(.42<=area_ratio<=.78 and canonical_ratio_ok and balance_ok),
      "detail_scale_ok":bool(paw_width_ratio<=.22 and paw_height_ratio<=.19 and detail_width_ratio<=.18 and detail_height_ratio<=.16),
      "negative_space_ok":bool(hole_margin>=2.0)
    }
    req["_blender_profile"]={"engine":"Blender","version":profile.get("blender_version"),"profile":profile.get("profile"),"outline_points":len(points),"source_file":"source.blend"}
    return [
        {"name":"BODY","role":"main_body","physical_separate":True,"editable_separate":True,"color":color_hex(c.get("body_color") or "#efe9d9"),"shape":body,"geom":None,"min_feature_mm":.8},
        {"name":"PET_ACCENT","role":"semantic_relief","physical_separate":False,"editable_separate":True,"border_inner_xy":list(inner.exterior.coords),"paw_bounds_xy":[[float(pbb.xmin),float(pbb.ymin),float(pbb.xmax),float(pbb.ymax)]],"color":color_hex(c.get("accent_color") or "#8b9687"),"shape":accent,"geom":None,"min_feature_mm":.8},
        {"name":"PET_DETAIL","role":"semantic_relief","physical_separate":False,"editable_separate":True,"color":color_hex(c.get("detail_color") or "#4f5c50"),"shape":detail,"geom":None,"min_feature_mm":.8}
    ]

def build_cad_family(req):
    c=req.get("cad_contract") or req.get("recipe") or {}
    family=str(c.get("family") or "rounded_plate")
    if family in ("hybrid_bone_tag","static_functional_utensil_vessel"):
        raise ValueError("LEGACY_SPECIALIZED_FAMILY_DISABLED_USE_UNIVERSAL_CAD_RECIPE:"+family)
    if family=="universal_cad_recipe":return universal_cad_recipe(c),[],[],[]
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

def hybrid_overlap(parts,mesh_bounds,tolerance=.01):
    by={str(b.get("part")):b for b in mesh_bounds}
    body=by.get("BODY")
    if not body:raise ValueError("HYBRID_BODY_BOUNDS_MISSING")
    collisions=[];body_top=float(body["max"][2])
    for name in ("PET_ACCENT","PET_DETAIL"):
        b=by.get(name)
        if not b:raise ValueError("HYBRID_PART_BOUNDS_MISSING:"+name)
        if float(b["min"][2])<body_top-tolerance:collisions.append({"a":"BODY","b":name,"reason":"mesh_z_intrusion"})
    accent=by["PET_ACCENT"];detail=by["PET_DETAIL"]
    overlap=[min(float(accent["max"][i]),float(detail["max"][i]))-max(float(accent["min"][i]),float(detail["min"][i])) for i in range(3)]
    if all(v>tolerance for v in overlap):
        accent_part=next((p for p in parts if p.get("name")=="PET_ACCENT"),{})
        inner_xy=accent_part.get("border_inner_xy") or []
        paw_bounds=accent_part.get("paw_bounds_xy") or []
        zone=shapely_box(float(detail["min"][0]),float(detail["min"][1]),float(detail["max"][0]),float(detail["max"][1]))
        border_clear=bool(len(inner_xy)>=4 and Polygon(inner_xy).buffer(-tolerance).contains(zone))
        paws_clear=all(not shapely_box(*bounds).intersects(zone) for bounds in paw_bounds)
        if not (border_clear and paws_clear):collisions.append({"a":"PET_ACCENT","b":"PET_DETAIL","reason":"border_or_paw_overlap","dimensions_mm":[round(v,3) for v in overlap],"border_clear":border_clear,"paws_clear":paws_clear})
    return collisions

def pair_overlap(parts,assembled=False):
    collisions=[]
    for i in range(len(parts)):
        for j in range(i+1,len(parts)):
            try:
                a=_part_shape(parts[i],assembled);b=_part_shape(parts[j],assembled)
                ba=a.val().BoundingBox();bb=b.val().BoundingBox()
                if ba.xmax<=bb.xmin+1e-5 or bb.xmax<=ba.xmin+1e-5 or ba.ymax<=bb.ymin+1e-5 or bb.ymax<=ba.ymin+1e-5 or ba.zmax<=bb.zmin+1e-5 or bb.zmax<=ba.zmin+1e-5:continue
                vol=shape_volume(a.intersect(b))
            except Exception:vol=0
            if vol>0.001:collisions.append({"a":parts[i]["name"],"b":parts[j]["name"],"volume_mm3":round(vol,4)})
    return collisions

def _universal_contact_semantics(c,collisions):
    plan=c.get("universal_recipe") or c.get("recipe") or {}
    nodes=list((plan.get("feature_graph") or {}).get("nodes") or [])
    allowed={}
    for n in nodes:
        if n.get("type")!="assembly_relation":continue
        sem=str(n.get("assembly_semantics") or "")
        if sem not in ("SOURCE_INTENDED_ATTACHMENT","SOURCE_NEAR_ATTACHMENT","RESOLVED_MATE"):continue
        a=str(n.get("part_a") or "");b=str(n.get("part_b") or "")
        if a and b:allowed[tuple(sorted((a,b)))]=sem
    intentional=[];unexpected=[]
    for x in collisions:
        key=tuple(sorted((str(x.get("a") or ""),str(x.get("b") or ""))))
        if key in allowed:intentional.append({**x,"semantics":allowed[key]})
        else:unexpected.append(x)
    return intentional,unexpected

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

def _u_design_fidelity_artifact_gate(c,parts):
    plan=c.get("universal_recipe") or c.get("recipe") or {}
    gate=plan.get("design_fidelity_gate") or {}
    required=list(((gate.get("artifact_validation") or {}).get("required_signature_ids") or []))
    if not required:
        required=[str(x.get("id") or x.get("signature_id") or "") for x in (gate.get("required_signatures") or []) if str(x.get("id") or x.get("signature_id") or "")]
    if not required:
        return {"status":"PASS","checks":[],"required_signature_ids":[],"mode":"no_required_signatures"}
    nodes=list((plan.get("feature_graph") or {}).get("nodes") or [])
    stack=next((x for x in nodes if x.get("type")=="section_stack"),None)
    sections=sorted(list((stack or {}).get("sections") or []),key=lambda q:f(q.get("at_mm")))
    planar=next((x for x in nodes if x.get("type")=="measured_outline" and str(x.get("operation") or "").upper()=="EXTRUDE"),None)
    planar_outline=list((planar or {}).get("outline") or [])
    planar_area=abs(_u_signed_area(planar_outline)) if len(planar_outline)>=3 else 0.0
    planar_thickness=f((planar or {}).get("thickness_mm"))
    planar_expected_volume=planar_area*planar_thickness if planar_area>0 and planar_thickness>0 else None
    if planar_expected_volume is not None:
        for op in nodes:
            if str(op.get("operation") or "").upper()!="CUT" or not bool(op.get("through")):continue
            if str(op.get("type") or "")=="circular_hole" and f(op.get("diameter_mm"))>0:
                planar_expected_volume-=math.pi*(f(op.get("diameter_mm"))/2.0)**2*planar_thickness
    rows=[]
    for sec in sections:
        loops=sorted(list(sec.get("profile_loops") or []),key=lambda x:abs(_u_signed_area(x)),reverse=True)
        if not loops:continue
        pts=[p for p in loops[0] if isinstance(p,(list,tuple)) and len(p)>=2]
        if len(pts)<3:continue
        xs=[f(p[0]) for p in pts];ys=[f(p[1]) for p in pts]
        rows.append({"at_mm":f(sec.get("at_mm")),"width_mm":max(xs)-min(xs),"height_mm":max(ys)-min(ys),"loop_count":len(loops)})
    widths=[r["width_mm"] for r in rows if r["width_mm"]>0];heights=[r["height_mm"] for r in rows if r["height_mm"]>0]
    def variation(vals):
        return (max(vals)-min(vals))/max(vals) if vals and max(vals)>1e-9 else 0.0
    width_var=variation(widths);height_var=variation(heights)
    anis=max([abs(r["width_mm"]-r["height_mm"])/max(r["width_mm"],r["height_mm"],1e-9) for r in rows] or [0.0])
    counts=[r["loop_count"] for r in rows]
    inner_idx=[i for i,n in enumerate(counts) if n>=2]
    contiguous=bool(inner_idx) and max(inner_idx)-min(inner_idx)+1==len(inner_idx)
    open_boundary=bool(inner_idx) and (min(inner_idx)==0 or max(inner_idx)==len(counts)-1)
    final_volume=sum(shape_volume(p.get("shape")) for p in parts if p.get("shape") is not None)
    outer_ratio=None
    if stack and len(sections)>=3 and parts:
        outer_sections=list(sections)
        src_min=stack.get("source_axis_min_mm");src_max=stack.get("source_axis_max_mm")
        if src_min is not None and f(src_min)<f(outer_sections[0].get("at_mm"))-1e-5:outer_sections=[{**outer_sections[0],"at_mm":f(src_min)}]+outer_sections
        if src_max is not None and f(src_max)>f(outer_sections[-1].get("at_mm"))+1e-5:outer_sections=outer_sections+[{**outer_sections[-1],"at_mm":f(src_max)}]
        try:
            outer_ref=_u_loft_from_loops(str(stack.get("axis") or "Z").upper(),outer_sections,0,ruled=bool(inner_idx))
            ov=shape_volume(outer_ref)
            outer_ratio=(final_volume/ov) if ov>1e-9 else None
        except Exception:
            outer_ratio=None
    signature_source={str(x.get("id") or x.get("signature_id") or ""):(x.get("source_evidence") or x.get("evidence") or {}) for x in (gate.get("required_signatures") or [])}
    planar_volume_error=None
    planar_volume_match=False
    if planar_expected_volume is not None and planar_expected_volume>1e-6:
        planar_volume_error=abs(final_volume-planar_expected_volume)/planar_expected_volume
        planar_volume_match=planar_volume_error<=.02
    checks=[]
    for sid0 in required:
        sid=str(sid0)
        source_sig=signature_source.get(sid) or {}
        ok=False;evidence={"executor_mode":str(plan.get("reconstruction_mode") or ""),"section_count":len(rows)}
        if sid=="non_axisymmetric_form":
            if rows:
                ok=anis>=.04
                evidence.update({"max_section_anisotropy_ratio":round(anis,4),"artifact_basis":"section_stack"})
            else:
                aspect=f(source_sig.get("plan_aspect_ratio"),1.0);var=f(source_sig.get("width_variation_ratio"),0.0)
                ok=bool(planar_volume_match and (abs(1.0-aspect)>=.04 or var>=.04))
                evidence.update({"plan_aspect_ratio":round(aspect,4),"source_width_variation_ratio":round(var,4),"planar_volume_error_ratio":round(planar_volume_error,5) if planar_volume_error is not None else None,"artifact_basis":"planar_extrusion_volume"})
        elif sid=="section_progression":
            v=max(width_var,height_var);ok=v>=.08
            evidence.update({"width_variation_ratio":round(width_var,4),"height_variation_ratio":round(height_var,4),"artifact_basis":"section_stack"})
        elif sid=="variable_plan_silhouette":
            if rows:
                v=max(width_var,height_var);ok=v>=.08
                evidence.update({"width_variation_ratio":round(width_var,4),"height_variation_ratio":round(height_var,4),"artifact_basis":"section_stack"})
            else:
                var=f(source_sig.get("variation_ratio"),0.0)
                ok=bool(planar_volume_match and var>=.14)
                evidence.update({"source_variation_ratio":round(var,4),"expected_planar_volume_mm3":round(planar_expected_volume,3) if planar_expected_volume is not None else None,"actual_volume_mm3":round(final_volume,3),"planar_volume_error_ratio":round(planar_volume_error,5) if planar_volume_error is not None else None,"artifact_basis":"planar_extrusion_volume"})
        elif sid=="necked_transition":
            if rows:
                ratios=[]
                if widths:ratios.append(min(widths)/max(widths))
                if heights:ratios.append(min(heights)/max(heights))
                ratio=min(ratios) if ratios else 1.0;ok=ratio<=.72
                evidence.update({"min_to_max_section_ratio":round(ratio,4),"artifact_basis":"section_stack"})
            else:
                ratio=f(source_sig.get("ratio"),1.0);ok=bool(planar_volume_match and ratio<=.72)
                evidence.update({"source_min_to_max_ratio":round(ratio,4),"planar_volume_error_ratio":round(planar_volume_error,5) if planar_volume_error is not None else None,"artifact_basis":"planar_extrusion_volume"})
        elif sid=="longitudinal_height_progression":
            boundary=[x for x in nodes if x.get("type")=="measured_height_boundary"]
            deltas=[abs(f(x.get("end_height_delta_ratio"))) for x in boundary]
            delta=max(deltas or [0.0])
            mode=str(plan.get("reconstruction_mode") or "")
            mesh_preserved=any(
                x.get("type")=="source_part" and
                str(((x.get("geometry_evidence") or {}).get("mesh_brep") or {}).get("status") or "").lower()=="ready" and
                str(((x.get("geometry_evidence") or {}).get("mesh_brep") or {}).get("strategy") or "")=="FACETED_MESH_BREP" and
                int(f(((x.get("geometry_evidence") or {}).get("mesh_brep") or {}).get("open_edges"),0))==0 and
                int(f(((x.get("geometry_evidence") or {}).get("mesh_brep") or {}).get("nonmanifold_edges"),0))==0
                for x in nodes
            )
            geometry_preserving_executor=(mode=="SECTION_LOFT_RECONSTRUCTION") or (mode=="MULTIPART_RELATION_REBUILD" and mesh_preserved)
            ok=delta>=.10 and geometry_preserving_executor
            evidence.update({"boundary_delta_ratio":round(delta,4),"geometry_preserving_executor":geometry_preserving_executor,"faceted_mesh_brep_preserved":mesh_preserved})
        elif sid=="section_topology_transition":
            ok=len(set(counts))>1
            evidence.update({"loop_counts":counts})
        elif sid=="open_cavity_and_inner_wall":
            ok=len(inner_idx)>=3 and contiguous and open_boundary and outer_ratio is not None and outer_ratio<.985
            evidence.update({"loop_counts":counts,"inner_sections":len(inner_idx),"contiguous_inner":contiguous,"open_to_boundary":open_boundary,"final_to_outer_volume_ratio":round(outer_ratio,4) if outer_ratio is not None else None})
        else:
            evidence.update({"reason":"artifact_signature_check_not_implemented"})
        checks.append({"signature_id":sid,"status":"PASS" if ok else "FAIL","evidence":evidence})
    return {"status":"PASS" if checks and all(x["status"]=="PASS" for x in checks) else "FAIL","checks":checks,"required_signature_ids":required,"mode":"executed_geometry_evidence_v1"}

def validate_parts(req,parts,svg_geoms,hole_tools,invalid):
    c=req.get("cad_contract") or {}; profile=(req.get("parameters") or {}).get("manufacturing_profile") or {}
    min_feature=f(profile.get("min_feature_mm"),MIN_FEATURE_DEFAULT); max_colors=int(f(profile.get("max_colors"),MAX_COLORS_DEFAULT))
    build=profile.get("build_volume_mm") or BUILD_MM
    if not isinstance(build,list) or len(build)!=3:build=BUILD_MM
    build=[f(x,BUILD_MM[i]) for i,x in enumerate(build)]
    nozzle=f(profile.get("nozzle_mm"),.4);nozzle_ok=abs(nozzle-.4)<=1e-6

    family=str(c.get("family") or "")
    uplan0=(c.get("universal_recipe") or c.get("recipe") or {}) if family=="universal_cad_recipe" else {}
    assembled_family=family in ("sculpted_lidded_container",) or (family=="universal_cad_recipe" and bool((uplan0.get("geometry_evidence") or {}).get("assembly_pose_resolved")))
    valid=all(bool(p["shape"].val().isValid()) for p in parts)
    print_tol=effective_validation_mesh_tolerance(c)
    topo,printable_part_bounds,fingerprint=mesh_validation_summaries(parts,print_tol)
    open_edges=sum(t["open_edges"] for t in topo); nonmanifold=sum(t["nonmanifold_edges"] for t in topo)
    degenerate=sum(t.get("degenerate_triangles",0) for t in topo)
    volumes=[{"part":p["name"],"volume_mm3":round(shape_volume(p["shape"]),4)} for p in parts]
    zero_volume=any(x["volume_mm3"]<=.001 for x in volumes)
    raw_collisions=pair_overlap(parts,assembled=assembled_family)
    intentional_contacts=[];collisions=list(raw_collisions)
    if family=="universal_cad_recipe":intentional_contacts,collisions=_universal_contact_semantics(c,raw_collisions)
    printable_parts=_parts_for_bbox(parts,False)
    bb=aggregate_bbox(printable_parts);dims=dims_from_bbox(bb)
    assembled_bb=aggregate_bbox(_parts_for_bbox(parts,True)) if assembled_family else bb
    assembled_dims=dims_from_bbox(assembled_bb)
    pd=c.get("product_dimensions_mm") or [c.get("width_mm"),c.get("height_mm"),c.get("thickness_mm")]
    if family=="universal_cad_recipe":
        up=c.get("universal_recipe") or c.get("recipe") or {};ue=up.get("geometry_evidence") or {};ud=ue.get("assembly_dimensions_mm") or ue.get("dimensions_mm") or []
        product_dims=[f(ud[i],assembled_dims[i]) for i in range(3)] if isinstance(ud,list) and len(ud)==3 else list(assembled_dims)
    else:
        product_dims=[f(pd[i],assembled_dims[i]) for i in range(3)] if isinstance(pd,list) and len(pd)==3 else list(assembled_dims)
    occ_tol=.01
    part_by_name={str(p.get("name") or ""):p for p in parts}
    primary_cluster_bounds=[];separate_bed_bounds=[]
    if family=="universal_cad_recipe" and str(((c.get("universal_recipe") or c.get("recipe") or {}).get("reconstruction_mode") or ""))=="MULTIPART_RELATION_REBUILD":
        for x in printable_part_bounds:
            pose=str(part_by_name.get(str(x.get("part") or ""),{}).get("source_pose_class") or "")
            if pose in ("PRIMARY_BODY","SURFACE_ATTACHED","SOURCE_ATTACHED_PART"):primary_cluster_bounds.append(x)
            elif pose=="SEPARATE_PRINT_PART":separate_bed_bounds.append(x)
        cluster_min_z=min([f(x["min"][2]) for x in primary_cluster_bounds],default=0.0)
        primary_cluster_contact=(-occ_tol<=cluster_min_z<=.05)
        separate_contact=all(x["min"][2]>=-occ_tol and x["min"][2]<=.05 for x in separate_bed_bounds)
        bed_required_bounds=separate_bed_bounds
        first_layer_ok=primary_cluster_contact and separate_contact
    else:
        bed_required_bounds=[x for x in printable_part_bounds if (part_by_name.get(str(x.get("part") or ""),{}).get("physical_separate") is True)]
        if not bed_required_bounds:bed_required_bounds=printable_part_bounds[:1]
        cluster_min_z=None;primary_cluster_contact=True;separate_contact=all(x["min"][2]>=-occ_tol and x["min"][2]<=.05 for x in bed_required_bounds)
        first_layer_ok=separate_contact
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

    curve_quality=str(c.get("curve_quality") or "").lower()
    smooth_required=curve_quality.startswith("high")
    smooth_checks=[]
    svg_parts=(req.get("svg_artifact") or {}).get("parts") or []
    for sp in svg_parts:
        d=str(sp.get("path_d") or "")
        line_cmds=sum(d.count(ch) for ch in ("L","l"))
        curve_cmds=sum(d.count(ch) for ch in ("A","a","C","c","Q","q","S","s"))
        suspicious=(line_cmds>36 and curve_cmds==0)
        smooth_checks.append({"part":sp.get("id"),"method":"svg_curve_commands","line_commands":line_cmds,"curve_commands":curve_cmds,"suspected_stair_step_trace":suspicious,"ok":not suspicious})
    mesh_tol=effective_mesh_tolerance(c)
    blender_curve_mode=("blender" in curve_quality)
    if not smooth_required:
        smooth_vector_ok=True
    elif svg_parts:
        smooth_vector_ok=(mesh_tol<=.05 and not any(x.get("suspected_stair_step_trace") for x in smooth_checks))
    elif blender_curve_mode:
        blender_smooth_ok=(mesh_tol<=.10)
        smooth_checks.append({"method":"blender_profile_mesh_tolerance","mesh_tolerance_mm":round(mesh_tol,4),"required_max_mm":.10,"ok":blender_smooth_ok})
        smooth_vector_ok=blender_smooth_ok
    else:
        smooth_checks.append({"method":"missing_smoothness_evidence","ok":False})
        smooth_vector_ok=False

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
    elif family=="hybrid_bone_tag":
        expected_dimensions_mm=[f(x) for x in (c.get("product_dimensions_mm") or [])]
        mesh_min=[min(float(b["min"][i]) for b in printable_part_bounds) for i in range(3)]
        mesh_max=[max(float(b["max"][i]) for b in printable_part_bounds) for i in range(3)]
        contract_measured_dimensions_mm=[round(mesh_max[i]-mesh_min[i],3) for i in range(3)]
        contract_dimensions_ok=len(expected_dimensions_mm)==3 and all(abs(contract_measured_dimensions_mm[i]-expected_dimensions_mm[i])<=.10 for i in range(3))
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
    universal_fidelity=None;design_fidelity_gate=None;design_fidelity_ok=True
    cad_dimension_constraints=list(c.get("_cad_dimension_bindings") or [])
    cad_dimension_constraints_ok=True
    if family=="universal_cad_recipe":
        plan=c.get("universal_recipe") or c.get("recipe") or {}
        expected_constraint_ids={str(x.get("id")) for x in list(((plan.get("feature_graph") or {}).get("nodes") or [])) if str(x.get("operation") or "").upper()=="CONSTRAIN_DIMENSION" and str(x.get("execution_state") or "")=="PARAMETRIC_MAPPING_RESOLVED"}
        applied_constraint_ids={str(x.get("constraint_id")) for x in cad_dimension_constraints}
        cad_dimension_constraints_ok=expected_constraint_ids==applied_constraint_ids
        evidence=plan.get("geometry_evidence") or {}
        multipart_mode=str(plan.get("reconstruction_mode") or "")=="MULTIPART_RELATION_REBUILD"
        contract_layout=c.get("execution_layout_dimensions_mm") or []
        src_dims=(contract_layout if multipart_mode else None) or (evidence.get("reconstruction_layout_dimensions_mm") if multipart_mode else None) or (evidence.get("print_layout_dimensions_mm") if multipart_mode else None) or evidence.get("dimensions_mm") or []
        fidelity_mode="source_reconstruction_layout_gate_v2" if multipart_mode else "overall_dimension_gate_v1"
        if not isinstance(src_dims,list) or len(src_dims)!=3 or any(f(x)<=0 for x in src_dims):
            contract_dimensions_ok=False
            universal_fidelity={"status":"FAIL","reason":"missing_source_dimensions","mode":fidelity_mode}
        else:
            expected_dimensions_mm=[f(x) for x in src_dims]
            contract_measured_dimensions_mm=[round(x,6) for x in dims]
            tolerances=[max(.60,expected_dimensions_mm[i]*.015) for i in range(3)]
            errors=[abs(contract_measured_dimensions_mm[i]-expected_dimensions_mm[i]) for i in range(3)]
            contract_dimensions_ok=all(errors[i]<=tolerances[i]+1e-9 for i in range(3))
            universal_fidelity={
              "status":"PASS" if contract_dimensions_ok else "FAIL",
              "mode":fidelity_mode,
              "dimension_semantics":c.get("dimension_semantics") or evidence.get("dimension_semantics") or None,
              "source_dimensions_mm":[round(x,3) for x in expected_dimensions_mm],
              "rebuilt_dimensions_mm":[round(x,3) for x in contract_measured_dimensions_mm],
              "absolute_error_mm":[round(x,3) for x in errors],
              "tolerance_mm":[round(x,3) for x in tolerances],
              "product_dimensions_mm":[round(f(x),3) for x in (c.get("product_dimensions_mm") or [])] if isinstance(c.get("product_dimensions_mm"),list) else None
            }
        design_fidelity_gate=_u_design_fidelity_artifact_gate(c,parts)
        design_fidelity_ok=design_fidelity_gate.get("status")=="PASS"

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
    known_support_free=family in ("silhouette_plate","rounded_plate","keychain_plate","nfc_keychain","open_box","phone_stand","sculpted_lidded_container")
    support_deferred=family=="universal_cad_recipe"
    support_required=not known_support_free
    island_free=feature_containment_ok
    mesh_ok=open_edges==0 and nonmanifold==0 and degenerate==0
    expected_parts=c.get("expected_parts") or []
    actual_parts=[{"name":str(p.get("name") or ""),"role":str(p.get("role") or "part"),"semantic_name":p.get("semantic_name"),"source_pose_class":p.get("source_pose_class"),"assembly_pose_status":p.get("assembly_pose_status"),"physical_separate":bool(p.get("physical_separate",False)),"editable_separate":bool(p.get("editable_separate",True))} for p in parts]
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
    assembly_pose_status=(evidence.get("assembly_pose_status") if family=="universal_cad_recipe" else "NOT_APPLICABLE") or "UNAVAILABLE"
    assembly_pose_resolved=assembly_pose_status in ("RESOLVED","NOT_APPLICABLE")
    repair_needed=(not valid) or (not mesh_ok) or zero_volume or self_intersection or bool(collisions) or (not assembly_parts_ok) or (not part_intent_ok) or (not orphan_geometry_free) or (not unintended_through_cut_free)

    checks={
      "brep_valid":valid and not zero_volume,"watertight":valid and mesh_ok and not zero_volume,"open_edges":open_edges,"nonmanifold_edges":nonmanifold,"degenerate_triangles":degenerate,
      "zero_volume":zero_volume,"volume_checks":volumes,"geometry_fingerprint":fingerprint,
      "self_intersection":self_intersection,"self_intersection_details":invalid,
      "part_overlap":bool(collisions),"overlaps":collisions,"source_intended_contacts":intentional_contacts,"source_intended_contact_count":len(intentional_contacts),
      "hole_penetration":hole_ok,"hole_wall_checks":hole_wall_checks,"pocket_checks":pocket_checks,
      "through_intent_checks":through_intent_checks,"unintended_through_cut_free":unintended_through_cut_free,
      "part_intent_ok":part_intent_ok,"orphan_geometry_free":orphan_geometry_free,"unexpected_parts":unexpected_parts,"part_intent_issues":intent_issues,
      "smooth_vector_ok":smooth_vector_ok,"smooth_vector_required":smooth_required,"mesh_tolerance_mm":mesh_tol,"smoothness_checks":smooth_checks,
      "typography_ok":typography_ok,"text_min_stroke_ok":text_min_stroke_ok,"glyph_clearance_ok":glyph_clearance_ok,"text_internal_clearance_ok":text_internal_clearance_ok,"text_slicer_no_merge_ok":text_slicer_no_merge_ok,"text_layout_bounds_ok":text_layout_bounds_ok,"text_readability_ok":text_readability_ok,"typography_checks":typography_checks,
      "min_feature_ok":min_ok and feature_containment_ok,"min_feature_mm":min_feature,"feature_checks":feats,"structural_checks":structural_checks,"structural_min_ok":structural_min_ok,"feature_containment":feature_containment,"feature_containment_ok":feature_containment_ok,
      "clearance_ok":clearance_ok,"clearance_checks":clearance_checks,
      "assembly_interference":bool(collisions),"assembly_parts_ok":assembly_parts_ok,"assembly_pose_status":assembly_pose_status,"assembly_pose_resolved":assembly_pose_resolved,"expected_parts":expected_parts,"actual_parts":actual_parts,"missing_expected_parts":missing_expected,"repair_needed":repair_needed,"first_layer_contact_ok":first_layer_ok,"first_layer_primary_cluster_contact":primary_cluster_contact,"first_layer_primary_cluster_min_z":round(cluster_min_z,4) if cluster_min_z is not None else None,"first_layer_separate_parts_contact":separate_contact,"island_free":island_free,
      "support_required":support_required,"support_profile_ok":known_support_free or support_deferred,"support_profile_mode":"defer_to_bambu_slice" if support_deferred else ("known_support_free" if known_support_free else "required"),
      "a1_mini_fit":fit,"build_volume_mm":build,"dimensions_mm":dims,"product_dimensions_mm":[round(x,3) for x in product_dims],"execution_layout_dimensions_mm":[round(f(x),3) for x in (c.get("execution_layout_dimensions_mm") or expected_dimensions_mm or [])] if isinstance((c.get("execution_layout_dimensions_mm") or expected_dimensions_mm or []),list) else None,"dimension_semantics":c.get("dimension_semantics") or (evidence.get("dimension_semantics") if family=="universal_cad_recipe" else None),"bbox_mm":[round(x,3) for x in bb],"printable_part_bounds":printable_part_bounds,
      "ams_colors_ok":ams_ok,"ams_color_count":len(colors),"ams_colors":colors,
      "nozzle_mm":nozzle,"nozzle_profile_ok":nozzle_ok,"real_slicer_verified":False,"slice_status":"NOT_RUN",
      "contract_svg_match":contract_svg_match,"contract_dimensions_ok":contract_dimensions_ok,"expected_dimensions_mm":expected_dimensions_mm,"contract_measured_dimensions_mm":contract_measured_dimensions_mm,
      "universal_fidelity":universal_fidelity,"design_fidelity_gate":design_fidelity_gate,"design_fidelity_ok":design_fidelity_ok,
      "cad_dimension_constraints_ok":cad_dimension_constraints_ok,"cad_dimension_constraints":cad_dimension_constraints,
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

def write_source_mesh_glb(z,parts,path,max_parts=60,max_triangles=700000):
    ranked=sorted(parts,key=lambda p:max(.001,f((p.get("dimensions_mm") or [0,0,0])[0]))*max(.001,f((p.get("dimensions_mm") or [0,0,0])[1]))*max(.001,f((p.get("dimensions_mm") or [0,0,0])[2])),reverse=True)
    selected=[];tri_budget=0
    for p in ranked:
        tc=int(p.get("triangle_count") or 0)
        if len(selected)>=max_parts:break
        if selected and tri_budget+tc>max_triangles:continue
        selected.append(p);tri_budget+=tc
    pos_tmp=pathlib.Path(str(path)+".pos.tmp");idx_tmp=pathlib.Path(str(path)+".idx.tmp")
    vcount=tcount=0;mn=[float("inf")]*3;mx=[float("-inf")]*3;used=[]
    with open(pos_tmp,"wb",buffering=1024*1024) as pf,open(idx_tmp,"wb",buffering=1024*1024) as inf:
        for p in selected:
            try:mesh=_dw_extract_mesh(z,p,p.get("world_transform"))
            except Exception:continue
            v=mesh.get("vertices") or [];t=mesh.get("triangles") or []
            if not v or not t:continue
            base=vcount
            for q in v:
                x,y,z0=f(q[0]),f(q[1]),f(q[2]);pf.write(struct.pack("<fff",x,y,z0));vcount+=1
                mn[0]=min(mn[0],x);mn[1]=min(mn[1],y);mn[2]=min(mn[2],z0);mx[0]=max(mx[0],x);mx[1]=max(mx[1],y);mx[2]=max(mx[2],z0)
            for a,b,c0 in t:inf.write(struct.pack("<III",int(a)+base,int(b)+base,int(c0)+base));tcount+=1
            used.append(str(p.get("source_object_id") or p.get("name") or "part"))
            del mesh,v,t;gc.collect()
    if not vcount:
        for tmp in (pos_tmp,idx_tmp):
            try:tmp.unlink()
            except Exception:pass
        raise ValueError("source glb: no mesh vertices")
    pos_len=pos_tmp.stat().st_size;idx_len=idx_tmp.stat().st_size;binary_len=pos_len+idx_len
    doc={"asset":{"version":"2.0","generator":"MakerSence source 3MF preview"},
      "scene":0,"scenes":[{"nodes":[0]}],"nodes":[{"mesh":0}],
      "meshes":[{"primitives":[{"attributes":{"POSITION":0},"indices":1}]}],
      "buffers":[{"byteLength":binary_len}],
      "bufferViews":[{"buffer":0,"byteOffset":0,"byteLength":pos_len,"target":34962},{"buffer":0,"byteOffset":pos_len,"byteLength":idx_len,"target":34963}],
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
    return {"status":"READY","part_count":len(used),"source_part_count":len(parts),"partial":len(used)<len(parts),"vertex_count":vcount,"triangle_count":tcount,"bounds_mm":{"min":[round(x,3) for x in mn],"max":[round(x,3) for x in mx],"dimensions":[round(mx[i]-mn[i],3) for i in range(3)]},"included_part_ids":used}

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

def render_product_png(req,parts,svg_geoms,path,validation):
    render_tol=resource_policy(req)["render_mesh_tolerance_mm"]
    meshes=tessellated_parts(parts,render_tol,assembled=True)
    allv=[v for m in meshes for v in m["vertices"]]
    if not allv:raise ValueError("render: no mesh vertices")
    final_w,final_h=1400,1050;ss=2;W,H=final_w*ss,final_h*ss
    intent=((req.get("parameters") or {}).get("manufacturing_profile") or {}).get("research_dna",{}).get("product_intent") or {}
    warm=str(intent.get("archetype") or "")=="music_photo_nfc_keepsake"
    top=(244,239,226) if warm else (236,238,235);bot=(210,205,191) if warm else (197,203,198)
    img=Image.new("RGB",(W,H),top);d=ImageDraw.Draw(img)
    for y in range(H):
        t=y/max(1,H-1);d.line([(0,y),(W,y)],fill=_mix(top,bot,t))
    # Camera = front/top-right; exact geometry is the source, scene dressing is not manufacturing geometry.
    hybrid_bone=str((req.get("cad_contract") or {}).get("family") or "")=="hybrid_bone_tag"
    view=_vnorm((0.0,-0.18,1.0) if hybrid_bone else (0.62,-0.70,0.78));right=_vnorm(_vcross((0,0,1),view));up=_vnorm(_vcross(view,right))
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
    camera_ok=(not off_canvas) and coverage>=.08 and coverage<=.72
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
    if warm:
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
    if raised:
        # Compositing known CAD relief layers in height order avoids the triangle
        # painter algorithm drawing an underlying cheek over an eye or nose.
        layer_order={"PET_ACCENT":0,"PET_DETAIL":1}
        for m in sorted(raised,key=lambda item:layer_order.get(str(item.get("name")),4)):
            draw_mesh_set([m])
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
      "intent_archetype":intent.get("archetype"),"mock_content":["photo_insert"] if warm else [],
      "camera_ok":camera_ok,"off_canvas":off_canvas,"projected_coverage":round(coverage,4),
      "note":"Mock content is presentation-only; product geometry is rendered from the same formal parts exported to 3MF."
    }

def commercial_visual_gate(req,parts,validation):
    render=validation.get("product_render") or {}
    cad=req.get("cad_contract") or {}
    family=str(cad.get("family") or "")
    coverage=f(render.get("projected_coverage"),0)
    checks={
      "render_completed":render.get("status")=="PASS",
      "camera_composition":render.get("camera_ok") is True and render.get("off_canvas") is False,
      "product_frame_coverage":coverage>=.12 and coverage<=.68,
      "smooth_surface_language":validation.get("smooth_vector_ok") is True,
      "semantic_geometry_explained":validation.get("part_intent_ok") is True and validation.get("orphan_geometry_free") is True
    }
    metrics={"projected_coverage":round(coverage,4),"family":family,"part_count":len(parts)}
    if family=="hybrid_bone_tag":
        vm=req.get("_hybrid_visual_metrics") or {}
        checks.update({
          "recognizable_silhouette":vm.get("silhouette_profile_ok") is True,
          "canonical_bone_proportion":vm.get("canonical_bone_ratio_ok") is True,
          "symmetry_balance":vm.get("symmetry_balance_ok") is True,
          "semantic_scope":vm.get("semantic_scope_ok") is True,
          "integrated_keyring_hole":f(vm.get("keyring_edge_margin_mm"),0)>=2.0,
          "balanced_detail_scale":vm.get("detail_scale_ok") is True,
          "negative_space_preserved":vm.get("negative_space_ok") is True
        })
        metrics.update(vm)
    ok=all(v is True for v in checks.values())
    return {
      "status":"PASS" if ok else "FAIL",
      "checks":checks,
      "metrics":metrics,
      "policy":"Printable geometry is not sufficient: commercial output must also preserve recognizable silhouette, visual hierarchy, negative space and integrated functional features.",
      "action":"ACCEPT" if ok else "REDESIGN_VISUAL_LANGUAGE"
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
    try:
        validation["product_render"]=render_product_png(req,parts,svg_geoms,png,validation)
        validation["commercial_visual_gate"]=commercial_visual_gate(req,parts,validation)
        validation["commercial_visual_ok"]=validation["commercial_visual_gate"].get("status")=="PASS"
        if png.exists() and png.stat().st_size>1024:
            artifacts.append({"type":"png","name":"product_render_main.png","url":"/v1/artifacts/"+folder.name+"/product_render_main.png"})
        else:
            validation["product_render"]={"status":"FAIL","error":"render output missing or empty"}
            validation["commercial_visual_gate"]=commercial_visual_gate(req,parts,validation)
            validation["commercial_visual_ok"]=False
    except Exception as ex:
        validation["product_render"]={"status":"FAIL","error":str(ex)}
        validation["commercial_visual_gate"]=commercial_visual_gate(req,parts,validation)
        validation["commercial_visual_ok"]=False
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
        req["_job_folder"]=str(folder)
        parts,svg_geoms,hole_tools,invalid=build(req)
        JOBS[jid].update({"stage":"validating","updated_at":time.time()})
        rss_built=memory_guard(req,"build_done",hard=True)
        validation=validate_parts(req,parts,svg_geoms,hole_tools,invalid)
        if (req.get("cad_contract") or {}).get("family")=="hybrid_bone_tag":
            validation["blender_hybrid"]=req.get("_blender_profile") or {"status":"FAIL"}
            validation["blender_hybrid_ok"]=bool(req.get("_blender_profile")) and (folder/"source.blend").is_file()
            contract=req.get("cad_contract") or {}
            actual=dict(req.get("_hybrid_measured_dims") or {})
            bounds=validation.get("printable_part_bounds") or []
            base=next((b for b in bounds if b.get("part")=="BODY"),None)
            if base and bounds:
                base_top=float(base["max"][2])
                actual["relief_height_mm"]=round(max(float(b["max"][2]) for b in bounds)-base_top,4)
            key_checks=[]
            for item in contract.get("key_dimension_targets") or []:
                field=str(item.get("contract_field") or "")
                expected=f(item.get("value_mm"))
                tolerance=f(item.get("tolerance_mm"))
                measured=actual.get(field)
                key_checks.append({"name":item.get("name"),"field":field,"target_mm":expected,"actual_mm":measured,"tolerance_mm":tolerance,"ok":measured is not None and abs(measured-expected)<=tolerance+1e-6})
            measured_dims=validation.get("contract_measured_dimensions_mm") or []
            target_dims=contract.get("product_dimensions_mm") or []
            if len(measured_dims)==3 and len(target_dims)==3:
                for axis,index in (("X",0),("Y",1),("Z",2)):
                    measured=float(measured_dims[index]);target=float(target_dims[index]);key_checks.append({"name":"成品 "+axis,"field":"product_dimensions_mm."+axis,"target_mm":target,"actual_mm":measured,"tolerance_mm":.35,"ok":abs(measured-target)<=.35})
            names={str(p.get("name") or "") for p in parts}
            allowed_names={"BODY","PET_ACCENT","PET_DETAIL"}
            appearance_scope_ok=names==allowed_names
            validation["appearance_scope_gate"]={"status":"PASS" if appearance_scope_ok else "FAIL","actual_parts":sorted(names),"allowed_parts":sorted(allowed_names),"policy":"Only concept-backed semantic geometry may be emitted; unrequested character faces, mascots or ornaments are forbidden."}
            validation["appearance_scope_ok"]=appearance_scope_ok
            signature_evidence={"bone_silhouette":validation["blender_hybrid_ok"] and "BODY" in names,"paw_pad_relief":"PET_ACCENT" in names,"pet_surface_detail":"PET_DETAIL" in names,"nfc_zone":"BODY" in names and bool((contract.get("pockets") or []))}
            signature_checks=[{"signature_id":sid,"status":"PASS" if signature_evidence.get(str(sid)) else "FAIL"} for sid in (contract.get("required_signature_ids") or [])]
            signature_ok=bool(signature_checks) and all(x["status"]=="PASS" for x in signature_checks) and all(x["ok"] for x in key_checks)
            validation["hybrid_signature_gate"]={"status":"PASS" if signature_ok else "FAIL","checks":signature_checks,"key_dimensions":key_checks,"source":"verified_blender_profile_and_cad_feature_operations"}
            validation["hybrid_signature_ok"]=signature_ok
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
        required.append("commercial_visual_ok")
        if family=="universal_cad_recipe":
            required.append("cad_dimension_constraints_ok")
            if ((validation.get("design_fidelity_gate") or {}).get("required_signature_ids") or []):required.append("design_fidelity_ok")
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
        try:
            # Reclaim allocator/native CAD leftovers from the previous job before
            # applying the admission guard. This matters for long-lived workers.
            gc.collect();_malloc_trim()
            memory_guard(req,"generate_start",hard=True)
            return _run_generate_job_inner(jid,req,folder)
        except Exception as ex:
            # Admission failures happen outside _run_generate_job_inner(), so they
            # must explicitly terminate the public job instead of leaving it stuck
            # forever in processing/queued.
            job=JOBS.get(jid) or {}
            if job.get("status") not in ("completed","failed"):
                created=float(job.get("created_at") or time.time())
                msg=str(ex)
                JOBS[jid].update({"status":"failed","stage":"failed","error":msg,
                    "validation":{"status":"FAIL","geometry_error":msg},
                    "artifacts":[],"duration_ms":int((time.time()-created)*1000),
                    "updated_at":time.time()})
            print("generate admission failed:",jid,repr(ex),flush=True)
        finally:
            # _run_generate_job_inner has returned here, so its local CAD objects
            # are no longer retained; trim once more to keep sequential jobs flat.
            gc.collect();_malloc_trim()

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
    isolated=family=="static_functional_utensil_vessel"
    bounded=family=="universal_cad_recipe"
    target=_run_generate_job_isolated if isolated else _run_generate_job
    execution_mode="isolated_subprocess" if isolated else ("bounded_inprocess" if bounded else "in_process")
    JOBS[jid]["execution_mode"]=execution_mode
    threading.Thread(target=target,args=(jid,req,folder),daemon=True,name=("makersence-iso-" if isolated else "makersence-")+jid[:8]).start()
    return {"job_id":jid,"status":"processing","execution_mode":execution_mode}

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
    probe_fracs=(.05,.15,.30,.50,.70,.85,.95)
    for axis in range(3):
        d=b['dimensions'][axis]
        if d<.8:continue
        for fr in probe_fracs:samples.append((axis,fr,_dw_section_holes(mesh,axis,b['min'][axis]+d*fr)))
    for axis in range(3):
        groups=[];d=b['dimensions'][axis]
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
            frs=sorted(set(round(x,2) for x in g['fr']));count=len(frs)
            if count<2:continue
            aspect=max(g['bbox'])/max(.001,min(g['bbox']));circ=sum(g['circ'])/len(g['circ'])
            touches_low=min(frs)<=.16;touches_high=max(frs)>=.84;through=touches_low and touches_high
            if circ>.78 and aspect<1.25:kind='circular_hole' if through else ('blind_circular_pocket' if touches_low or touches_high else 'internal_circular_cavity')
            elif aspect>1.45:kind='slot_or_elongated_hole' if through else ('blind_slot_or_channel' if touches_low or touches_high else 'internal_elongated_cavity')
            else:kind='through_opening' if through else ('blind_pocket' if touches_low or touches_high else 'internal_opening')
            eq=2*math.sqrt((sum(g['areas'])/len(g['areas']))/math.pi)
            observed=max(0,(max(frs)-min(frs))*d)
            conf='HIGH' if through and count>=5 else ('HIGH' if (touches_low or touches_high) and count>=3 else 'MEDIUM')
            opening_face='MIN' if touches_low and not touches_high else ('MAX' if touches_high and not touches_low else None)
            out.append({'axis':'XYZ'[axis],'kind':kind,'center_2d_mm':[round(x,2) for x in g['c']],'opening_size_mm':[round(x,2) for x in g['bbox']],'equivalent_diameter_mm':round(eq,2),'circularity':round(circ,3),'persistence':str(count)+'/'+str(len(probe_fracs)),'sample_fractions':frs,'through_candidate':through,'opening_face':opening_face,'observed_depth_mm':round(observed,3) if not through else None,'confidence':conf,'measurement_quality':'CALCULATED' if through else 'ESTIMATED_FROM_SECTIONS'})
    return out

def _dw_planar_face_clusters(mesh):
    if len(mesh.get('triangles') or [])>160000:return []
    vv=mesh.get('vertices') or [];tt=mesh.get('triangles') or [];out=[]
    for axis in range(3):
        raw=[];uv=[i for i in range(3) if i!=axis]
        for ti,t in enumerate(tt):
            a,b,c=vv[t[0]],vv[t[1]],vv[t[2]];n=_dw_normal(a,b,c)
            if abs(n[axis])<=.985:continue
            ux=b[0]-a[0];uy=b[1]-a[1];uz=b[2]-a[2];vx=c[0]-a[0];vy=c[1]-a[1];vz=c[2]-a[2]
            cx=uy*vz-uz*vy;cy=uz*vx-ux*vz;cz=ux*vy-uy*vx;area=.5*math.sqrt(cx*cx+cy*cy+cz*cz)
            if area<=.01:continue
            pts=[a,b,c];raw.append({
                'coord':sum(p[axis] for p in pts)/3,
                'sign':1 if n[axis]>=0 else -1,
                'area':area,
                'center':[sum(p[uv[0]] for p in pts)/3,sum(p[uv[1]] for p in pts)/3],
                'min2':[min(p[uv[0]] for p in pts),min(p[uv[1]] for p in pts)],
                'max2':[max(p[uv[0]] for p in pts),max(p[uv[1]] for p in pts)]
            })
        raw.sort(key=lambda x:(x['sign'],x['coord']));clusters=[];tol=.06
        for f0 in raw:
            q=clusters[-1] if clusters else None
            if q is None or q['sign']!=f0['sign'] or abs(f0['coord']-q['coord'])>tol:
                q={'axis':'XYZ'[axis],'axis_index':axis,'sign':f0['sign'],'coord':f0['coord'],'area':0.0,'center':[0.0,0.0],'min2':f0['min2'][:],'max2':f0['max2'][:],'triangle_count':0};clusters.append(q)
            total=q['area']+f0['area']
            q['coord']=(q['coord']*q['area']+f0['coord']*f0['area'])/max(total,1e-9)
            q['center']=[(q['center'][i]*q['area']+f0['center'][i]*f0['area'])/max(total,1e-9) for i in range(2)]
            q['min2']=[min(q['min2'][i],f0['min2'][i]) for i in range(2)]
            q['max2']=[max(q['max2'][i],f0['max2'][i]) for i in range(2)]
            q['area']=total;q['triangle_count']+=1
        total_area=sum(x['area'] for x in clusters) or 1.0
        for i,q in enumerate(clusters,1):
            q['face_id']='MESH_FACE_'+q['axis']+('_POS_' if q['sign']>0 else '_NEG_')+str(i)
            q['plane_mm']=round(q['coord'],4);q['area_mm2']=round(q['area'],3)
            q['center_2d_mm']=[round(x,3) for x in q['center']]
            q['bbox_2d_mm']=[[round(x,3) for x in q['min2']],[round(x,3) for x in q['max2']]]
            q['size_2d_mm']=[round(q['max2'][i]-q['min2'][i],3) for i in range(2)]
            q['support_ratio']=round(q['area']/total_area,4)
            q['normal']=[0,0,0];q['normal'][axis]=q['sign']
            for k in ('coord','min2','max2','center','sign','axis_index'):q.pop(k,None)
            out.append(q)
    return out

def _dw_attach_face_evidence(mesh,openings):
    if not openings:return openings
    b=_dw_bbox(mesh.get('vertices') or []);faces=_dw_planar_face_clusters(mesh)
    if not faces:return openings
    axes='XYZ'
    for h in openings:
        kind=str(h.get('kind') or '')
        if not kind.startswith('blind_'):continue
        axis=str(h.get('axis') or '').upper()
        if axis not in axes:continue
        ai=axes.index(axis);opening_side=str(h.get('opening_face') or '')
        if opening_side not in ('MIN','MAX'):continue
        center=h.get('center_2d_mm') or [];size=h.get('opening_size_mm') or []
        if len(center)!=2 or len(size)!=2 or min(size)<=0:continue
        outer_coord=b['min'][ai] if opening_side=='MIN' else b['max'][ai]
        opposite_coord=b['max'][ai] if opening_side=='MIN' else b['min'][ai]
        want_sign=-1 if opening_side=='MIN' else 1
        same=[x for x in faces if x.get('axis')==axis and (x.get('normal') or [0,0,0])[ai]==want_sign]
        opposite=[x for x in faces if x.get('axis')==axis and (x.get('normal') or [0,0,0])[ai]==-want_sign]
        open_faces=sorted(same,key=lambda x:(abs(float(x.get('plane_mm') or 0)-outer_coord),-float(x.get('area_mm2') or 0)))
        opening_face=open_faces[0] if open_faces and abs(float(open_faces[0].get('plane_mm') or 0)-outer_coord)<=.15 else None
        bottom=[]
        for x in same:
            plane=float(x.get('plane_mm') or 0);depth=abs(outer_coord-plane)
            if depth<.18 or depth>max(30,b['dimensions'][ai]*.92):continue
            bb=x.get('bbox_2d_mm') or [];sz=x.get('size_2d_mm') or []
            if len(bb)!=2 or len(sz)!=2:continue
            if not (bb[0][0]-.6<=center[0]<=bb[1][0]+.6 and bb[0][1]-.6<=center[1]<=bb[1][1]+.6):continue
            ratios=[sz[i]/max(.01,float(size[i])) for i in range(2)]
            if not all(.48<=r<=1.35 for r in ratios):continue
            center_err=math.dist(center,x.get('center_2d_mm') or center)
            size_err=sum(abs(1-r) for r in ratios)
            score=size_err+center_err/max(1,max(size))*.8+depth/max(.1,b['dimensions'][ai])*.15
            bottom.append((score,x,depth))
        bottom.sort(key=lambda q:q[0]);bottom_face=bottom[0][1] if bottom else None
        opp_faces=sorted(opposite,key=lambda x:(abs(float(x.get('plane_mm') or 0)-opposite_coord),-float(x.get('area_mm2') or 0)))
        opposite_face=opp_faces[0] if opp_faces and abs(float(opp_faces[0].get('plane_mm') or 0)-opposite_coord)<=.15 else None
        if bottom_face:
            cavity_depth=abs(float(bottom_face.get('plane_mm'))-outer_coord)
            residual=abs(float(bottom_face.get('plane_mm'))-float(opposite_face.get('plane_mm'))) if opposite_face else None
            conf='HIGH' if opening_face and opposite_face else 'MEDIUM'
            h['face_evidence']={
                'status':'RESOLVED' if opening_face and opposite_face else 'PARTIAL',
                'confidence':conf,
                'opening_face':opening_face,
                'bottom_face':bottom_face,
                'opposite_exterior_face':opposite_face,
                'cavity_depth_mm':round(cavity_depth,3),
                'residual_wall_mm':round(residual,3) if residual is not None else None,
                'measurement_axis':axis,
                'measurement_quality':'MEASURED_PLANAR_FACE_CLUSTERS'
            }
            h['observed_depth_mm']=round(cavity_depth,3)
    return openings

def _dw_planar_spacing(mesh):
    if len(mesh.get('triangles') or [])>160000:return []
    vv=mesh.get('vertices') or [];tt=mesh.get('triangles') or [];axes='XYZ';out=[]
    for axis in range(3):
        faces=[]
        for t in tt:
            a,b,c=vv[t[0]],vv[t[1]],vv[t[2]];n=_dw_normal(a,b,c)
            if abs(n[axis])<=.985:continue
            ux=b[0]-a[0];uy=b[1]-a[1];uz=b[2]-a[2];vx=c[0]-a[0];vy=c[1]-a[1];vz=c[2]-a[2]
            cx=uy*vz-uz*vy;cy=uz*vx-ux*vz;cz=ux*vy-uy*vx;area=.5*math.sqrt(cx*cx+cy*cy+cz*cz)
            if area>.01:faces.append({'coord':(a[axis]+b[axis]+c[axis])/3,'area':area})
        if not faces:continue
        faces.sort(key=lambda x:x['coord']);clusters=[];tol=.08
        for f0 in faces:
            q=clusters[-1] if clusters else None
            if q is None or abs(f0['coord']-q['coord'])>tol:q={'coord':f0['coord'],'area':0.0,'count':0};clusters.append(q)
            total=q['area']+f0['area'];q['coord']=(q['coord']*q['area']+f0['coord']*f0['area'])/max(total,1e-9);q['area']=total;q['count']+=1
        total_area=sum(x['area'] for x in clusters);sig=[x for x in clusters if x['area']>=max(1,total_area*.015)];cand=[]
        for i in range(len(sig)):
            for j in range(i+1,len(sig)):
                d=abs(sig[j]['coord']-sig[i]['coord'])
                if d<.35 or d>25:continue
                support=min(sig[i]['area'],sig[j]['area'])/max(total_area,1e-9)
                cand.append({'axis':axes[axis],'spacing_mm':round(d,3),'plane_a_mm':round(sig[i]['coord'],3),'plane_b_mm':round(sig[j]['coord'],3),'support_ratio':round(support,4),'confidence':'HIGH' if support>.12 else ('MEDIUM' if support>.04 else 'LOW'),'measurement_quality':'CALCULATED_PAIRED_PLANES'})
        cand.sort(key=lambda x:({'HIGH':0,'MEDIUM':1,'LOW':2}.get(x['confidence'],9),-x['support_ratio'],x['spacing_mm']))
        out.extend(cand[:5])
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
        if typ=='chamfer':out.append({'kind':'chamfer','location':key,'center_mm':[round(q,2) for q in center],'angle_deg':round(mean,1),'size_mm':est,'callout':('C'+str(est)+' �� '+str(round(mean))+'�X') if est else ('�˨� �P '+str(round(mean))+'�X'),'confidence':conf,'measurement_quality':'ESTIMATED' if est else 'DETECTED'})
        else:out.append({'kind':'fillet','location':key,'center_mm':[round(q,2) for q in center],'radius_mm':est,'callout':('R'+str(est)) if est else '�ꨤ','confidence':conf,'measurement_quality':'ESTIMATED' if est else 'DETECTED'})
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

def _dw_projected_profile(mesh,axis):
    vv=mesh.get("vertices") or [];tris=mesh.get("triangles") or [];polys=[]
    if len(tris)>120000:return []
    for t in tris:
        try:
            pts=[_dw_to2(vv[int(t[k])],axis) for k in range(3)]
            p=Polygon([(f(q[0]),f(q[1])) for q in pts])
            if p.is_valid and p.area>1e-7:polys.append(p)
        except Exception:
            continue
    if not polys:return []
    try:g=unary_union(polys)
    except Exception:return []
    out=[]
    gs=[g] if isinstance(g,Polygon) else ([x for x in g.geoms if isinstance(x,Polygon)] if isinstance(g,(MultiPolygon,GeometryCollection)) else [])
    for p in gs:
        ext=list(p.exterior.coords)[:-1]
        if len(ext)>=3:out.append(_dw_compact_loop(ext,220))
        for ring in p.interiors:
            pts=list(ring.coords)[:-1]
            if len(pts)>=3:out.append(_dw_compact_loop(pts,220))
    return out

def _dw_planar_prismatic(mesh,b):
    dims=list(b.get("dimensions") or [])
    if len(dims)!=3:return None
    hi=max(dims);lo=min(dims)
    if hi<=1e-6 or lo<=0 or lo/hi>.18:return None
    ai=dims.index(lo);best=None
    for fr in (.2,.35,.5,.65,.8):
        at=b["min"][ai]+lo*fr
        loops=[x for x in _dw_loops(_dw_slice(mesh,ai,at)) if abs(_dw_area(x))>.05]
        if not loops:continue
        score=sum(abs(_dw_area(x)) for x in loops)
        if best is None or score>best["score"]:best={"at":at,"loops":loops,"score":score,"fraction":fr}
    if best is None:
        projected=_dw_projected_profile(mesh,ai)
        if not projected:return None
        return {"status":"ready","axis":"XYZ"[ai],"thickness_mm":round(lo,3),"axis_min_mm":round(b["min"][ai],3),"axis_max_mm":round(b["max"][ai],3),"sample_at_mm":None,"sample_fraction":None,"profile_loops":projected,"loop_count":len(projected),"method":"measured_projected_mesh_multi_loop_even_odd"}
    compact=[_dw_compact_loop(x,220) for x in best["loops"]]
    return {"status":"ready","axis":"XYZ"[ai],"thickness_mm":round(lo,3),"axis_min_mm":round(b["min"][ai],3),"axis_max_mm":round(b["max"][ai],3),"sample_at_mm":round(best["at"],3),"sample_fraction":best["fraction"],"profile_loops":compact,"loop_count":len(compact),"method":"measured_multisample_planar_multi_loop_even_odd"}

def _u_planar_multiloop_shape(ev):
    loops=[x for x in (ev.get("profile_loops") or []) if isinstance(x,list) and len(x)>=3]
    if not loops:raise ValueError("UNIVERSAL_PLANAR_MULTI_LOOP_MISSING")
    geom=None
    for loop in loops:
        try:
            p=Polygon([(f(q[0]),f(q[1])) for q in loop])
            if not p.is_valid:p=p.buffer(0)
            if p.is_empty or p.area<=1e-6:continue
            geom=p if geom is None else geom.symmetric_difference(p)
        except Exception:
            continue
    if geom is None or geom.is_empty:raise ValueError("UNIVERSAL_PLANAR_MULTI_LOOP_INVALID")
    polys=[geom] if isinstance(geom,Polygon) else ([x for x in geom.geoms if isinstance(x,Polygon)] if isinstance(geom,(MultiPolygon,GeometryCollection)) else [])
    if not polys:raise ValueError("UNIVERSAL_PLANAR_MULTI_LOOP_NO_POLYGONS")
    axis=str(ev.get("axis") or "Z").upper();t=f(ev.get("thickness_mm"));amin=f(ev.get("axis_min_mm"));amax=f(ev.get("axis_max_mm"))
    if t<=0:raise ValueError("UNIVERSAL_PLANAR_MULTI_LOOP_THICKNESS")
    if axis=="Y":plane=cq.Plane(origin=(0,amax,0),xDir=(1,0,0),normal=(0,-1,0))
    else:plane=_u_plane(axis,amin)
    solids=[]
    for poly in polys:
        ext=list(poly.exterior.coords)[:-1]
        if len(ext)<3:continue
        wp=cq.Workplane(plane).polyline(ext).close()
        for ring in poly.interiors:
            pts=list(ring.coords)[:-1]
            if len(pts)>=3:wp=wp.polyline(pts).close()
        sh=wp.extrude(t)
        solids.extend(sh.solids().vals())
    if not solids:raise ValueError("UNIVERSAL_PLANAR_MULTI_LOOP_EMPTY")
    comp=cq.Compound.makeCompound(solids)
    if comp.isNull() or not comp.isValid():raise ValueError("UNIVERSAL_PLANAR_MULTI_LOOP_BREP_INVALID")
    return cq.Workplane("XY").newObject([comp])

def _dw_mesh_brep_evidence(mesh,reconstruction):
    vv=mesh.get("vertices") or [];tt=mesh.get("triangles") or []
    counts=[len(s.get("profile_loops") or []) for s in (reconstruction or {}).get("sections") or []]
    inner_idx=[i for i,n in enumerate(counts) if n>=2]
    sealed_internal_cavity=bool(inner_idx) and min(inner_idx)>0 and max(inner_idx)<len(counts)-1
    complex_topology=(max(counts or [0])>2) or f((reconstruction or {}).get("topology_stability"),1)<.42 or sealed_internal_cavity
    if not complex_topology:return None
    if len(tt)<4 or len(tt)>30000 or len(vv)>18000:
        return {"status":"unavailable","reason":"mesh_budget_exceeded","vertex_count":len(vv),"triangle_count":len(tt)}
    try:
        cleaned=clean_mesh_data(vv,tt,.02);cv,ct=cleaned["vertices"],cleaned["triangles"]
        if int(cleaned.get("open_edges") or 0)!=0 or int(cleaned.get("nonmanifold_edges") or 0)!=0:
            return {"status":"unavailable","reason":"source_mesh_not_closed_manifold","open_edges":cleaned.get("open_edges"),"nonmanifold_edges":cleaned.get("nonmanifold_edges"),"vertex_count":len(cv),"triangle_count":len(ct)}
        max_edge=0.0
        for tri in ct:
            a,b,c=[cv[int(i)] for i in tri]
            max_edge=max(max_edge,math.dist(a,b),math.dist(b,c),math.dist(c,a))
        payload={"v":[[round(f(p[0]),4),round(f(p[1]),4),round(f(p[2]),4)] for p in cv],"t":[[int(x) for x in q] for q in ct]}
        raw=json.dumps(payload,separators=(",",":")).encode("utf-8")
        packed=base64.b64encode(zlib.compress(raw,6)).decode("ascii")
        return {"status":"ready","encoding":"zlib_base64_json_v1","payload":packed,"vertex_count":len(cv),"triangle_count":len(ct),"open_edges":0,"nonmanifold_edges":0,"max_triangle_edge_mm":round(max_edge,3),"source":"MEASURED_TRANSFORMED_3MF_MESH","strategy":"FACETED_MESH_BREP"}
    except Exception as ex:
        return {"status":"unavailable","reason":"mesh_evidence_error:"+str(ex)[:180]}

def _u_faceted_mesh_brep_shape(ev):
    if str((ev or {}).get("status") or "").lower()!="ready":raise ValueError("UNIVERSAL_FACETED_BREP_EVIDENCE_NOT_READY")
    if str(ev.get("encoding") or "")!="zlib_base64_json_v1":raise ValueError("UNIVERSAL_FACETED_BREP_ENCODING")
    try:
        data=json.loads(zlib.decompress(base64.b64decode(ev.get("payload") or "")).decode("utf-8"))
        vv=data.get("v") or [];tt=data.get("t") or []
    except Exception as ex:
        raise ValueError("UNIVERSAL_FACETED_BREP_DECODE:"+str(ex))
    if len(tt)<4 or len(tt)>30000 or len(vv)>18000:raise ValueError("UNIVERSAL_FACETED_BREP_BUDGET")
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon,BRepBuilderAPI_MakeFace,BRepBuilderAPI_Sewing
    from OCP.gp import gp_Pnt
    sewing=BRepBuilderAPI_Sewing(0.001)
    face_count=0
    for tri in tt:
        try:
            ids=[int(tri[0]),int(tri[1]),int(tri[2])]
            pts=[vv[i] for i in ids]
            if len(pts)!=3:continue
            mp=BRepBuilderAPI_MakePolygon()
            for p in pts:mp.Add(gp_Pnt(f(p[0]),f(p[1]),f(p[2])))
            mp.Close()
            if not mp.IsDone():continue
            mf=BRepBuilderAPI_MakeFace(mp.Wire())
            if not mf.IsDone():continue
            sewing.Add(mf.Face());face_count+=1
        except Exception:
            continue
    if face_count<4:raise ValueError("UNIVERSAL_FACETED_BREP_NO_FACES")
    sewing.Perform()
    sewn=cq.Shape.cast(sewing.SewedShape())
    shells=sewn.Shells()
    if not shells and isinstance(sewn,cq.Shell):shells=[sewn]
    solids=[]
    for sh in shells:
        try:
            s=cq.Solid.makeSolid(sh)
            if s and not s.isNull() and s.isValid() and s.Volume()>1e-5:solids.append(s)
        except Exception:
            continue
    if not solids:raise ValueError("UNIVERSAL_FACETED_BREP_SOLIDIFY_FAILED")
    shape=solids[0] if len(solids)==1 else cq.Compound.makeCompound(solids)
    if shape.isNull() or not shape.isValid():raise ValueError("UNIVERSAL_FACETED_BREP_INVALID")
    return cq.Workplane("XY").newObject([shape])

def _mechanism_shape_distance_checked(a,b):
    try:
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape
        aa=a.val().wrapped if hasattr(a,"val") else a.wrapped
        bb=b.val().wrapped if hasattr(b,"val") else b.wrapped
        d=BRepExtrema_DistShapeShape(aa,bb);d.Perform()
        return float(d.Value()) if d.IsDone() else None
    except Exception:
        return None

def _mechanism_shape_distance(a,b):
    d=_mechanism_shape_distance_checked(a,b)
    return 9999.0 if d is None else d

def _mechanism_bbox_gap(a,b):
    try:
        ba=a.val().BoundingBox();bb=b.val().BoundingBox()
        gaps=[
            max(0.0,ba.xmin-bb.xmax,bb.xmin-ba.xmax),
            max(0.0,ba.ymin-bb.ymax,bb.ymin-ba.ymax),
            max(0.0,ba.zmin-bb.zmax,bb.zmin-ba.zmax)
        ]
        return (sum(x*x for x in gaps))**0.5
    except Exception:
        return None

def _mechanism_contact_distance(a,b,threshold=1.5):
    gap=_mechanism_bbox_gap(a,b)
    gate=max(.05,f(threshold,1.5))
    if gap is not None and gap>gate+.25:
        return gap
    return _mechanism_shape_distance(a,b)

def _motion_memory_checkpoint(counter,every=4):
    try:
        if int(counter)%max(1,int(every))==0:
            gc.collect();_malloc_trim()
    except Exception:
        pass

def _mechanism_collision_volume(a,b):
    try:
        ba=a.val().BoundingBox();bb=b.val().BoundingBox()
        if ba.xmax<=bb.xmin+1e-6 or bb.xmax<=ba.xmin+1e-6 or ba.ymax<=bb.ymin+1e-6 or bb.ymax<=ba.ymin+1e-6 or ba.zmax<=bb.zmin+1e-6 or bb.zmax<=ba.zmin+1e-6:return 0.0
        separation=_mechanism_shape_distance_checked(a,b)
        if separation is not None and separation>1e-5:return 0.0
        return max(0.0,shape_volume(a.intersect(b)))
    except Exception:
        return 1e30

def _mechanism_pose_probe(req):
    part_ev=req.get("part_mesh_brep") or {};parent_ev=req.get("parent_mesh_brep") or {}
    part=_u_faceted_mesh_brep_shape(part_ev);parent=_u_faceted_mesh_brep_shape(parent_ev)
    obstacles=[]
    for x in req.get("obstacles") or []:
        try:obstacles.append((str(x.get("name") or "obstacle"),_u_faceted_mesh_brep_shape(x.get("mesh_brep") or {})))
        except Exception:continue
    pv=max(1e-6,shape_volume(part));max_ratio=max(0.0,min(.02,f(req.get("max_collision_ratio"),.0025)));max_gap=max(.05,min(2.0,f(req.get("max_contact_gap_mm"),.8)))
    def audit(shape,strategy,translation,angle):
        parent_collision=_mechanism_collision_volume(shape,parent);parent_ratio=parent_collision/pv
        parent_distance=_mechanism_shape_distance(shape,parent)
        obstacle_rows=[];obstacle_total=0.0
        for name,o in obstacles:
            vol=_mechanism_collision_volume(shape,o);obstacle_total+=vol
            if vol>.001:obstacle_rows.append({"name":name,"collision_volume_mm3":round(vol,5)})
        bbox=shape.val().BoundingBox()
        contact_ok=parent_distance<=max_gap+1e-9
        collision_ok=parent_ratio<=max_ratio+1e-9 and obstacle_total<=max(.001,pv*max_ratio)
        score=100.0-min(70.0,parent_ratio*10000.0)-min(50.0,obstacle_total/max(pv,.001)*10000.0)-min(25.0,parent_distance*12.0)
        return {"strategy":strategy,"translation_mm":[round(f(x),4) for x in translation],"rotation_deg":round(f(angle),3),"parent_distance_mm":round(parent_distance,5),"parent_collision_volume_mm3":round(parent_collision,5),"parent_collision_ratio":round(parent_ratio,7),"obstacle_collision_volume_mm3":round(obstacle_total,5),"obstacle_collisions":obstacle_rows,"contact_ok":contact_ok,"collision_ok":collision_ok,"score":round(score,3),"bbox_mm":[round(bbox.xmin,3),round(bbox.ymin,3),round(bbox.zmin,3),round(bbox.xmax,3),round(bbox.ymax,3),round(bbox.zmax,3)]}
    candidates=[]
    if bool(req.get("prefer_source_pose",False)):
        candidates.append(audit(part,"PRESERVE_SOURCE_POSE",[0,0,0],0))
    axis=str(req.get("axis") or "").upper();pc=req.get("part_axis_center_mm") or [];qc=req.get("parent_axis_center_mm") or []
    if axis in ("X","Y","Z") and isinstance(pc,list) and isinstance(qc,list) and len(pc)==3 and len(qc)==3:
        delta=[f(qc[i])-f(pc[i]) for i in range(3)]
        axis_vec={"X":[1,0,0],"Y":[0,1,0],"Z":[0,0,1]}[axis]
        for angle in req.get("candidate_angles_deg") or [0,90,180,270]:
            try:
                shape=part
                if abs(f(angle))>1e-9:
                    shape=shape.rotate(tuple(f(x) for x in pc),tuple(f(pc[i])+axis_vec[i] for i in range(3)),f(angle))
                shape=shape.translate(tuple(delta))
                candidates.append(audit(shape,"AXIS_CENTER_ALIGNMENT",delta,angle))
            except Exception as ex:
                candidates.append({"strategy":"AXIS_CENTER_ALIGNMENT","rotation_deg":f(angle),"status":"ERROR","error":str(ex)[:180],"score":-999})
    valid=[x for x in candidates if x.get("contact_ok") is True and x.get("collision_ok") is True]
    valid.sort(key=lambda x:f(x.get("score")) ,reverse=True)
    if not valid:
        return {"status":"UNRESOLVED","version":"assembly-pose-solver-v3","reason":"no_collision_free_contact_pose","part_volume_mm3":round(pv,4),"candidates":candidates}
    best=valid[0];second=valid[1] if len(valid)>1 else None
    source_best=best.get("strategy")=="PRESERVE_SOURCE_POSE"
    unique=source_best or second is None or f(best.get("score"))-f(second.get("score"))>=5.0
    if not unique:
        return {"status":"REVIEW_REQUIRED","version":"assembly-pose-solver-v3","reason":"ambiguous_pose_candidates","part_volume_mm3":round(pv,4),"best":best,"second":second,"candidates":candidates}
    return {"status":"RESOLVED","version":"assembly-pose-solver-v3","strategy":best.get("strategy"),"assembly_translate_mm":best.get("translation_mm"),"assembly_rotation":{"axis":axis or None,"degrees":best.get("rotation_deg")},"resolved_bbox_mm":best.get("bbox_mm"),"execution_supported":best.get("strategy")=="PRESERVE_SOURCE_POSE" or abs(f(best.get("rotation_deg")))<1e-9,"validation":{"contact_ok":best.get("contact_ok"),"collision_ok":best.get("collision_ok"),"parent_distance_mm":best.get("parent_distance_mm"),"parent_collision_volume_mm3":best.get("parent_collision_volume_mm3"),"parent_collision_ratio":best.get("parent_collision_ratio"),"obstacle_collision_volume_mm3":best.get("obstacle_collision_volume_mm3")},"candidates":candidates}

def _motion_rotate(shape,axis_point,axis,angle):
    av={"X":(1,0,0),"Y":(0,1,0),"Z":(0,0,1)}.get(str(axis or "").upper())
    if av is None:raise ValueError("MECHANISM_MOTION_AXIS_INVALID")
    p=tuple(f(x) for x in axis_point);q=tuple(p[i]+av[i] for i in range(3))
    return shape.rotate(p,q,f(angle))

def _bbox_overlap_dims(a,b):
    try:
        aa=a.val().BoundingBox();bb=b.val().BoundingBox()
        return [max(0.0,min(aa.xmax,bb.xmax)-max(aa.xmin,bb.xmin)),max(0.0,min(aa.ymax,bb.ymax)-max(aa.ymin,bb.ymin)),max(0.0,min(aa.zmax,bb.zmax)-max(aa.zmin,bb.zmin))]
    except Exception:return [0.0,0.0,0.0]

def _mechanism_motion_probe(req):
    moving=_u_faceted_mesh_brep_shape(req.get("moving_mesh_brep") or {})
    static=_u_faceted_mesh_brep_shape(req.get("static_mesh_brep") or {})
    companions=[]
    for x in req.get("moving_companions") or []:
        try:companions.append({"name":str(x.get("name") or "companion"),"shape":_u_faceted_mesh_brep_shape(x.get("mesh_brep") or {}),"terminal_engagement":bool(x.get("terminal_engagement",False))})
        except Exception:continue
    axis=str(req.get("axis") or "").upper();point=req.get("axis_point_mm") or []
    if axis not in ("X","Y","Z") or not isinstance(point,list) or len(point)!=3:return {"status":"UNRESOLVED","version":"mechanism-motion-solver-v1","reason":"axis_missing"}
    step=max(5.0,min(30.0,f(req.get("step_deg"),15)));max_angle=max(step,min(190.0,f(req.get("max_angle_deg"),180)))
    directions=req.get("directions") or [1,-1]
    moving_vol=max(.001,shape_volume(moving));comp_vol={x["name"]:max(.001,shape_volume(x["shape"])) for x in companions}
    max_ratio=max(0.0,min(.01,f(req.get("max_collision_ratio"),.0005)))
    terminal_ratio=max(max_ratio,min(.05,f(req.get("max_terminal_collision_ratio"),.025)))
    terminal_gap=max(.05,min(3.0,f(req.get("terminal_contact_gap_mm"),1.2)))
    rows=[]
    for direction0 in directions:
        direction=1 if f(direction0,1)>=0 else -1
        samples=[];path_ok=True;max_collision=0.0;first_fail=None
        angles=[];a=0.0
        while a<max_angle-1e-6:angles.append(a);a+=step
        angles.append(max_angle)
        for idx,base_angle in enumerate(angles):
            angle=direction*base_angle;terminal=idx==len(angles)-1
            ms=_motion_rotate(moving,point,axis,angle)
            vol=_mechanism_collision_volume(ms,static);ratio=vol/moving_vol
            max_collision=max(max_collision,ratio)
            companion_rows=[];companion_bad=False
            for cp in companions:
                cs=_motion_rotate(cp["shape"],point,axis,angle)
                cv=_mechanism_collision_volume(cs,static);cr=cv/comp_vol[cp["name"]]
                dist=_mechanism_contact_distance(cs,static,terminal_gap) if terminal and cp["terminal_engagement"] else None
                allowed=terminal_ratio if terminal and cp["terminal_engagement"] else max_ratio
                ok=cr<=allowed+1e-9
                if not ok:companion_bad=True
                companion_rows.append({"name":cp["name"],"collision_volume_mm3":round(cv,5),"collision_ratio":round(cr,7),"distance_mm":round(dist,5) if dist is not None else None,"terminal_engagement":cp["terminal_engagement"],"ok":ok})
            main_ok=ratio<=max_ratio+1e-9
            sample_ok=main_ok and not companion_bad
            if not sample_ok and path_ok:
                path_ok=False;first_fail={"angle_deg":round(angle,3),"main_collision_ratio":round(ratio,7),"companions":companion_rows}
            if idx in (0,len(angles)-1) or not sample_ok or idx%2==0:
                samples.append({"angle_deg":round(angle,3),"main_collision_volume_mm3":round(vol,5),"main_collision_ratio":round(ratio,7),"main_ok":main_ok,"companions":companion_rows})
            _motion_memory_checkpoint(idx+1,4)
            if first_fail is not None and base_angle<max_angle-step-1e-6:
                break
        early_block=first_fail is not None and abs(f(first_fail.get("angle_deg"),0))<max_angle-step-1e-6
        if early_block:
            score=-min(25,max_collision*10000)
            rows.append({"direction":direction,"status":"FAIL","path_clear":False,"first_failure":first_fail,"max_collision_ratio":round(max_collision,7),"endpoint_distance_mm":None,"endpoint_bbox_overlap_mm":[],"terminal_engagement":[],"terminal_engagement_ok":False,"endpoint_near_static":False,"score":round(score,3),"samples":samples,"early_exit":True})
            continue
        end_angle=direction*max_angle
        end_moving=_motion_rotate(moving,point,axis,end_angle)
        end_distance=_mechanism_contact_distance(end_moving,static,terminal_gap)
        overlap=_bbox_overlap_dims(end_moving,static)
        engagement=[]
        for cp in companions:
            if not cp["terminal_engagement"]:continue
            cs=_motion_rotate(cp["shape"],point,axis,end_angle)
            cv=_mechanism_collision_volume(cs,static);cr=cv/comp_vol[cp["name"]];dist=_mechanism_contact_distance(cs,static,terminal_gap)
            engagement.append({"name":cp["name"],"distance_mm":round(dist,5),"collision_volume_mm3":round(cv,5),"collision_ratio":round(cr,7),"engaged":dist<=terminal_gap+1e-9 or (cv>0 and cr<=terminal_ratio+1e-9)})
        terminal_engagement_ok=all(x["engaged"] for x in engagement) if engagement else True
        endpoint_near=end_distance<=terminal_gap+1e-9 or sum(1 for x in overlap if x>.2)>=2
        score=(100 if path_ok else 0)+(20 if terminal_engagement_ok else 0)+(10 if endpoint_near else 0)-min(25,max_collision*10000)
        rows.append({"direction":direction,"status":"PASS" if path_ok and terminal_engagement_ok and endpoint_near else "FAIL","path_clear":path_ok,"first_failure":first_fail,"max_collision_ratio":round(max_collision,7),"endpoint_distance_mm":round(end_distance,5),"endpoint_bbox_overlap_mm":[round(x,4) for x in overlap],"terminal_engagement":engagement,"terminal_engagement_ok":terminal_engagement_ok,"endpoint_near_static":endpoint_near,"score":round(score,3),"samples":samples})
    valid=[x for x in rows if x["status"]=="PASS"]
    valid.sort(key=lambda x:x["score"],reverse=True)
    if not valid:return {"status":"FAIL","version":"mechanism-motion-solver-v1","reason":"no_collision_free_motion_direction","axis":axis,"axis_point_mm":[round(f(x),4) for x in point],"directions":rows}
    best=valid[0];second=valid[1] if len(valid)>1 else None
    unique=second is None or best["score"]-second["score"]>=5 or best["max_collision_ratio"]<second["max_collision_ratio"]*.5
    if not unique:return {"status":"REVIEW_REQUIRED","version":"mechanism-motion-solver-v1","reason":"ambiguous_motion_direction","axis":axis,"axis_point_mm":[round(f(x),4) for x in point],"best":best,"second":second,"directions":rows}
    return {"status":"PASS","version":"mechanism-motion-solver-v1","motion_type":"HINGE_ROTATION","axis":axis,"axis_point_mm":[round(f(x),4) for x in point],"open_angle_deg":0,"closed_angle_deg":best["direction"]*max_angle,"direction":best["direction"],"path_clear":best["path_clear"],"max_collision_ratio":best["max_collision_ratio"],"terminal_engagement_ok":best["terminal_engagement_ok"],"terminal_engagement":best["terminal_engagement"],"endpoint_distance_mm":best["endpoint_distance_mm"],"directions":rows}

def _mechanism_motion_probe_v2(req):
    base=_mechanism_motion_probe(req)
    base["version"]="mechanism-motion-solver-v2"
    if base.get("status")=="PASS":
        base["hinge_status"]="PASS";base["latch_status"]="PASS" if base.get("terminal_engagement_ok") else "NOT_REQUIRED"
        return base
    dirs=base.get("directions") or [];max_angle=max(5.0,min(190.0,f(req.get("max_angle_deg"),180)));step=max(5.0,min(30.0,f(req.get("step_deg"),15)))
    terminal_candidates=[]
    for row in dirs:
        ff=row.get("first_failure") or {};fa=abs(f(ff.get("angle_deg"),0))
        prior=[s for s in row.get("samples") or [] if abs(f(s.get("angle_deg"),0))<fa-1e-6]
        prior_clear=all(s.get("main_ok") is True and all(c.get("ok") is True for c in (s.get("companions") or [])) for s in prior)
        if prior_clear and fa>=max_angle-step-1e-6:
            terminal_candidates.append(row)
    if not terminal_candidates:
        base["hinge_status"]="FAIL";base["latch_status"]="UNVERIFIED";return base
    candidate=sorted(terminal_candidates,key=lambda x:f(x.get("max_collision_ratio"),999))[0];direction=1 if f(candidate.get("direction"),1)>=0 else -1
    moving=_u_faceted_mesh_brep_shape(req.get("moving_mesh_brep") or {});static=_u_faceted_mesh_brep_shape(req.get("static_mesh_brep") or {})
    companions=[]
    for x in req.get("moving_companions") or []:
        try:companions.append({"name":str(x.get("name") or "companion"),"shape":_u_faceted_mesh_brep_shape(x.get("mesh_brep") or {}),"terminal_engagement":bool(x.get("terminal_engagement",False))})
        except Exception:continue
    axis=str(req.get("axis") or "").upper();point=req.get("axis_point_mm") or [];moving_vol=max(.001,shape_volume(moving));comp_vol={x["name"]:max(.001,shape_volume(x["shape"])) for x in companions}
    max_ratio=max(0.0,min(.01,f(req.get("max_collision_ratio"),.0005)));terminal_ratio=max(max_ratio,min(.05,f(req.get("max_terminal_collision_ratio"),.025)));terminal_gap=max(.05,min(5.0,f(req.get("terminal_contact_gap_mm"),1.5)))
    start=max(0.0,max_angle-step);refined=[];last_clear=None;first_stop=None;best_engagement=None;refine_iter=0
    a=start
    while a<=max_angle+1e-6:
        angle=direction*a;ms=_motion_rotate(moving,point,axis,angle);mv=_mechanism_collision_volume(ms,static);mr=mv/moving_vol;main_clear=mr<=max_ratio+1e-9
        comps=[]
        for cp in companions:
            cs=_motion_rotate(cp["shape"],point,axis,angle);cv=_mechanism_collision_volume(cs,static);cr=cv/comp_vol[cp["name"]]
            dist=_mechanism_contact_distance(cs,static,terminal_gap) if cp["terminal_engagement"] else None
            engaged=bool(cp["terminal_engagement"] and (dist<=terminal_gap+1e-9 or (cv>0 and cr<=terminal_ratio+1e-9)))
            row={"name":cp["name"],"distance_mm":round(dist,5) if dist is not None else None,"collision_volume_mm3":round(cv,5),"collision_ratio":round(cr,7),"terminal_engagement":cp["terminal_engagement"],"engaged":engaged}
            comps.append(row)
            if engaged and (best_engagement is None or f(row.get("distance_mm"),999)<f(best_engagement.get("distance_mm"),999)):best_engagement={**row,"angle_deg":round(angle,3)}
        refined.append({"angle_deg":round(angle,3),"main_collision_volume_mm3":round(mv,5),"main_collision_ratio":round(mr,7),"main_clear":main_clear,"companions":comps})
        if main_clear:last_clear=angle
        elif first_stop is None:first_stop=angle
        refine_iter+=1;_motion_memory_checkpoint(refine_iter,4)
        a+=1.0
    hinge_pass=last_clear is not None and abs(last_clear)>=start-1e-6 and (first_stop is not None or abs(last_clear)>=max_angle-1e-6)
    latch_parts=[x for x in companions if x["terminal_engagement"]];latch_pass=(best_engagement is not None) if latch_parts else True
    base.update({"motion_type":"HINGE_ROTATION","direction":direction,"hinge_status":"PASS" if hinge_pass else "FAIL","mechanical_stop_angle_deg":round(first_stop,3) if first_stop is not None else None,"last_collision_free_angle_deg":round(last_clear,3) if last_clear is not None else None,"terminal_refinement":refined,"best_terminal_engagement":best_engagement,"latch_status":"PASS" if latch_pass else ("PENDING" if latch_parts else "NOT_REQUIRED")})
    if hinge_pass and latch_pass:
        base.update({"status":"PASS","reason":None,"closed_angle_deg":round((best_engagement or {}).get("angle_deg",last_clear),3),"path_clear":True,"terminal_engagement_ok":True})
    elif hinge_pass:
        base.update({"status":"PARTIAL","reason":"hinge_motion_valid_latch_engagement_pending","closed_angle_deg":round(last_clear,3),"path_clear":True,"terminal_engagement_ok":False})
    else:
        base.update({"status":"FAIL","reason":"hinge_terminal_refinement_failed"})
    return base

def _nested_motion_shape(shape,relative_pivot,relative_angle,main_point,main_axis,main_angle):
    out=shape
    rp=relative_pivot or {};raxis=str(rp.get("axis") or "").upper();rcenter=rp.get("center_3d_mm") or []
    if raxis in ("X","Y","Z") and isinstance(rcenter,list) and len(rcenter)==3 and abs(f(relative_angle))>1e-9:
        out=_motion_rotate(out,rcenter,raxis,relative_angle)
    return _motion_rotate(out,main_point,main_axis,main_angle)

def _mechanism_motion_probe_v3(req):
    base=_mechanism_motion_probe_v2(req)
    base["version"]="mechanism-motion-solver-v3"
    if base.get("status")=="PASS":
        return base
    if base.get("hinge_status")!="PASS" or base.get("latch_status") not in ("PENDING","FAIL"):
        return base
    main_axis=str(req.get("axis") or "").upper();main_point=req.get("axis_point_mm") or []
    closed=f(base.get("last_collision_free_angle_deg"),f(base.get("closed_angle_deg"),0))
    if main_axis not in ("X","Y","Z") or len(main_point)!=3 or abs(closed)<1e-6:return base
    moving=_u_faceted_mesh_brep_shape(req.get("moving_mesh_brep") or {})
    static=_u_faceted_mesh_brep_shape(req.get("static_mesh_brep") or {})
    lid_closed=_motion_rotate(moving,main_point,main_axis,closed)
    latch_rows=[]
    for x in req.get("moving_companions") or []:
        if not bool(x.get("terminal_engagement",False)):continue
        rp=x.get("relative_pivot") or {}
        if str(rp.get("axis") or "").upper() not in ("X","Y","Z") or not isinstance(rp.get("center_3d_mm"),list) or len(rp.get("center_3d_mm"))!=3:continue
        try:latch_rows.append({"name":str(x.get("name") or "latch"),"shape":_u_faceted_mesh_brep_shape(x.get("mesh_brep") or {}),"relative_pivot":rp})
        except Exception:continue
    if not latch_rows:
        base["latch_status"]="PENDING";base["latch_reason"]="latch_pivot_missing";return base
    latch=latch_rows[0];lv=max(.001,shape_volume(latch["shape"]))
    max_lid_ratio=max(.0005,min(.02,f(req.get("latch_parent_max_collision_ratio"),.004)))
    max_body_ratio=max(.001,min(.05,f(req.get("max_terminal_collision_ratio"),.03)))
    contact_gap=max(.05,min(3.0,f(req.get("terminal_contact_gap_mm"),1.5)))
    search_min=max(-160.0,min(-5.0,f(req.get("latch_angle_min_deg"),-100)))
    search_max=min(160.0,max(5.0,f(req.get("latch_angle_max_deg"),100)))
    search_step=max(2.0,min(15.0,f(req.get("latch_angle_step_deg"),5)))
    candidates=[];angle=search_min;motion_iter=0
    while angle<=search_max+1e-6:
        ls=_nested_motion_shape(latch["shape"],latch["relative_pivot"],angle,main_point,main_axis,closed)
        lid_cv=_mechanism_collision_volume(ls,lid_closed);lid_cr=lid_cv/lv;lid_dist=_mechanism_contact_distance(ls,lid_closed,1.0)
        body_cv=_mechanism_collision_volume(ls,static);body_cr=body_cv/lv;body_dist=_mechanism_contact_distance(ls,static,contact_gap)
        pivot_retained=(lid_dist<=1.0+1e-9) or (lid_cv>0 and lid_cr<=max_lid_ratio+1e-9)
        body_engaged=(body_dist<=contact_gap+1e-9) or (body_cv>0 and body_cr<=max_body_ratio+1e-9)
        collision_ok=lid_cr<=max_lid_ratio+1e-9 and body_cr<=max_body_ratio+1e-9
        score=(100 if body_engaged else 0)+(30 if pivot_retained else 0)+(20 if collision_ok else 0)-min(40,lid_cr*3000)-min(50,body_cr*1200)-min(20,body_dist*2)
        candidates.append({"relative_angle_deg":round(angle,3),"lid_distance_mm":round(lid_dist,5),"lid_collision_volume_mm3":round(lid_cv,5),"lid_collision_ratio":round(lid_cr,7),"body_distance_mm":round(body_dist,5),"body_collision_volume_mm3":round(body_cv,5),"body_collision_ratio":round(body_cr,7),"pivot_retained":pivot_retained,"body_engaged":body_engaged,"collision_ok":collision_ok,"score":round(score,3)})
        motion_iter+=1;_motion_memory_checkpoint(motion_iter,4)
        angle+=search_step
    valid=[x for x in candidates if x["pivot_retained"] and x["body_engaged"] and x["collision_ok"]]
    valid.sort(key=lambda x:x["score"],reverse=True)
    if not valid:
        base["latch_status"]="PENDING";base["latch_reason"]="no_rigid_engagement_pose";base["latch_engagement_candidates"]=candidates;return base
    coarse=valid[0];fine=[];a=max(search_min,coarse["relative_angle_deg"]-search_step)
    hi=min(search_max,coarse["relative_angle_deg"]+search_step)
    while a<=hi+1e-6:
        ls=_nested_motion_shape(latch["shape"],latch["relative_pivot"],a,main_point,main_axis,closed)
        lid_cv=_mechanism_collision_volume(ls,lid_closed);lid_cr=lid_cv/lv;lid_dist=_mechanism_contact_distance(ls,lid_closed,1.0)
        body_cv=_mechanism_collision_volume(ls,static);body_cr=body_cv/lv;body_dist=_mechanism_contact_distance(ls,static,contact_gap)
        pivot_retained=(lid_dist<=1.0+1e-9) or (lid_cv>0 and lid_cr<=max_lid_ratio+1e-9)
        body_engaged=(body_dist<=contact_gap+1e-9) or (body_cv>0 and body_cr<=max_body_ratio+1e-9)
        collision_ok=lid_cr<=max_lid_ratio+1e-9 and body_cr<=max_body_ratio+1e-9
        score=(100 if body_engaged else 0)+(30 if pivot_retained else 0)+(20 if collision_ok else 0)-min(40,lid_cr*3000)-min(50,body_cr*1200)-min(20,body_dist*2)
        fine.append({"relative_angle_deg":round(a,3),"lid_distance_mm":round(lid_dist,5),"lid_collision_volume_mm3":round(lid_cv,5),"lid_collision_ratio":round(lid_cr,7),"body_distance_mm":round(body_dist,5),"body_collision_volume_mm3":round(body_cv,5),"body_collision_ratio":round(body_cr,7),"pivot_retained":pivot_retained,"body_engaged":body_engaged,"collision_ok":collision_ok,"score":round(score,3)})
        motion_iter+=1;_motion_memory_checkpoint(motion_iter,4)
        a+=1.0
    fine_valid=[x for x in fine if x["pivot_retained"] and x["body_engaged"] and x["collision_ok"]]
    fine_valid.sort(key=lambda x:x["score"],reverse=True)
    best=fine_valid[0] if fine_valid else coarse
    base.update({"status":"PASS","reason":None,"motion_type":"HINGE_ROTATION_WITH_LATCH","hinge_status":"PASS","latch_status":"PASS","latch_relative_axis":latch["relative_pivot"].get("axis"),"latch_relative_axis_point_mm":latch["relative_pivot"].get("center_3d_mm"),"latch_relative_angle_deg":best["relative_angle_deg"],"latch_engagement":best,"latch_engagement_candidates":candidates,"latch_engagement_refinement":fine,"terminal_engagement_ok":True,"closed_angle_deg":closed,"path_clear":True})
    return base

def _motion_job_runner(jid,req):
    folder=ROOT/jid;folder.mkdir(parents=True,exist_ok=True)
    request_path=folder/"motion_request.json";result_path=folder/"motion_result.json";log_path=folder/"motion_child.log"
    started=time.time()
    with HEAVY_JOB_SEMAPHORE:
        try:
            request_path.write_text(json.dumps(req,ensure_ascii=False),encoding="utf-8")
            MOTION_JOBS[jid].update({"status":"processing","stage":"isolated_starting","execution_mode":"isolated_subprocess","started_at":started,"updated_at":time.time()})
            with log_path.open("wb") as log:
                proc=subprocess.Popen([sys.executable,str(pathlib.Path(__file__).resolve()),"--motion-child",jid,str(folder)],stdout=log,stderr=subprocess.STDOUT,env=os.environ.copy())
                MOTION_JOBS[jid].update({"stage":"isolated_running","child_pid":int(proc.pid),"updated_at":time.time()})
                timeout=max(60,min(600,int(f(req.get("motion_timeout_seconds"),240))))
                try:rc=proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill();proc.wait(timeout=10)
                    raise TimeoutError("MECHANISM_MOTION_CHILD_TIMEOUT:"+str(timeout))
            if not result_path.exists():
                raise RuntimeError("MECHANISM_MOTION_CHILD_NO_RESULT:exit="+str(rc)+":"+_job_log_tail(log_path,2500))
            child=json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(child,dict) or child.get("status")!="completed" or not isinstance(child.get("result"),dict):
                raise RuntimeError("MECHANISM_MOTION_CHILD_INVALID_RESULT:"+str(child)[:800])
            MOTION_JOBS[jid].update({"status":"completed","stage":"completed","result":child["result"],"error":None,"execution_mode":"isolated_subprocess","child_exit_code":int(rc),"duration_ms":int((time.time()-started)*1000),"completed_at":time.time(),"updated_at":time.time()})
        except Exception as ex:
            MOTION_JOBS[jid].update({"status":"failed","stage":"failed","error":str(ex)[:1200],"execution_mode":"isolated_subprocess","duration_ms":int((time.time()-started)*1000),"child_log_tail":_job_log_tail(log_path,2500),"completed_at":time.time(),"updated_at":time.time()})
        finally:
            gc.collect();_malloc_trim()

def _motion_child_cli(jid,folder):
    folder=pathlib.Path(folder);request_path=folder/"motion_request.json";result_path=folder/"motion_result.json"
    req=json.loads(request_path.read_text(encoding="utf-8"))
    result=_mechanism_motion_probe_v3(req)
    out={"status":"completed","result":result,"completed_at":time.time()}
    tmp=result_path.with_suffix(".tmp");tmp.write_text(json.dumps(out,ensure_ascii=False),encoding="utf-8");tmp.replace(result_path)
    return 0

def _submit_motion_job(req):
    jid="motion-"+str(uuid.uuid4())
    MOTION_JOBS[jid]={"status":"queued","stage":"queued","result":None,"error":None,"execution_mode":"isolated_subprocess","created_at":time.time(),"updated_at":time.time()}
    t=threading.Thread(target=_motion_job_runner,args=(jid,req),daemon=True,name="makersence-motion-"+jid[-8:]);t.start()
    return {"job_id":jid,"status":"queued","execution_mode":"isolated_subprocess"}

def _dw_drawing(z,part,world=None):
    if not part:return {'version':'cad-drawing-v1','status':'unavailable','reason':'no_part'}
    mesh=_dw_extract_mesh(z,part,world)
    if not mesh['vertices'] or not mesh['triangles']:return {'version':'cad-drawing-v1','status':'unavailable','reason':'no_mesh'}
    b=_dw_bbox(mesh['vertices']);openings=_dw_attach_face_evidence(mesh,_dw_openings(mesh));thickness_candidates=_dw_planar_spacing(mesh);segs,treatments,edge_status=_dw_edges(mesh);reconstruction=_dw_reconstruction(mesh,b);planar_prismatic=_dw_planar_prismatic(mesh,b);mesh_brep=_dw_mesh_brep_evidence(mesh,reconstruction);dims=[]
    for i,axis in enumerate('XYZ'):dims.append({'id':'OVERALL_'+axis,'type':'linear','axis':axis,'value_mm':round(b['dimensions'][i],3),'label':str(round(b['dimensions'][i],2)),'priority':'REQUIRED','quality':'MEASURED'})
    for i,h in enumerate(openings,1):
        view={'X':'right','Y':'front','Z':'top'}[h['axis']]
        if h['kind']=='circular_hole':dims.append({'id':'HOLE_'+str(i),'type':'diameter','feature':'hole','axis':h['axis'],'view':view,'value_mm':h['equivalent_diameter_mm'],'label':'Ø'+format(h['equivalent_diameter_mm'],'.2f'),'center_2d_mm':h['center_2d_mm'],'priority':'REQUIRED','confidence':h['confidence'],'quality':'CALCULATED'})
        elif h['kind']=='blind_circular_pocket':
            dims.append({'id':'POCKET_'+str(i),'type':'diameter','feature':'blind_circular_pocket','axis':h['axis'],'view':view,'value_mm':h['equivalent_diameter_mm'],'label':'Ø'+format(h['equivalent_diameter_mm'],'.2f'),'center_2d_mm':h['center_2d_mm'],'priority':'REQUIRED_IF_CONFIRMED','confidence':h['confidence'],'quality':'CALCULATED'})
            if h.get('observed_depth_mm') is not None:dims.append({'id':'POCKET_DEPTH_'+str(i),'type':'depth','feature':'blind_circular_pocket','axis':h['axis'],'view':view,'value_mm':h.get('observed_depth_mm'),'label':'depth≈'+format(h.get('observed_depth_mm'),'.2f'),'center_2d_mm':h['center_2d_mm'],'priority':'REVIEW','confidence':'MEDIUM','quality':'ESTIMATED_FROM_SECTIONS'})
        else:dims.append({'id':'OPENING_'+str(i),'type':'opening','feature':h['kind'],'axis':h['axis'],'view':view,'size_mm':h['opening_size_mm'],'label':'OPENING '+' × '.join(format(v,'.2f') for v in h['opening_size_mm']),'center_2d_mm':h['center_2d_mm'],'priority':'REQUIRED','confidence':h['confidence'],'quality':h.get('measurement_quality','CALCULATED')})
    for i,x in enumerate(treatments,1):dims.append({'id':('R_' if x['kind']=='fillet' else 'C_')+str(i),'type':x['kind'],'feature':x['kind'],'center_mm':x['center_mm'],'value_mm':x.get('radius_mm') or x.get('size_mm'),'label':x['callout'],'priority':'REQUIRED_IF_CONFIRMED','confidence':x['confidence'],'quality':x['measurement_quality']})
    sections=[]
    for i,h in enumerate(sorted(openings,key=lambda x:0 if x.get('confidence')=='HIGH' else 1)[:2]):
        L=chr(65+i);c=h.get('center_2d_mm') or [0,0]
        if h['axis']=='Z':cut,at,src='Y',c[1],'top'
        elif h['axis']=='Y':cut,at,src='Z',c[1],'front'
        else:cut,at,src='Z',c[1],'right'
        sections.append({'id':'SECTION_'+L+L,'label':'SECTION '+L+'-'+L,'cut_axis':cut,'at_mm':round(at,2),'through_feature':h['kind'],'feature_axis':h['axis'],'source_view':src,'reason':'�����ռ�/�}�f�L�k�u�a�~�[���ϧ�����F','priority':'AUTO_REQUIRED','confidence':h.get('confidence','MEDIUM')})
    if not sections:
        ai=b['dimensions'].index(min(b['dimensions']));sections=[{'id':'SECTION_AA','label':'SECTION A-A','cut_axis':'XYZ'[ai],'at_mm':round((b['min'][ai]+b['max'][ai])/2,2),'through_feature':'body_midplane','source_view':'top' if ai==2 else 'front','reason':'�۰ʤ��孱�A�Ω�T�{�p�׻P�����h��','priority':'AUTO_RECOMMENDED','confidence':'MEDIUM'}]
    for s in sections:
        ai='XYZ'.index(s['cut_axis']);s['profile_loops']=[[ [round(q[0],3),round(q[1],3)] for q in loop[:500] ] for loop in _dw_loops(_dw_slice(mesh,ai,float(s.get('at_mm') or 0)))[:32]];s['view_axes']=['Y','Z'] if ai==0 else (['X','Z'] if ai==1 else ['X','Y'])
    return {'version':'cad-drawing-v1','status':'ready','source_geometry':'3MF_MESH_REVERSE_ENGINEERING','primary_part':mesh['name'],'bounds_mm':{'min':[round(x,3) for x in b['min']],'max':[round(x,3) for x in b['max']],'dimensions':[round(x,3) for x in b['dimensions']]},'views':[_dw_view(mesh,v,segs) for v in ('front','top','right')],'sections':sections,'reconstruction':reconstruction,'planar_prismatic_evidence':planar_prismatic,'mesh_brep_evidence':mesh_brep,'details':[],'dimensions':dims,'features':{'holes':[x for x in openings if x['kind']=='circular_hole'],'slots':[x for x in openings if x['kind']=='slot_or_elongated_hole'],'other_openings':[x for x in openings if x['kind'] not in ('circular_hole','slot_or_elongated_hole')],'edge_treatments':treatments,'thickness_candidates':thickness_candidates},'drawing_rules':{'standard':'ISO-like mechanical drawing','dimension_strategy':'minimal_complete_non_redundant','automatic_centerlines':True,'automatic_section_selection':True,'automatic_detail_views':True,'units':'mm'},'edge_analysis_status':edge_status,'confidence_note':'3MF �O�T������A���t��l CAD Feature Tree�C�~�Τؤo������F�ռѥѦh�I���X��p��F�ꨤ/�˨��Ѻ���k�V�P��t�X��ϱ�����ܫH�ߵ��šC�Y�ɥR STEP/B-Rep�A���H B-Rep �@����T�S�x�ӷ��C'}

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
                wb=btx(o["bbox"],world);dims0=[max(0,wb[i+3]-wb[i]) for i in range(3)];center0=[(wb[i]+wb[i+3])/2 for i in range(3)]
                parts.append({"name":o.get("name") or ("Object "+str(o.get("id") or len(parts)+1)),"role":"unknown_part","dimensions_mm":[round(x,3) for x in dims0],"world_bounds_mm":{"min":[round(wb[0],3),round(wb[1],3),round(wb[2],3)],"max":[round(wb[3],3),round(wb[4],3),round(wb[5],3)],"dimensions":[round(x,3) for x in dims0]},"world_center_mm":[round(x,3) for x in center0],"world_transform":[[round(f(v),6) for v in row] for row in world],"vertex_count":o["ov"],"triangle_count":o["ot"],"source_object_id":o["id"],"source_model_path":key[0],"root_build_index":root_index,"plate_index":None,"extruder":None,"subtype":None,"bambu_editable_part":True,"physical_detachable":None,"physical_detachability_status":"UNKNOWN","essential":True})
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
        # Persist compact per-part reverse-engineering evidence only when the
        # assembly is small enough for safe bounded analysis. This avoids
        # exploding JSON/memory for decorative 20+ object color stacks while
        # enabling true generic multipart reconstruction for normal assemblies.
        total_part_triangles=sum(int(p0.get("triangle_count") or 0) for p0 in parts)
        per_part_budget_ok=len(parts)<=12 and total_part_triangles<=320000
        per_part_evidence_count=0
        if per_part_budget_ok:
            for p0 in parts:
                try:
                    d0=_dw_drawing(z,p0,p0.get("world_transform"))
                    views0=[]
                    for v0 in d0.get("views") or []:
                        if v0.get("outline"):
                            views0.append({"id":v0.get("id"),"outline":v0.get("outline")[:220]})
                    p0["geometry_evidence"]={"status":d0.get("status"),"bounds_mm":d0.get("bounds_mm"),"reconstruction":d0.get("reconstruction"),"planar_prismatic":d0.get("planar_prismatic_evidence"),"mesh_brep":d0.get("mesh_brep_evidence"),"views":views0,"features":d0.get("features") or {}}
                    if d0.get("status")=="ready":per_part_evidence_count+=1
                except Exception as ex:
                    p0["geometry_evidence"]={"status":"unavailable","reason":str(ex)[:240]}
        # Lightweight feature scan is intentionally separate from full per-part reconstruction.
        # It can safely inspect larger multipart/color-stack projects and only emits bounded
        # opening/thickness evidence. This prevents "largest part only" from hiding a real
        # through-hole or blind cavity on another functional part.
        feature_openings=[];feature_thickness=[];feature_scan_count=0
        feature_ranked=sorted(
            [p0 for p0 in parts if int(p0.get("triangle_count") or 0)<=160000 and all(float(x)>=0 for x in (p0.get("dimensions_mm") or [0,0,0]))],
            key=lambda p0:max(.001,float((p0.get("dimensions_mm") or [0,0,0])[0]))*max(.001,float((p0.get("dimensions_mm") or [0,0,0])[1]))*max(.001,float((p0.get("dimensions_mm") or [0,0,0])[2])),
            reverse=True
        )[:18]
        for p0 in feature_ranked:
            try:
                f0=((p0.get("geometry_evidence") or {}).get("features") or {})
                opens=(f0.get("holes") or [])+(f0.get("slots") or [])+(f0.get("other_openings") or [])
                thick=f0.get("thickness_candidates") or []
                if not opens and not thick:
                    mesh0=_dw_extract_mesh(z,p0,p0.get("world_transform"))
                    opens=_dw_attach_face_evidence(mesh0,_dw_openings(mesh0));thick=_dw_planar_spacing(mesh0)
                for x in opens:
                    axis=str(x.get("axis") or "").upper();c2=x.get("center_2d_mm") or [];wb=p0.get("world_bounds_mm") or {};mn=wb.get("min") or [];mx=wb.get("max") or [];c3=None
                    if axis in ("X","Y","Z") and len(c2)==2 and len(mn)==3 and len(mx)==3:
                        mid=[(f(mn[i])+f(mx[i]))/2 for i in range(3)]
                        c3=[mid[0],f(c2[0]),f(c2[1])] if axis=="X" else ([f(c2[0]),mid[1],f(c2[1])] if axis=="Y" else [f(c2[0]),f(c2[1]),mid[2]])
                        c3=[round(v,3) for v in c3]
                    feature_openings.append({**x,"part_id":str(p0.get("source_object_id") or ""),"part_name":p0.get("name"),"part_dimensions_mm":p0.get("dimensions_mm"),"part_world_bounds_mm":p0.get("world_bounds_mm"),"center_3d_mm":c3})
                for x in thick:
                    feature_thickness.append({**x,"part_id":str(p0.get("source_object_id") or ""),"part_name":p0.get("name"),"part_dimensions_mm":p0.get("dimensions_mm"),"name":x.get("name") or "paired planar surfaces","value_mm":x.get("spacing_mm")})
                p0["feature_evidence"]={"status":"analyzed","opening_count":len(opens),"thickness_count":len(thick)}
                feature_scan_count+=1
            except Exception as ex:
                p0["feature_evidence"]={"status":"unavailable","reason":str(ex)[:180]}
        spatial_contacts=[]
        if len(parts)<=12:
            for ia in range(len(parts)):
                for ib in range(ia+1,len(parts)):
                    if parts[ia].get("root_build_index")!=parts[ib].get("root_build_index"):continue
                    a0=parts[ia].get("world_bounds_mm") or {};b0=parts[ib].get("world_bounds_mm") or {};amin=a0.get("min") or [];amax=a0.get("max") or [];bmin=b0.get("min") or [];bmax=b0.get("max") or []
                    if len(amin)!=3 or len(amax)!=3 or len(bmin)!=3 or len(bmax)!=3:continue
                    gaps=[max(0.0,f(bmin[i])-f(amax[i]),f(amin[i])-f(bmax[i])) for i in range(3)]
                    overlaps=[min(f(amax[i]),f(bmax[i]))-max(f(amin[i]),f(bmin[i])) for i in range(3)]
                    gap=math.sqrt(sum(x*x for x in gaps))
                    if gap<=1.5:
                        spatial_contacts.append({"part_a":parts[ia].get("name"),"part_b":parts[ib].get("name"),"relation":"overlap_or_contact" if all(x>=0 for x in overlaps) else "near_contact","gap_mm":round(gap,3),"overlap_mm":[round(max(0,x),3) for x in overlaps],"confidence":"HIGH" if gap<=.35 else "MEDIUM","measurement_quality":"CALCULATED_WORLD_BOUNDS"})
        largest=max(parts,key=lambda x:max(.001,x["dimensions_mm"][0])*max(.001,x["dimensions_mm"][1])*max(.001,x["dimensions_mm"][2])) if parts else None
        dims=list(largest["dimensions_mm"]) if largest else list(layout_dims)
        cad_drawing=_dw_drawing(z,largest,part_world.get((largest.get("source_model_path"),largest.get("source_object_id")))) if largest else {"version":"cad-drawing-v1","status":"unavailable","reason":"no_part"}
        part_max_dims=[max([p0["dimensions_mm"][i] for p0 in parts] or [0]) for i in range(3)]
        meta=[n for n in names if n.lower().startswith("metadata/")]
        bambu_hints=_bambu_metadata_hints(z,names)
        assembly_pose=_assembly_pose_from_source(parts,bambu_hints,spatial_contacts)
        plate_ids=set()
        for n0 in meta:
            m0=re.search(r"plate_(\d+)(?:\.|_)",n0,re.I)
            if m0:plate_ids.add(int(m0.group(1)))
        fits=all(all(float(v)<=180.0001 for v in p0["dimensions_mm"]) for p0 in parts)
        detachable=[x for x in parts if x.get("physical_detachable") is True]
        source_preview={"status":"UNAVAILABLE"}
        try:
            preview_path=folder/"source_preview.glb"
            preview_meta=write_source_mesh_glb(z,parts,preview_path)
            source_preview={**preview_meta,"url":"/v1/artifacts/"+folder.name+"/source_preview.glb","name":"source_preview.glb"}
        except Exception as ex:
            source_preview={"status":"UNAVAILABLE","error":str(ex)[:240]}
        assembly={"part_count":len(parts),"bambu_editable_part_count":len(parts),"detachable_part_count":len(detachable),"detachable_parts":[{"name":x["name"],"dimensions_mm":x["dimensions_mm"]} for x in detachable],"roles":{"unknown_part":len(parts)},"multipart":len(parts)>1,"build_root_count":len(build),"plate_count":len(plate_ids),"build_layout_dimensions_mm":[round(x,3) for x in layout_dims],"print_layout_dimensions_mm":[round(x,3) for x in layout_dims],"primary_assembly_dimensions_mm":assembly_pose.get("primary_assembly_dimensions_mm") or [round(x,3) for x in dims],"assembly_pose":assembly_pose,"largest_part_dimensions_mm":[round(x,3) for x in dims],"part_max_dimensions_mm":[round(x,3) for x in part_max_dims],"contact_graph":spatial_contacts,"preservation_policy":"preserve_editable_parts" if len(parts)>1 else "single_part","source_structure":"transform_aware_streaming_build_graph","per_part_reconstruction_budget_ok":per_part_budget_ok,"per_part_reconstruction_evidence_count":per_part_evidence_count,"per_part_reconstruction_coverage":round(per_part_evidence_count/max(1,len(parts)),3),"feature_scan_count":feature_scan_count,"feature_scan_limit":18}
        return {"format":"3mf","file_size_bytes":total,"model_entry":main,"unit":"millimeter","object_count":objects,"package_model_count":len(models),"build_item_count":len(build),"vertex_count":vc,"triangle_count":tc,"bounds_mm":{"min":[0,0,0],"max":[round(x,3) for x in dims],"dimensions":[round(x,3) for x in dims],"semantics":"largest_printable_part"},"build_layout_bounds_mm":{"min":[round(total_bbox[0],3),round(total_bbox[1],3),round(total_bbox[2],3)],"max":[round(total_bbox[3],3),round(total_bbox[4],3),round(total_bbox[5],3)],"dimensions":[round(x,3) for x in layout_dims]},"engineering_features":{"summary":{"part_count":len(parts),"analysis_mode":"remote_worker_transform_aware_streaming+drawing_v2_feature_locator","feature_scan_count":feature_scan_count,"opening_count":len(feature_openings),"thickness_count":len(feature_thickness),"face_evidence_count":sum(1 for x in feature_openings if (x.get('face_evidence') or {}).get('status') in ('RESOLVED','PARTIAL'))},"opening_candidates":feature_openings,"thickness_candidates":feature_thickness,"edge_treatments":((cad_drawing.get("features") or {}).get("edge_treatments") or []),"contact_graph":spatial_contacts},"cad_drawing":cad_drawing,"source_preview":source_preview,"part_inventory":parts,"assembly_analysis":assembly,"a1_mini_fit":fits,"fit_margin_mm":[round(180-x,3) for x in part_max_dims],"metadata_entries":meta[:100],"bambu_project":{"detected":bool(meta),"plate_count":assembly["plate_count"],"project_settings":{},"metadata_hints":bambu_hints,"raw_entries":meta[:80]},"measurement_quality":"MEASURED_REMOTE_WORKER_TRANSFORM_AWARE","caveats":["�h���� 3MF �� bounds_mm �N���̤j�i�C�L���A���⤣�P�C�L��m���s�󶡶Z�~�����ӫ~�ؤo�Fbuild_layout_bounds_mm �t�O�d����C�L�����C","�Y�@�̤�r���Ѳո˫�к٤ؤo�AProduct Intent ���u���θӤؤo�y�z�ӫ~����C"],"download_filename":filename}

def _mate_loop_box(loop):
    pts=[q for q in (loop or []) if isinstance(q,(list,tuple)) and len(q)>=2]
    if len(pts)<3:return None
    xs=[f(q[0]) for q in pts];ys=[f(q[1]) for q in pts]
    mn=[min(xs),min(ys)];mx=[max(xs),max(ys)]
    d=[mx[0]-mn[0],mx[1]-mn[1]]
    return {"min":mn,"max":mx,"dimensions_mm":d,"center_mm":[(mn[0]+mx[0])/2,(mn[1]+mx[1])/2],"bbox_area":max(0,d[0])*max(0,d[1])}

def _mate_planar_profile(part):
    pe=((part.get("geometry_evidence") or {}).get("planar_prismatic") or {})
    if str(pe.get("status") or "").lower()!="ready":return None
    axis=str(pe.get("axis") or "").upper()
    if axis not in ("X","Y","Z"):return None
    boxes=[]
    for loop in pe.get("profile_loops") or []:
        b=_mate_loop_box(loop)
        if b:boxes.append(b)
    if not boxes:return None
    outer=max(boxes,key=lambda x:x["bbox_area"])
    inners=[x for x in boxes if x is not outer]
    return {"axis":axis,"axis_min_mm":f(pe.get("axis_min_mm")),"axis_max_mm":f(pe.get("axis_max_mm")),"thickness_mm":f(pe.get("thickness_mm")),"outer":outer,"inners":inners}

def _resolve_separate_planar_mates(parts,primary):
    pp=_mate_planar_profile(primary)
    if not pp or not pp.get("inners"):return []
    ai="XYZ".index(pp["axis"]);plane_axes=[i for i in range(3) if i!=ai]
    pb=primary.get("world_bounds_mm") or {};pmn=pb.get("min") or [];pmx=pb.get("max") or []
    if len(pmn)!=3 or len(pmx)!=3:return []
    attached_parts=[p for p in parts if p.get("source_pose_class") in ("SURFACE_ATTACHED","SOURCE_ATTACHED_PART")]
    pos_count=0;neg_count=0
    for p in attached_parts:
        b=p.get("world_bounds_mm") or {};mn=b.get("min") or [];mx=b.get("max") or []
        if len(mn)!=3 or len(mx)!=3:continue
        if f(mn[ai])>=f(pmx[ai])-.45:pos_count+=1
        if f(mx[ai])<=f(pmn[ai])+.45:neg_count+=1
    resolved=[]
    visual_rx=re.compile(r"(frame|bezel|trim|photo|display|graphic|decor|ornament|label)",re.I)
    for p in parts:
        if p.get("source_pose_class")!="SEPARATE_PRINT_PART":continue
        sp=_mate_planar_profile(p)
        if not sp or sp["axis"]!=pp["axis"]:continue
        sem=str(p.get("semantic_name") or p.get("name") or "")
        candidates=[]
        for ti,target in enumerate(pp["inners"]):
            td=target["dimensions_mm"];oc=sp["outer"]["dimensions_mm"]
            for si,inner in enumerate(sp["inners"] or []):
                idm=inner["dimensions_mm"]
                diff=[idm[k]-td[k] for k in range(2)]
                border=[(oc[k]-td[k])/2 for k in range(2)]
                if min(diff)>=-.25 and max(diff)<=6.0 and min(border)>=.45 and max(border)<=8.0:
                    score=100.0-sum(abs(diff[k]-2.0)*4.0 for k in range(2))-sum(abs(border[k]-2.0)*1.5 for k in range(2))
                    if visual_rx.search(sem):score+=8
                    candidates.append({"strategy":"OVERLAY_AROUND_OPENING","target_loop_index":ti,"part_inner_loop_index":si,"target":target,"score":score,"fit_margin_mm":[round(diff[k]/2,3) for k in range(2)],"border_mm":[round(border[k],3) for k in range(2)]})
            clearance=[td[k]-oc[k] for k in range(2)]
            if min(clearance)>=.10 and max(clearance)<=2.4:
                score=95.0-sum(abs(clearance[k]-.5)*8.0 for k in range(2))
                candidates.append({"strategy":"INSERT_INTO_OPENING","target_loop_index":ti,"target":target,"score":score,"fit_margin_mm":[round(clearance[k]/2,3) for k in range(2)]})
        if not candidates:continue
        candidates.sort(key=lambda x:x["score"],reverse=True)
        best=candidates[0];second=candidates[1]["score"] if len(candidates)>1 else -999
        if best["score"]<72 or best["score"]-second<6:continue
        side=None
        if visual_rx.search(sem) and pos_count!=neg_count:side="POSITIVE" if pos_count>neg_count else "NEGATIVE"
        elif pos_count>=2 and neg_count==0:side="POSITIVE"
        elif neg_count>=2 and pos_count==0:side="NEGATIVE"
        if side is None:continue
        target=best["target"];delta=[0.0,0.0,0.0]
        sc=sp["outer"]["center_mm"];tc=target["center_mm"]
        delta[plane_axes[0]]=tc[0]-sc[0];delta[plane_axes[1]]=tc[1]-sc[1]
        b=p.get("world_bounds_mm") or {};mn=b.get("min") or [];mx=b.get("max") or []
        if len(mn)!=3 or len(mx)!=3:continue
        if side=="POSITIVE":delta[ai]=f(pmx[ai])-f(mn[ai])
        else:delta[ai]=f(pmn[ai])-f(mx[ai])
        p["assembly_translate_mm"]=[round(x,6) for x in delta]
        p["assembly_parent"]=str(primary.get("name") or "")
        p["assembly_pose_status"]="RESOLVED_BY_MATE_SOLVER"
        p["mate_contract"]={"strategy":best["strategy"],"parent":p["assembly_parent"],"axis":pp["axis"],"side":side,"target_loop_index":best["target_loop_index"],"score":round(best["score"],2),"fit_margin_mm":best.get("fit_margin_mm"),"border_mm":best.get("border_mm"),"target_center_2d_mm":[round(x,3) for x in target["center_mm"]],"target_dimensions_mm":[round(x,3) for x in target["dimensions_mm"]],"source":"MEASURED_PLANAR_PROFILE_MATCH"}
        resolved.append({"part":str(p.get("name") or ""),"parent":p["assembly_parent"],"assembly_translate_mm":p["assembly_translate_mm"],"mate_contract":p["mate_contract"],"confidence":"HIGH"})
    return resolved

def _assembly_pose_from_source(parts,bambu_hints,spatial_contacts):
    if not parts:return {"status":"UNAVAILABLE","reason":"no_parts"}
    meta_parts=[]
    for obj in (bambu_hints or {}).get("model_settings") or []:
        for mp in obj.get("parts") or []:
            meta_parts.append(mp)
    by_id={str(x.get("part_id") or ""):x for x in meta_parts if str(x.get("part_id") or "")}
    for p in parts:
        mp=by_id.get(str(p.get("source_object_id") or ""))
        if not mp:continue
        md=mp.get("metadata") or {}
        p["bambu_part_id"]=str(mp.get("part_id") or "")
        p["semantic_name"]=str(md.get("name") or p.get("name") or "")
        p["extruder"]=md.get("extruder")
        p["bambu_matrix"]=md.get("matrix")
        p["bambu_subtype"]=mp.get("subtype")
    def volbox(p):
        d=p.get("dimensions_mm") or []
        return max(.001,f(d[0]))*max(.001,f(d[1]))*max(.001,f(d[2])) if len(d)==3 else 0.0
    semantic_primary=[]
    for p in parts:
        n=str(p.get("semantic_name") or p.get("name") or "").strip().lower()
        if re.search(r"(^|[ _-])(body|main|base|shell|housing|case)([ _-]|$)",n):
            semantic_primary.append(p)
    primary=max(semantic_primary,key=volbox) if semantic_primary else max(parts,key=volbox)
    pb=primary.get("world_bounds_mm") or {};pmn=pb.get("min") or [];pmx=pb.get("max") or []
    primary_name=str(primary.get("name") or "")
    attached=[];separate=[];unresolved=[]
    detail_rx=re.compile(r"(title|time|heart|bar|icon|text|logo|label|accent|detail|inlay|mark|letter|number|symbol)",re.I)
    for p in parts:
        name=str(p.get("name") or "")
        if p is primary:
            p["source_pose_class"]="PRIMARY_BODY";p["assembly_pose_status"]="RESOLVED_FROM_SOURCE"
            continue
        b=p.get("world_bounds_mm") or {};mn=b.get("min") or [];mx=b.get("max") or []
        if len(mn)!=3 or len(mx)!=3 or len(pmn)!=3 or len(pmx)!=3:
            p["source_pose_class"]="UNRESOLVED";p["assembly_pose_status"]="UNRESOLVED";unresolved.append(name);continue
        gaps=[max(0.0,f(mn[i])-f(pmx[i]),f(pmn[i])-f(mx[i])) for i in range(3)]
        gap=math.sqrt(sum(x*x for x in gaps))
        overlap=[min(f(mx[i]),f(pmx[i]))-max(f(mn[i]),f(pmn[i])) for i in range(3)]
        xy_overlap=overlap[0]>0.01 and overlap[1]>0.01
        z_touch=(f(mn[2])<=f(pmx[2])+.35 and f(mx[2])>=f(pmn[2])-.35)
        bed_contact=f(mn[2])<=.08
        semantic=str(p.get("semantic_name") or name)
        source_attached=gap<=.35 and xy_overlap and z_touch
        detail_semantic=bool(detail_rx.search(semantic))
        if source_attached and (detail_semantic or f(mn[2])>=f(pmx[2])-.40):
            p["source_pose_class"]="SURFACE_ATTACHED"
            p["assembly_pose_status"]="RESOLVED_FROM_SOURCE_CONTACT"
            p["assembly_parent"]=primary_name
            attached.append(name)
        elif bed_contact and gap>.35:
            p["source_pose_class"]="SEPARATE_PRINT_PART"
            p["assembly_pose_status"]="ASSEMBLY_POSE_UNRESOLVED"
            separate.append(name)
        elif gap>1.5:
            p["source_pose_class"]="SEPARATE_PRINT_PART"
            p["assembly_pose_status"]="ASSEMBLY_POSE_UNRESOLVED"
            separate.append(name)
        elif source_attached:
            p["source_pose_class"]="SOURCE_ATTACHED_PART"
            p["assembly_pose_status"]="RESOLVED_FROM_SOURCE_CONTACT"
            p["assembly_parent"]=primary_name
            attached.append(name)
        else:
            p["source_pose_class"]="UNRESOLVED"
            p["assembly_pose_status"]="UNRESOLVED"
            unresolved.append(name)
    resolved_mates=_resolve_separate_planar_mates(parts,primary)
    resolved_mate_names=set(x.get("part") for x in resolved_mates)
    assembly_members=[p for p in parts if p.get("source_pose_class") in ("PRIMARY_BODY","SURFACE_ATTACHED","SOURCE_ATTACHED_PART") or str(p.get("name") or "") in resolved_mate_names]
    ab=None
    for p in assembly_members:
        b=p.get("world_bounds_mm") or {};mn=b.get("min") or [];mx=b.get("max") or []
        if len(mn)!=3 or len(mx)!=3:continue
        tr=p.get("assembly_translate_mm") or [0,0,0]
        if not isinstance(tr,list) or len(tr)!=3:tr=[0,0,0]
        bb=[f(mn[0])+f(tr[0]),f(mn[1])+f(tr[1]),f(mn[2])+f(tr[2]),f(mx[0])+f(tr[0]),f(mx[1])+f(tr[1]),f(mx[2])+f(tr[2])]
        ab=bb if ab is None else [min(ab[0],bb[0]),min(ab[1],bb[1]),min(ab[2],bb[2]),max(ab[3],bb[3]),max(ab[4],bb[4]),max(ab[5],bb[5])]
    adims=[round(max(0,ab[i+3]-ab[i]),3) for i in range(3)] if ab is not None else list(primary.get("dimensions_mm") or [])
    allowed=[]
    attached_set=set(attached+[primary_name])
    for c in spatial_contacts:
        a=str(c.get("part_a") or "");b=str(c.get("part_b") or "")
        sem=None
        if c.get("relation")=="overlap_or_contact" and a in attached_set and b in attached_set:
            sem="SOURCE_INTENDED_ATTACHMENT"
        elif c.get("relation")=="near_contact" and ((a==primary_name and b in attached_set) or (b==primary_name and a in attached_set)):
            sem="SOURCE_NEAR_ATTACHMENT"
        if sem:
            c["assembly_semantics"]=sem;allowed.append({"part_a":a,"part_b":b,"semantics":sem})
    unresolved_separate=[n for n in separate if n not in resolved_mate_names]
    for m in resolved_mates:
        allowed.append({"part_a":m.get("parent"),"part_b":m.get("part"),"semantics":"RESOLVED_MATE","mate_contract":m.get("mate_contract")})
    status="RESOLVED" if not unresolved and not unresolved_separate else ("PARTIAL" if assembly_members else "UNRESOLVED")
    return {
      "version":"assembly-pose-solver-v2",
      "status":status,
      "primary_part":primary_name,
      "primary_semantic_name":primary.get("semantic_name") or primary_name,
      "primary_assembly_parts":[str(p.get("name") or "") for p in assembly_members],
      "surface_attached_parts":attached,
      "separate_print_parts":separate,
      "resolved_separate_parts":[x.get("part") for x in resolved_mates],
      "unresolved_separate_parts":unresolved_separate,
      "resolved_mates":resolved_mates,
      "unresolved_parts":unresolved,
      "primary_assembly_dimensions_mm":adims,
      "source_intended_contact_pairs":allowed,
      "metadata_part_name_coverage":round(sum(1 for p in parts if p.get("semantic_name"))/max(1,len(parts)),3),
      "separate_part_assembly_pose_resolved":len(unresolved_separate)==0,
      "full_assembly_pose_resolved":status=="RESOLVED"
    }

def _bambu_metadata_hints(z,names):
    out={"model_settings":[],"plate_instances":[],"json_summaries":[]}
    target=next((n for n in names if n.lower()=="metadata/model_settings.config"),None)
    if target:
        try:
            with z.open(target) as fh:
                current_object=None;current_part=None;current_instance=None
                for ev,el in ET.iterparse(fh,events=("start","end")):
                    tag=str(el.tag).rsplit("}",1)[-1]
                    if ev=="start":
                        if tag=="object":
                            current_object={"object_id":str(el.attrib.get("id") or ""),"metadata":{},"parts":[]};out["model_settings"].append(current_object)
                        elif tag=="part" and current_object is not None:
                            current_part={"part_id":str(el.attrib.get("id") or ""),"subtype":el.attrib.get("subtype"),"metadata":{}};current_object["parts"].append(current_part)
                        elif tag=="model_instance":
                            current_instance={"metadata":{}};out["plate_instances"].append(current_instance)
                        elif tag=="metadata":
                            k=str(el.attrib.get("key") or "");v=el.attrib.get("value")
                            if current_part is not None:current_part["metadata"][k]=v
                            elif current_instance is not None:current_instance["metadata"][k]=v
                            elif current_object is not None:current_object["metadata"][k]=v
                        continue
                    if tag=="part":current_part=None
                    elif tag=="model_instance":current_instance=None
                    elif tag=="object":current_object=None
                    el.clear()
        except Exception as ex:
            out["model_settings_error"]=str(ex)[:240]
    for n in names:
        nl=n.lower()
        if not (nl.startswith("metadata/plate_") and nl.endswith(".json")):continue
        try:
            raw=z.read(n)
            if len(raw)>2_000_000:continue
            obj=json.loads(raw.decode("utf-8","ignore"))
            def compact(v,depth=0):
                if depth>2:return None
                if isinstance(v,dict):
                    keep={}
                    for k,x in v.items():
                        ks=str(k)
                        if any(t in ks.lower() for t in ("object","instance","part","plate","name","id","transform","matrix","assemble")):
                            keep[ks]=compact(x,depth+1)
                    return keep
                if isinstance(v,list):return [compact(x,depth+1) for x in v[:40]]
                if isinstance(v,(str,int,float,bool)) or v is None:return v
                return str(v)[:160]
            out["json_summaries"].append({"entry":n,"summary":compact(obj)})
        except Exception:
            pass
    return out

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

def _dw_face_evidence_selftest():
    body=cq.Workplane("XY").box(60,40,5,centered=(True,True,False))
    pocket=cq.Workplane("XY").workplane(offset=3.6).circle(13.2).extrude(1.6)
    shape=body.cut(pocket)
    vv,tt=mesh_of(shape,.04);mesh={"name":"face-selftest","vertices":vv,"triangles":tt}
    openings=_dw_attach_face_evidence(mesh,_dw_openings(mesh))
    blind=[x for x in openings if str(x.get("kind") or "").startswith("blind_")]
    best=sorted(blind,key=lambda x:abs(f(x.get("equivalent_diameter_mm"))-26.4))[0] if blind else None
    ev=(best or {}).get("face_evidence") or {}
    depth=f(ev.get("cavity_depth_mm"),-1);wall=f(ev.get("residual_wall_mm"),-1)
    checks={
        "blind_detected":best is not None,
        "face_evidence_resolved":ev.get("status")=="RESOLVED",
        "opening_face_present":bool(ev.get("opening_face")),
        "bottom_face_present":bool(ev.get("bottom_face")),
        "opposite_face_present":bool(ev.get("opposite_exterior_face")),
        "depth_measured":abs(depth-1.4)<=.12,
        "residual_wall_measured":abs(wall-3.6)<=.12
    }
    return {"status":"PASS" if all(checks.values()) else "FAIL","checks":checks,"candidate":best,"face_cluster_count":len(_dw_planar_face_clusters(mesh))}

def _u_cad_evidence_binding_selftest():
    body=cq.Workplane("XY").box(40,30,5,centered=(True,True,False))
    before=shape_volume(body)
    nodes=[
        {"id":"OPENING_1","type":"blind_circular_pocket","operation":"CUT","axis":"Z","center_2d_mm":[0,0],"diameter_mm":10.0,"depth_mm":1.0,"through":False,"opening_face":"MAX",
         "face_evidence":{"status":"RESOLVED","opening_face":{"plane_mm":5.0},"opposite_exterior_face":{"plane_mm":0.0}}},
        {"id":"CAD_DIM_1","type":"verified_dimension_constraint","operation":"CONSTRAIN_DIMENSION","name":"cavity diameter","value_mm":12.0,"execution_state":"PARAMETRIC_MAPPING_RESOLVED","target_feature_id":"OPENING_1","target_parameter":"diameter_mm"},
        {"id":"CAD_DIM_2","type":"verified_dimension_constraint","operation":"CONSTRAIN_DIMENSION","name":"residual wall","value_mm":2.0,"execution_state":"PARAMETRIC_MAPPING_RESOLVED","target_feature_id":"OPENING_1","target_parameter":"residual_wall_mm"}
    ]
    bound,binding_rows=_u_bind_dimension_constraints(nodes)
    target=next(x for x in bound if x.get("id")=="OPENING_1")
    cut=_u_apply_openings(body,bound)
    removed=before-shape_volume(cut)
    expected=math.pi*(12.0/2.0)**2*3.0
    checks={
        "two_constraints_bound":len(binding_rows)==2,
        "diameter_overridden":abs(f(target.get("diameter_mm"))-12.0)<=1e-6,
        "residual_wall_bound":abs(f(target.get("required_residual_wall_mm"))-2.0)<=1e-6,
        "blind_cavity_boolean_applied":removed>0,
        "boolean_volume_matches_bound_geometry":abs(removed-expected)<=2.0
    }
    return {"status":"PASS" if all(checks.values()) else "FAIL","checks":checks,"bindings":binding_rows,"removed_volume_mm3":round(removed,3),"expected_removed_volume_mm3":round(expected,3)}

class Handler(BaseHTTPRequestHandler):
    server_version="MakerSenceCAD/2.55.0-generic-runtime-cleanup"
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
        if path=="/health":return self.send_json(200,{"ok":True,"service":"makersence-cad-worker","version":"2.55.0-generic-runtime-cleanup","engine":"cadquery+blender+svgpathtools+shapely+pillow","blender":{"available":pathlib.Path(BLENDER_BIN).exists(),"binary":BLENDER_BIN},"bambu_slicer":{"available":bool(BAMBU_BIN and pathlib.Path(BAMBU_BIN).exists()),"engine":"Bambu Studio","version":BAMBU_VERSION},"capabilities":["compact_step_brep","bambu_native_parts","detachable_parts","assembly_render","product_dimensions","open_edges_zero_gate","formal_mesh_render","artifact_reaudit","rectangular_blind_pockets","geometry_intent_gate","orphan_geometry_gate","unintended_through_cut_gate","welded_3mf_meshes","exported_3mf_topology_gate","true_font_outline_text","high_smooth_vector_mesh","multilingual_font_fallback","actual_text_stroke_gate","adaptive_cjk_regular_first","cjk_internal_clearance_gate","cjk_counter_preservation_gate","text_mesh_topology_candidate_gate","remote_3mf_stream_analyzer","remote_3mf_xml_iterparse","remote_3mf_transform_aware_bounds","remote_3mf_cad_drawing_v1","remote_3mf_reconstruction_sections_v2","auto_hole_slot_detection","blind_cavity_detection_v1","planar_face_cluster_locator_v1","cavity_bottom_face_match_v1","residual_wall_normal_distance_v1","paired_plane_thickness_v1","bounded_per_part_feature_scan_v1","auto_fillet_chamfer_candidates","auto_section_view_plan","multipart_dimension_semantics","supplementary_stl_step_analyzer","sculpted_lidded_container_v1","smooth_pumpkin_container_v2","generic_memory_budget_v1","streaming_3mf_glb_export","source_3mf_glb_preview_v1","world_space_feature_center_v1","auto_text_boldening","typography_layout_bounds","script_aware_glyph_spacing","glyph_clearance_gate","text_readability_gate","bambu_04_text_profile","text_slicer_no_merge_gate","separate_structural_text_min_feature","arachne_text_project_settings","bambu_cli_real_slice","gcode_3mf_toolpath_gate","print_ready_plate_3mf","plate_part_coverage_gate","universal_cad_recipe_v2","cad_evidence_parametric_binding_v1","blind_cavity_recipe_cut_v1","planar_design_fidelity_artifact_gate_v1","universal_design_fidelity_artifact_gate_v1","section_loft_reconstruction_v1","section_loft_open_cavity_v2","multipart_relation_rebuild_v1","per_part_reconstruction_evidence_v1","planar_multiloop_extrusion_v1","planar_mesh_projection_fallback_v1","bambu_assembly_metadata_evidence_v1","assembly_pose_solver_v1","assembly_pose_solver_v3","mechanism_pose_brep_probe_v1","mechanism_motion_solver_v1","mechanism_motion_solver_v2","mechanism_motion_solver_v3","hinge_sweep_collision_gate_v1","terminal_stop_refinement_v1","latch_relative_pivot_engagement_v1","motion_bbox_prefilter_v1","motion_memory_checkpoint_v1","motion_exact_separation_prefilter_v1","motion_fail_closed_collision_v1","motion_early_direction_exit_v1","motion_isolated_subprocess_v1","motion_timeout_guard_v1","async_motion_jobs_v1","separate_part_mate_resolver_v1","planar_ring_opening_match_v1","faceted_mesh_brep_v1","complex_topology_mesh_fallback_v1","sealed_internal_cavity_mesh_fallback_v1","multipart_dimension_semantics_v2","source_intended_contact_qa_v1","same_root_assembly_guard_v1","axisymmetric_revolve_reconstruction_v1","bounded_inprocess_universal_jobs_v1","planar_prismatic_reconstruction_v1","commercial_visual_release_gate_v1",],"profiles":["bambu_a1_mini_04"],"universal_executor_probe":{"cavity_loft_policy":UNIVERSAL_CAVITY_LOFT_POLICY,"loft_argcount":_u_loft_from_loops.__code__.co_argcount}})
        if path=="/v1/selftest/face-evidence":
            if not self.authorized():return
            return self.send_json(200,_dw_face_evidence_selftest())
        if path=="/v1/selftest/cad-evidence-binding":
            if not self.authorized():return
            return self.send_json(200,_u_cad_evidence_binding_selftest())
        if path.startswith("/v1/jobs/"):
            if not self.authorized():return
            jid=path.split("/")[-1];j=JOBS.get(jid)
            return self.send_json(200,{"job_id":jid,**j}) if j else self.send_json(404,{"error":"job not found"})
        if path.startswith("/v1/motion-jobs/"):
            if not self.authorized():return
            jid=path.split("/")[-1];j=MOTION_JOBS.get(jid)
            return self.send_json(200,{"job_id":jid,**j}) if j else self.send_json(404,{"error":"motion job not found"})
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
            _,_,jid,name=parts;allowed={"model.stl","model.step","model.3mf","preview.glb","source_preview.glb","product_render_main.png","manifest.json","source.blend"}
            plate_file=bool(re.fullmatch(r"print_plate_\d+\.3mf",name))
            if name not in allowed and not plate_file:return self.send_json(404,{"error":"not found"})
            p=ROOT/jid/name
            if not p.exists():return self.send_json(404,{"error":"not found"})
            typ="model/3mf" if plate_file else {"model.stl":"model/stl","model.step":"application/step","model.3mf":"model/3mf","preview.glb":"model/gltf-binary","source_preview.glb":"model/gltf-binary","product_render_main.png":"image/png","manifest.json":"application/json","source.blend":"application/x-blender"}.get(name,"application/octet-stream")
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
            if path=="/v1/resolve-mechanism-pose":
                if n<=0 or n>2_000_000:return self.send_json(400,{"error":"invalid mechanism pose request"})
                req=json.loads(self.rfile.read(n).decode("utf-8"))
                return self.send_json(200,_mechanism_pose_probe(req))
            if path=="/v1/validate-mechanism-motion":
                if n<=0 or n>4_000_000:return self.send_json(400,{"error":"invalid mechanism motion request"})
                req=json.loads(self.rfile.read(n).decode("utf-8"))
                return self.send_json(200,_mechanism_motion_probe(req))
            if path=="/v1/submit-mechanism-motion":
                if n<=0 or n>4_000_000:return self.send_json(400,{"error":"invalid mechanism motion request"})
                req=json.loads(self.rfile.read(n).decode("utf-8"))
                return self.send_json(202,_submit_motion_job(req))
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
    if len(sys.argv)>=4 and sys.argv[1]=="--motion-child":
        raise SystemExit(_motion_child_cli(sys.argv[2],sys.argv[3]))
    print("MakerSence CAD Worker 2.51.0-motion-exact-prefilter starting on",PORT,"Bambu Studio",BAMBU_VERSION,"available",bool(BAMBU_BIN and pathlib.Path(BAMBU_BIN).exists()),flush=True)
    ThreadingHTTPServer(("0.0.0.0",PORT),Handler).serve_forever()