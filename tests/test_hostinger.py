from __future__ import annotations

import json
from pathlib import Path

import pytest

from backuprestoredrill.hostinger import WEBSITE_BACKUP_NOTE, HostingerClient, HostingerError


class _Response:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode()

    def read(self, _limit: int = -1) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


def test_list_websites_is_get_only() -> None:
    seen: list[tuple[str, str]] = []

    def opener(request, timeout=30):
        seen.append((request.get_method(), request.full_url))
        assert request.get_header("Authorization") == "Bearer test-token"
        return _Response(
            {
                "data": [
                    {
                        "domain": "brochure.example.test",
                        "website_type": "other",
                        "is_enabled": True,
                        "username": "u123",
                    }
                ],
                "meta": {"last_page": 1},
            }
        )

    client = HostingerClient("test-token", opener=opener)
    sites = client.list_websites()
    assert sites == [
        {
            "domain": "brochure.example.test",
            "website_type": "other",
            "is_enabled": True,
            "username": "u123",
        }
    ]
    assert seen[0][0] == "GET"
    assert seen[0][1].startswith("https://developers.hostinger.com/api/hosting/v1/websites")


def test_website_backups_are_not_downloaded() -> None:
    def opener(request, timeout=30):
        raise AssertionError("website backup lookup must not call the API")

    client = HostingerClient("test-token", opener=opener)
    result = client.website_backups("brochure.example.test")
    assert result["available"] is False
    assert "hPanel" in result["message"]
    assert result["message"] == WEBSITE_BACKUP_NOTE
    assert not hasattr(client, "restore_backup")


def test_token_is_not_echoed_from_an_error_body() -> None:
    token = "super-secret-token"

    def opener(request, timeout=30):
        return _Response({"data": [], "echo": token})

    client = HostingerClient(token, opener=opener)
    with pytest.raises(HostingerError) as caught:
        client.list_websites()
    assert token not in str(caught.value)


def test_vps_backup_list_is_get_only() -> None:
    def opener(request, timeout=30):
        assert request.get_method() == "GET"
        assert request.full_url.endswith("/api/vps/v1/virtual-machines/7/backups")
        return _Response({"data": [{"id": 3, "created_at": "2026-01-15T00:00:00Z"}]})

    rows = HostingerClient("test-token", opener=opener).list_vps_backups(7)
    assert rows == [{"id": 3, "created_at": "2026-01-15T00:00:00Z"}]


def test_package_has_no_ftp_or_ssh_client() -> None:
    root = Path(__file__).resolve().parents[1] / "backuprestoredrill"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "ftplib" not in text
        assert "paramiko" not in text
        assert "dns_records_update" not in text
