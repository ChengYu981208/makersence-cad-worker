from __future__ import annotations
import os
from pathlib import Path
import httpx
from fastapi import FastAPI, Request, HTTPException, Depends, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app import jobs
from app.assets import put_asset, get_asset
from app.analyzers import analyze_bytes

VERSION='3.0.0-interface-adapters'
app=FastAPI(title='MakerSence CAD Worker',version=VERSION)
_bearer=HTTPBearer(auto_error=False)

def auth(c:HTTPAuthorizationCredentials|None=Depends(_bearer)):
    token=os.getenv('WORKER_TOKEN','')
    if token and (c is None or c.credentials!=token):raise HTTPException(401,'unauthorized')
    return True

@app.get('/health')
def health(_:bool=Depends(auth)):
    return {
        'ok':True,'service':'makersence-cad-worker','version':VERSION,'engine':'cadquery+trimesh+shapely',
        'capabilities':['compact_step_brep','detachable_parts','assembly_render','product_dimensions','open_edges_zero_gate','formal_mesh_render','artifact_reaudit','source_asset_upload','source_interface_section_extraction','INTERFACE_LOCKED_CAD','INTERFACE_MECHANISM_CAD','MECHANISM_CAD','legacy_adapter_bridge'],
        'profiles':['bambu_a1_mini_04'],
        'adapters':{
            'INTERFACE_LOCKED_CAD':'ready','INTERFACE_MECHANISM_CAD':'ready','MECHANISM_CAD':'ready'
        }
    }

@app.post('/v1/generate')
async def generate(req:Request,_:bool=Depends(auth)):
    try:payload=await req.json()
    except Exception:raise HTTPException(400,'invalid json')
    return jobs.submit(payload)

@app.get('/v1/jobs/{job_id}')
def get_job(job_id:str,_:bool=Depends(auth)):
    try:return jobs.get(job_id)
    except KeyError:raise HTTPException(404,'job not found')

@app.get('/v1/artifacts/{job_id}/{name}')
def artifact(job_id:str,name:str,_:bool=Depends(auth)):
    try:p=jobs.artifact_path(job_id,name)
    except (KeyError,FileNotFoundError):raise HTTPException(404,'artifact not found')
    ctype={'.stl':'model/stl','.step':'application/step','.3mf':'model/3mf','.glb':'model/gltf-binary','.png':'image/png','.json':'application/json'}.get(p.suffix,'application/octet-stream')
    return FileResponse(p,media_type=ctype,filename=p.name)

@app.post('/v1/reference-assets')
async def reference_asset(req:Request,format:str=Query(...),_:bool=Depends(auth)):
    data=await req.body()
    if len(data)>80*1024*1024:raise HTTPException(413,'reference asset too large')
    try:
        a=put_asset(data,format);analysis=analyze_bytes(data,format)
        return {'asset_id':a.asset_id,'format':a.format,'size':a.size,'analysis':analysis}
    except Exception as e:raise HTTPException(400,str(e))

@app.post('/v1/analyze-source-file')
async def analyze_source(req:Request,format:str=Query(...),_:bool=Depends(auth)):
    data=await req.body()
    if len(data)>80*1024*1024:raise HTTPException(413,'source file too large')
    try:return analyze_bytes(data,format)
    except Exception as e:raise HTTPException(400,str(e))

@app.post('/v1/analyze-3mf-url')
async def analyze_3mf_url(req:Request,_:bool=Depends(auth)):
    body=await req.json();url=str(body.get('url') or '')
    if not url.startswith('https://'):raise HTTPException(400,'https url required')
    try:
        async with httpx.AsyncClient(timeout=60,follow_redirects=True) as client:
            r=await client.get(url);r.raise_for_status();data=r.content
        if len(data)>80*1024*1024:raise HTTPException(413,'3MF too large')
        return analyze_bytes(data,'3mf')
    except HTTPException:raise
    except Exception as e:raise HTTPException(502,f'3MF download/analyze failed: {e}')
