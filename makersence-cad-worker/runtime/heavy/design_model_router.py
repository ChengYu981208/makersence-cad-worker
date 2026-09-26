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
    proxy = _env("MODAL_TRIPOSG_PROXY_URL")
    return {
        "contract_version": CONTRACT_VERSION,
        "providers": {
            "modal_triposg": {
                "configured": bool((endpoint and token) or proxy),
                "direct_configured": bool(endpoint and token),
                "endpoint_configured": bool(endpoint),
                "token_configured": bool(token),
                "private_proxy_configured": bool(proxy),
            }
        },
        "default_provider": _env("MAKERSENCE_DESIGN_MODEL_PROVIDER") or None,
        "fail_closed": True,
    }


def _validate_private_proxy_url(value):
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
    except Exception as exc:
        raise DesignModelProviderError("DESIGN_MODEL_PROXY_URL_INVALID") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "http" or not host.endswith(".railway.internal"):
        raise DesignModelProviderError("DESIGN_MODEL_PROXY_PRIVATE_URL_REQUIRED")
    if not parsed.path or parsed.path == "/":
        raise DesignModelProviderError("DESIGN_MODEL_PROXY_PATH_REQUIRED")
    return raw


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
    proxy = _env("MODAL_TRIPOSG_PROXY_URL")
    use_proxy = not (endpoint and token)
    if use_proxy:
        if not proxy:
            raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_NOT_CONFIGURED:modal_triposg")
        endpoint = _validate_private_proxy_url(proxy)
        headers = {
            "Content-Type": "application/json",
            "Accept": "model/gltf-binary",
            "User-Agent": "MakerSence-Heavy/DesignModelRouter-v1-private-proxy",
        }
    else:
        endpoint = _validate_https_url(endpoint, "modal_triposg_endpoint")
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "model/gltf-binary",
            "User-Agent": "MakerSence-Heavy/DesignModelRouter-v1",
        }

    payload = json.dumps(req, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    http_req = Request(endpoint,data=payload,headers=headers,method="POST")
    try:
        timeout = max(30, min(2400, int(_env("MODAL_TRIPOSG_TIMEOUT_SEC") or "240")))
    except Exception:
        timeout = 240

    try:
        with urlopen(http_req, timeout=timeout) as response:
            status_code = int(getattr(response, "status", 200) or 200)
            headers = getattr(response, "headers", {})
            content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            provider_version = str(headers.get("X-MakerSence-Provider-Version") or "").strip() or None
            claimed_sha256 = str(headers.get("X-MakerSence-Artifact-Sha256") or "").strip().lower()
            raw = response.read(64_000_001)
    except HTTPError as exc:
        raise DesignModelProviderError(f"DESIGN_MODEL_PROVIDER_HTTP_{int(exc.code)}") from exc
    except (URLError, TimeoutError, socket.timeout) as exc:
        raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_UNREACHABLE") from exc
    except Exception as exc:
        raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_REQUEST_FAILED") from exc

    if status_code < 200 or status_code >= 300:
        raise DesignModelProviderError(f"DESIGN_MODEL_PROVIDER_HTTP_{status_code}")
    if len(raw) < 20:
        raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_ARTIFACT_EMPTY")
    if len(raw) > 64_000_000:
        raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_ARTIFACT_TOO_LARGE")
    if raw[:4] != b"glTF":
        raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_GLB_MAGIC_INVALID")
    if content_type not in {"model/gltf-binary", "application/octet-stream"}:
        raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_CONTENT_TYPE_INVALID:" + content_type)

    actual_sha256 = __import__("hashlib").sha256(raw).hexdigest()
    if claimed_sha256:
        if len(claimed_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in claimed_sha256):
            raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_SHA256_INVALID")
        if claimed_sha256 != actual_sha256:
            raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_SHA256_MISMATCH")

    return {
        "status": "PASS",
        "contract_version": CONTRACT_VERSION,
        "provider": "modal_triposg",
        "provider_version": provider_version,
        "content_type": "model/gltf-binary",
        "artifact_bytes": raw,
        "artifact_bytes_count": len(raw),
        "artifact_sha256": actual_sha256,
        "fail_closed": True,
    }


def route_design_model(req):
    normalized = _normalize_request(req)
    provider = normalized["provider"]
    if provider == "modal_triposg":
        return _modal_triposg(normalized)
    raise DesignModelProviderError("DESIGN_MODEL_PROVIDER_UNSUPPORTED:" + provider)
