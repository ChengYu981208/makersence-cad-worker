# Railway migration plan

The current production service reconstructs `/tmp/app.py` from `WORKER_SRC_*` variables. The connected Railway OAuth session exposes variable names but redacts their values, so the current source cannot be copied byte-for-byte through this session.

This repository is therefore a compatibility reconstruction based on the Hub's real HTTP contract plus the required new adapters. Do not cut over production until shadow validation passes.

## Phase 1 — GitHub / shadow service
1. Create private repository `ChengYu981208/makersence-cad-worker`.
2. Push this tree to `main`.
3. In Railway, create a new service from that repository, suggested name `cad-worker-v6-shadow`.
4. Set the same `WORKER_TOKEN` as the current CAD Worker.
5. Do **not** change Hub `cad_worker_url` yet.

## Phase 2 — shadow verification
Verify:
- `GET /health` returns `ok=true` and all three new adapters are `ready`.
- local/CI pytest passes.
- Hub production self-test payload succeeds against shadow worker.
- reference asset upload -> interface generation works.
- artifacts are downloadable and validation reports `watertight=true`, `open_edges=0`.

## Phase 3 — Hub integration
Hub should upload/persist a MakerWorld 3MF/STL into `/v1/reference-assets`, store the returned `reference_asset_id` in the engineering source record, then pass it to the adapter CAD contract.

Feature-gate this behavior on `/health.capabilities` containing `source_asset_upload` and the requested adapter ID. Old v5 remains compatible until the flag is present.

## Phase 4 — cutover
1. Change only Hub `cad_worker_url` to the shadow service URL.
2. Leave `slicer_worker_url` unchanged (it is a separate service).
3. Run Production Selftest + Product Contract + Routing Benchmark + Photo NFC selftest.
4. Run Development #25 in engineering mode.
5. Keep old `cad-worker-v5` running for rollback.

## Phase 5 — cleanup
After stable operation, remove `WORKER_SRC_0..6`, `WORKER_SRC_CHUNKS` and the env-based source reconstruction start command. Keep secrets only as secrets (`WORKER_TOKEN`, etc.).
