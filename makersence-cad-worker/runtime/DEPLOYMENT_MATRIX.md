# MakerSence Runtime Deployment Matrix

Staging branch: consolidation/runtime-snapshots-20260925
Production traffic: NOT connected

| Role | Repository root | Dockerfile | Port | Health | Production service today |
|---|---|---|---:|---|---|
| Engineering Adapter | makersence-cad-worker | Dockerfile | 8000 | /health | cad-worker-v4 |
| CAD Core | makersence-cad-worker/runtime/cad-core-v5 | Dockerfile | 8000 | /health | cad-worker-v5 |
| Heavy / Blender / Motion | makersence-cad-worker/runtime/heavy | Dockerfile | 8000 | /health | cad-worker-v2 |
| Dedicated Bambu Slicer | makersence-cad-worker/runtime/slicer | Dockerfile | 8080 | /health | cad-worker |

## Required secret/config variables

Engineering Adapter:
- WORKER_TOKEN

CAD Core:
- WORKER_TOKEN

Heavy / Blender / Motion:
- WORKER_TOKEN
- MAKERSENCE_HEAVY_CONCURRENCY=1

Dedicated Bambu Slicer:
- SLICER_TOKEN

Do not copy source chunks into new services.

Forbidden source variables for new deployments:
- WORKER_SRC_*
- SLICER_SRC_*
- APP_PY_BASE64*
- STATIC_FUNCTIONAL_HOTFIX_B64 as hidden code source

## Current exception

The live Engineering Adapter still executes STATIC_FUNCTIONAL_HOTFIX_B64 during startup.
Railway OAuth currently redacts variable values, so the exact hotfix content cannot be recovered automatically.
Do not remove or replace the live engineering service until this behavior is reproduced and regression-tested from repository source.
