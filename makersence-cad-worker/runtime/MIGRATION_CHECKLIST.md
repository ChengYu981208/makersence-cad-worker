# Production Migration Checklist

## Phase A — source preservation
- [x] Heavy / Blender / Motion source preserved in GitHub.
- [x] CAD Core v5 source preserved in GitHub.
- [x] Dedicated Bambu Slicer source preserved in GitHub.
- [x] All three snapshots consolidated on one staging branch.
- [x] GitHub-deployable Docker packaging added for the three recovered runtimes.
- [x] Existing Engineering Adapter source preserved from main.
- [ ] Engineering hidden runtime hotfix fully recovered or behavior replaced by repository code.

## Phase B — shadow deployment
Create NEW non-routed services only. Do not modify current Hub URLs.

Required gates for every shadow service:
- /health returns 200.
- Reported runtime version matches expected role.
- Auth token behavior matches production.
- No source reconstruction from Railway environment variables.
- Restart from cold boot succeeds.

Additional Heavy gates:
- Blender available.
- Universal/source reconstruction works.
- multipart/assembly route works.
- mechanism pose/motion route works.
- heavy concurrency remains 1.

Additional CAD Core gates:
- normal generation works.
- STL/STEP/3MF analysis works.
- text QA works.
- topology gates work.
- Bambu Studio is available where expected.

Additional Slicer gates:
- Bambu Studio 2.8.2.61 detected.
- real slice completes.
- sliced 3MF produced.
- toolpath/gcode evidence produced.

## Phase C — Hub cutover
Only after all shadow tests pass:
1. Change one Hub worker URL at a time.
2. Run production selftest after each role.
3. Keep old service online for rollback.
4. Observe errors/memory before moving the next role.

## Phase D — retirement
Only after stable observation:
- Remove WORKER_SRC_* / SLICER_SRC_* source chunks.
- Remove APP_PY_BASE64* source backups.
- Remove STATIC_FUNCTIONAL_HOTFIX_B64 only after its behavior exists in GitHub source.
- Retire old version-numbered services after rollback window.
