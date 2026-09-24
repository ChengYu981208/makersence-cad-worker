# MakerSence Dedicated Bambu Slicer Snapshot

Snapshot date: 2026-09-25

Purpose: preserve the currently deployed dedicated Bambu Studio slicing service before source-of-truth consolidation.

Source provenance:
- Hatchable: public/worker/slicer_app.py
- Railway runtime role: cad-worker
- Runtime version: 1.1.11
- Bambu Studio target version: 2.8.2.61

This snapshot is NOT yet the production deployment source.
Do not repoint production traffic until parity/regression tests pass.

Responsibilities:
- real Bambu Studio slicing
- sliced 3MF production
- gcode/toolpath evidence
- display/runtime management for Bambu Studio
- single-job concurrency
