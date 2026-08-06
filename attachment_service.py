"""Armazenamento e validação de anexos protegidos."""
from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from config import settings
from database import Attachment, Comment, Ticket, User

PREVIEW_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
PREVIEW_TYPES = PREVIEW_IMAGE_TYPES | {"application/pdf"}


def save_uploads(
    db: Session,
    ticket: Ticket,
    uploader: User,
    uploads: list[UploadFile],
    comment: Comment | None = None,
) -> list[Attachment]:
    valid_uploads = [upload for upload in uploads if upload.filename]
    if not valid_uploads:
        return []
    if len(valid_uploads) > settings.MAX_ATTACHMENTS_PER_MESSAGE:
        raise HTTPException(
            status_code=400,
            detail=f"Envie no máximo {settings.MAX_ATTACHMENTS_PER_MESSAGE} arquivos.",
        )

    root = Path(settings.UPLOAD_DIR).resolve()
    ticket_dir = (root / str(ticket.id)).resolve()
    if root not in ticket_dir.parents:
        raise HTTPException(status_code=500, detail="Diretório de anexos inválido")
    ticket_dir.mkdir(parents=True, exist_ok=True)

    saved: list[Attachment] = []
    created_paths: list[Path] = []
    max_bytes = settings.MAX_ATTACHMENT_SIZE_MB * 1024 * 1024
    try:
        for upload in valid_uploads:
            original_name = _safe_original_name(upload.filename or "arquivo")
            blocked = {item.strip().lower() for item in settings.BLOCKED_ATTACHMENT_EXTENSIONS.split(",") if item.strip()}
            if Path(original_name).suffix.lower() in blocked:
                raise HTTPException(status_code=415, detail=f'O tipo do arquivo "{original_name}" não é permitido.')
            stored_name = f"{ticket.id}/{uuid.uuid4().hex}"
            destination = (root / stored_name).resolve()
            if root not in destination.parents:
                raise HTTPException(status_code=400, detail="Nome de arquivo inválido")

            size = 0
            created_paths.append(destination)
            with destination.open("wb") as output:
                while chunk := upload.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f'O arquivo "{original_name}" excede '
                                f"{settings.MAX_ATTACHMENT_SIZE_MB} MB."
                            ),
                        )
                    output.write(chunk)
            _scan_attachment(destination, original_name)
            attachment = Attachment(
                ticket=ticket,
                comment=comment,
                uploader=uploader,
                original_name=original_name,
                stored_name=stored_name.replace("\\", "/"),
                content_type=upload.content_type or "application/octet-stream",
                size_bytes=size,
            )
            db.add(attachment)
            saved.append(attachment)
        return saved
    except Exception:
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise


def attachment_path(attachment: Attachment) -> Path:
    root = Path(settings.UPLOAD_DIR).resolve()
    path = (root / attachment.stored_name).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    return path


def delete_saved_uploads(attachments: list[Attachment]) -> None:
    """Remove arquivos já gravados quando a transação do banco não confirma."""
    root = Path(settings.UPLOAD_DIR).resolve()
    for attachment in attachments:
        path = (root / attachment.stored_name).resolve()
        if root in path.parents:
            path.unlink(missing_ok=True)


def is_previewable(content_type: str) -> bool:
    return content_type.lower() in PREVIEW_TYPES


def is_preview_image(content_type: str) -> bool:
    return content_type.lower() in PREVIEW_IMAGE_TYPES


def _safe_original_name(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:500] or "arquivo"


def _scan_attachment(path: Path, original_name: str) -> None:
    if not settings.REQUIRE_ANTIVIRUS:
        return
    try:
        result = subprocess.run(
            [settings.ANTIVIRUS_COMMAND, "--no-summary", str(path)],
            capture_output=True,
            text=True,
            timeout=settings.ANTIVIRUS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(
            status_code=503,
            detail="A verificacao de seguranca do anexo esta indisponivel.",
        ) from exc
    if result.returncode != 0:
        raise HTTPException(
            status_code=415,
            detail=f'O arquivo "{original_name}" foi recusado pela verificacao de seguranca.',
        )
