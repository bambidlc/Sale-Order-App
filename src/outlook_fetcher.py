import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from .network_resilience import NetworkRequestError, atomic_write_bytes, atomic_write_json, request_with_retry


class OutlookConfigurationError(RuntimeError):
    pass


class OutlookApiError(RuntimeError):
    pass


class OutlookNoMessagesError(OutlookApiError):
    pass


@dataclass(slots=True, frozen=True)
class OutlookSettings:
    base_dir: Path
    client_id: str
    tenant_id: str
    client_secret: str
    folder_name: str
    processed_folder_name: str
    delete_after_processing: bool
    cleanup_mailbox: str
    user_email: str
    download_path: Path
    allowed_extensions: tuple[str, ...]
    state_file: Path
    log_file: Path


def _get_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("sale_order_app.outlook_fetcher")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_file.parent.mkdir(parents=True, exist_ok=True)
    target = str(log_file)
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename == target:
            return logger

    handler = RotatingFileHandler(target, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _resolve_path(base_dir: Path, raw_value: str, fallback_name: str) -> Path:
    value = raw_value.strip() if raw_value else fallback_name
    path = Path(value)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def _parse_extensions(raw_value: str | None) -> tuple[str, ...]:
    if not raw_value:
        return (".xlsx", ".xls")
    parts = [part.strip() for chunk in raw_value.split(",") for part in chunk.split()]
    normalized = []
    for part in parts:
        if not part:
            continue
        normalized.append(part if part.startswith(".") else f".{part}")
    extensions = tuple(sorted({ext.lower() for ext in normalized}))
    return extensions or (".xlsx", ".xls")


def _parse_bool(raw_value: str | None, *, default: bool = False) -> bool:
    if raw_value is None or not raw_value.strip():
        return default
    normalized = raw_value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise OutlookConfigurationError(f"Invalid boolean setting: {raw_value!r}")


def load_outlook_settings(base_dir: str | Path | None = None) -> OutlookSettings:
    root = Path(base_dir or Path.cwd()).resolve()
    env_values = _parse_env_file(root / ".env")

    def get_value(key: str, *, required: bool = False, default: str = "") -> str:
        value = env_values.get(key) or default
        if required and not value:
            raise OutlookConfigurationError(f"Missing required setting: {key}")
        return value

    return OutlookSettings(
        base_dir=root,
        client_id=get_value("AZURE_CLIENT_ID", required=True),
        tenant_id=get_value("AZURE_TENANT_ID", required=True),
        client_secret=get_value("AZURE_CLIENT_SECRET", required=True),
        folder_name=get_value("OUTLOOK_FOLDER_NAME", required=True).strip(),
        processed_folder_name=get_value("OUTLOOK_PROCESSED_FOLDER", default="").strip(),
        delete_after_processing=_parse_bool(
            get_value("OUTLOOK_DELETE_AFTER_PROCESSING", default="false")
        ),
        cleanup_mailbox=get_value(
            "OUTLOOK_CLEANUP_MAILBOX",
            default="shop@maderas3c.com",
        ).strip(),
        user_email=get_value("OUTLOOK_USER_EMAIL", required=True).strip(),
        download_path=_resolve_path(root, get_value("DOWNLOAD_PATH", default="downloads"), "downloads"),
        allowed_extensions=_parse_extensions(get_value("ALLOWED_EXTENSIONS", default=".xlsx,.xls")),
        state_file=_resolve_path(root, get_value("STATE_FILE", default=".sync_state.json"), ".sync_state.json"),
        log_file=_resolve_path(root, get_value("LOG_FILE", default="outlook_fetcher.log"), "outlook_fetcher.log"),
    )


def _sanitize_filename(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "", value).strip()
    return clean or "report.xlsx"


class OutlookStateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, state)

    def save_fetch(self, summary: dict[str, Any]) -> None:
        state = self.load()
        state["outlook_fetch"] = {
            "fetched_at": datetime.now().isoformat(),
            "summary": summary,
        }
        self._save(state)

    def last_attachment(self) -> str | None:
        return self.load().get("outlook_fetch", {}).get("summary", {}).get("attachment_id")

    def queue_cleanup(self, summary: dict[str, Any], *, mailbox: str) -> None:
        state = self.load()
        state["pending_outlook_cleanup"] = {
            "queued_at": datetime.now().isoformat(),
            "mailbox": mailbox,
            "folder_name": summary.get("folder_name") or "",
            "message_id": summary.get("message_id") or "",
            "attachment_id": summary.get("attachment_id") or "",
            "download_path": summary.get("download_path") or "",
        }
        self._save(state)

    def pending_cleanup(self) -> dict[str, Any] | None:
        pending = self.load().get("pending_outlook_cleanup")
        return pending if isinstance(pending, dict) and pending.get("message_id") else None

    def clear_pending_cleanup(self, message_id: str) -> None:
        state = self.load()
        pending = state.get("pending_outlook_cleanup")
        if isinstance(pending, dict) and pending.get("message_id") == message_id:
            state.pop("pending_outlook_cleanup", None)
            self._save(state)


