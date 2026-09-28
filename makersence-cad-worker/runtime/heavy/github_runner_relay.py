"""Narrow GitHub Actions OIDC relay for heavy geometry jobs.

Only task metadata and compact alignment evidence live in this process. The Hub
remains the durable queue of record and re-submits a task if this worker restarts.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = "https://token.actions.githubusercontent.com/.well-known/jwks"
AUDIENCE = "https://cad-worker-v2-production.up.railway.app/v1/github-runner"
REPOSITORY = "ChengYu981208/makersence-cad-worker"
WORKFLOW_PATH = REPOSITORY + "/.github/workflows/geometry-worker.yml@"
TRUSTED_WORKFLOWS = {
    "refs/heads/main": WORKFLOW_PATH + "refs/heads/main",
}
MAX_TASKS = 32
MAX_RESULT_BYTES = 800_000
TASK_TTL_SECONDS = 3 * 60 * 60
CLAIM_LEASE_SECONDS = 55 * 60
_tasks: dict[str, dict] = {}
_lock = threading.RLock()
_jwks: list[dict] = []
_jwks_expiry = 0.0


class RelayError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def _decode_part(value: str) -> bytes:
    raw = str(value or "").replace("-", "+").replace("_", "/")
    raw += "=" * ((4 - len(raw) % 4) % 4)
    return base64.b64decode(raw, validate=True)


def _get_jwks() -> list[dict]:
    global _jwks, _jwks_expiry
    now = time.time()
    if _jwks and now < _jwks_expiry:
        return _jwks
    req = Request(JWKS_URL, headers={"Accept": "application/json", "User-Agent": "MakerSence-CAD-Worker"})
    try:
        with urlopen(req, timeout=10) as response:
            body = response.read(256_000)
        payload = json.loads(body.decode("utf-8"))
        keys = payload.get("keys")
        if not isinstance(keys, list) or not keys:
            raise ValueError("invalid jwks")
    except Exception as exc:
        raise RelayError(503, "OIDC_KEY_SERVICE_UNAVAILABLE") from exc
    with _lock:
        _jwks = keys
        _jwks_expiry = now + 15 * 60
    return _jwks


def verify_github_oidc(authorization: str) -> dict:
    match = re.fullmatch(r"Bearer ([A-Za-z0-9._-]{1,12000})", str(authorization or "").strip())
    if not match:
        raise RelayError(401, "OIDC_TOKEN_REQUIRED")
    parts = match.group(1).split(".")
    if len(parts) != 3:
        raise RelayError(401, "OIDC_TOKEN_INVALID")
    try:
        header = json.loads(_decode_part(parts[0]).decode("utf-8"))
        claims = json.loads(_decode_part(parts[1]).decode("utf-8"))
        signature = _decode_part(parts[2])
    except Exception as exc:
        raise RelayError(401, "OIDC_TOKEN_INVALID") from exc
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise RelayError(401, "OIDC_TOKEN_INVALID")
    key = next((item for item in _get_jwks()
                if item.get("kid") == header["kid"] and item.get("kty") == "RSA"
                and item.get("alg", "RS256") == "RS256" and item.get("use", "sig") == "sig"), None)
    if not key:
        raise RelayError(401, "OIDC_SIGNING_KEY_UNKNOWN")
    try:
        modulus = int.from_bytes(_decode_part(key["n"]), "big")
        exponent = int.from_bytes(_decode_part(key["e"]), "big")
        public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
        public_key.verify(signature, (parts[0] + "." + parts[1]).encode("ascii"),
                           padding.PKCS1v15(), hashes.SHA256())
    except Exception as exc:
        raise RelayError(401, "OIDC_SIGNATURE_INVALID") from exc
    now = int(time.time())
    audiences = claims.get("aud")
    if not isinstance(audiences, list):
        audiences = [audiences]
    ref = str(claims.get("ref") or "")
    if (claims.get("iss") != ISSUER or AUDIENCE not in audiences
            or claims.get("repository") != REPOSITORY
            or TRUSTED_WORKFLOWS.get(ref) != claims.get("workflow_ref")
            or not isinstance(claims.get("run_id"), (str, int))
            or not re.fullmatch(r"\d{1,30}", str(claims.get("run_id") or ""))
            or not re.fullmatch(r"\d{1,3}", str(claims.get("run_attempt") or ""))
            or not re.fullmatch(r"[a-f0-9]{40,64}", str(claims.get("sha") or "").lower())
            or int(claims.get("exp") or 0) < now
            or int(claims.get("iat") or 0) > now + 30
            or now - int(claims.get("iat") or 0) > 600
            or int(claims.get("nbf") or 0) > now + 30):
        raise RelayError(401, "OIDC_CLAIMS_REJECTED")
    return claims


def _cleanup(now: float) -> None:
    for task_id, task in list(_tasks.items()):
        if task.get("status") == "claimed" and now - float(task.get("updated_at", now)) > CLAIM_LEASE_SECONDS:
            task.update(status="queued", updated_at=now)
            for key in ("run_id", "run_attempt", "sha"):
                task.pop(key, None)
        if now - float(task.get("updated_at", now)) > TASK_TTL_SECONDS:
            _tasks.pop(task_id, None)


def _valid_request(request: object) -> dict:
    if not isinstance(request, dict):
        raise RelayError(400, "GEOMETRY_REQUEST_INVALID")
    for name in ("source_url", "counterpart_url"):
        value = request.get(name)
        if not isinstance(value, str) or not value.startswith("https://") or len(value) > 4096:
            raise RelayError(400, "GEOMETRY_URL_INVALID")
    expected = request.get("expected_instances", 1)
    if isinstance(expected, bool) or not isinstance(expected, int) or not 1 <= expected <= 12:
        raise RelayError(400, "EXPECTED_INSTANCES_INVALID")
    part_ids = request.get("source_part_ids")
    if part_ids is not None and (not isinstance(part_ids, list) or not 1 <= len(part_ids) <= 32
                                 or any(not isinstance(x, str) or not x.strip() or len(x) > 128 for x in part_ids)):
        raise RelayError(400, "SOURCE_PART_IDS_INVALID")
    return request


def enqueue_task(task_id: str, request: object) -> dict:
    task_id = str(task_id or "").strip()
    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise RelayError(400, "TASK_ID_INVALID")
    request = _valid_request(request)
    now = time.time()
    with _lock:
        _cleanup(now)
        existing = _tasks.get(task_id)
        if existing:
            return {"task_id": task_id, "status": existing["status"], "already_queued": True}
        if sum(1 for item in _tasks.values() if item.get("status") in ("queued", "claimed")) >= MAX_TASKS:
            raise RelayError(429, "RUNNER_QUEUE_FULL")
        _tasks[task_id] = {
            "task_id": task_id, "request": request, "status": "queued",
            "created_at": now, "updated_at": now,
        }
    return {"task_id": task_id, "status": "queued", "already_queued": False}


def _claims_for(headers: object, body: object) -> tuple[dict, dict]:
    authorization = headers.get("Authorization", "") if hasattr(headers, "get") else ""
    claims = verify_github_oidc(authorization)
    if not isinstance(body, dict):
        raise RelayError(400, "RUNNER_BODY_INVALID")
    if (str(body.get("run_id") or "") != str(claims["run_id"])
            or int(body.get("run_attempt") or 0) != int(claims["run_attempt"])
            or str(body.get("sha") or "").lower() != str(claims["sha"]).lower()):
        raise RelayError(401, "RUNNER_BODY_IDENTITY_MISMATCH")
    return claims, body


def claim_task(headers: object, body: object) -> dict:
    claims, _ = _claims_for(headers, body)
    now = time.time()
    with _lock:
        _cleanup(now)
        pending = sorted((item for item in _tasks.values() if item.get("status") == "queued"),
                         key=lambda item: item.get("created_at", now))
        if not pending:
            return {"ok": True, "task": None}
        task = pending[0]
        task.update(status="claimed", updated_at=now,
                    run_id=str(claims["run_id"]), run_attempt=int(claims["run_attempt"]),
                    sha=str(claims["sha"]).lower())
        return {"ok": True, "task": {"task_id": task["task_id"], "request": task["request"]}}


def complete_task(headers: object, body: object) -> dict:
    claims, body = _claims_for(headers, body)
    task_id = str(body.get("task_id") or "")
    status = str(body.get("status") or "")
    if not re.fullmatch(r"[a-f0-9]{32}", task_id) or status not in ("completed", "failed"):
        raise RelayError(400, "RUNNER_RESULT_FIELDS_INVALID")
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            raise RelayError(404, "RUNNER_TASK_NOT_FOUND")
        if (task.get("run_id") != str(claims["run_id"])
                or task.get("run_attempt") != int(claims["run_attempt"])
                or task.get("sha") != str(claims["sha"]).lower()):
            raise RelayError(409, "RUNNER_TASK_NOT_CLAIMED_BY_THIS_RUN")
        if task.get("status") in ("completed", "failed"):
            return {"ok": True, "status": task["status"], "already_completed": True}
        if status == "completed":
            alignment = body.get("alignment")
            if not isinstance(alignment, dict) or alignment.get("algorithm_version") != "counterpart-alignment-v4-exact-mesh-gap-bounded":
                raise RelayError(422, "RUNNER_ALIGNMENT_VERSION_INVALID")
            if alignment.get("status") not in ("ALIGNED", "REVIEW_REQUIRED"):
                raise RelayError(422, "RUNNER_ALIGNMENT_STATUS_INVALID")
            triangle_count = ((alignment.get("source") or {}).get("triangle_count"))
            if not isinstance(triangle_count, (int, float)) or triangle_count <= 0:
                raise RelayError(422, "RUNNER_SOURCE_EVIDENCE_MISSING")
            raw = json.dumps(alignment, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if len(raw) > MAX_RESULT_BYTES:
                raise RelayError(413, "RUNNER_RESULT_TOO_LARGE")
            task["alignment"] = alignment
        else:
            task["failure_code"] = re.sub(r"[^A-Z0-9_]", "", str(body.get("failure_code") or "WORKER_FAILED").upper())[:100] or "WORKER_FAILED"
        task.update(status=status, updated_at=time.time())
    return {"ok": True, "status": status, "already_completed": False}


def get_task(task_id: str) -> dict | None:
    task_id = str(task_id or "")
    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        return None
    now = time.time()
    with _lock:
        _cleanup(now)
        task = _tasks.get(task_id)
        if not task:
            return None
        if task["status"] == "queued":
            status = "queued"
        elif task["status"] == "claimed":
            status = "processing"
        else:
            status = task["status"]
        result = {"task_id": task_id, "status": status, "updated_at": task.get("updated_at")}
        if task.get("alignment") is not None:
            result["alignment"] = task["alignment"]
        if task.get("failure_code"):
            result["failure_code"] = task["failure_code"]
        return result
