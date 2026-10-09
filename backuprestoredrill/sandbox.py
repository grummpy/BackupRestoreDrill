"""Throwaway nginx, php-apache, MariaDB, or Python static servers."""

from __future__ import annotations

import secrets
import shutil
import socket
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from backuprestoredrill.docker import DOCKER_SETUP, DockerCLI, DockerError
from backuprestoredrill.wordpress import WordPressError, replace_site_urls, rewrite_wp_config

NGINX_IMAGE = "nginx:1.27-alpine"
# The maintained WordPress image includes the MySQL PHP extensions that the
# bare php:8.2-apache image lacks.
PHP_IMAGE = "wordpress:6.6.2-php8.2-apache"
MARIADB_IMAGE = "mariadb:11.4"

NGINX_CONF = """server {
    listen 80;
    server_name localhost;
    root /usr/share/nginx/html;
    index index.html index.htm;
    location / {
        try_files $uri $uri/ =404;
    }
}
"""

APACHE_CONF = """<Directory /var/www/html>
    Options FollowSymLinks
    AllowOverride All
    Require all granted
</Directory>
"""


class SandboxError(Exception):
    """The throwaway environment could not be started."""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return


class Sandbox:
    """One drill's containers or static server. ``teardown`` is safe to call twice."""

    def __init__(
        self,
        *,
        docker: DockerCLI | None,
        static_only: bool,
        log,
        scratch: Path,
        ready_probe=None,
    ):
        self.docker = docker
        self.static_only = static_only
        self.log = log
        self.scratch = scratch
        self.ready_probe = ready_probe or _http_ready
        self.containers: list[str] = []
        self.network: str | None = None
        self.db_container: str | None = None
        self.mode = ""
        self.base_url: str | None = None
        self.torn_down = False
        self._httpd: ThreadingHTTPServer | None = None
        self.db_password = ""

    def start_static(self, docroot: Path, drill_id: str) -> str:
        if self.static_only or not self._docker_ready():
            if not self.static_only:
                self.log(DOCKER_SETUP)
                self.log("Falling back to static-only mode for this static site.")
            else:
                self.log("Static-only mode: serving restored files with Python's http server.")
            self.mode = "static-python"
            return self._python_server(docroot)
        self.mode = "static-nginx"
        network = self._network(drill_id)
        conf = self.scratch / "nginx.conf"
        conf.write_text(NGINX_CONF, encoding="utf-8")
        port = free_port()
        self.log(f"Starting nginx on 127.0.0.1:{port}")
        container = self.docker.run(
            name=f"brd-{drill_id}-web",
            image=NGINX_IMAGE,
            network=network,
            publish=f"127.0.0.1:{port}:80",
            volumes=[
                (str(docroot), "/usr/share/nginx/html:ro"),
                (str(conf), "/etc/nginx/conf.d/default.conf:ro"),
            ],
            env={},
        )
        self.containers.append(container)
        url = f"http://127.0.0.1:{port}"
        self._wait_http(url, container)
        self.base_url = url
        return url

    def start_wordpress(
        self,
        *,
        docroot: Path,
        wp_config: Path,
        sql_dump: Path,
        production_url: str,
        drill_id: str,
        production_host: str | None,
    ) -> str:
        if self.static_only:
            raise SandboxError(
                "WordPress drills cannot run in static-only mode. They need PHP and MariaDB in Docker."
            )
        if self.docker is None or not self._docker_ready(log_setup=True):
            raise SandboxError(DOCKER_SETUP)
        if not sql_dump.is_file():
            raise SandboxError(
                f"SQL dump not found: {sql_dump}. Download it from hPanel and set sql_dump. "
                "This app does not fetch backup archives from Hostinger."
            )
        self.mode = "wordpress"
        password = secrets.token_hex(16)
        self.db_password = password
        port = free_port()
        site_url = f"http://127.0.0.1:{port}"
        original = wp_config.read_text(encoding="utf-8", errors="replace")
        wp_config.write_text(
            rewrite_wp_config(
                original,
                db_name="drill",
                db_user="drill",
                db_password=password,
                db_host="db",
                site_url=site_url,
            ),
            encoding="utf-8",
        )
        rewritten = self.scratch / "sandbox.sql"
        sql_text = sql_dump.read_text(encoding="utf-8", errors="replace")
        rewritten.write_text(replace_site_urls(sql_text, production_url, site_url), encoding="utf-8")
        self.rewritten_sql = rewritten
        cnf = self.scratch / "client.cnf"
        cnf.write_text(
            "[client]\nuser=drill\npassword=" + password + "\nhost=127.0.0.1\n",
            encoding="utf-8",
        )
        cnf.chmod(0o600)
        network = self._network(drill_id)
        extra_hosts = [f"{production_host}:127.0.0.1"] if production_host else []
        self.log("Starting MariaDB on the sandbox network (no published port).")
        db_id = self.docker.run(
            name=f"brd-{drill_id}-db",
            image=MARIADB_IMAGE,
            network=network,
            publish=None,
            volumes=[(str(cnf), "/client.cnf:ro")],
            env={
                "MYSQL_ROOT_PASSWORD": secrets.token_hex(16),
                "MYSQL_DATABASE": "drill",
                "MYSQL_USER": "drill",
                "MYSQL_PASSWORD": password,
            },
            aliases=["db"],
            extra_hosts=extra_hosts,
        )
        self.containers.append(db_id)
        self.db_container = db_id
        self._wait_db(db_id)
        self.log("Importing the SQL dump into the sandbox database.")
        imported = self.docker.exec(
            db_id,
            ["mariadb", "--defaults-extra-file=/client.cnf", "drill"],
            input_bytes=rewritten.read_bytes(),
        )
        if imported.returncode != 0:
            raise SandboxError(
                "Could not import the SQL dump into MariaDB. "
                + (imported.stderr or imported.stdout or "").strip()[-500:]
            )
        apache = self.scratch / "drill-apache.conf"
        apache.write_text(APACHE_CONF, encoding="utf-8")
        self.log(f"Starting php-apache on 127.0.0.1:{port}")
        web_id = self.docker.run(
            name=f"brd-{drill_id}-web",
            image=PHP_IMAGE,
            network=network,
            publish=f"127.0.0.1:{port}:80",
            volumes=[
                (str(docroot), "/var/www/html"),
                (str(apache), "/etc/apache2/conf-enabled/drill.conf:ro"),
                (str(cnf), "/client.cnf:ro"),
            ],
            env={},
            command=["bash", "-lc", "a2enmod rewrite && apache2-foreground"],
            extra_hosts=extra_hosts,
        )
        self.containers.append(web_id)
        self._wait_http(site_url, web_id)
        self.base_url = site_url
        return site_url

    def check_database(self) -> bool:
        if not self.db_container or self.docker is None:
            return False
        result = self.docker.exec(
            self.db_container,
            ["mariadb", "--defaults-extra-file=/client.cnf", "-e", "SELECT 1"],
        )
        if result.returncode != 0:
            self.log("Database check failed: " + (result.stderr or "").strip()[-300:])
            return False
        self.log("Database answered SELECT 1.")
        return True

    def teardown(self) -> None:
        if self.torn_down:
            return
        self.torn_down = True
        problems: list[str] = []
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception as exc:  # noqa: BLE001 - teardown must continue
                problems.append(str(exc))
            self._httpd = None
        if self.docker is not None:
            for container in list(self.containers):
                try:
                    result = self.docker.remove(container)
                    if result is not None and result.returncode != 0:
                        problems.append(f"container {container}: {(result.stderr or result.stdout).strip()}")
                except Exception as exc:  # noqa: BLE001
                    problems.append(str(exc))
            self.containers.clear()
            self.db_container = None
            if self.network:
                try:
                    result = self.docker.remove_network(self.network)
                    if result is not None and result.returncode != 0:
                        problems.append(f"network {self.network}: {(result.stderr or result.stdout).strip()}")
                except Exception as exc:  # noqa: BLE001
                    problems.append(str(exc))
                self.network = None
        if self.scratch.exists():
            shutil.rmtree(self.scratch, ignore_errors=True)
        if problems:
            self.log("Teardown finished with issues: " + "; ".join(problems))
        else:
            self.log("Sandbox torn down.")

    def _docker_ready(self, log_setup: bool = False) -> bool:
        if self.docker is None:
            if log_setup:
                self.log(DOCKER_SETUP)
            return False
        ok, detail = self.docker.available()
        if not ok and log_setup:
            self.log(detail)
        return ok

    def _network(self, drill_id: str) -> str:
        name = f"brd-{drill_id}"
        assert self.docker is not None
        self.docker.create_network(name)
        self.network = name
        return name

    def _python_server(self, docroot: Path) -> str:
        handler = partial(_QuietHandler, directory=str(docroot))
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        except OSError as exc:
            raise SandboxError(f"Could not start the static-only server: {exc}") from exc
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self._httpd = httpd
        port = httpd.server_address[1]
        url = f"http://127.0.0.1:{port}"
        self.base_url = url
        self.log(f"Static-only server is on {url}")
        return url

    def _wait_http(self, url: str, container: str) -> None:
        for _ in range(40):
            if self.ready_probe(url):
                self.log(f"Sandbox is answering on {url}")
                return
            time.sleep(0.5)
        logs = ""
        if self.docker is not None:
            try:
                logs = self.docker.logs(container)
            except DockerError:
                logs = ""
        raise SandboxError(f"Sandbox did not answer on {url}. {logs}".strip())

    def _wait_db(self, container: str) -> None:
        assert self.docker is not None
        last = ""
        for _ in range(40):
            result = self.docker.exec(container, ["mariadb-admin", "ping", "--silent"])
            if result.returncode == 0:
                self.log("MariaDB is ready.")
                return
            last = (result.stderr or result.stdout or "").strip()
            time.sleep(0.5)
        raise SandboxError("MariaDB did not become ready. " + last)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_ready(url: str) -> bool:
    try:
        with urlopen(url, timeout=2) as response:
            return int(response.status) < 500
    except Exception:
        return False


def resolve_docroot(root: Path, web_root: str | None, site_type: str) -> Path:
    if web_root:
        candidate = (root / web_root).resolve()
        root_resolved = root.resolve()
        if root_resolved != candidate and root_resolved not in candidate.parents:
            raise SandboxError("web_root escapes the restored backup.")
        if not candidate.is_dir():
            raise SandboxError(f"web_root not found in the backup: {web_root}")
        return candidate
    if site_type == "wordpress":
        from backuprestoredrill.wordpress import find_wp_config

        found = find_wp_config(root)
        if found is None:
            raise WordPressError(
                "No wp-config.php in the backup. WordPress drills need the site files."
            )
        return found.parent
    for name in ("index.html", "index.htm"):
        if (root / name).is_file():
            return root
    public = root / "public_html"
    if public.is_dir():
        return public
    return root
