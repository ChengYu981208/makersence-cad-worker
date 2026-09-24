import json, os, time, math
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE="http://127.0.0.1:"+str(os.environ.get("PORT","8080"))
TOKEN=os.environ.get("WORKER_TOKEN","")

def req(path, method="GET", body=None, auth=True):
    headers={"Accept":"application/json"}
    if auth:
        headers["Authorization"]="Bearer "+TOKEN
    data=None
    if body is not None:
        data=json.dumps(body,separators=(",",":")).encode()
        headers["Content-Type"]="application/json"
    r=Request(BASE+path,data=data,headers=headers,method=method)
    with urlopen(r,timeout=20) as x:
        return json.loads(x.read().decode())

def circle(r,n=48):
    out=[]
    for i in range(n):
        a=math.pi*2*i/n
        out.append([round(math.cos(a)*r,3),round(math.sin(a)*r,3)])
    return out

def main():
    health=req("/health",auth=False)
    caps=health.get("capabilities") or []
    sections=[
      {"at_mm":0.5,"profile_loops":[circle(18)]},
      {"at_mm":8,"profile_loops":[circle(21)]},
      {"at_mm":15,"profile_loops":[circle(23)]},
      {"at_mm":22,"profile_loops":[circle(21)]},
      {"at_mm":29.5,"profile_loops":[circle(18)]},
    ]
    plan={
      "version":"universal-cad-recipe-shadow-selftest",
      "status":"READY","executor_ready":True,"reconstruction_mode":"SECTION_LOFT_RECONSTRUCTION",
      "geometry_evidence":{
        "dimensions_mm":[46,46,30],"source_part_count":1,"reconstruction_axis":"Z",
        "reconstruction_section_count":5,"reconstruction_topology_stability":1,
        "section_loop_counts":[1,1,1,1,1],"section_loop_correspondence":True,
        "single_part_executor":True
      },
      "hard_blockers":[],
      "feature_graph":{"version":"universal-feature-graph-v1","nodes":[
        {"id":"OVERALL_ENVELOPE","type":"measurement_envelope","dimensions_mm":[46,46,30],"operation":"CONSTRAINT_ONLY"},
        {"id":"SECTION_STACK","type":"section_stack","operation":"LOFT_OR_SWEEP","axis":"Z","sections":sections}
      ]}
    }
    cad={
      "family":"universal_cad_recipe","universal_recipe":plan,
      "wall_mm":1.2,"thickness_mm":1.2,"xy_clearance_mm":0.25,
      "mesh_tolerance_mm":0.06,"body_color":"#6d7480",
      "expected_parts":[{"name":"BODY","role":"main_body","physical_separate":True,"editable_separate":True}],
      "part_manifest":[{"name":"BODY","role":"main_body","function":"reconstructed primary body","geometry_strategy":"section loft","physical_separate":True,"editable_separate":True}],
      "enforce_part_intent":False
    }
    key="shadow-heavy-"+str(int(time.time()*1000))
    payload={
      "module":"universal_shadow_selftest","module_version":"v1",
      "printer_profile":"bambu_a1_mini_04","idempotency_key":key,
      "appearance_hash":key,"appearance_lock":{"appearance_hash":key},
      "svg_artifact":{"parts":[]},"cad_contract":cad,
      "parameters":{"geometry_recipe":cad,"manufacturing_profile":{
        "min_feature_mm":0.8,"max_colors":4,"nozzle_mm":0.4,
        "build_volume_mm":[180,180,180],"xy_clearance_mm":0.25
      }}
    }
    submit=req("/v1/generate","POST",payload)
    jid=submit.get("job_id")
    if not jid:
        print("SHADOW_SMOKE_RESULT="+json.dumps({"status":"FAIL","stage":"submit","submit":submit},ensure_ascii=False),flush=True)
        return 2
    job=None
    for _ in range(90):
        job=req("/v1/jobs/"+jid)
        if str(job.get("status","")) in ("completed","failed"):
            break
        time.sleep(0.5)
    v=(job or {}).get("validation") or {}
    arts=(job or {}).get("artifacts") or []
    checks={
      "worker_completed":(job or {}).get("status")=="completed",
      "validation_pass":str(v.get("status","")).upper()=="PASS",
      "brep_valid":v.get("brep_valid") is True,
      "watertight":v.get("watertight") is True,
      "open_edges_zero":int(v.get("open_edges") or 0)==0,
      "exported_open_edges_zero":int(v.get("exported_3mf_open_edges") or 0)==0,
      "preview_matches_export":v.get("preview_matches_export") is True,
      "a1_mini_fit":v.get("a1_mini_fit") is True,
      "contract_dimensions_ok":v.get("contract_dimensions_ok") is True,
      "has_3mf":any(str(x.get("type","")).lower()=="3mf" for x in arts),
      "has_step":any(str(x.get("type","")).lower()=="step" for x in arts),
      "has_glb":any(str(x.get("type","")).lower()=="glb" for x in arts),
      "blender_available":bool((health.get("blender") or {}).get("available")),
      "motion_capability":"mechanism_motion_solver_v3" in caps,
      "universal_capability":"universal_cad_recipe_v2" in caps,
    }
    passed=all(checks.values())
    result={
      "status":"PASS" if passed else "FAIL",
      "worker_version":health.get("version"),
      "job_status":(job or {}).get("status"),
      "error":(job or {}).get("error"),
      "checks":checks,
      "product_dimensions_mm":v.get("product_dimensions_mm"),
      "artifact_types":[x.get("type") for x in arts]
    }
    print("SHADOW_SMOKE_RESULT="+json.dumps(result,separators=(",",":"),ensure_ascii=False),flush=True)
    return 0 if passed else 3

if __name__=="__main__":
    raise SystemExit(main())
