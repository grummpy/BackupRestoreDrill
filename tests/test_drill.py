from __future__ import annotations

import sys
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


def test_frozen_default_demo_resolves_from_bundled_resources(tmp_path: Path, monkeypatch) -> None:
    from backuprestoredrill.drill import _resolve

    bundle = tmp_path / "bundle"
    demo = bundle / "examples" / "brochure"
    demo.mkdir(parents=True)
    state = tmp_path / "read-only-state"
    state.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    assert _resolve(state, "examples/brochure") == demo


def test_frozen_first_drill_and_relaunch_keep_history(tmp_path: Path, monkeypatch) -> None:
    """A fresh frozen bundle reads its demo while state/history stay writable."""
    from backuprestoredrill.cli import _load
    from backuprestoredrill.drill import _resolve, run_drill

    bundle = tmp_path / "bundle"
    demo = bundle / "examples" / "brochure"
    demo.mkdir(parents=True)
    (demo / "index.html").write_text(
        "<title>Northwind Brochure</title>Welcome to the Northwind brochure", encoding="utf-8"
    )
    (demo / "sitemap.xml").write_text("<?xml version='1.0'?><urlset></urlset>", encoding="utf-8")
    (bundle / "config.example.yaml").write_text(
        (Path(__file__).parents[1] / "config.example.yaml").read_text(), encoding="utf-8"
    )
    state = tmp_path / "state"
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    config, history, reports, work = _load(state)
    result = run_drill(
        config.site("brochure"), root=state, history=history, reports_dir=reports, work_root=work,
        static_only=True, crawl_fn=lambda url, site: [PageResult(url=url, status=200, ok=True)],
    )
    assert result.passed
    history.close()
    _config2, history2, _reports2, _work2 = _load(state)
    assert history2.latest("brochure").passed
    history2.close()
    # The same writable state root remains usable on a subsequent launch.
    assert _resolve(state, "examples/brochure") == demo


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


def test_teardown_failure_keeps_owned_ids_and_fails_drill(tmp_path: Path) -> None:
    class FailingRemove(FakeDocker):
        def remove(self, container):
            self.removed.append(container)
            return RunResult(1, "", "permission denied")

        def remove_network(self, name):
            self.removed_networks.append(name)
            return RunResult(1, "", "still attached")

    backup, dump = _layout(tmp_path)
    result, history = _run(
        tmp_path,
        site=_site(backup, dump),
        docker=FailingRemove(),
        crawl_fn=lambda url, site: [PageResult(url=url, status=200, ok=True)],
    )
    assert not result.passed
    assert not result.torn_down
    assert result.error and "Incomplete sandbox cleanup" in result.error
    assert history.latest("journal").passed is False


def test_teardown_exception_keeps_owned_ids_and_fails_drill(tmp_path: Path) -> None:
    class RaisingRemove(FakeDocker):
        def remove(self, container):
            raise RuntimeError("remove exploded")

        def remove_network(self, name):
            raise RuntimeError("network remove exploded")

    backup, dump = _layout(tmp_path)
    result, _history = _run(
        tmp_path, site=_site(backup, dump), docker=RaisingRemove(),
        crawl_fn=lambda url, site: [PageResult(url=url, status=200, ok=True)],
    )
    assert not result.passed and not result.torn_down
    assert "Incomplete sandbox cleanup" in (result.error or "")


def test_scratch_removal_failure_is_not_claimed_clean(tmp_path: Path, monkeypatch) -> None:
    import backuprestoredrill.sandbox as sandbox_module
    from backuprestoredrill.sandbox import Sandbox

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(sandbox_module.shutil, "rmtree", lambda path: (_ for _ in ()).throw(OSError("busy")))
    sandbox = Sandbox(docker=None, static_only=True, log=lambda message: None, scratch=scratch)
    sandbox.teardown()
    assert not sandbox.torn_down
    assert any("scratch" in problem for problem in sandbox.cleanup_problems)


def test_partial_cleanup_retry_retains_only_residual_ids(tmp_path: Path) -> None:
    from backuprestoredrill.sandbox import Sandbox

    class Partial(FakeDocker):
        def __init__(self):
            super().__init__()
            self.fail = True
        def remove(self, name):
            self.removed.append(name)
            return RunResult(1, "", "busy") if name == "bad" and self.fail else RunResult(0, "", "")
        def remove_network(self, name): return RunResult(0, "", "")

    docker = Partial()
    sandbox = Sandbox(docker=docker, static_only=False, log=lambda message: None, scratch=tmp_path / "scratch")
    sandbox.containers = ["good", "bad"]
    sandbox.network = "net"
    sandbox.teardown()
    assert sandbox.containers == ["bad"] and sandbox.network is None and sandbox.cleanup_problems
    docker.fail = False
    sandbox.teardown()
    assert sandbox.torn_down and sandbox.containers == [] and sandbox.cleanup_problems == []


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
    assert any(call["image"] == "wordpress:6.6.2-php8.2-apache" for call in fake.calls)
    assert any("brochure.example.test:127.0.0.1" in (call.get("extra_hosts") or []) for call in fake.calls)
    assert fake.removed
    assert fake.removed_networks
    joined = "\n".join(result.log)
    assert "prod-secret-password" not in joined
    assert history.latest("journal").passed is True
    assert result.report_path is not None
    report = result.report_path.read_text(encoding="utf-8")
    assert "PASS" in report
    assert "no FTP, SSH, or DNS changes" in report


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
    fake = FakeDocker(fail_image="wordpress")
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
