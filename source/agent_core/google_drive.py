#!/usr/bin/env python3
"""Local-only Google Drive upload adapter for SmartAgent.

OAuth credentials never enter the planner/tool envelope.  The OAuth client
secret and refresh token live under localdata/secure/google_drive by default.
"""
from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from typing import Any

from .workspace import AGENT_PROJECT_ROOT
from .paths import secure_root

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
STATE_ROOT = secure_root() / "google_drive"
DEFAULT_CLIENT_SECRETS = STATE_ROOT / "client_secret.json"
DEFAULT_TOKEN = STATE_ROOT / "token.json"


def _credential_paths() -> tuple[Path, Path]:
    client = Path(
        os.environ.get("SMARTAGENT_GOOGLE_DRIVE_CLIENT_SECRETS", "")
        or DEFAULT_CLIENT_SECRETS
    ).expanduser().resolve()
    token = Path(
        os.environ.get("SMARTAGENT_GOOGLE_DRIVE_TOKEN", "")
        or DEFAULT_TOKEN
    ).expanduser().resolve()
    return client, token


def _load_credentials():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError(
            "google_drive_dependencies_missing: install google-api-python-client, "
            "google-auth-httplib2 and google-auth-oauthlib"
        ) from exc

    client_path, token_path = _credential_paths()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    credentials = None
    if token_path.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(
                str(token_path), [DRIVE_SCOPE]
            )
        except Exception:
            credentials = None

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    elif not credentials or not credentials.valid:
        if not client_path.is_file():
            raise RuntimeError(
                "google_drive_client_secrets_missing: place OAuth Desktop client JSON at "
                f"{client_path} or set SMARTAGENT_GOOGLE_DRIVE_CLIENT_SECRETS"
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_path), scopes=[DRIVE_SCOPE]
        )
        credentials = flow.run_local_server(port=0, open_browser=True)

    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def upload_file_to_drive(
    path: str | Path,
    *,
    folder_id: str = "",
    name: str = "",
) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(str(source))

    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError(
            "google_drive_dependencies_missing: install google-api-python-client, "
            "google-auth-httplib2 and google-auth-oauthlib"
        ) from exc

    credentials = _load_credentials()
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    metadata: dict[str, Any] = {"name": str(name or source.name)}
    if folder_id:
        metadata["parents"] = [str(folder_id)]
    mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    media = MediaFileUpload(str(source), mimetype=mime_type, resumable=True)
    created = (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id,name,size,mimeType,webViewLink,parents",
        )
        .execute()
    )
    file_id = str(created.get("id", "") or "")
    if not file_id:
        raise RuntimeError("google_drive_upload_missing_file_id")
    return {
        "status": "UPLOADED",
        "file_id": file_id,
        "name": str(created.get("name", metadata["name"])),
        "size_bytes": int(created.get("size") or source.stat().st_size),
        "mime_type": str(created.get("mimeType", mime_type)),
        "web_view_link": str(
            created.get("webViewLink")
            or f"https://drive.google.com/file/d/{file_id}/view"
        ),
        "parents": list(created.get("parents") or []),
        "source_path": str(source),
    }
