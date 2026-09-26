# MakerSence Modal TripoSG Provider

This integration is the external GPU provider for MakerSence's Design Model Router.

## Scope

- Provider: `modal_triposg`
- Upstream model: VAST-AI-Research/TripoSG
- Source pin: `fc5c40990181e2a756c4e0b1c2f4d6b5202faf8c`
- GPU: Modal A10G
- Input: authenticated JSON with an HTTPS source image URL
- Output: raw GLB binary (`model/gltf-binary`)
- Provider contract: `makersence-design-model-v1`

This provider does not replace Universal CAD, Generic Blender, or Engineering adapters. It is an opt-in upstream design-model generator for organic / appearance-driven products.

## Security

The web endpoint requires:

`Authorization: Bearer <MAKERSENCE_MODAL_SHARED_TOKEN>`

Source image URLs are constrained to HTTPS port 443 and reject:
- localhost / .local
- literal private, loopback, link-local, reserved, multicast, or unspecified IPs
- DNS names resolving to forbidden IP ranges
- redirects to forbidden hosts
- URL userinfo
- oversized image bodies

The service returns generic provider errors to clients and logs internal exceptions server-side.

## Modal objects

The scaffold expects:
- Secret: `makersence-triposg-shared`
  - key: `MAKERSENCE_MODAL_SHARED_TOKEN`
- Volume: `makersence-triposg-models`
  - created automatically when missing
  - caches TripoSG and RMBG weights

## Deployment

This repository does not auto-deploy Modal.

After Modal authentication is available:

```bash
pip install modal
modal setup
modal secret create makersence-triposg-shared MAKERSENCE_MODAL_SHARED_TOKEN=<random-long-token>
modal deploy makersence-cad-worker/integrations/modal_triposg/app.py
```

Do not place the shared token in GitHub, Railway source, logs, or chat.

After deployment, capture the Modal `/generate` URL and configure Heavy Worker with:
- `MODAL_TRIPOSG_ENDPOINT`
- `MODAL_TRIPOSG_TOKEN`
- optional `MODAL_TRIPOSG_TIMEOUT_SEC`

Do not enable production routing until:
1. Modal health passes.
2. A real image produces a valid GLB.
3. Heavy validates GLB magic + SHA-256.
4. Trimesh / Manifold inspection passes.
5. Blender / CAD reconstruction gates pass.
6. Existing Universal CAD and Generic Blender regressions remain green.

## Current status

Scaffold only. Not deployed to Modal. Not configured in Railway production.
