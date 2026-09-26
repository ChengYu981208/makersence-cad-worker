# Checkpoint — 2026-09-26 — Disable silent legacy CAD fallback

## Scope completed
- Audited the live Railway topology before modifying production.
- Confirmed production Hub worker split still includes:
  - cad-worker-v5: CAD Core
  - cad-worker-v4: engineering adapters
  - cad-worker-v2: heavy / universal / hybrid
  - cad-worker: slicer
- Found one existing staged Railway environment patch affecting cad-worker-v2. It was intentionally left untouched.

## Root cause fixed in source
The engineering adapter registry previously routed every unknown or unspecified generation strategy to LEGACY.
LegacyAdapter also defaulted missing family to silhouette_plate.

That meant an unknown future product could silently become a generic plate instead of failing engineering preparation.

## Source changes
Branch: consolidation/runtime-snapshots-20260925

Commits:
- 1285995af9cf80bdd864511620929aa43c06e267 — disable silent legacy CAD fallback
- 12d8d9153300913e6dd1b71ecb3263832f19217f — require explicit legacy family
- b8296d375750e86d6233ab6998782375b35e70a5 — regression tests

Behavior now in branch source:
- INTERFACE_LOCKED_CAD / INTERFACE_MECHANISM_CAD / MECHANISM_CAD remain explicit.
- Legacy compatibility remains available only through LEGACY or LEGACY_CAD.
- Missing generation strategy fails closed.
- Missing legacy family fails closed.
- Regression test verifies unknown future product does not fall back to silhouette_plate.

The same safety changes were also mirrored to main:
- d190ced06c11c87675851b740e57b51241dda4a0
- d47eebbf00891732b0f8bad0467808af883cf047
- afcee5bc6f47d2c988b36ebb2a296a4a9c105e4d

## Deployment status
cad-worker-v4 is still running commit f376c166b53edc6cc95dafe4e3a50739f83b8922.

A Railway redeploy completed successfully, but Railway reused the old pinned snapshot. It did NOT deploy b8296d375750e86d6233ab6998782375b35e70a5.

Railway reports cad-worker-v4 source commitSha is pinned and there is an unrelated staged environment patch:
- patch: 123092ae-50da-448f-9cd3-ac52a0d47864
- affected service: cad-worker-v2

Do not discard or accept that v2 patch blindly. Its exact before/after fields could not be retrieved through the available Railway API.

## Current safety status
- Source fix: COMPLETE
- Regression coverage in source: COMPLETE
- Current production v4: HEALTHY but still OLD SNAPSHOT
- v2 staged change: PRESERVED / NOT TOUCHED
- v5: NOT TOUCHED

## Next safe step
1. Resolve or identify the existing cad-worker-v2 staged patch without overwriting work.
2. Remove the cad-worker-v4 commit pin / reattach it to the latest HEAD of consolidation/runtime-snapshots-20260925.
3. Deploy b8296d375750e86d6233ab6998782375b35e70a5 to v4.
4. Require Railway preDeploy pytest to pass.
5. Recheck /health and engineering adapter behavior.
6. Continue audit for remaining product-specific or silent fallback routes only after this checkpoint is live.


## Update — CI and shadow validation complete

GitHub CI infrastructure was corrected by adding a repository-root workflow:
- commit 60d24bd94fa13c9fb41633aa2b485ca7c724336b — root worker-tests workflow
- pytest result: 8 passed, 1 warning

Full ephemeral runtime shadow was re-triggered at:
- commit b63aea78452a87c161b71f84513445d9117a87aa

Validation result: ALL GREEN
- Shadow engineering tooling: SUCCESS
  - engineering image build
  - health/tooling selftest
  - Manifold/lib3mf/Inkscape path
  - SVG normalize real request
- Shadow heavy: SUCCESS
  - health/capability gate
  - Universal CAD real job
  - Generic Blender profile real job
  - unknown family fail-closed
- Shadow cad-core-v5: SUCCESS
  - health/capability gate
  - Universal CAD real job
  - generated 3MF/STEP/GLB path
  - isolated Bambu Studio real slicing
  - unknown family fail-closed

## Railway cleanup

The stale environment patch was inspected before action.
- cad-worker-v2 staged content was only source.commitSha=null.
- accidental cad-worker-v3 staged fields from this session were never applied.
- the staged patch was discarded without deployment.
- live v2/v3 configs remained unchanged and all services remained healthy.

## Current Railway staged state

A new v4-only change is staged, NOT DEPLOYED:
- service: cad-worker-v4
- only staged field: source.commitSha = null
- live v4 remains pinned to 6abbf3d7e3e285f5f3241038adecff14baf512bb until deployment is explicitly accepted.
- branch remains consolidation/runtime-snapshots-20260925
- rootDirectory remains makersence-cad-worker
- Dockerfile/start command/preDeploy pytest/healthcheck/variables/domain are unchanged.

## Deployment gate

All source tests and ephemeral runtime checks are green. The next action is an explicit production deployment acceptance for the v4-only staged commit-pin removal. Do not add other staged changes before that deploy.
