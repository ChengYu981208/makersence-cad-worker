import io, json, os, time, zipfile
from urllib.request import Request, urlopen

BASE="http://127.0.0.1:"+str(os.environ.get("PORT","8080"))
TOKEN=os.environ.get("SLICER_TOKEN","")

def json_req(path, method="GET", body=None, headers=None, timeout=30):
    h={"Accept":"application/json","Authorization":"Bearer "+TOKEN}
    if headers: h.update(headers)
    r=Request(BASE+path,data=body,headers=h,method=method)
    with urlopen(r,timeout=timeout) as x:
        return json.loads(x.read().decode())

def make_cube_3mf():
    content_types='''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>'''
    rels='''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>'''
    model='''<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <metadata name="Title">MakerSence Slicer Shadow Cube</metadata>
  <resources>
    <object id="1" type="model" name="SHADOW_CUBE">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0"/><vertex x="20" y="0" z="0"/>
          <vertex x="20" y="20" z="0"/><vertex x="0" y="20" z="0"/>
          <vertex x="0" y="0" z="10"/><vertex x="20" y="0" z="10"/>
          <vertex x="20" y="20" z="10"/><vertex x="0" y="20" z="10"/>
        </vertices>
        <triangles>
          <triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="3" v3="2"/>
          <triangle v1="4" v2="5" v3="6"/><triangle v1="4" v2="6" v3="7"/>
          <triangle v1="0" v2="1" v3="5"/><triangle v1="0" v2="5" v3="4"/>
          <triangle v1="1" v2="2" v3="6"/><triangle v1="1" v2="6" v3="5"/>
          <triangle v1="2" v2="3" v3="7"/><triangle v1="2" v2="7" v3="6"/>
          <triangle v1="3" v2="0" v3="4"/><triangle v1="3" v2="4" v3="7"/>
        </triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1"/></build>
</model>'''
    b=io.BytesIO()
    with zipfile.ZipFile(b,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",content_types)
        z.writestr("_rels/.rels",rels)
        z.writestr("3D/3dmodel.model",model)
    return b.getvalue()

def main():
    with urlopen(BASE+"/health",timeout=10) as r:
        health=json.loads(r.read().decode())
    data=make_cube_3mf()
    headers={
      "Authorization":"Bearer "+TOKEN,
      "Content-Type":"model/3mf",
      "Content-Length":str(len(data)),
      "X-Idempotency-Key":"slicer-shadow-"+str(int(time.time()*1000))
    }
    r=Request(BASE+"/v1/slice",data=data,headers=headers,method="POST")
    with urlopen(r,timeout=20) as x:
        submit=json.loads(x.read().decode())
    jid=submit.get("job_id")
    if not jid:
        print("SLICER_SHADOW_SMOKE_RESULT="+json.dumps({"status":"FAIL","stage":"submit"},separators=(",",":")),flush=True)
        return 2
    job=None
    for _ in range(360):
        job=json_req("/v1/slice-jobs/"+jid)
        if str(job.get("status","")) in ("completed","failed"):
            break
        time.sleep(1)
    v=(job or {}).get("validation") or {}
    artifact_ok=False
    artifact_bytes=0
    artifact_zip=False
    if (job or {}).get("artifact"):
        try:
            req=Request(BASE+job["artifact"],headers={"Authorization":"Bearer "+TOKEN})
            with urlopen(req,timeout=30) as x:
                out=x.read()
            artifact_bytes=len(out)
            artifact_zip=out[:2]==b"PK"
            artifact_ok=artifact_bytes>64 and artifact_zip
        except Exception:
            pass
    checks={
      "version":health.get("version")=="1.1.11",
      "bambu_available":health.get("bambu_available") is True,
      "xvfb_available":health.get("xvfb_available") is True,
      "glxinfo_available":health.get("glxinfo_available") is True,
      "display_mode":health.get("display_mode")=="x11_flatpak",
      "x11_ready":health.get("x11_ready") is True,
      "display_ready":health.get("display_ready") is True,
      "osmesa_available":health.get("osmesa_available") is True,
      "job_completed":(job or {}).get("status")=="completed",
      "validation_pass":str(v.get("status","")).upper()=="PASS",
      "real_slicer_verified":v.get("real_slicer_verified") is True,
      "toolpath_checked":v.get("toolpath_checked") is True,
      "output_nonempty":int(v.get("output_3mf_bytes") or 0)>64,
      "toolpath_packaged":v.get("deterministic_toolpath_package") is True,
      "artifact_valid":artifact_ok
    }
    passed=all(checks.values())
    result={
      "status":"PASS" if passed else "FAIL",
      "worker_version":health.get("version"),
      "engine_version":health.get("engine_version"),
      "job_status":(job or {}).get("status"),
      "error":(job or {}).get("error"),
      "returncode":(job or {}).get("returncode"),
      "checks":checks,
      "native_slice_completed":v.get("native_slice_completed"),
      "package_mode":v.get("package_mode"),
      "recovered_bambu_toolpath":v.get("recovered_bambu_toolpath"),
      "output_3mf_bytes":v.get("output_3mf_bytes"),
      "artifact_bytes":artifact_bytes
    }
    print("SLICER_SHADOW_SMOKE_RESULT="+json.dumps(result,separators=(",",":"),ensure_ascii=False),flush=True)
    return 0 if passed else 3

if __name__=="__main__":
    raise SystemExit(main())