class MicrosoftGraphClient:
    def __init__(self, settings: OutlookSettings):
        self.settings = settings
        self.session = requests.Session()
        self.base_url = "https://graph.microsoft.com/v1.0"
        self._token: str | None = None
        self._token_expires_at = 0.0
        self.logger = logging.getLogger("sale_order_app.outlook_fetcher")

    def _access_token(self) -> str:
        if self._token and (not self._token_expires_at or time.monotonic() < self._token_expires_at):
            return self._token

        token_url = f"https://login.microsoftonline.com/{self.settings.tenant_id}/oauth2/v2.0/token"
        try:
            payload = request_with_retry(
                self.session.post, url=token_url,
                data={'client_id': self.settings.client_id, 'client_secret': self.settings.client_secret,
                      'grant_type': 'client_credentials', 'scope': 'https://graph.microsoft.com/.default'},
                replay_safe=True, operation='Microsoft authentication', logger=self.logger,
                decode=lambda response: response.json(),
            )
        except NetworkRequestError as exc:
            raise OutlookApiError(str(exc)) from exc
        token = payload.get("access_token")
        if not token:
            raise OutlookApiError("Microsoft Graph token response did not include access_token")
        self._token = token
        self._token_expires_at = time.monotonic() + max(1, int(payload.get('expires_in', 3600)) - 120)
        return token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "Accept": "application/json",
        }

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return request_with_retry(
                self.session.get, url=url, headers=self._headers(), params=params,
                replay_safe=True, operation='Orders Graph read', logger=self.logger,
                decode=lambda response: response.json(),
            )
        except NetworkRequestError as exc:
            raise OutlookApiError(str(exc)) from exc

    def list_mail_folders(self, *, folder_id: str | None = None) -> list[dict[str, Any]]:
        if folder_id:
            url = f"{self.base_url}/users/{self.settings.user_email}/mailFolders/{folder_id}/childFolders"
        else:
            url = f"{self.base_url}/users/{self.settings.user_email}/mailFolders"

        folders: list[dict[str, Any]] = []
        next_url: str | None = url
        params: dict[str, Any] | None = {
            "$top": 100,
            "$select": "id,displayName,parentFolderId,childFolderCount",
        }

        while next_url:
            payload = self.get_json(next_url, params=params)
            folders.extend(payload.get("value", []))
            next_url = payload.get("@odata.nextLink")
            params = None

        return folders

    def find_folder_by_name(self, display_name: str) -> dict[str, Any]:
        queue = self.list_mail_folders()
        visited: set[str] = set()

        while queue:
            folder = queue.pop(0)
            folder_id = folder["id"]
            if folder_id in visited:
                continue
            visited.add(folder_id)

            if str(folder.get("displayName") or "").strip().casefold() == display_name.strip().casefold():
                return folder

            child_count = int(folder.get("childFolderCount") or 0)
            if child_count:
                queue.extend(self.list_mail_folders(folder_id=folder_id))

        raise OutlookApiError(f"Could not find Outlook folder named '{display_name}'")

    def latest_message_with_attachment(self, folder_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/users/{self.settings.user_email}/mailFolders/{folder_id}/messages"
        payload = self.get_json(
            url,
            params={
                "$top": 50,
                "$orderby": "receivedDateTime desc",
                "$select": "id,subject,receivedDateTime,hasAttachments,internetMessageId",
            },
        )
        messages = [message for message in payload.get("value", []) if message.get("hasAttachments")]
        if not messages:
            raise OutlookNoMessagesError(
                f"No messages with attachments found in folder '{self.settings.folder_name}'"
            )
        return messages[0]

    def list_attachments(self, message_id: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/users/{self.settings.user_email}/messages/{message_id}/attachments"
        payload = self.get_json(
            url,
            params={"$top": 50, "$select": "id,name,size,lastModifiedDateTime"},
        )
        return payload.get("value", [])

    def get_attachment(self, message_id: str, attachment_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/users/{self.settings.user_email}/messages/{message_id}/attachments/{attachment_id}"
        return self.get_json(url)

    def permanent_delete_message(self, message_id: str) -> None:
        encoded_message_id = quote(message_id, safe="")
        url = (
            f"{self.base_url}/users/{self.settings.user_email}/messages/"
            f"{encoded_message_id}/permanentDelete"
        )
        for attempt in range(6):
            response = self.session.post(url, headers=self._headers(), timeout=(10, 60))
            if response.status_code in {204, 404}:
                return
            if response.status_code == 429 and attempt < 5:
                try:
                    retry_after = float(response.headers.get("Retry-After", 0) or 0)
                except (TypeError, ValueError):
                    retry_after = 0
                wait_seconds = max(retry_after, float(2 ** (attempt + 1)))
                self.logger.warning(
                    "Microsoft Graph throttled processed order deletion; retrying in %.1fs",
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                raise OutlookApiError(
                    f"Microsoft Graph permanentDelete failed ({response.status_code}): "
                    f"{response.text[:1000]}"
                ) from exc
        raise OutlookApiError("Microsoft Graph permanentDelete exhausted its retry attempts")


class OutlookFetcherService:
    def __init__(self, base_dir: str | Path | None = None):
        self.settings = load_outlook_settings(base_dir)
        self.client = MicrosoftGraphClient(self.settings)
        self.state_store = OutlookStateStore(self.settings.state_file)
        self.logger = _get_logger(self.settings.log_file)
        self._source_folder: dict[str, Any] | None = None
        self.settings.download_path.mkdir(parents=True, exist_ok=True)

    def _get_source_folder(self) -> dict[str, Any]:
        if self._source_folder is None:
            self._source_folder = self.client.find_folder_by_name(self.settings.folder_name)
        return self._source_folder

    def health(self) -> dict[str, Any]:
        folder = self._get_source_folder()
        summary = {
            "status": "ok",
            "user_email": self.settings.user_email,
            "folder_name": self.settings.folder_name,
            "folder_id": folder["id"],
            "download_path": str(self.settings.download_path),
        }
        self.logger.info(
            "Outlook Health | mailbox=%s | folder=%s | download_path=%s",
            summary["user_email"],
            summary["folder_name"],
            summary["download_path"],
        )
        return summary

    def _validate_cleanup_scope(self) -> None:
        allowed_mailbox = "shop@maderas3c.com"
        configured_mailbox = self.settings.user_email.strip().casefold()
        cleanup_mailbox = self.settings.cleanup_mailbox.strip().casefold()
        if configured_mailbox != allowed_mailbox or cleanup_mailbox != allowed_mailbox:
            raise OutlookConfigurationError(
                "Post-processing cleanup is locked to shop@maderas3c.com; "
                "no other mailbox can be modified."
            )
        if not self.settings.delete_after_processing:
            raise OutlookConfigurationError(
                "OUTLOOK_DELETE_AFTER_PROCESSING must be true for scheduled Shop order processing."
            )

    def _delete_processed_local_file(self, raw_path: str) -> None:
        if not raw_path:
            return
        download_root = self.settings.download_path.resolve()
        candidate = Path(raw_path).resolve()
        try:
            candidate.relative_to(download_root)
        except ValueError as exc:
            raise OutlookConfigurationError(
                f"Refusing to delete a processed file outside DOWNLOAD_PATH: {candidate}"
            ) from exc
        if candidate.is_file():
            candidate.unlink()
            self.logger.info("Deleted processed local order attachment | file=%s", candidate.name)

    def retry_pending_cleanup(self) -> bool:
        pending = self.state_store.pending_cleanup()
        if pending is None:
            return True

        self._validate_cleanup_scope()
        pending_mailbox = str(pending.get("mailbox") or "").strip().casefold()
        if pending_mailbox != self.settings.user_email.casefold():
            raise OutlookConfigurationError(
                "Pending cleanup mailbox does not match the configured Shop mailbox."
            )

        message_id = str(pending["message_id"])
        self._delete_processed_local_file(str(pending.get("download_path") or ""))
        self.client.permanent_delete_message(message_id)
        self.state_store.clear_pending_cleanup(message_id)
        self.logger.info(
            "Permanently deleted processed Outlook order message | mailbox=%s | folder=%s",
            self.settings.user_email,
            pending.get("folder_name") or self.settings.folder_name,
        )
        return True

    def complete_processing(self, fetch_summary: dict[str, Any]) -> bool:
        self._validate_cleanup_scope()
        message_id = str(fetch_summary.get("message_id") or "")
        if not message_id:
            raise OutlookApiError("Fetch summary does not contain a message_id for cleanup.")
        self.state_store.queue_cleanup(fetch_summary, mailbox=self.settings.user_email)
        return self.retry_pending_cleanup()

    def fetch_latest_report(self, *, force: bool = False) -> dict[str, Any]:
        folder = self._get_source_folder()
        message = self.client.latest_message_with_attachment(folder["id"])
        attachments = self.client.list_attachments(message["id"])

        selected = None
        for attachment in attachments:
            name = str(attachment.get("name") or "")
            suffix = Path(name).suffix.lower()
            attachment_type = str(attachment.get("@odata.type") or "")
            if suffix in self.settings.allowed_extensions and "fileAttachment" in attachment_type:
                selected = attachment
                break

        if selected is None:
            raise OutlookApiError(
                f"No allowed Excel attachment found in the newest message from '{self.settings.folder_name}'"
            )

        attachment_id = selected["id"]
        attachment_name = str(selected.get("name") or "report.xlsx")
        received_at = datetime.fromisoformat(str(message["receivedDateTime"]).replace("Z", "+00:00"))
        filename = f"{received_at.strftime('%Y%m%d_%H%M%S')}_{_sanitize_filename(attachment_name)}"
        local_path = self.settings.download_path / filename

        if not force and self.state_store.last_attachment() == attachment_id and local_path.exists():
            summary = {
                "status": "already_downloaded",
                "folder_name": self.settings.folder_name,
                "message_id": message["id"],
                "attachment_id": attachment_id,
                "attachment_name": attachment_name,
                "received_at": message["receivedDateTime"],
                "download_path": str(local_path),
            }
            self.logger.info(
                "Fetch Summary | status=%s | folder=%s | file=%s | saved=%s",
                summary["status"],
                summary["folder_name"],
                Path(summary["download_path"]).name,
                summary["download_path"],
            )
            return summary

        attachment = self.client.get_attachment(message["id"], attachment_id)
        content_bytes = attachment.get("contentBytes")
        if not content_bytes:
            raise OutlookApiError("Attachment payload did not include contentBytes")

        atomic_write_bytes(local_path, base64.b64decode(content_bytes, validate=True))
        summary = {
            "status": "downloaded",
            "folder_name": self.settings.folder_name,
            "message_id": message["id"],
            "attachment_id": attachment_id,
            "attachment_name": attachment_name,
            "subject": message.get("subject") or "",
            "received_at": message["receivedDateTime"],
            "download_path": str(local_path),
        }
        self.state_store.save_fetch(summary)
        self.logger.info(
            "Fetch Summary | status=%s | folder=%s | file=%s | saved=%s | subject=%s",
            summary["status"],
            summary["folder_name"],
            Path(summary["download_path"]).name,
            summary["download_path"],
            summary["subject"],
        )
        return summary
