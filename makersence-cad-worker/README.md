# MakerSence CAD Worker

Maintainable CAD/geometry worker for MakerSence Hub.

## Goals
- Preserve the Hub HTTP contract (`/health`, `/v1/generate`, `/v1/jobs/{id}`, artifact downloads, geometry analysis).
- Route products to purpose-built adapters instead of falling back to a generic plate/box.
- Keep protected mating interfaces separate from redesignable appearance geometry.
- Make adapter behavior testable and versioned.

## New adapter families
- `INTERFACE_LOCKED_CAD`: external compatibility is fixed; non-interface form may be redesigned.
- `INTERFACE_MECHANISM_CAD`: protected external interface plus an internal mechanism.
- `MECHANISM_CAD`: internal hinge/slider/snap/rotary mechanism without an external mating interface.

## Local run
```bash
pip install -r requirements.txt
export WORKER_TOKEN=dev-token
uvicorn app.main:app --host 0.0.0.0 --port 8000
pytest -q
```

## Railway
Railway should deploy this repository directly. Do not restore source code from `WORKER_SRC_*` environment variables.
