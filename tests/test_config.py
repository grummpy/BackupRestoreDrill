from __future__ import annotations

from pathlib import Path

import pytest

from backuprestoredrill.config import ConfigError, load_config, validate_config

REPO = Path(__file__).resolve().parents[1]


def _site(**overrides):
    item = {
        "name": "Brochure",
        "slug": "brochure",
        "type": "static",
        "backup": "examples/brochure",
        "urls": [{"path": "/", "title_contains": "Hi"}],
    }
    item.update(overrides)
    return item


def test_example_config_is_valid_and_hostinger_is_off() -> None:
    config = load_config(REPO / "config.example.yaml")
    assert config.hostinger_enabled is False
    assert config.static_only is False
    assert config.stale_after_days == 35
    assert config.open_browser is True
    assert config.sites[0].slug == "brochure"
    assert config.sites[0].type == "static"
    assert config.sites[0].use_sitemap is True


def test_rejects_non_loopback_bind_and_bad_types() -> None:
    with pytest.raises(ConfigError, match="bind_host"):
        validate_config({"bind_host": "0.0.0.0", "sites": [_site()]})
    with pytest.raises(ConfigError, match="type"):
        validate_config({"sites": [_site(type="ftp")]})
    with pytest.raises(ConfigError, match="production_url"):
        validate_config({"sites": [_site(type="wordpress", slug="journal")]})
    with pytest.raises(ConfigError, match="unique"):
        validate_config({"sites": [_site(), _site()]})
    with pytest.raises(ConfigError, match="urls"):
        validate_config({"sites": [_site(urls=[], use_sitemap=False)]})
    with pytest.raises(ConfigError, match="stale_after_days"):
        validate_config({"stale_after_days": 0, "sites": []})


def test_wordpress_site_and_url_rules() -> None:
    config = validate_config(
        {
            "hostinger": {"enabled": False},
            "sites": [
                _site(
                    type="wordpress",
                    slug="journal",
                    name="Journal",
                    production_url="https://journal.example.test/",
                    sql_dump="backups/journal.sql",
                    web_root="public_html",
                    urls=[{"path": "/", "text_contains": ["Hello"], "expect_status": 200}],
                )
            ],
        }
    )
    site = config.site("journal")
    assert site.production_url == "https://journal.example.test"
    assert site.production_host == "journal.example.test"
    assert site.sql_dump == "backups/journal.sql"
    with pytest.raises(ConfigError, match="web_root"):
        validate_config({"sites": [_site(web_root="../secret")]})
    with pytest.raises(ConfigError, match="path"):
        validate_config({"sites": [_site(urls=[{"path": "index.html"}])]})
