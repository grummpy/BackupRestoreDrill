"""Optional read-only Hostinger API client. Off unless the user enables it."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_ORIGIN = "https://developers.hostinger.com"
WEBSITE_BACKUP_NOTE = (
    "Hostinger's public API can list websites, but it does not expose backup archives "
    "for Shared, Cloud, or Agency websites. Those downloads live in hPanel. Download the "
    "files archive and, for WordPress, the database dump, then set backup and sql_dump "
    "in the site config. This app will not ask Hostinger to restore anything."
)


class HostingerError(Exception):
    """A read-only Hostinger request failed."""


class _ApiRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).hostname != "developers.hostinger.com":
            raise HostingerError("Refusing a redirect away from the Hostinger API.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener():
    opener = build_opener(_ApiRedirect())

    def _open(request, timeout=30):
        return opener.open(request, timeout=timeout)

    return _open


class HostingerClient:
    """GET-only client. There is no restore, DNS, FTP, or SSH method here."""

    def __init__(self, token: str, opener=None):
        if not token or not token.strip():
            raise HostingerError("Missing Hostinger token.")
        self._token = token.strip()
        self._opener = opener or _default_opener()

    def list_websites(self) -> list[dict]:
        items: list[dict] = []
        for page in range(1, 6):
            payload = self._get("/api/hosting/v1/websites", {"page": page, "per_page": 100})
            batch = payload.get("data") or []
            if not isinstance(batch, list):
                break
            items.extend(self._website(item) for item in batch if isinstance(item, dict))
            meta = payload.get("meta") or {}
            last = meta.get("last_page") or meta.get("total_pages")
            if not batch or len(batch) < 100 or (last and page >= int(last)):
                break
        return items

    def website_backups(self, domain: str) -> dict:
        """Website backup files are not in the public API. Explain that, and do not download."""
        return {
            "domain": domain,
            "available": False,
            "message": WEBSITE_BACKUP_NOTE,
        }

    def list_vps_backups(self, virtual_machine_id: int) -> list[dict]:
        """Read backup metadata for a VPS. This does not download or restore the machine."""
        payload = self._get(f"/api/vps/v1/virtual-machines/{int(virtual_machine_id)}/backups")
        data = payload.get("data") or payload
        rows = data if isinstance(data, list) else []
        cleaned = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            cleaned.append(
                {
                    "id": item.get("id"),
                    "created_at": item.get("created_at") or item.get("date") or item.get("createdAt"),
                }
            )
        return cleaned

    def _website(self, item: dict) -> dict:
        return {
            "domain": item.get("domain"),
            "website_type": item.get("website_type"),
            "is_enabled": item.get("is_enabled"),
            "username": item.get("username"),
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        if not path.startswith("/api/"):
            raise HostingerError("Refusing a non-API path.")
        query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
        url = API_ORIGIN + path + (f"?{query}" if query else "")
        request = Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "User-Agent": "BackupRestoreDrill/1.0",
            },
        )
        if request.get_method() != "GET":
            raise HostingerError("Only read-only GET requests are allowed.")
        try:
            with self._opener(request, timeout=30) as response:
                body = response.read(1_000_000)
        except HostingerError:
            raise
        except HTTPError as exc:
            raise HostingerError(f"Hostinger API returned HTTP {exc.code} for {path}.") from None
        except URLError as exc:
            raise HostingerError(f"Could not reach the Hostinger API: {exc.reason}") from None
        except TimeoutError as exc:
            raise HostingerError(f"Hostinger API timed out: {exc}") from None
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HostingerError("Hostinger API returned a response that was not JSON.") from exc
        if not isinstance(payload, dict):
            raise HostingerError("Hostinger API returned an unexpected payload.")
        text = json.dumps(payload)
        if self._token and self._token in text:
            raise HostingerError("Refusing to handle a response that echoed the API token.")
        return payload
