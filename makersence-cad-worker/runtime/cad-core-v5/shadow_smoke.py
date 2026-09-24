import json, os, time
from urllib.request import Request, urlopen

BASE="http://127.0.0.1:"+str(os.environ.get("PORT","8080"))
TOKEN=os.environ.get("WORKER_TOKEN","")

def req(path, method="GET", body=None, auth=True, timeout=30):
    headers={"Accept":"application/json"}
    if auth:
        headers["Authorization"]="Bearer "+TOKEN
    data=None
    if body is not None:
        data=json.dumps(body,separators=(",",":")).encode()
        headers["Content-Type"]="application/json"
    r=Request(BASE+path,data=data,headers=headers,method=method)
    with urlopen(r,timeout=timeout) as x:
        return json.loads(x.read().decode())

def main():
    health=req("/health",auth=False)
    h="makersence-core-shadow-v1"
    payload={
      "module":"system_selftest","module_version":"shadow-v1","printer_profile":"bambu_a1_mini_04",
      "idempotency_key":"core-shadow-"+str(int(time.time()*1000)),"appearance_hash":h,
      "appearance_lock":{"appearance_hash":h,"version":1,"protected_cad_fields":["family","width_mm","height_mm"]},
      "svg_artifact":{"version":1,"view_box":[0,0,60,38],"design_box_mm":[60,38],"min_feature_mm":0.8,"max_colors":4,"gradient":False,"vector_hash":"core-shadow-vector-v1","parts":[
        {"id":"BODY","role":"silhouette","color":"#17191a","path_d":"M 0 0 L 60 0 L 60 38 L 0 38 Z","min_feature_mm":2.4,"z_mm":0,"height_mm":0.5,"stroke_width_mm":None},
        {"id":"HOLE","role":"through_hole","color":"#17191a","path_d":"M 4.4 7 A 2.6 2.6 0 1 0 9.6 7 A 2.6 2.6 0 1 0 4.4 7 Z","min_feature_mm":5.2,"z_mm":0,"height_mm":0.5,"stroke_width_mm":None},
        {"id":"ACCENT","role":"emboss","color":"#f1efe8","path_d":"M 18 14 L 42 14 L 42 24 L 18 24 Z","min_feature_mm":10,"z_mm":0,"height_mm":0.6,"stroke_width_mm":None}
      ]},
      "cad_contract":{
        "family":"silhouette_plate","width_mm":60,"height_mm":38,"thickness_mm":2.4,
        "product_dimensions_mm":[60,38,3.6],"wall_mm":2.4,"xy_clearance_mm":0.25,
        "holes":[],"pockets":[],"clearances":[],
        "part_manifest":[
          {"name":"BODY","role":"main_body","physical_separate":True,"editable_separate":True},
          {"name":"ACCENT","role":"player_ui","physical_separate":False,"editable_separate":True},
          {"name":"TEST_FRAME","role":"photo_frame","physical_separate":True,"editable_separate":True}
        ],
        "expected_parts":[
          {"name":"BODY","role":"main_body","physical_separate":True,"editable_separate":True},
          {"name":"ACCENT","role":"player_ui","physical_separate":False,"editable_separate":True},
          {"name":"TEST_FRAME","role":"photo_frame","physical_separate":True,"editable_separate":True}
        ],
        "separate_parts":[
          {"name":"TEST_FRAME","role":"photo_frame","kind":"rect_frame","outer_width_mm":20,"outer_height_mm":20,"inner_width_mm":16,"inner_height_mm":16,"thickness_mm":1.2,"min_feature_mm":2,"color":"#17191a","print_x_mm":42,"print_y_mm":0,"assembly_translate":[-42,0,-1.2]}
        ],
        "protected_fields":["family","width_mm","height_mm"]
      },
      "parameters":{
        "geometry_recipe":{"family":"silhouette_plate","width_mm":60,"height_mm":38,"thickness_mm":2.4,"wall_mm":2.4,"xy_clearance_mm":0.25},
        "manufacturing_profile":{"printer_profile":"bambu_a1_mini_04","build_volume_mm":[180,180,180],"nozzle_mm":0.4,"max_colors":4,"min_feature_mm":0.8,"min_text_stroke_mm":0.55,"text_line_width_mm":0.42,"text_cjk_gap_mm":0.55,"text_latin_gap_mm":0.50,"wall_generator":"arachne","preferred_wall_mm":2.4,"xy_clearance_mm":0.25,"gradient":False}
      }
    }
    submit=req("/v1/generate","POST",payload)
    jid=submit.get("job_id")
    if not jid:
        print("CORE_SHADOW_SMOKE_RESULT="+json.dumps({"status":"FAIL","stage":"submit"},separators=(",",":")),flush=True)
        return 2
    job=None
    for _ in range(90):
        job=req("/v1/jobs/"+jid)
        if str(job.get("status","")) in ("completed","failed"):
            break
        time.sleep(0.5)
    v=(job or {}).get("validation") or {}
    arts=(job or {}).get("artifacts") or []
    types=[str(x.get("type","")).lower() for x in arts]
    b=(health.get("bambu_slicer") or {})
    checks={
      "worker_version":health.get("version")=="2.10.5-universal-static",
      "bambu_available":b.get("available") is True,
      "worker_completed":(job or {}).get("status")=="completed",
      "validation_pass":str(v.get("status","")).upper()=="PASS",
      "brep_valid":v.get("brep_valid") is True,
      "watertight":v.get("watertight") is True,
      "open_edges_zero":int(v.get("open_edges") or 0)==0,
      "exported_open_edges_zero":int(v.get("exported_3mf_open_edges") or 0)==0,
      "part_intent_ok":v.get("part_intent_ok") is True,
      "orphan_geometry_free":v.get("orphan_geometry_free") is True,
      "unintended_through_cut_free":v.get("unintended_through_cut_free") is True,
      "a1_mini_fit":v.get("a1_mini_fit") is True,
      "contract_dimensions_ok":v.get("contract_dimensions_ok") is True,
      "preview_matches_export":v.get("preview_matches_export") is True,
      "native_bambu_parts":((v.get("export_audit") or {}).get("three_mf") or {}).get("native_bambu_parts") is True,
      "render_pass":(v.get("product_render") or {}).get("status")=="PASS",
      "has_3mf":"3mf" in types,
      "has_step":"step" in types,
      "has_glb":"glb" in types
    }
    passed=all(checks.values())
    result={
      "status":"PASS" if passed else "FAIL",
      "worker_version":health.get("version"),
      "bambu_version":b.get("version"),
      "job_status":(job or {}).get("status"),
      "error":(job or {}).get("error"),
      "checks":checks,
      "product_dimensions_mm":v.get("product_dimensions_mm"),
      "artifact_types":types
    }
    print("CORE_SHADOW_SMOKE_RESULT="+json.dumps(result,separators=(",",":"),ensure_ascii=False),flush=True)
    return 0 if passed else 3

if __name__=="__main__":
    raise SystemExit(main())
