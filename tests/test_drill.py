from __future__ import annotations

from pathlib import Path

from backuprestoredrill.crawler import PageResult
from backuprestoredrill.docker import DockerError, RunResult
from backuprestoredrill.history import History

PRODUCTION = "https://brochure.example.test"
WP_CONFIG = """<?php
define( 'DB_NAME', 'wp_prod' );
define( 'DB_USER', 'wp_prod' );
define( 'DB_PASSWORD', 'prod-secret-password' );
define( 'DB_HOST', 'localhost' );
$table_prefix = 'wp_';
/* That's all, stop editing! Happy publishing. */
"""
SQL = (
    "INSERT INTO `wp_options` VALUES (1,'siteurl','https://brochure.example.test','yes');\n"
    'INSERT INTO `wp_options` VALUES (2,\'home\',\'s:29:"https://brochure.example.test";\',\'yes\');\n'
)


class FakeDocker:
    def __init__(self, fail_image: str | None = None):
        self.calls: list[dict] = []
        self.execs: list[tuple] = []
        self.removed: list[str] = []
        self.removed_networks: list[str] = []
        self.fail_image = fail_image

    def available(self) -> tuple[bool, str]:
        return True, "Docker test"

    def create_network(self, name: str) -> None:
        return None

    def remove_network(self, name: str) -> None:
        self.removed_networks.append(name)

    def run(self, **kwargs) -> str:
        self.calls.append(kwargs)
        if self.fail_image and str(kwargs["image"]).startswith(self.fail_image):
            raise DockerError("forced failure")
        return "cid-" + kwargs["name"]

    def exec(self, container: str, command: list[str], *, input_bytes: bytes | None = None) -> RunResult:
        self.execs.append((container, command, input_bytes))
        return RunResult(0, "ok", "")

    def logs(self, container: str) -> str:
        return ""

    def remove(self, container: str) -> None:
        self.removed.append(container)


def _layout(tmp_path: Path):
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "wp-config.php").write_text(WP_CONFIG, encoding="utf-8")
    (backup / "index.php").write_text("<?php echo 'hi';\n", encoding="utf-8")
    dump = tmp_path / "dump.sql"
    dump.write_text(SQL, encoding="utf-8")
    return backup, dump


def _site(backup: Path, dump: Path):
    from backuprestoredrill.config import validate_config

    return validate_config(
        {
            "sites": [
                {
                    "name": "Journal",
                    "slug": "journal",
                    "type": "wordpress",
                    "backup": str(backup),
                    "sql_dump": str(dump),
                    "production_url": PRODUCTION,
                    "urls": [{"path": "/", "title_contains": "Journal"}],
                }
            ]
        }
    ).sites[0]


def _run(tmp_path: Path, **kwargs):
    from backuprestoredrill.drill import run_drill

    history = History(tmp_path / "drills.sqlite")
    site = kwargs.pop("site")
    result = run_drill(
        site,
        root=tmp_path,
        history=history,
        reports_dir=tmp_path / "reports",
        work_root=tmp_path / "work",
        echo=False,
        ready_probe=lambda url: True,
        **kwargs,
    )
    return result, history


def test_wordpress_restore_with_mocked_docker(tmp_path: Path) -> None:
    backup, dump = _layout(tmp_path)
    original_sql = dump.read_bytes()
    original_config = (backup / "wp-config.php").read_text(encoding="utf-8")
    fake = FakeDocker()
    captured: dict = {}

    def on_ready(sandbox, docroot, url):
        captured["wp"] = (docroot / "wp-config.php").read_text(encoding="utf-8")
        captured["sql"] = sandbox.rewritten_sql.read_text(encoding="utf-8")
        captured["url"] = url

    def crawl_fn(url, site):
        return [PageResult(url=url + "/", status=200, ok=True, title="Journal", title_ok=True)]

    result, history = _run(
        tmp_path,
        site=_site(backup, dump),
        docker=fake,
        on_ready=on_ready,
        crawl_fn=crawl_fn,
    )
    assert result.passed is True
    assert result.mode == "wordpress"
    assert result.db_ok is True
    assert result.torn_down is True
    assert captured["url"].startswith("http://127.0.0.1:")
    assert "prod-secret-password" not in captured["wp"]
    assert "define( 'DB_HOST', 'db' );" in captured["wp"]
    assert captured["url"] in captured["wp"]
    assert f's:{len(captured["url"])}:"{captured["url"]}";' in captured["sql"]
    assert dump.read_bytes() == original_sql
    assert (backup / "wp-config.php").read_text(encoding="utf-8") == original_config
    publishes = [call["publish"] for call in fake.calls]
    assert None in publishes
    assert any(item and item.startswith("127.0.0.1:") for item in publishes)
    assert all(item is None or item.startswith("127.0.0.1:") for item in publishes)
    assert any(call["image"].startswith("mariadb") and call["publish"] is None for call in fake.calls)
    assert any(call["image"].startswith("php") for call in fake.calls)
    assert any("brochure.example.test:127.0.0.1" in (call.get("extra_hosts") or []) for call in fake.calls)
    assert fake.removed
    assert fake.removed_networks
    joined = "\n".join(result.log)
    assert "prod-secret-password" not in joined
    assert history.latest("journal").passed is True
    assert result.report_path is not None
    report = result.report_path.read_text(encoding="utf-8")
    assert "PASS" in report
    assert "Production was not modified" in report


def test_teardown_runs_when_crawl_fails(tmp_path: Path) -> None:
    backup, dump = _layout(tmp_path)
    fake = FakeDocker()

    def crawl_fn(url, site):
        raise RuntimeError("crawler blew up")

    result, history = _run(
        tmp_path,
        site=_site(backup, dump),
        docker=fake,
        crawl_fn=crawl_fn,
    )
    assert result.passed is False
    assert result.torn_down is True
    assert result.error and "crawler blew up" in result.error
    assert any(name.endswith("-db") for name in fake.removed)
    assert any(name.endswith("-web") for name in fake.removed)
    assert fake.removed_networks
    assert history.latest("journal") is not None
    assert result.report_path is not None
    assert "FAIL" in result.report_path.read_text(encoding="utf-8")


def test_teardown_runs_when_php_container_fails(tmp_path: Path) -> None:
    backup, dump = _layout(tmp_path)
    fake = FakeDocker(fail_image="php")
    result, _history = _run(tmp_path, site=_site(backup, dump), docker=fake)
    assert result.passed is False
    assert result.torn_down is True
    assert "forced failure" in (result.error or "")
    assert any(name.endswith("-db") for name in fake.removed)
    assert not any(name.endswith("-web") for name in fake.removed)
    assert fake.removed_networks


def test_missing_backup_still_tears_down(tmp_path: Path) -> None:
    from backuprestoredrill.config import validate_config
    from backuprestoredrill.drill import run_drill

    site = validate_config(
        {
            "sites": [
                {
                    "name": "Missing",
                    "slug": "missing",
                    "type": "static",
                    "backup": str(tmp_path / "nope.zip"),
                    "urls": [{"path": "/"}],
                }
            ]
        }
    ).sites[0]
    history = History(tmp_path / "drills.sqlite")
    result = run_drill(
        site,
        root=tmp_path,
        history=history,
        reports_dir=tmp_path / "reports",
        work_root=tmp_path / "work",
    )
    assert result.torn_down is True
    assert result.passed is False
    assert "not found" in (result.error or "").lower() or "Backup not found" in (result.error or "")
    assert history.latest("missing") is not None
