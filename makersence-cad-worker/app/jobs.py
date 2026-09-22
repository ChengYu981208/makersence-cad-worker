from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
import json, os, threading, traceback, uuid
from app.adapters.registry import resolve_adapter
from app.exporters import export_result

ROOT=Path(os.getenv('WORKER_ARTIFACT_DIR','/tmp/makersence-worker-artifacts'));ROOT.mkdir(parents=True,exist_ok=True)
_executor=ThreadPoolExecutor(max_workers=max(1,int(os.getenv('WORKER_MAX_JOBS','2'))))
_lock=threading.Lock();_jobs:dict[str,dict[str,Any]]={};_idem:dict[str,str]={}


def _public(job_id:str,j:dict[str,Any])->dict[str,Any]:
    out={k:v for k,v in j.items() if k not in {'payload','dir'}}
    return out


def submit(payload:dict[str,Any])->dict[str,Any]:
    key=str(payload.get('idempotency_key') or '')
    with _lock:
        if key and key in _idem:
            jid=_idem[key];return _public(jid,_jobs[jid])
        jid=uuid.uuid4().hex
        j={'job_id':jid,'status':'queued','artifacts':[],'validation':{},'error':None,'payload':payload,'dir':str(ROOT/jid)}
        _jobs[jid]=j
        if key:_idem[key]=jid
    _executor.submit(_run,jid)
    return _public(jid,j)


def _run(jid:str):
    with _lock:_jobs[jid]['status']='processing'
    j=_jobs[jid];payload=j['payload'];outdir=Path(j['dir']);outdir.mkdir(parents=True,exist_ok=True)
    try:
        adapter,recipe=resolve_adapter(payload)
        context={'payload':payload,'svg_artifact':payload.get('svg_artifact') or {},'appearance_lock':payload.get('appearance_lock') or {},'classification':payload.get('module') or '', 'reference_asset_id':recipe.get('reference_asset_id')}
        result=adapter.build(recipe,context);result.require_parts()
        artifacts,validation=export_result(result,outdir,payload.get('appearance_hash'))
        for a in artifacts:a['url']=f"/v1/artifacts/{jid}/{a['name']}"
        with _lock:
            j.update({'status':'completed','artifacts':artifacts,'validation':validation,'appearance_hash':payload.get('appearance_hash'),'adapter':result.adapter})
    except Exception as e:
        with _lock:
            j.update({'status':'failed','error':str(e),'trace':traceback.format_exc(limit=8)})


def get(jid:str)->dict[str,Any]:
    with _lock:
        if jid not in _jobs:raise KeyError(jid)
        return _public(jid,_jobs[jid])


def artifact_path(jid:str,name:str)->Path:
    if name not in {'model.stl','model.step','model.3mf','preview.glb','product_render_main.png','manifest.json'}:raise KeyError(name)
    with _lock:
        if jid not in _jobs:raise KeyError(jid)
        p=Path(_jobs[jid]['dir'])/name
    if not p.exists():raise FileNotFoundError(name)
    return p
