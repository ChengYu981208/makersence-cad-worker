# Checkpoint — Design Model Router Step 1

Date: 2026-09-26
Branch: consolidation/runtime-snapshots-20260925

## Goal

Introduce the first reversible integration seam for external generative 3D tooling without changing existing production routing or silently falling back to primitive CAD.

Target provider:
- modal_triposg
- TripoSG image-to-3D
- Modal GPU endpoint

## Architecture decision

The design-model provider returns GLB binary directly to Heavy Worker.

Reason:
- avoids adding a second object-storage service solely to exchange generated GLB files
- avoids base64 inflation for large meshes
- lets Heavy Worker receive the exact GLB bytes and continue through Trimesh / Manifold / Blender / CAD validation
- keeps provider failure isolated and fail-closed

Contract:
- request: JSON
- provider: modal_triposg
- mode: image_to_3d
- source_image_url: HTTPS only; local/private addresses forbidden
- output_format: glb
- target_faces: bounded
- response on success: model/gltf-binary
- GLB magic and size validated
- SHA-256 calculated and optionally cross-checked against provider response header
- missing provider/config/invalid output: fail closed

## Source commits

- 238c9c89db2c07f10c1a73f96ece3adce60c4fda
  - add runtime/heavy/design_model_router.py
- 558294fc50069d13eb9914007d893dad6dcd11bd
  - include router in Heavy Docker image
- 0a17c56ef9b3051d57373abe284cad6d811208dc
  - expose /v1/design-model/generate and provider health capability
- 484a8179a4396ca723c834a936098d53ecc6ea88
  - shadow fail-closed test
- 51abff3585c53bf0089d7b1e64be70928d5ac664
  - Runtime Source Check updated for 2.61.0 design-model-router
- b779105e772ee9756ea98646bec553c5aa8866da
  - switch provider contract to direct GLB binary
- 67ff38cd92d6e2309dbf797062a7d11ea51e15c8
  - Heavy endpoint streams GLB binary
- 4845421b55fccb378e7bceb9729bf1310ac90122
  - add positive mocked binary provider contract test

## Validation

Runtime Source Check: PASS
- Python syntax PASS
- design_model_router.py compile PASS
- Design model router mocked positive contract PASS
- version markers PASS
- generic anti-regression PASS
- design-model source guards PASS

worker-tests: PASS

Ephemeral Runtime Shadow: ALL PASS
- Engineering tooling: PASS
- Heavy: PASS
  - build PASS
  - health/capability PASS
  - Universal CAD real job PASS
  - Generic Blender real job PASS
  - Design model provider unconfigured fail-closed PASS
  - Unknown family fail-closed PASS
- CAD Core v5: PASS
  - Universal CAD real job PASS
  - real Bambu Studio slice PASS
  - unknown-family fail-closed PASS

## Production status

NOT DEPLOYED.

The new design-model router exists only on the consolidation branch and in ephemeral validation.
Railway production Heavy Worker v2 has not been changed in this step.
No Modal endpoint/token has been added to Railway.

## Safety guarantees at this checkpoint

- Existing Universal CAD is unchanged.
- Existing Generic Blender is unchanged.
- No product-specific automatic routing was added.
- No request is automatically sent to TripoSG.
- Modal must be explicitly selected.
- Missing Modal endpoint/token fails closed.
- Invalid/private source URLs fail closed.
- Invalid GLB responses fail closed.
- No primitive/plate fallback is allowed from design-model provider failure.

## Next step

Create the Modal TripoSG GPU endpoint scaffold with the exact binary contract above:
1. authenticated request
2. safe HTTPS image fetch
3. run official TripoSG image-to-3D inference
4. bounded target face count
5. GLB output
6. SHA-256 response header
7. provider version response header
8. health/self-test path
9. no Railway deployment until Modal endpoint validation succeeds
