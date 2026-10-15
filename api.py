import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool


app = FastAPI(title="ChatGPT ImageGen API")
logger = logging.getLogger(__name__)

ALLOWED_FORMATS = {"png", "jpeg", "webp"}
MAX_REFERENCES = 4
MAX_FILE_SIZE = 10 * 1024 * 1024
GENERATION_TIMEOUT = 420
PROCESS_TIMEOUT = 435
MAX_OUTPUT_SIZE = 30 * 1024 * 1024


def fail(status: int, code: str, message: str):
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def authorize(authorization: str | None):
    token = os.environ.get("IMAGEGEN_API_TOKEN", "").strip()
    if not token:
        fail(503, "imagegen_not_configured", "IMAGEGEN_API_TOKEN is not configured")
    expected = f"Bearer {token}".encode()
    if not secrets.compare_digest((authorization or "").encode(), expected):
        fail(401, "imagegen_unauthorized", "Unauthorized")


def generation_failure(stderr: str):
    # Do not return CLI output: it can include prompts, paths and upstream bodies.
    # Discard progress lines, which include a prompt preview and timeout=420s.
    stderr = "\n".join(line for line in stderr.splitlines() if not re.match(r"^\s*\[\s*\d+(?:\.\d+)?s\]", line))
    if re.search(r"HTTP\s+(401|403)\b|auth\.json not found|no ChatGPT OAuth|no token|token refresh failed", stderr, re.I):
        fail(503, "imagegen_auth_required", "The ImageGen ChatGPT session must be renewed")
    if re.search(r"model is not supported when using Codex with a ChatGPT account", stderr, re.I):
        fail(503, "imagegen_model_unsupported", "The configured model is not supported by this ChatGPT account")
    if re.search(r"HTTP\s+429\b|rate.limit|quota", stderr, re.I):
        fail(429, "imagegen_rate_limited", "Image generation is temporarily rate limited")
    if re.search(r"\bstalled: the image backend sent no data\b", stderr, re.I):
        fail(504, "imagegen_stalled", "The image provider stopped sending data")
    if re.search(r"\btimed out\b|\bTimeoutError\b|\bstream exceeded total timeout budget\b", stderr, re.I):
        fail(504, "imagegen_timeout", "Image generation timed out")
    fail(502, "imagegen_failed", "Image generation failed")


def image_type(contents: bytes) -> str | None:
    if contents.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if contents.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(contents) >= 12 and contents[:4] == b"RIFF" and contents[8:12] == b"WEBP":
        return "webp"
    if contents[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


@app.get("/health")
def health():
    return {"status": "ok", "service": "chatgpt-imagegen"}


@app.post("/generate")
async def generate(
    prompt: str = Form(...),
    size: str = Form("1024x1024"),
    output_format: str = Form("png"),
    references: List[UploadFile] = File(default=[]),
    authorization: str | None = Header(default=None),
):
    authorize(authorization)
    prompt = prompt.strip()
    if not prompt or len(prompt) > 8_000:
        fail(400, "invalid_prompt", "Prompt must contain between 1 and 8000 characters")
    output_format = output_format.lower()
    if output_format not in ALLOWED_FORMATS:
        fail(400, "invalid_format", "Invalid output format")
    if size != "auto" and not re.fullmatch(r"[1-9]\d{0,4}x[1-9]\d{0,4}", size):
        fail(400, "invalid_size", "Size must be auto or WIDTHxHEIGHT")
    if len(references) > MAX_REFERENCES:
        fail(400, "invalid_references", f"Maximum {MAX_REFERENCES} reference images")

    cli = Path(os.environ.get("IMAGEGEN_CLI_PATH") or Path(__file__).with_name("chatgpt-imagegen")).expanduser().resolve()
    if not cli.is_file():
        fail(503, "imagegen_not_configured", "ImageGen CLI was not found")
    backend = os.environ.get("CHATGPT_IMAGEGEN_BACKEND", "codex").strip()
    if backend not in {"auto", "codex", "web"}:
        fail(503, "imagegen_not_configured", "Invalid ImageGen backend configuration")

    with tempfile.TemporaryDirectory(prefix="imagegen_") as workdir:
        extension = "jpg" if output_format == "jpeg" else output_format
        output_path = Path(workdir) / f"generated.{extension}"
        command = [
            sys.executable, str(cli),
            "--backend", backend,
            "--size", size,
            "--format", output_format,
            "--timeout", str(GENERATION_TIMEOUT),
            # The CLI defaults to a 120s idle cutoff. Slow image rendering can
            # stay silent longer; let the API's existing total budget govern it.
            "--stall-timeout", str(GENERATION_TIMEOUT),
            "--quiet", "--no-progress", "--no-style",
            "-o", str(output_path),
        ]
        for index, uploaded in enumerate(references):
            contents = await uploaded.read(MAX_FILE_SIZE + 1)
            if not contents or len(contents) > MAX_FILE_SIZE:
                fail(400, "invalid_reference", f"Reference image {index + 1} must contain between 1 byte and 10 MB")
            kind = image_type(contents)
            if kind is None:
                fail(400, "invalid_reference", f"Reference image {index + 1} is not a supported image")
            path = Path(workdir) / f"reference_{index}.{kind}"
            path.write_bytes(contents)
            command.extend(["--ref", str(path)])

        # Keep prompts beginning with '-' (including '--help') as plain text.
        command.extend(["--", prompt])
        started = time.monotonic()
        try:
            result = await run_in_threadpool(
                subprocess.run, command,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=PROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            logger.warning("ImageGen failed code=imagegen_timeout elapsed_seconds=%.1f", time.monotonic() - started)
            fail(504, "imagegen_timeout", "Image generation timed out")
        except OSError:
            logger.exception("Could not start the ImageGen CLI")
            fail(503, "imagegen_unavailable", "ImageGen could not be started")

        if result.returncode != 0:
            try:
                generation_failure(result.stderr)
            except HTTPException as error:
                logger.warning("ImageGen failed code=%s elapsed_seconds=%.1f", error.detail["code"], time.monotonic() - started)
                raise
        if not output_path.is_file():
            fail(502, "imagegen_invalid_image", "Image was not generated")
        with output_path.open("rb") as stream:
            image = stream.read(MAX_OUTPUT_SIZE + 1)
        if len(image) > MAX_OUTPUT_SIZE or image_type(image) != output_format:
            fail(502, "imagegen_invalid_image", "ImageGen did not return the requested image format")

        return Response(
            content=image,
            media_type={"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}[output_format],
            headers={"Content-Disposition": f'inline; filename="generated.{extension}"', "Cache-Control": "no-store"},
        )
