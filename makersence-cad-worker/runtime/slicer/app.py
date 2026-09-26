import os, json, time, uuid, pathlib, threading, subprocess, zipfile, re, hashlib, shutil, ctypes.util, ipaddress, base64, hmac
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

PORT=int(os.environ.get("PORT","8080"))
TOKEN=os.environ.get("SLICER_TOKEN","")
PRIVATE_DOMAIN=os.environ.get("RAILWAY_PRIVATE_DOMAIN","").strip().lower()
MODAL_SHARED_TOKEN=os.environ.get("MAKERSENCE_MODAL_SHARED_TOKEN","").strip()
MODAL_TOKEN_ID=os.environ.get("MODAL_TOKEN_ID","").strip()
MODAL_TOKEN_SECRET=os.environ.get("MODAL_TOKEN_SECRET","").strip()
MODAL_TRIPOSG_ENDPOINT=os.environ.get("MODAL_TRIPOSG_ENDPOINT","https://zhbettychien--makersence-triposg-api.modal.run/generate").strip()

def effective_modal_shared_token():
    if MODAL_SHARED_TOKEN:
        return MODAL_SHARED_TOKEN
    if MODAL_TOKEN_ID.startswith("ak-") and MODAL_TOKEN_SECRET.startswith("as-"):
        digest=hmac.new(
            MODAL_TOKEN_SECRET.encode(),
            ("makersence-modal-shared-v1:"+MODAL_TOKEN_ID).encode(),
            hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return ""
BAMBU_BIN=os.environ.get("BAMBU_BIN","")
BAMBU_VERSION=os.environ.get("BAMBU_VERSION","2.8.2.61")
DISPLAY_MODE=os.environ.get("BAMBU_DISPLAY_MODE","hybrid_wayland")
ROOT=pathlib.Path(os.environ.get("SLICER_JOB_ROOT","/tmp/makersence_slicer_jobs"));ROOT.mkdir(parents=True,exist_ok=True)
RUNTIME=pathlib.Path("/tmp/makersence-display");RUNTIME.mkdir(parents=True,exist_ok=True)
try:RUNTIME.chmod(0o700)
except Exception:pass
HOME=pathlib.Path("/tmp/bambu-home");(HOME/".config"/"BambuStudio").mkdir(parents=True,exist_ok=True)
JOBS={}
LOCK=threading.Semaphore(1)
XVFB=None
WESTON=None
X_DISPLAY=":99"
WAYLAND_SOCKET="wayland-makersence"

def gcode_evidence_bytes(b):
    txt=b.decode("utf-8","ignore")
    markers=len(re.findall(r"(?:^|\n)(?:;\s*CHANGE_LAYER|;LAYER_CHANGE|; layer num/|;LAYER:)",txt,re.I))
    complete=bool(re.search(r"(?:M73\s+P100\s+R0|;\s*EXECUTABLE_BLOCK_END)",txt,re.I))
    return markers,complete

def object_id_map_3mf(path):
    out={}
    try:
        with zipfile.ZipFile(path,"r") as z:
            names=z.namelist()
            for n in names:
                ln=n.lower()
                if not (ln.endswith(".model") or ln.endswith("model_settings.config")):continue
                try:txt=z.read(n).decode("utf-8","ignore")
                except Exception:continue
                for m in re.finditer(r"<object\b([^>]*)>(.*?)</object>",txt,re.I|re.S):
                    attrs,body=m.group(1),m.group(2)
                    im=re.search(r'\bid=["\'](\d+)["\']',attrs,re.I)
                    if not im:continue
                    oid=im.group(1);name=None
                    nm=re.search(r'\bname=["\']([^"\']+)["\']',attrs,re.I)
                    if nm:name=nm.group(1)
                    if not name:
                        nm=re.search(r'<metadata\b[^>]*\bkey=["\'](?:name|object_name)["\'][^>]*\bvalue=["\']([^"\']+)["\']',body,re.I)
                        if nm:name=nm.group(1)
                    if not name:
                        nm=re.search(r'<metadata\b[^>]*\bname=["\'](?:name|object_name)["\'][^>]*>([^<]+)</metadata>',body,re.I)
                        if nm:name=nm.group(1).strip()
                    if name:out[oid]=name
                for m in re.finditer(r'<object\b([^>]*)/?>',txt,re.I):
                    attrs=m.group(1);im=re.search(r'\bid=["\'](\d+)["\']',attrs,re.I)
                    nm=re.search(r'\bname=["\']([^"\']+)["\']',attrs,re.I)
                    if im and nm and im.group(1) not in out:out[im.group(1)]=nm.group(1)
    except Exception:pass
    return out

def analyze_floating_gcode(path,object_map=None):
    object_map=object_map or {}
    z=None;obj=None;relative_e=True;last_e=0.0
    num=r"[-+]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)"
    mapping_comments=[];seen_comments=set()
    layer_bounds={};object_layers={};floating_blocks=[];current_block=None
    def add_bbox(rec,x,y,e=0.0):
        if x is not None:
            rec["min_x"]=x if rec.get("min_x") is None else min(rec["min_x"],x)
            rec["max_x"]=x if rec.get("max_x") is None else max(rec["max_x"],x)
        if y is not None:
            rec["min_y"]=y if rec.get("min_y") is None else min(rec["min_y"],y)
            rec["max_y"]=y if rec.get("max_y") is None else max(rec["max_y"],y)
        rec["moves"]=int(rec.get("moves") or 0)+1
        rec["positive_e"]=float(rec.get("positive_e") or 0.0)+max(0.0,e)
    def new_bbox():
        return {"min_x":None,"max_x":None,"min_y":None,"max_y":None,"moves":0,"positive_e":0.0}
    try:
        with open(path,"r",encoding="utf-8",errors="ignore") as fh:
            for raw in fh:
                line=raw.strip();low=line.lower()
                if line.startswith(";") and len(mapping_comments)<160 and any(k in low for k in ("object","label","name","instance")):
                    if line not in seen_comments and len(line)<700:
                        seen_comments.add(line);mapping_comments.append(line)
                if re.match(r"M83\\b",line,re.I):relative_e=True;continue
                if re.match(r"M82\\b",line,re.I):relative_e=False;continue
                m=re.match(r"G92\\b.*(?:^|\\s)E("+num+r")",line,re.I)
                if m:
                    try:last_e=float(m.group(1))
                    except Exception:pass
                    continue
                m=re.match(r";\\s*Z_HEIGHT:\\s*("+num+r")",line,re.I)
                if m:
                    try:z=float(m.group(1))
                    except Exception:pass
                m=re.match(r";\\s*OBJECT_ID:\\s*(\\d+)",line,re.I)
                if m:obj=m.group(1)
                m=re.match(r";\\s*FEATURE:\\s*(.+)",line,re.I)
                if m:
                    feature=m.group(1).strip()
                    if "floating" in feature.lower():
                        current_block={"kind":"bambu_floating_feature","feature":feature,"object_id":obj,"name":object_map.get(obj or ""),"min_z":z,"max_z":z,**new_bbox()}
                        floating_blocks.append(current_block)
                    else:current_block=None
                    continue
                if not re.match(r"G[01]\\b",line,re.I):continue
                zv=re.search(r"(?:^|\\s)Z("+num+r")",line,re.I)
                if zv:
                    try:z=float(zv.group(1))
                    except Exception:pass
                xv=re.search(r"(?:^|\\s)X("+num+r")",line,re.I)
                yv=re.search(r"(?:^|\\s)Y("+num+r")",line,re.I)
                ev=re.search(r"(?:^|\\s)E("+num+r")",line,re.I)
                x=float(xv.group(1)) if xv else None;y=float(yv.group(1)) if yv else None
                delta_e=0.0
                if ev:
                    e=float(ev.group(1))
                    if relative_e:delta_e=e
                    else:
                        delta_e=e-last_e;last_e=e
                if delta_e<=1e-7:continue
                if z is not None:
                    zk=round(float(z),3)
                    rec=layer_bounds.setdefault(zk,new_bbox());add_bbox(rec,x,y,delta_e)
                    okey=obj or "unknown"
                    orecs=object_layers.setdefault(okey,{})
                    orec=orecs.setdefault(zk,new_bbox());add_bbox(orec,x,y,delta_e)
                if current_block is not None:
                    if z is not None:
                        current_block["min_z"]=z if current_block.get("min_z") is None else min(current_block["min_z"],z)
                        current_block["max_z"]=z if current_block.get("max_z") is None else max(current_block["max_z"],z)
                    add_bbox(current_block,x,y,delta_e)
        zs=sorted(layer_bounds.keys())
        diffs=[round(zs[i]-zs[i-1],3) for i in range(1,len(zs)) if 0<zs[i]-zs[i-1]<=1.0]
        typical_step=sorted(diffs)[len(diffs)//2] if diffs else None
        support_gap=max(.35,(typical_step or .2)*1.8)
        def overlaps(a,b,margin=.6):
            if not a or not b:return False
            vals=[a.get("min_x"),a.get("max_x"),a.get("min_y"),a.get("max_y"),b.get("min_x"),b.get("max_x"),b.get("min_y"),b.get("max_y")]
            if any(v is None for v in vals):return False
            return not (a["max_x"]<b["min_x"]-margin or a["min_x"]>b["max_x"]+margin or a["max_y"]<b["min_y"]-margin or a["min_y"]>b["max_y"]+margin)
        def previous_layer(bz):
            candidates=[zz for zz in zs if bz is not None and zz<bz and bz-zz<=support_gap]
            pz=max(candidates) if candidates else None
            return pz,layer_bounds.get(pz) if pz is not None else None
        risk_blocks=[];assessed_features=[]
        for block in floating_blocks:
            bz=block.get("min_z");prev_z,prev=previous_layer(bz)
            supported=(bz is not None and bz<=.6) or overlaps(block,prev,.6)
            risk=bool(block.get("positive_e",0)>0.01 and bz is not None and bz>.6 and not supported)
            row={**block,"positive_e":round(float(block.get("positive_e") or 0.0),5),
                 "previous_layer_z":prev_z,"supported_by_previous_layer":supported,"floating_object_risk":risk}
            assessed_features.append(row)
            if risk:risk_blocks.append(row)
        object_start_checks=[]
        for oid,layers in object_layers.items():
            if not layers:continue
            first_z=min(layers.keys());first=layers[first_z];prev_z,prev=previous_layer(first_z)
            supported=(first_z<=.6) or overlaps(first,prev,.6)
            risk=bool(first_z>.6 and float(first.get("positive_e") or 0)>0.01 and not supported)
            row={"kind":"object_first_extrusion","object_id":oid,"name":object_map.get(oid),"first_z":first_z,
                 "previous_layer_z":prev_z,"supported_by_previous_layer":supported,"floating_object_risk":risk,
                 "positive_e":round(float(first.get("positive_e") or 0.0),5),
                 "min_x":first.get("min_x"),"max_x":first.get("max_x"),"min_y":first.get("min_y"),"max_y":first.get("max_y")}
            object_start_checks.append(row)
            if risk:risk_blocks.append(row)
        feature_detected=any(float(x.get("positive_e") or 0)>0.01 for x in assessed_features)
        analysis_complete=bool(zs)
        return {
            "detected":feature_detected,
            "floating_feature_detected":feature_detected,
            "analysis_complete":analysis_complete,
            "floating_object_risk":bool(risk_blocks) if analysis_complete else True,
            "risk_block_count":len(risk_blocks) if analysis_complete else 1,
            "risk_blocks":risk_blocks[:80] if analysis_complete else [{"kind":"analysis_incomplete","reason":"no_extrusion_layers_parsed"}],
            "blocks":assessed_features[:160],
            "object_start_checks":object_start_checks[:160],
            "assessment_method":"gcode_object_start_plus_previous_layer_extrusion_overlap_v3",
            "layer_count":len(zs),
            "min_extrusion_z":min(zs) if zs else None,
            "max_extrusion_z":max(zs) if zs else None,
            "typical_layer_step_mm":typical_step,
            "object_map":object_map,
            "mapping_comments":mapping_comments
        }
    except Exception as e:
        return {"detected":False,"floating_feature_detected":False,"analysis_complete":False,"floating_object_risk":True,
                "risk_block_count":1,"risk_blocks":[{"kind":"analysis_error","error":str(e)}],
                "assessment_method":"floating_analysis_error_fail_closed","error":str(e),"object_map":object_map}

def inspect_3mf(path):
    out={"zip_ok":False,"gcode_entries":[],"gcode_bytes":0,"layer_markers":0,"completion_marker":False,"toolpath_checked":False}
    if not path.exists() or path.stat().st_size<64:return out
    try:
        with zipfile.ZipFile(path,"r") as z:
            out["zip_ok"]=z.testzip() is None
            names=z.namelist()
            gc=[n for n in names if n.lower().endswith(".gcode") or ".gcode." in n.lower()]
            out["gcode_entries"]=gc
            total=0;markers=0;complete=False
            for n in gc[:8]:
                b=z.read(n);total+=len(b)
                m,c=gcode_evidence_bytes(b);markers+=m;complete=complete or c
            out["gcode_bytes"]=total
            out["layer_markers"]=markers
            out["completion_marker"]=complete
            out["toolpath_checked"]=bool(out["zip_ok"] and gc and total>1000 and markers>0 and complete)
    except Exception as e:out["error"]=str(e)
    return out

def package_completed_bambu_toolpath(inp,out,sd,started,recovered_flag=True,mode="recovered_completed_toolpath_after_native_crash"):
    root=pathlib.Path("/tmp/bamboo_model")
    candidates=[]
    try:
        for p in root.rglob("*.gcode"):
            try:
                st=p.stat()
                if st.st_mtime>=started-2 and st.st_size>100_000:candidates.append(p)
            except Exception:pass
    except Exception:pass
    candidates.sort(key=lambda p:(p.stat().st_mtime,p.stat().st_size),reverse=True)
    for gp in candidates[:12]:
        try:
            b=gp.read_bytes()
            markers,complete=gcode_evidence_bytes(b)
            if markers<1 or not complete or len(b)<100_000:continue
            object_map=object_id_map_3mf(inp)
            floating=analyze_floating_gcode(gp,object_map)
            shutil.copy2(inp,out)
            arc="Metadata/plate_0.gcode"
            with zipfile.ZipFile(out,"a",compression=zipfile.ZIP_DEFLATED,allowZip64=True) as z:
                existing=set(z.namelist())
                if arc in existing:arc="Metadata/makersence_recovered_plate_0.gcode"
                z.writestr(arc,b)
                z.writestr("Metadata/makersence_bambu_recovery.json",json.dumps({
                    "engine":"Bambu Studio","engine_version":BAMBU_VERSION,
                    "mode":mode,
                    "gcode_bytes":len(b),"layer_markers":markers,
                    "completion_marker":True,"source_temp_file":gp.name,
                    "floating_analysis":floating
                },ensure_ascii=False,indent=2))
            shutil.copy2(gp,sd/"plate_0.gcode")
            return {"packaged":True,"recovered":bool(recovered_flag),"mode":mode,"source":str(gp),"gcode_bytes":len(b),"layer_markers":markers,"completion_marker":True,"floating_analysis":floating}
        except Exception:pass
    return {"packaged":False,"recovered":False,"mode":mode}

def slicedata_report(folder):
    files=[];total=0
    try:
        for p in folder.rglob("*"):
            if p.is_file():
                sz=p.stat().st_size;total+=sz
                files.append({"name":str(p.relative_to(folder)),"bytes":sz})
    except Exception:pass
    return {"file_count":len(files),"bytes":total,"files":files[:120],"has_data":bool(files and total>1000)}

def display_env():
    e=os.environ.copy()
    e["DISPLAY"]=X_DISPLAY
    e["XDG_RUNTIME_DIR"]=str(RUNTIME)
    e["GDK_BACKEND"]="x11"
    e["QT_QPA_PLATFORM"]="xcb"
    e["LIBGL_ALWAYS_SOFTWARE"]="1"
    e["GALLIUM_DRIVER"]="llvmpipe"
    e["MESA_LOADER_DRIVER_OVERRIDE"]="llvmpipe"
    e["MESA_GL_VERSION_OVERRIDE"]="4.5COMPAT"
    e["MESA_GLSL_VERSION_OVERRIDE"]="450"
    e["WEBKIT_DISABLE_DMABUF_RENDERER"]="1"
    e["LD_LIBRARY_PATH"]=os.environ.get("BAMBU_LIBRARY_PATH",os.environ.get("LD_LIBRARY_PATH",""))
    e["LIBGL_DRIVERS_PATH"]=os.environ.get("LIBGL_DRIVERS_PATH","/usr/lib/x86_64-linux-gnu/dri")
    e["LC_ALL"]="C"
    if DISPLAY_MODE=="x11_flatpak":
        e["XDG_SESSION_TYPE"]="x11"
        e.pop("WAYLAND_DISPLAY",None)
        e.pop("EGL_PLATFORM",None)
    else:
        e["WAYLAND_DISPLAY"]=WAYLAND_SOCKET
        e["XDG_SESSION_TYPE"]="wayland"
        e["EGL_PLATFORM"]="wayland"
    return e

def ensure_display():
    global XVFB,WESTON
    xsock=pathlib.Path("/tmp/.X11-unix/X99")
    wsock=RUNTIME/WAYLAND_SOCKET
    x_ok=XVFB is not None and XVFB.poll() is None and xsock.exists()
    w_ok=WESTON is not None and WESTON.poll() is None and wsock.exists()
    if x_ok and (DISPLAY_MODE=="x11_flatpak" or w_ok):
        return True
    base=os.environ.copy()
    base["LIBGL_ALWAYS_SOFTWARE"]="1"
    base["GALLIUM_DRIVER"]="llvmpipe"
    base["MESA_LOADER_DRIVER_OVERRIDE"]="llvmpipe"
    base["LIBGL_DRIVERS_PATH"]="/usr/lib/x86_64-linux-gnu/dri"
    try:
        if not x_ok:
            xlog=open("/tmp/xvfb.log","ab",buffering=0)
            XVFB=subprocess.Popen(["Xvfb",X_DISPLAY,"-screen","0","1024x768x24","+extension","GLX","+render","-noreset"],
                                  stdout=xlog,stderr=subprocess.STDOUT,env=base)
            for _ in range(100):
                if xsock.exists() and XVFB.poll() is None: break
                if XVFB.poll() is not None: return False
                time.sleep(.1)
        xenv=base.copy();xenv["DISPLAY"]=X_DISPLAY
        probe=subprocess.run(["glxinfo","-B"],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=xenv,timeout=15)
        gtxt=probe.stdout.decode("utf-8","ignore")
        pathlib.Path("/tmp/glxinfo.log").write_text(gtxt,errors="ignore")
        if probe.returncode!=0 or not re.search(r"OpenGL version string",gtxt,re.I): return False
        if DISPLAY_MODE=="x11_flatpak":
            return True
        try:
            if wsock.exists(): wsock.unlink()
        except Exception: pass
        wenv=base.copy()
        wenv["DISPLAY"]=X_DISPLAY
        wenv["XDG_RUNTIME_DIR"]=str(RUNTIME)
        wenv.pop("WAYLAND_DISPLAY",None)
        wlog=open("/tmp/weston.log","ab",buffering=0)
        WESTON=subprocess.Popen([
            "weston","--backend=x11","--renderer=gl","--socket="+WAYLAND_SOCKET,
            "--width=1024","--height=768","--no-input","--no-config"
        ],stdout=wlog,stderr=subprocess.STDOUT,env=wenv)
        for _ in range(150):
            if wsock.exists() and WESTON.poll() is None:return True
            if WESTON.poll() is not None: break
            time.sleep(.1)
    except Exception as e:
        try:pathlib.Path("/tmp/display-init-error.log").write_text(str(e),errors="ignore")
        except Exception:pass
    return False

def run_job(jid,inp,out,sd):
    started=time.time()
    with LOCK:
        try:
            if not BAMBU_BIN or not pathlib.Path(BAMBU_BIN).exists():raise RuntimeError("Bambu Studio unavailable")
            if not ensure_display():
                tail=""
                try:tail=pathlib.Path("/tmp/display-init-error.log").read_text(errors="ignore")[-8000:]
                except Exception:pass
                raise RuntimeError("Headless display unavailable: "+tail)
            env=display_env();env["HOME"]=str(HOME)
            # Toolpath-first mode: Bambu Studio does the real slicing, while MakerSence
            # deterministically packages the completed G-code into the print project.
            # This avoids coupling slice validity to Bambu's headless --export-3mf path,
            # which is known to crash after a completed slice on some Linux CLI builds.
            cmd=[BAMBU_BIN,"--thumbnail-size=0x0","--skip-useless-pick=1","--min-save=1","--slice","0","--debug","2","--export-slicedata",str(sd),str(inp)]
            p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env,timeout=300)
            log=p.stdout.decode("utf-8","ignore")[-24000:]
            if p.returncode!=0:
                diag=[]
                for pat in ("bambu.strace*","bambu-lddebug*"):
                    try:
                        files=sorted(pathlib.Path("/home/slicer").glob(pat),key=lambda x:x.stat().st_mtime)[-3:]
                    except Exception:
                        files=[]
                    for fp in files:
                        try:
                            txt=fp.read_text(errors="ignore")[-12000:]
                            if txt.strip():diag.append("\n--- "+fp.name+" ---\n"+txt)
                        except Exception:pass
                if diag:log=(log+"".join(diag))[-40000:]
            native_slice_ok=bool(p.returncode==0)
            package=package_completed_bambu_toolpath(
                inp,out,sd,started,
                recovered_flag=not native_slice_ok,
                mode="native_toolpath_makersence_package" if native_slice_ok else "recovered_completed_toolpath_after_native_crash"
            )
            report=inspect_3mf(out);sdata=slicedata_report(sd)
            ok=report.get("toolpath_checked") is True and bool(package.get("packaged"))
            JOBS[jid].update({
              "status":"completed" if ok else "failed","returncode":p.returncode,"log_tail":log,
              "validation":{"status":"PASS" if ok else "FAIL","real_slicer_verified":ok,"toolpath_checked":bool(report.get("toolpath_checked")),
                "engine":"Bambu Studio","engine_version":BAMBU_VERSION,"slice_plate":0,
                "output_3mf_bytes":out.stat().st_size if out.exists() else 0,**report,"slicedata":sdata,
                "native_slice_completed":native_slice_ok,"native_export_completed":False,
                "deterministic_toolpath_package":bool(package.get("packaged")),
                "package_mode":package.get("mode"),"recovered_bambu_toolpath":bool(package.get("recovered")),
                "recovery":package,"native_returncode":p.returncode,
                "display_backend":"x11_flatpak_xvfb_glx" if DISPLAY_MODE=="x11_flatpak" else "xvfb_glx_plus_weston_x11_wayland"},
              "artifact":"/v1/slice-artifacts/"+jid+"/bambu_sliced.3mf" if out.exists() else None,
              "duration_ms":int((time.time()-started)*1000),"updated_at":time.time()
            })
        except Exception as e:
            JOBS[jid].update({"status":"failed","error":str(e),"validation":{"status":"FAIL","real_slicer_verified":False,
              "engine":"Bambu Studio","engine_version":BAMBU_VERSION,"display_backend":"xvfb_glx_plus_weston_x11_wayland"},
              "duration_ms":int((time.time()-started)*1000),"updated_at":time.time()})

def submit(data,key=None):
    if len(data)<64 or data[:2]!=b"PK":raise ValueError("input is not zip-based 3MF")
    key=key or hashlib.sha256(data).hexdigest()
    for jid,j in JOBS.items():
        if j.get("idempotency_key")==key:return {"job_id":jid,"status":j["status"]}
    jid=str(uuid.uuid4());folder=ROOT/("slice-"+jid);folder.mkdir(parents=True,exist_ok=True)
    inp=folder/"input.3mf";out=folder/"bambu_sliced.3mf";sd=folder/"slicedata";sd.mkdir()
    inp.write_bytes(data)
    JOBS[jid]={"status":"processing","idempotency_key":key,"folder":str(folder),"created_at":time.time(),"updated_at":time.time()}
    threading.Thread(target=run_job,args=(jid,inp,out,sd),daemon=True).start()
    return {"job_id":jid,"status":"processing"}


class _PreservePostRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if code in (301,302,303,307,308):
            return Request(newurl,data=req.data,headers=dict(req.headers),method="POST")
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def triposg_proxy(payload):
    shared_token=effective_modal_shared_token()
    if not shared_token:raise ValueError("modal shared token unavailable")
    if not isinstance(payload,dict):raise ValueError("invalid json body")
    provider=str(payload.get("provider") or "modal_triposg")
    mode=str(payload.get("mode") or "image_to_3d")
    source=str(payload.get("source_image_url") or "").strip()
    output=str(payload.get("output_format") or "glb").lower()
    try:faces=int(payload.get("target_faces") or 4000)
    except Exception:raise ValueError("invalid target_faces")
    if provider!="modal_triposg":raise ValueError("unsupported provider")
    if mode!="image_to_3d":raise ValueError("unsupported mode")
    if not re.match(r"^https://",source,re.I):raise ValueError("https source_image_url required")
    if output!="glb":raise ValueError("glb output required")
    if faces<1000 or faces>100000:raise ValueError("target_faces out of range")
    body=json.dumps({
      "contract_version":"makersence-design-model-v1",
      "provider":"modal_triposg",
      "mode":"image_to_3d",
      "source_image_url":source,
      "prompt":str(payload.get("prompt") or ""),
      "target_faces":faces,
      "output_format":"glb"
    },ensure_ascii=False,separators=(",",":")).encode("utf-8")
    req=Request(MODAL_TRIPOSG_ENDPOINT,data=body,headers={
      "Authorization":"Bearer "+shared_token,
      "Content-Type":"application/json",
      "Accept":"model/gltf-binary",
      "User-Agent":"MakerSence-v3-PrivateProxy/1"
    },method="POST")
    opener=build_opener(_PreservePostRedirect())
    try:
        with opener.open(req,timeout=600) as res:
            status=int(getattr(res,"status",200) or 200)
            ctype=str(res.headers.get("Content-Type") or "").split(";",1)[0].strip().lower()
            provider_version=str(res.headers.get("X-MakerSence-Provider-Version") or "").strip()
            claimed=str(res.headers.get("X-MakerSence-Artifact-Sha256") or "").strip().lower()
            data=res.read(64_000_001)
    except HTTPError as e:
        raise ValueError("modal HTTP "+str(e.code))
    except (URLError,TimeoutError) as e:
        raise ValueError("modal unreachable")
    if status<200 or status>=300:raise ValueError("modal status "+str(status))
    if len(data)<20 or len(data)>64_000_000 or data[:4]!=b"glTF":raise ValueError("invalid GLB")
    if ctype not in ("model/gltf-binary","application/octet-stream"):raise ValueError("invalid content type")
    sha=hashlib.sha256(data).hexdigest()
    if claimed and claimed!=sha:raise ValueError("GLB sha256 mismatch")
    return data,sha,provider_version


class H(BaseHTTPRequestHandler):
    server_version="MakerSenceSlicer/1.1.11"
    def log_message(self,fmt,*args):print(fmt%args,flush=True)
    def json(self,code,obj):
        b=json.dumps(obj,ensure_ascii=False).encode();self.send_response(code);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(b)));self.end_headers();self.wfile.write(b)
    def auth(self):
        if not TOKEN:return False
        return self.headers.get("Authorization")=="Bearer "+TOKEN
    def private_request(self):
        raw_host=str(self.headers.get("Host") or "").strip().lower()
        host=raw_host.split(":",1)[0]
        forwarded_host=str(self.headers.get("X-Forwarded-Host") or "").strip().lower()
        forwarded_proto=str(self.headers.get("X-Forwarded-Proto") or "").strip().lower()
        client=str((self.client_address or [""])[0]).strip()
        try:
            ip=ipaddress.ip_address(client)
            private_client=bool(ip.is_private or ip.is_loopback or ip.is_link_local)
        except Exception:
            private_client=False
        internal_host=host.endswith(".railway.internal")
        no_public_forwarding=not forwarded_host and not forwarded_proto
        ok=bool(internal_host and private_client and no_public_forwarding)
        if not ok:
            try:
                print("MAKERSENCE_PRIVATE_PROXY_REJECT",json.dumps({
                  "host":raw_host,
                  "forwarded_host":forwarded_host,
                  "forwarded_proto":forwarded_proto,
                  "client":client,
                  "internal_host":internal_host,
                  "private_client":private_client,
                  "no_public_forwarding":no_public_forwarding
                },sort_keys=True),flush=True)
            except Exception:pass
        return ok
    def do_GET(self):
        p=urlparse(self.path).path
        if p=="/health":
            return self.json(200,{"ok":True,"service":"makersence-slicer-worker","version":"1.1.11","engine":"Bambu Studio","engine_version":BAMBU_VERSION,
              "bambu_available":bool(BAMBU_BIN and pathlib.Path(BAMBU_BIN).exists()),"xvfb_available":bool(shutil.which("Xvfb")),
              "weston_available":bool(shutil.which("weston")),"glxinfo_available":bool(shutil.which("glxinfo")),
              "display_mode":DISPLAY_MODE,"x11_ready":bool(pathlib.Path("/tmp/.X11-unix/X99").exists()),"wayland_ready":bool((RUNTIME/WAYLAND_SOCKET).exists()),
              "display_ready":bool(pathlib.Path("/tmp/.X11-unix/X99").exists() and (DISPLAY_MODE=="x11_flatpak" or (RUNTIME/WAYLAND_SOCKET).exists())),
              "osmesa_available":bool(ctypes.util.find_library("OSMesa")),"bambu_bin":BAMBU_BIN,
              "home":os.environ.get("HOME"),"xdg_data_home":os.environ.get("XDG_DATA_HOME"),
              "xdg_runtime_dir":os.environ.get("XDG_RUNTIME_DIR"),"flatpak_user_dir":os.environ.get("FLATPAK_USER_DIR")})
        if not self.auth():return self.json(401,{"error":"unauthorized"})
        if p.startswith("/v1/slice-jobs/"):
            jid=p.split("/")[-1];j=JOBS.get(jid)
            return self.json(200,{"job_id":jid,**j}) if j else self.json(404,{"error":"slice job not found"})
        if p.startswith("/v1/slice-artifacts/"):
            parts=p.strip("/").split("/")
            if len(parts)!=4 or parts[-1]!="bambu_sliced.3mf":return self.json(404,{"error":"not found"})
            jid=parts[2];j=JOBS.get(jid);fp=pathlib.Path(j.get("folder",""))/"bambu_sliced.3mf" if j else pathlib.Path("/missing")
            if not fp.exists():return self.json(404,{"error":"not found"})
            data=fp.read_bytes();self.send_response(200);self.send_header("Content-Type","model/3mf");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data);return
        return self.json(404,{"error":"not found"})
    def do_POST(self):
        p=urlparse(self.path).path
        if p=="/v1/design-model/generate":
            if not self.private_request():return self.json(403,{"error":"private network only"})
            n=int(self.headers.get("Content-Length") or 0)
            if n<=0 or n>131072:return self.json(400,{"error":"invalid json size"})
            try:
                payload=json.loads(self.rfile.read(n).decode("utf-8"))
                data,sha,provider_version=triposg_proxy(payload)
                self.send_response(200)
                self.send_header("Content-Type","model/gltf-binary")
                self.send_header("Content-Length",str(len(data)))
                self.send_header("X-MakerSence-Artifact-Sha256",sha)
                self.send_header("X-MakerSence-Provider","modal_triposg")
                if provider_version:self.send_header("X-MakerSence-Provider-Version",provider_version)
                self.end_headers();self.wfile.write(data);return
            except Exception as e:
                safe=str(e)
                if len(safe)>240:safe=safe[:240]
                print("MAKERSENCE_MODAL_PROXY_ERROR",json.dumps({
                  "type":type(e).__name__,
                  "error":safe
                },ensure_ascii=False,sort_keys=True),flush=True)
                return self.json(502,{"error":safe})
        if not self.auth():return self.json(401,{"error":"unauthorized"})
        if p!="/v1/slice":return self.json(404,{"error":"not found"})
        n=int(self.headers.get("Content-Length") or 0)
        if n<64 or n>32_000_000:return self.json(400,{"error":"invalid 3mf size"})
        try:return self.json(202,submit(self.rfile.read(n),self.headers.get("X-Idempotency-Key")))
        except Exception as e:return self.json(422,{"error":str(e)})

if __name__=="__main__":
    print("MakerSence Slicer Worker 1.1.11",PORT,"Bambu",BAMBU_VERSION,"OSMesa",ctypes.util.find_library("OSMesa"),"display_mode",DISPLAY_MODE,"thumbnail_size=0x0 skip-useless-pick=1 min-save=1",flush=True)
    if not ensure_display():
        raise RuntimeError("Headless display initialization failed; see /tmp/xvfb.log, /tmp/glxinfo.log and /tmp/display-init-error.log")
    ThreadingHTTPServer(("0.0.0.0",PORT),H).serve_forever()