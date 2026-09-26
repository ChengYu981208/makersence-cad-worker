# Checkpoint — Modal TripoSG Provider Step 2

Date: 2026-09-26
Branch: consolidation/runtime-snapshots-20260925

## Goal

Create the deployable Modal GPU provider scaffold that matches MakerSence Heavy Worker's design-model binary contract.

## Official upstream basis

- TripoSG source: VAST-AI-Research/TripoSG
- pinned source commit: fc5c40990181e2a756c4e0b1c2f4d6b5202faf8c
- official inference flow: image -> TripoSG -> GLB
- official CLI supports bounded face count via --faces
- official stated GPU requirement: CUDA GPU with at least 8 GB VRAM
- scaffold target: Modal A10G

## Files

- integrations/modal_triposg/app.py
- integrations/modal_triposg/README.md

## Commits

- 686ef4b919824e98d4d4714afb861fa0ad517859
  - Modal TripoSG GPU provider scaffold
- 73d002c59147d08571fb676b68695fce3bf5eeb1
  - SSRF / source-image URL hardening
- 717081c30b83877659c4a1278bdc67e9e90c7c23
  - deployment and security README
- f61012ea617f8d0e00fa019d000eb388eb659cef
  - CI source guards for Modal scaffold

## Provider design

Modal objects:
- App: makersence-triposg
- GPU class: TripoSGModel
- GPU: A10G
- model cache Volume: makersence-triposg-models
- auth Secret: makersence-triposg-shared
- secret key: MAKERSENCE_MODAL_SHARED_TOKEN

Endpoint:
- GET /health
- POST /generate

POST contract:
- Authorization: Bearer <shared token>
- makersence-design-model-v1
- modal_triposg
- image_to_3d
- HTTPS source_image_url
- bounded target_faces
- output_format=glb

Success response:
- Content-Type: model/gltf-binary
- X-MakerSence-Contract-Version
- X-MakerSence-Provider
- X-MakerSence-Provider-Version
- X-MakerSence-Artifact-Sha256
- raw GLB bytes

## Security

Source-image fetch rejects:
- non-HTTPS
- URL userinfo
- non-443 ports
- localhost / .local
- literal private/loopback/link-local/reserved/multicast/unspecified IPs
- DNS names resolving to forbidden IP ranges
- redirects to forbidden hosts
- non-image MIME responses
- empty files
- files above 12 MB

Generated GLB rejects:
- empty/tiny artifact
- wrong glTF magic
- artifact above 64 MB

## Validation

Runtime Source Check for f61012...: SUCCESS
- integration app Python syntax PASS
- pinned upstream commit guard PASS
- GPU declaration guard PASS
- shared-token auth source guard PASS
- SSRF guard markers PASS
- binary GLB contract markers PASS

worker-tests for f61012...: SUCCESS

Heavy binary provider router was separately validated by full Ephemeral Runtime Shadow:
- Heavy PASS
- existing Universal CAD PASS
- existing Generic Blender PASS
- unconfigured modal_triposg fail-closed PASS
- unknown family fail-closed PASS
- Engineering PASS
- CAD Core / real Bambu Studio slicing PASS

## Production status

NOT DEPLOYED TO MODAL.
NOT CONFIGURED IN RAILWAY.
NO PRODUCTION TRAFFIC USES TRIPOSG.

## External credential gate

ChatGPT plugin search found no Modal connector.

Actual deployment now requires Modal account authentication.
Modal currently supports API-token auth via:
- MODAL_TOKEN_ID
- MODAL_TOKEN_SECRET

The shared MakerSence provider token must also be created as Modal Secret:
- makersence-triposg-shared
- MAKERSENCE_MODAL_SHARED_TOKEN

No token value should be committed, pasted into source, or logged.

## Next step after credentials exist

1. Authenticate Modal.
2. Create makersence-triposg-shared secret.
3. Deploy integrations/modal_triposg/app.py.
4. Verify /health.
5. Send one real image and validate returned GLB.
6. Run Trimesh/Manifold checks on that GLB.
7. Only then configure Railway Heavy:
   - MODAL_TRIPOSG_ENDPOINT
   - MODAL_TRIPOSG_TOKEN
8. Run Shadow again.
9. Only after all gates pass consider production routing.


## Deployment control prepared

A manual-only deployment workflow was added to the default branch `main`:
- file: .github/workflows/modal-triposg-deploy.yml
- commit: e499c0db70d33bf1ee30eb04d313efcdebb94c13
- trigger: workflow_dispatch only
- default source_ref: consolidation/runtime-snapshots-20260925
- it does not run on push and does not modify Railway

Required GitHub repository secrets before running it:
- MODAL_TOKEN_ID
- MODAL_TOKEN_SECRET
- MAKERSENCE_MODAL_SHARED_TOKEN

The workflow checks out the selected source ref, validates the pinned TripoSG source/GPU declaration, creates or updates the Modal shared secret, then runs `modal deploy` with a rolling strategy.


## Update — real Modal deployment attempts and current external gate

Real GitHub Actions deployment was executed using the repository secret `MODAL_TOKEN_SET_COMMAND`. The workflow successfully:
- parsed Modal API credentials
- authenticated to Modal
- created/updated `makersence-triposg-shared`
- validated the pinned TripoSG source

Build issues discovered and fixed:
1. `diso` PEP-517 build isolation could not see PyTorch.
   - fixed by installing TripoSG requirements without `diso`, then installing `diso` with `--no-build-isolation`
2. `diso` required wheel/ninja/build tools.
   - added wheel, setuptools, ninja, build-essential
3. `diso` required CUDA headers and Linux g++.
   - switched to CUDA devel image
   - forced CC=gcc / CXX=g++
   - exposed CUDA_HOME/include/lib paths

The final image build succeeded, including:
- PyTorch CUDA 12.4 install
- TripoSG dependencies
- `diso-0.1.4` wheel successfully built and installed

Modal then rejected deployment because the workspace has no payment method.
Observed real deployment errors:
- A10G: `Please add a payment method to use A10G GPU functions.`
- T4: `Please add a payment method to use T4 GPU functions.`

Therefore the blocker is now ACCOUNT/BILLING, not source code, dependency resolution, GPU image build, GitHub secrets, or Modal authentication.

Current target is T4 because it has 16 GB VRAM, exceeds TripoSG's stated minimum 8 GB VRAM, and is cheaper than A10-class GPU for validation. GPU choice can be raised later if latency is unacceptable; quality is model-dependent, not improved by using A10G instead of T4 for the same inference settings.

### Current gate

User must add a payment method in Modal before any GPU function can be deployed on this workspace. After that, re-trigger `Deploy Modal TripoSG`; no further credential setup is required.

Do NOT redo the dependency/image fixes above unless a new build error appears.
