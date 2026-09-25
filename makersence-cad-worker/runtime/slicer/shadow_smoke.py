import json, os, time, zipfile, tempfile, pathlib
from urllib.request import Request, urlopen

BASE="http://127.0.0.1:"+str(os.environ.get("PORT","8080"))
TOKEN=os.environ.get("SLICER_TOKEN","")

def req_json(path, method="GET", body=None, headers=None, timeout=30):
    h={"Accept":"application/json"}
    if headers:h.update(headers)
    if path!="/health":h["Authorization"]="Bearer "+TOKEN
    r=Request(BASE+path,data=body,headers=h,method=method)
    with urlopen(r,timeout=timeout) as x:
        return json.loads(x.read().decode())

def cube_3mf():
    v=[(0,0,0),(20,0,0),(20,20,0),(0,20,0),(0,0,4),(20,0,4),(20,20,4),(0,20,4)]
    t=[(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),(1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
    verts="".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x,y,z in v)
    tris="".join(f'<triangle v1="{a}" v2="{b}" v3="{c}" pid="1" p1="0"/>' for a,b,c in t)
    model='<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources><basematerials id="1"><base name="Color 1" displaycolor="#FFFFFFff"/></basematerials><object id="2" type="model" name="SHADOW_CUBE"><mesh><vertices>'+verts+'</vertices><triangles>'+tris+'</triangles></mesh></object><object id="3" type="model" name="MakerSense Product"><components><component objectid="2"/></components></object></resources><build><item objectid="3"/></build></model>'
    ct='<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'
    rel='<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
    settings='<?xml version="1.0" encoding="UTF-8"?><config><object id="3"><metadata key="name" value="MakerSense Product"/><part id="2" subtype="normal_part"><metadata key="name" value="SHADOW_CUBE"/><metadata key="extruder" value="1"/><metadata key="MakerSenseRole" value="main_body"/><metadata key="MakerSensePhysicalSeparate" value="1"/><metadata key="MakerSenseAssemblyTranslate" value="0 0 0"/></part></object></config>'
    project={
      "filament_colour":["#ffffff"],"filament_type":["PLA"],"wall_generator":"arachne",
      "min_bead_width":"40%","line_width":"0.42","outer_wall_line_width":"0.42",
      "inner_wall_line_width":"0.45","top_surface_line_width":"0.42","detect_thin_wall":"1",
      "MakerSense":"true","MakerSense3MF":"native_parts_v1",
      "MakerSenseArachneMinWallWidthPercent":"40","MakerSenseTypography":"bambu_04_slicer_safe_v3"
    }
    plate={"plate_index":1,"name":"Shadow Plate","objects":[{"object_id":3,"name":"MakerSense Product","parts":[{"part_id":2,"name":"SHADOW_CUBE","role":"main_body","extruder":1}]}],"filaments":[1]}
    fp=pathlib.Path(tempfile.gettempdir())/"makersence-shadow-cube.3mf"
    with zipfile.ZipFile(fp,"w",zipfile.ZIP_DEFLATED,allowZip64=True) as z:
        z.writestr("[Content_Types].xml",ct)
        z.writestr("_rels/.rels",rel)
        z.writestr("3D/3dmodel.model",model)
        z.writestr("Metadata/model_settings.config",settings)
        z.writestr("Metadata/project_settings.config",json.dumps(project,separators=(",",":")))
        z.writestr("Metadata/plate_1.json",json.dumps(plate,separators=(",",":")))
    return fp.read_bytes()

def main():
    h=req_json("/health")
    data=cube_3mf()
    submit=req_json("/v1/slice","POST",data,{"Content-Type":"model/3mf","X-Idempotency-Key":"slicer-shadow-"+str(int(time.time()*1000))},timeout=30)
    jid=submit.get("job_id")
    if not jid:
        print("SLICER_SHADOW_SMOKE_RESULT="+json.dumps({"status":"FAIL","stage":"submit","submit":submit},separators=(",",":")),flush=True)
        return 2
    job=None
    for _ in range(360):
        job=req_json("/v1/slice-jobs/"+jid,timeout=20)
        if str(job.get("status","")) in ("completed","failed"):break
        time.sleep(1)
    v=(job or {}).get("validation") or {}
    artifact_ok=False;artifact_bytes=0
    if (job or {}).get("artifact"):
        try:
            rq=Request(BASE+job["artifact"],headers={"Authorization":"Bearer "+TOKEN})
            with urlopen(rq,timeout=30) as x:
                b=x.read()
            artifact_bytes=len(b);artifact_ok=len(b)>128 and b[:2]==b"PK"
        except Exception:pass
    checks={
      "version":h.get("version")=="1.1.11",
      "engine_version":h.get("engine_version")=="2.8.2.61",
      "bambu_available":h.get("bambu_available") is True,
      "display_ready":h.get("display_ready") is True,
      "x11_ready":h.get("x11_ready") is True,
      "osmesa_available":h.get("osmesa_available") is True,
      "completed":(job or {}).get("status")=="completed",
      "validation_pass":str(v.get("status","")).upper()=="PASS",
      "real_slicer_verified":v.get("real_slicer_verified") is True,
      "toolpath_checked":v.get("toolpath_checked") is True,
      "deterministic_toolpath_package":v.get("deterministic_toolpath_package") is True,
      "output_3mf_nonzero":int(v.get("output_3mf_bytes") or 0)>128,
      "artifact_download_ok":artifact_ok
    }
    passed=all(checks.values())
    result={
      "status":"PASS" if passed else "FAIL",
      "worker_version":h.get("version"),"engine_version":h.get("engine_version"),
      "display_mode":h.get("display_mode"),"job_status":(job or {}).get("status"),
      "error":(job or {}).get("error"),"checks":checks,
      "package_mode":v.get("package_mode"),"native_slice_completed":v.get("native_slice_completed"),
      "native_returncode":v.get("native_returncode"),"output_3mf_bytes":v.get("output_3mf_bytes"),
      "artifact_bytes":artifact_bytes,
      "slicedata":v.get("slicedata"),
      "recovery":v.get("recovery"),
      "log_tail":str((job or {}).get("log_tail") or "")[-8000:]
    }
    print("SLICER_SHADOW_SMOKE_RESULT="+json.dumps(result,separators=(",",":"),ensure_ascii=False),flush=True)
    return 0 if passed else 3

if __name__=="__main__":
    raise SystemExit(main())
