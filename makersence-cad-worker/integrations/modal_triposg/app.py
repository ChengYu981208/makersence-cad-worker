import hashlib
import io
import ipaddress
import json
import os
import socket
import sys
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import modal

APP_NAME = "makersence-triposg"
TRIPOSG_SOURCE_COMMIT = "fc5c40990181e2a756c4e0b1c2f4d6b5202faf8c"
CONTRACT_VERSION = "makersence-design-model-v1"
MAX_SOURCE_IMAGE_BYTES = 12_000_000
MAX_GLB_BYTES = 64_000_000

app = modal.App(APP_NAME)
model_volume = modal.Volume.from_name("makersence-triposg-models", create_if_missing=True)
shared_secret = modal.Secret.from_name(
    "makersence-triposg-shared",
    required_keys=["MAKERSENCE_MODAL_SHARED_TOKEN"],
)

triposg_image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04",
        add_python="3.10",
    )
    .apt_install("git", "build-essential", "libgl1", "libglib2.0-0", "libxrender1", "libxext6")
    .env({
        "CUDA_HOME": "/usr/local/cuda",
        "CC": "gcc",
        "CXX": "g++",
        "CPATH": "/usr/local/cuda/include",
        "CPLUS_INCLUDE_PATH": "/usr/local/cuda/include",
        "LIBRARY_PATH": "/usr/local/cuda/lib64",
        "LD_LIBRARY_PATH": "/usr/local/cuda/lib64",
    })
    .run_commands(
        "test -f /usr/local/cuda/include/cuda_runtime.h",
        "g++ --version",
        "git clone https://github.com/VAST-AI-Research/TripoSG.git /opt/TripoSG",
        f"cd /opt/TripoSG && git checkout {TRIPOSG_SOURCE_COMMIT}",
        "python -m pip install --upgrade pip wheel setuptools ninja",
        "python -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124",
        "grep -v '^diso' /opt/TripoSG/requirements.txt > /tmp/triposg-requirements-no-diso.txt",
        "python -m pip install -r /tmp/triposg-requirements-no-diso.txt",
        "CUDA_HOME=/usr/local/cuda CC=gcc CXX=g++ CPATH=/usr/local/cuda/include CPLUS_INCLUDE_PATH=/usr/local/cuda/include LIBRARY_PATH=/usr/local/cuda/lib64 LD_LIBRARY_PATH=/usr/local/cuda/lib64 python -m pip install --no-build-isolation diso",
    )
)

web_image = modal.Image.debian_slim(python_version="3.11").pip_install("fastapi")


