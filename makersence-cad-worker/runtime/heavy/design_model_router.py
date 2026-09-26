import ipaddress
import json
import os
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CONTRACT_VERSION = "makersence-design-model-v1"
SUPPORTED_PROVIDERS = {"modal_triposg"}


class DesignModelProviderError(RuntimeError):
    pass


def _env(name):
    return str(os.environ.get(name, "") or "").strip()


def design_model_provider_status():
    endpoint = _env("MODAL_TRIPOSG_ENDPOINT")
    token = _env("MODAL_TRIPOSG_TOKEN")
    return {
        "contract_version": CONTRACT_VERSION,
        "providers": {
            "modal_triposg": {
                "configured": bool(endpoint and token),
                "endpoint_configured": bool(endpoint),
                "token_configured": bool(token),
            }
        },
        "default_provider": _env("MAKERSENCE_DESIGN_MODEL_PROVIDER") or None,
        "fail_closed": True,
    }


def _validate_https_url(value, field):
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
    except Exception as exc:
        raise DesignModelProviderError(f"{field.upper()}_INVALID") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise DesignModelProviderError(f"{field.upper()}_HTTPS_REQUIRED")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise DesignModelProviderError(f"{field.upper()}_PRIVATE_HOST_FORBIDDEN")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast):
        raise DesignModelProviderError(f"{field.upper()}_PRIVATE_HOST_FORBIDDEN")
    return raw


def _provider_from_request(req):
    requested = str((req or {}).get("provider") or "").strip().lower()
    provider = requested or _env("MAKERSENCE_DESIGN_MODEL_PROVIDER").lower()
    if not provider:
        raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_REQUIRED")
    if provider not in SUPPORTED_PROVIDERS:
        raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_UNSUPPORTED:" + provider)
    return provider


def _normalize_request(req):
    if not isinstance(req, dict):
        raise DesignModelProviderError("DESIGN_MODEL_REQUEST_OBJECT_REQUIRED")
    provider = _provider_from_request(req)
    mode = str(req.get("mode") or "image_to_3d").strip().lower()
    if mode != "image_to_3d":
        raise DesignModelProviderError("DESIGN_MODEL_MODE_UNSUPPORTED:" + mode)

    source_image_url = _validate_https_url(req.get("source_image_url"), "source_image_url")
    prompt = str(req.get("prompt") or "").strip()
    if len(prompt) > 800:
        raise DesignModelProviderError("DESIGN_MODEL_PROMPT_TOO_LONG")

    try:
        target_faces = int(req.get("target_faces") or 5000)
    except Exception as exc:
        raise DesignModelProviderError("DESIGN_MODEL_TARGET_FACES_INVALID") from exc
    if target_faces < 1000 or target_faces > 100000:
        raise DesignModelProviderError("DESIGN_MODEL_TARGET_FACES_RANGE")

    output_format = str(req.get("output_format") or "glb").strip().lower()
    if output_format != "glb":
        raise DesignModelProviderError("DESIGN_MODEL_OUTPUT_FORMAT_UNSUPPORTED:" + output_format)

    return {
        "contract_version": CONTRACT_VERSION,
        "provider": provider,
        "mode": mode,
        "source_image_url": source_image_url,
        "prompt": prompt,
        "target_faces": target_faces,
        "output_format": output_format,
    }


def _modal_triposg(req):
    endpoint = _env("MODAL_TRIPOSG_ENDPOINT")
    token = _env("MODAL_TRIPOSG_TOKEN")
    if not endpoint or not token:
        raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_NOT_CONFIGURED:modal_triposg")
    endpoint = _validate_https_url(endpoint, "modal_triposg_endpoint")

    payload = json.dumps(req, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    http_req = Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "MakerSence-Heavy/DesignModelRouter-v1",
        },
        method="POST",
    )
    try:
        timeout = max(30, min(600, int(_env("MODAL_TRIPOSG_TIMEOUT_SEC") or "240")))
    except Exception:
        timeout = 240

    try:
        with urlopen(http_req, timeout=timeout) as response:
            raw = response.read(2_000_000)
            status_code = int(getattr(response, "status", 200) or 200)
    except HTTPError as exc:
        raise DesignModelProviderError(f"DESIGN_MODEL_PROVIDER_HTTP_{int(exc.code)}") from exc
    except (URLError, TimeoutError, socket.timeout) as exc:
        raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_UNREACHABLE") from exc
    except Exception as exc:
        raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_REQUEST_FAILED") from exc

    if status_code < 200 or status_code >= 300:
        raise DesignModelProviderError(f"DESIGN_MODEL_PROVIDER_HTTP_{status_code}")
    try:
        out = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_INVALID_JSON") from exc
    if not isinstance(out, dict):
        raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_INVALID_RESPONSE")

    provider_status = str(out.get("status") or "").strip().lower()
    if provider_status not in {"ok", "success", "completed"}:
        err = str(out.get("error") or provider_status or "unknown")
        raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_FAILED:" + err[:240])

    artifact_url = _validate_https_url(out.get("artifact_url") or out.get("glb_url"), "artifact_url")
    sha256 = str(out.get("sha256") or out.get("artifact_sha256") or "").strip().lower()
    if sha256 and (len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256)):
        raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_SHA256_INVALID")

    return {
        "status": "PASS",
        "contract_version": CONTRACT_VERSION,
        "provider": "modal_triposg",
        "provider_version": str(out.get("provider_version") or out.get("model_version") or "") or None,
        "artifact": {
            "type": "glb",
            "url": artifact_url,
            "sha256": sha256 or None,
        },
        "metadata": out.get("metadata") if isinstance(out.get("metadata"), dict) else {},
        "fail_closed": True,
    }


def route_design_model(req):
    normalized = _normalize_request(req)
    provider = normalized["provider"]
    if provider == "modal_triposg":
        return _modal_triposg(normalized)
    raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_UNSUPPORTED:" + provider)
