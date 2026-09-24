# MakerSence Runtime Source Layout

Consolidation branch created: 2026-09-25

This branch is a source-of-truth staging branch. It is NOT connected to production traffic yet.

## Runtime roles

### CAD Core
Path: runtime/cad-core-v5/
Current snapshot: 2.10.5-universal-static
Role: normal CAD generation, source analysis, text/geometry QA, printable artifact generation.

### Heavy CAD / Blender / Motion
Path: runtime/heavy/
Current snapshot: 2.51.0-motion-exact-prefilter
Role: Universal CAD, complex reconstruction, Hybrid/Blender, multipart assembly, mechanism pose/motion validation.

### Dedicated Bambu Slicer
Path: runtime/slicer/
Current snapshot: 1.1.11
Role: real Bambu Studio slicing, sliced 3MF and toolpath evidence.

### Engineering Adapter
Current existing repository implementation remains on the main repository code path and is not replaced by these snapshots yet.

## Governance

- GitHub is the intended source of truth.
- No new source code should be stored in Railway WORKER_SRC_*, SLICER_SRC_*, APP_PY_BASE64 or hotfix variables.
- Production Railway services must NOT be repointed to this branch until parity and regression tests pass.
- Heavy concurrency remains 1 until memory tests prove otherwise.
- Existing production services remain the rollback source during migration.
