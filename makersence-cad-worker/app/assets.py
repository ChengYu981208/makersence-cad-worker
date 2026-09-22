from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import hashlib, os, threading

ROOT = Path(os.getenv("WORKER_ASSET_DIR", "/tmp/makersence-worker-assets"))
ROOT.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()

@dataclass
class Asset:
    asset_id: str
    path: Path
    format: str
    size: int


def put_asset(data: bytes, fmt: str) -> Asset:
    fmt = fmt.lower().lstrip('.').replace('stp','step')
    if fmt not in {"stl","3mf","step"}:
        raise ValueError("unsupported asset format")
    h = hashlib.sha256(data).hexdigest()[:24]
    asset_id = f"ref_{h}"
    p = ROOT / f"{asset_id}.{fmt}"
    with _lock:
        if not p.exists():
            p.write_bytes(data)
    return Asset(asset_id, p, fmt, len(data))


def get_asset(asset_id: str) -> Asset:
    if not asset_id.startswith("ref_"):
        raise KeyError(asset_id)
    found = list(ROOT.glob(asset_id + ".*"))
    if not found:
        raise KeyError(asset_id)
    p = found[0]
    return Asset(asset_id, p, p.suffix.lstrip('.'), p.stat().st_size)
