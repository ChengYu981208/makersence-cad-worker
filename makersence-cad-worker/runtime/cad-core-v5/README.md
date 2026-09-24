# MakerSence CAD Core v5 Runtime Snapshot

Snapshot date: 2026-09-25

Purpose: preserve the currently deployed CAD Core v5 source before source-of-truth consolidation.

Source provenance:
- Hatchable route: /api/worker-source
- Railway runtime role: cad-worker-v5
- Expected runtime version: 2.10.5-universal-static

This snapshot is NOT yet the production deployment source.
Do not repoint Railway production traffic until parity/regression tests pass.

Primary responsibilities:
- normal CAD generation
- Bambu-native parts / printable geometry
- geometry intent and topology gates
- multilingual/text printability QA
- 3MF/STL/STEP/source analysis
- Universal CAD recipe support used by the normal CAD path

Production routing is unchanged by this snapshot.
