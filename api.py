import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import Response


app = FastAPI(title="ChatGPT ImageGen API")

API_TOKEN = os.environ.get("IMAGEGEN_API_TOKEN")

ALLOWED_FORMATS = {"png", "jpeg", "webp"}
MAX_REFERENCES = 4
MAX_FILE_SIZE = 15 * 1024 * 1024


def authorize(authorization: str | None):
    if not API_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="IMAGEGEN_API_TOKEN is not configured"
        )

    expected = f"Bearer {API_TOKEN}"

    if authorization != expected:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "chatgpt-imagegen"
    }


@app.post("/generate")
async def generate(
    prompt: str = Form(...),
    size: str = Form("1024x1024"),
    output_format: str = Form("png"),
    references: List[UploadFile] = File(default=[]),
    authorization: str | None = Header(default=None),
):
    authorize(authorization)

    output_format = output_format.lower()

    if output_format not in ALLOWED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail="Invalid output format"
        )

    if len(references) > MAX_REFERENCES:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_REFERENCES} reference images"
        )

    workdir = tempfile.mkdtemp(prefix="imagegen_")

    extension = "jpg" if output_format == "jpeg" else output_format
    output_path = os.path.join(
        workdir,
        f"generated.{extension}"
    )

    reference_paths = []

    try:
        for index, uploaded in enumerate(references):
            contents = await uploaded.read()

            if len(contents) > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=400,
                    detail=f"Reference image {index + 1} is too large"
                )

            suffix = Path(uploaded.filename or "reference.png").suffix

            if not suffix:
                suffix = ".png"

            path = os.path.join(
                workdir,
                f"reference_{index}{suffix}"
            )

            with open(path, "wb") as f:
                f.write(contents)

            reference_paths.append(path)

        command = [
            "/app/chatgpt-imagegen",
            prompt,
            "--backend",
            "codex",
            "--size",
            size,
            "--format",
            output_format,
            "--timeout",
            "420",
            "--quiet",
            "-o",
            output_path,
        ]

        for path in reference_paths:
            command.extend([
                "--ref",
                path
            ])

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=450,
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=result.stderr[-4000:]
            )

        if not os.path.exists(output_path):
            raise HTTPException(
                status_code=500,
                detail="Image was not generated"
            )

        with open(output_path, "rb") as f:
            image = f.read()

        media_type = {
            "png": "image/png",
            "jpeg": "image/jpeg",
            "webp": "image/webp",
        }[output_format]

        return Response(
            content=image,
            media_type=media_type,
            headers={
                "Content-Disposition":
                    f'inline; filename="generated.{extension}"'
            }
        )

    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="Image generation timed out"
        )

    finally:
        shutil.rmtree(
            workdir,
            ignore_errors=True
        )
