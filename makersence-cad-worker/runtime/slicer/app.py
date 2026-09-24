import os, json, time, uuid, pathlib, threading, subprocess, zipfile, re, hashlib, shutil, ctypes.util
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

PORT=int(os.environ.get("PORT","8080"))
TOKEN=os.environ.get("SLICER_TOKEN","")
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
    blocks=0;active=False;z=None;obj=None
    min_z=None;max_z=None;xs=[];ys=[];positive_e=0.0;moves=0
    by_object={};by_z={};mapping_comments=[];seen_comments=set()
    num=r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
    try:
        with open(path,"r",encoding="utf-8",errors="ignore") as f:
            for raw in f:
                line=raw.strip()
                low=line.lower()
                if line.startswith(";") and len(mapping_comments)<160 and any(k in low for k in ("object","label","name","instance")):
                    if line not in seen_comments and len(line)<700:
                        seen_comments.add(line);mapping_comments.append(line)
                m=re.match(r";\s*Z_HEIGHT:\s*("+num+r")",line,re.I)
                if m:
                    try:z=float(m.group(1))
                    except Exception:pass
                m=re.match(r";\s*OBJECT_ID:\s*(\d+)",line,re.I)
                if m:obj=m.group(1)
                m=re.match(r";\s*FEATURE:\s*(.+)",line,re.I)
                if m:
                    is_float="floating" in m.group(1).lower()
                    if is_float and not active:
                        blocks+=1
                        key=obj or "unknown"
                        rec=by_object.setdefault(key,{"blocks":0,"moves":0,"positive_e":0.0,"min_z":None,"max_z":None,"min_x":None,"max_x":None,"min_y":None,"max_y":None})
                        rec["blocks"]+=1
                    active=is_float
                    continue
                if not active:continue
                if z is not None:
                    min_z=z if min_z is None else min(min_z,z);max_z=z if max_z is None else max(max_z,z)
                    zk=("%.3f"%z).rstrip("0").rstrip(".");by_z[zk]=by_z.get(zk,0)+1
                if not re.match(r"G[01]\b",line,re.I):continue
                xv=re.search(r"(?:^|\s)X("+num+r")",line,re.I)
                yv=re.search(r"(?:^|\s)Y("+num+r")",line,re.I)
                ev=re.search(r"(?:^|\s)E("+num+r")",line,re.I)
                x=float(xv.group(1)) if xv else None;y=float(yv.group(1)) if yv else None
                e=float(ev.group(1)) if ev else 0.0
                moves+=1
                if x is not None:xs.append(x)
                if y is not None:ys.append(y)
                if e>0:positive_e+=e
                key=obj or "unknown";rec=by_object.setdefault(key,{"blocks":0,"moves":0,"positive_e":0.0,"min_z":None,"max_z":None,"min_x":None,"max_x":None,"min_y":None,"max_y":None})
                rec["moves"]+=1
                if e>0:rec["positive_e"]+=e
                if z is not None:
                    rec["min_z"]=z if rec["min_z"] is None else min(rec["min_z"],z)
                    rec["max_z"]=z if rec["max_z"] is None else max(rec["max_z"],z)
                for val,lo,hi in ((x,"min_x","max_x"),(y,"min_y","max_y")):
                    if val is not None:
                        rec[lo]=val if rec[lo] is None else min(rec[lo],val)
                        rec[hi]=val if rec[hi] is None else max(rec[hi],val)
        for oid,rec in by_object.items():
            rec["name"]=object_map.get(oid)
            rec["positive_e"]=round(rec["positive_e"],5)
        zs=sorted(float(k) for k in by_z.keys())
        ranges=[]
        if zs:
            a=b=zs[0];count=1
            for zz in zs[1:]:
                if zz-b<=0.21:
                    b=zz;count+=1
                else:
                    ranges.append({"min_z":round(a,3),"max_z":round(b,3),"layers":count});a=b=zz;count=1
            ranges.append({"min_z":round(a,3),"max_z":round(b,3),"layers":count})
        return {"detected":blocks>0,"block_count":blocks,"move_count":moves,"positive_e":round(positive_e,5),
                "min_z":min_z,"max_z":max_z,"distinct_z_count":len(by_z),"z_values":list(by_z.keys())[:300],"z_ranges":ranges,
                "xy_bounds":{"min_x":min(xs) if xs else None,"max_x":max(xs) if xs else None,"min_y":min(ys) if ys else None,"max_y":max(ys) if ys else None},
                "objects":by_object,"object_map":object_map,"mapping_comments":mapping_comments}
    except Exception as e:return {"detected":False,"error":str(e),"object_map":object_map}

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

class H(BaseHTTPRequestHandler):
    server_version="MakerSenceSlicer/1.1.11"
    def log_message(self,fmt,*args):print(fmt%args,flush=True)
    def json(self,code,obj):
        b=json.dumps(obj,ensure_ascii=False).encode();self.send_response(code);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(b)));self.end_headers();self.wfile.write(b)
    def auth(self):
        if not TOKEN:return False
        return self.headers.get("Authorization")=="Bearer "+TOKEN
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