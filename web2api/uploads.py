"""Safe asynchronous handling for temporary endpoint uploads."""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile

UPLOAD_CHUNK_SIZE = 64 * 1024


def sanitize_upload_filename(raw_filename: str, *, fallback_index: int) -> str:
    """Return a display-safe basename stripped of path components."""
    normalized = raw_filename.replace("\\", "/")
    candidate = Path(normalized).name.strip()
    if not candidate or candidate in {".", ".."}:
        return f"upload_{fallback_index}"
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", candidate)[:180]
    if not sanitized or sanitized in {".", ".."}:
        return f"upload_{fallback_index}"
    return sanitized


def _unique_upload_name(raw_filename: str, *, index: int) -> str:
    safe_name = sanitize_upload_filename(raw_filename, fallback_index=index)
    return f"{index:03d}_{uuid4().hex}_{safe_name}"


@asynccontextmanager
async def saved_uploads(
    files: Sequence[UploadFile] | None,
    *,
    endpoint_name: str,
    accepts_files: bool,
    max_files: int,
    max_bytes: int,
) -> AsyncIterator[list[str]]:
    """Save uploads under unique names and remove the temporary tree afterward."""
    if not files:
        yield []
        return
    if not accepts_files:
        raise HTTPException(
            status_code=400,
            detail=f"endpoint '{endpoint_name}' does not accept file uploads",
        )
    if len(files) > max_files:
        raise HTTPException(
            status_code=413,
            detail=f"too many files; maximum is {max_files}",
        )

    temp_dir = await asyncio.to_thread(tempfile.mkdtemp, prefix="web2api_upload_")
    temp_dir_path = Path(temp_dir).resolve()
    saved_paths: list[str] = []
    try:
        for index, upload in enumerate(files):
            if not upload.filename:
                continue
            display_name = sanitize_upload_filename(upload.filename, fallback_index=index)
            dest = (temp_dir_path / _unique_upload_name(upload.filename, index=index)).resolve()
            if temp_dir_path not in dest.parents:
                raise HTTPException(status_code=400, detail="invalid upload filename")

            handle = await asyncio.to_thread(dest.open, "xb")
            try:
                total_bytes = 0
                while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"file '{display_name}' exceeds {max_bytes} bytes",
                        )
                    await asyncio.to_thread(handle.write, chunk)
            finally:
                await asyncio.to_thread(handle.close)
            saved_paths.append(str(dest))
        yield saved_paths
    finally:
        await asyncio.to_thread(shutil.rmtree, temp_dir, True)