def _is_forbidden_ip(ip) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_https_url(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("SOURCE_IMAGE_URL_HTTPS_REQUIRED")
    if parsed.username or parsed.password:
        raise ValueError("SOURCE_IMAGE_URL_USERINFO_FORBIDDEN")
    if parsed.port not in (None, 443):
        raise ValueError("SOURCE_IMAGE_URL_PORT_FORBIDDEN")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("SOURCE_IMAGE_URL_PRIVATE_HOST_FORBIDDEN")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal and _is_forbidden_ip(literal):
        raise ValueError("SOURCE_IMAGE_URL_PRIVATE_HOST_FORBIDDEN")
    try:
        resolved = {
            ipaddress.ip_address(row[4][0])
            for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        }
    except Exception as exc:
        raise ValueError("SOURCE_IMAGE_URL_RESOLUTION_FAILED") from exc
    if not resolved or any(_is_forbidden_ip(ip) for ip in resolved):
        raise ValueError("SOURCE_IMAGE_URL_PRIVATE_HOST_FORBIDDEN")
    return raw


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_source_image(url: str) -> bytes:
    safe = _validate_https_url(url)
    req = Request(
        safe,
        headers={
            "Accept": "image/*",
            "User-Agent": "MakerSence-TripoSG/1.0",
        },
        method="GET",
    )
    opener = build_opener(_SafeRedirectHandler())
    with opener.open(req, timeout=30) as response:
        _validate_https_url(response.geturl())
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
        if not content_type.startswith("image/"):
            raise ValueError("SOURCE_IMAGE_CONTENT_TYPE_INVALID")
        raw = response.read(MAX_SOURCE_IMAGE_BYTES + 1)
    if not raw:
        raise ValueError("SOURCE_IMAGE_EMPTY")
    if len(raw) > MAX_SOURCE_IMAGE_BYTES:
        raise ValueError("SOURCE_IMAGE_TOO_LARGE")
    return raw


@app.cls(
    image=triposg_image,
    gpu="T4",
    timeout=1800,
    volumes={"/models": model_volume},
)
class TripoSGModel:
    @modal.enter()
    def load(self):
        os.chdir("/opt/TripoSG")
        sys.path.insert(0, "/opt/TripoSG")

        import torch
        from huggingface_hub import snapshot_download
        from scripts.briarmbg import BriaRMBG
        from triposg.pipelines.pipeline_triposg import TripoSGPipeline

        triposg_dir = "/models/TripoSG"
        rmbg_dir = "/models/RMBG-1.4"
        if not os.path.exists(os.path.join(triposg_dir, "model_index.json")):
            snapshot_download(repo_id="VAST-AI/TripoSG", local_dir=triposg_dir)
        if not os.path.exists(os.path.join(rmbg_dir, "config.json")):
            snapshot_download(repo_id="briaai/RMBG-1.4", local_dir=rmbg_dir)
        model_volume.commit()

        self.torch = torch
        self.rmbg_net = BriaRMBG.from_pretrained(rmbg_dir).to("cuda")
        self.rmbg_net.eval()
        self.pipe = TripoSGPipeline.from_pretrained(triposg_dir).to("cuda", torch.float16)

        from scripts.inference_triposg import run_triposg
        self.run_triposg = run_triposg

    @modal.method()
    def generate(self, image_bytes: bytes, target_faces: int, seed: int = 42) -> bytes:
        from PIL import Image

        if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
            raise ValueError("SOURCE_IMAGE_BYTES_REQUIRED")
        target_faces = int(target_faces)
        if target_faces < 1000 or target_faces > 100000:
            raise ValueError("TARGET_FACES_RANGE")

        image = Image.open(io.BytesIO(bytes(image_bytes))).convert("RGBA")
        mesh = self.run_triposg(
            self.pipe,
            image_input=image,
            rmbg_net=self.rmbg_net,
            seed=int(seed),
            num_inference_steps=50,
            guidance_scale=7.0,
            faces=target_faces,
        )
        glb = mesh.export(file_type="glb")
        if not isinstance(glb, (bytes, bytearray)):
            raise RuntimeError("TRIPOSG_GLB_EXPORT_INVALID")
        glb = bytes(glb)
        if len(glb) < 20 or glb[:4] != b"glTF":
            raise RuntimeError("TRIPOSG_GLB_MAGIC_INVALID")
        if len(glb) > MAX_GLB_BYTES:
            raise RuntimeError("TRIPOSG_GLB_TOO_LARGE")
        return glb


@app.function(
    image=web_image,
    secrets=[shared_secret],
    timeout=1860,
)
@modal.asgi_app()
def api():
    from fastapi import FastAPI, HTTPException, Request as FastAPIRequest
    from fastapi.responses import JSONResponse, Response

    web = FastAPI(title="MakerSence TripoSG Provider", version="1.0.0")

    def authorize(request: FastAPIRequest):
        expected = str(os.environ.get("MAKERSENCE_MODAL_SHARED_TOKEN") or "")
        supplied = str(request.headers.get("Authorization") or "")
        if not expected or supplied != "Bearer " + expected:
            raise HTTPException(status_code=401, detail="unauthorized")

    @web.get("/health")
    async def health():
        return {
            "ok": True,
            "service": APP_NAME,
            "provider": "modal_triposg",
            "contract_version": CONTRACT_VERSION,
            "source_commit": TRIPOSG_SOURCE_COMMIT,
            "gpu": "T4",
            "output_format": "glb",
        }

    @web.post("/generate")
    async def generate(request: FastAPIRequest):
        authorize(request)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("REQUEST_OBJECT_REQUIRED")
            if str(body.get("contract_version") or "") != CONTRACT_VERSION:
                raise ValueError("CONTRACT_VERSION_MISMATCH")
            if str(body.get("provider") or "") != "modal_triposg":
                raise ValueError("PROVIDER_MISMATCH")
            if str(body.get("mode") or "") != "image_to_3d":
                raise ValueError("MODE_UNSUPPORTED")
            if str(body.get("output_format") or "") != "glb":
                raise ValueError("OUTPUT_FORMAT_UNSUPPORTED")
            target_faces = int(body.get("target_faces") or 5000)
            if target_faces < 1000 or target_faces > 100000:
                raise ValueError("TARGET_FACES_RANGE")
            image_bytes = _fetch_source_image(str(body.get("source_image_url") or ""))
            glb = TripoSGModel().generate.remote(image_bytes, target_faces, 42)
            if len(glb) < 20 or glb[:4] != b"glTF":
                raise RuntimeError("PROVIDER_GLB_INVALID")
            sha256 = hashlib.sha256(glb).hexdigest()
            return Response(
                content=glb,
                media_type="model/gltf-binary",
                headers={
                    "X-MakerSence-Contract-Version": CONTRACT_VERSION,
                    "X-MakerSence-Provider": "modal_triposg",
                    "X-MakerSence-Provider-Version": "TripoSG@" + TRIPOSG_SOURCE_COMMIT[:12],
                    "X-MakerSence-Artifact-Sha256": sha256,
                },
            )
        except HTTPException:
            raise
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"error": str(exc)})
        except Exception as exc:
            print("TripoSG provider failed:", repr(exc), flush=True)
            return JSONResponse(status_code=502, content={"error": "TRIPOSG_PROVIDER_FAILED"})

    return web
