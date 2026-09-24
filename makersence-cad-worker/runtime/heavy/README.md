# MakerSence Heavy Runtime Snapshot

Snapshot date: 2026-09-25

Purpose: preserve the currently deployed Heavy / Universal CAD / Hybrid Blender / Mechanism Motion worker source before source-of-truth consolidation.

Source provenance:
- Hatchable: public/worker/app.py
- Railway runtime role: cad-worker-v2
- Expected runtime version: 2.51.0-motion-exact-prefilter

This snapshot is NOT yet the production deployment source.
Do not point Railway production traffic here until parity/regression tests pass.

Contained roles:
- Universal CAD / source reconstruction
- Hybrid / Blender execution
- multipart / assembly logic
- mechanism pose and motion validation
- exact/bbox collision prefilter
- isolated subprocess / timeout guard

Operational rule:
- Keep Heavy concurrency at 1 until memory regression proves a higher value is safe.
