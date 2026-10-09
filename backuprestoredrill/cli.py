"""Command line entry point. With no arguments, the local dashboard opens."""

from __future__ import annotations

import argparse
import getpass
import shutil
import sys
import webbrowser
from pathlib import Path

from backuprestoredrill import __version__
from backuprestoredrill.config import ConfigError, load_config
from backuprestoredrill.docker import DockerCLI
from backuprestoredrill.drill import run_all, run_drill
from backuprestoredrill.history import History
from backuprestoredrill.hostinger import WEBSITE_BACKUP_NOTE, HostingerClient, HostingerError
from backuprestoredrill.schedule import (
    install_cron,
    install_launchd,
    install_windows,
    uninstall_cron,
    uninstall_launchd,
    uninstall_windows,
)
from backuprestoredrill.secrets import delete_token, get_token, set_token
from backuprestoredrill.web import AppState, make_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backuprestoredrill",
        description="Restore a site backup into a throwaway sandbox and check that pages load.",
    )
    parser.add_argument("--version", action="version", version=f"BackupRestoreDrill {__version__}")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Project folder. Defaults to the current directory.",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Open the local dashboard")
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument("--static-only", action="store_true")

    drill = sub.add_parser("drill", help="Run a drill in the terminal")
    drill.add_argument("slug", nargs="?")
    drill.add_argument("--all", action="store_true")
    drill.add_argument("--static-only", action="store_true")

    schedule = sub.add_parser("schedule", help="Install or remove a monthly drill")
    schedule_sub = schedule.add_subparsers(dest="schedule_command", required=True)
    install = schedule_sub.add_parser("install")
    install.add_argument("platform", choices=("macos", "windows", "linux"))
    remove = schedule_sub.add_parser("uninstall")
    remove.add_argument("platform", choices=("macos", "windows", "linux"))

    hostinger = sub.add_parser("hostinger", help="Read-only Hostinger listing")
    hostinger_sub = hostinger.add_subparsers(dest="hostinger_command", required=True)
    hostinger_sub.add_parser("sites")
    backups = hostinger_sub.add_parser("backups")
    backups.add_argument("--vps-id", type=int, default=None)

    token = sub.add_parser("token", help="Store the Hostinger token in the OS keychain")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    token_sub.add_parser("set")
    token_sub.add_parser("clear")

    sub.add_parser("validate", help="Check data/config.yaml")
    sub.add_parser("cleanup", help="Remove leftover brd- sandbox containers")

    args = parser.parse_args(argv)
    # Bundled application resources are immutable; state belongs beside the
    # user's writable configuration, not in the launcher's current directory.
    bundled = getattr(sys, "_MEIPASS", None)
    root = (args.root or (Path.home() / ".backuprestoredrill" if bundled else Path.cwd())).resolve()
    command = args.command or "serve"
    try:
        if command == "serve":
            return _serve(root, args)
        if command == "drill":
            return _drill(root, args)
        if command == "validate":
            _config, history, _reports, _work = _load(root)
            history.close()
            print(f"Config is valid: {root / 'data' / 'config.yaml'}")
            return 0
        if command == "schedule":
            return _schedule(root, args)
        if command == "hostinger":
            return _hostinger(args)
        if command == "token":
            return _token(args)
        if command == "cleanup":
            return _cleanup()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    print(f"Unknown command {command}", file=sys.stderr)
    return 2


def ensure_layout(root: Path) -> tuple[Path, Path, Path]:
    data = root / "data"
    reports = data / "reports"
    work = data / "work"
    reports.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    config_path = data / "config.yaml"
    bundled = getattr(sys, "_MEIPASS", None)
    example = (Path(bundled) / "config.example.yaml") if bundled else root / "config.example.yaml"
    if not config_path.exists():
        if not example.exists():
            raise ConfigError("config.example.yaml is missing, so there is nothing to start from.")
        shutil.copy(example, config_path)
        print(f"Created {config_path} from the example. Edit it before pointing it at real backups.")
    return config_path, reports, work


def asset_dir(root: Path) -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidate = Path(bundled) / "assets"
        if candidate.exists():
            return candidate
    return root / "assets"


def _load(root: Path):
    config_path, reports, work = ensure_layout(root)
    config = load_config(config_path)
    history = History(root / "data" / "drills.sqlite")
    return config, history, reports, work


def _serve(root: Path, args) -> int:
    config, history, reports, work = _load(root)
    if getattr(args, "static_only", False):
        config.static_only = True
    state = AppState(
        config=config,
        history=history,
        root=root,
        reports_dir=reports,
        work_root=work,
        assets=asset_dir(root),
    )
    port = args.port if getattr(args, "port", None) is not None else config.port
    httpd = make_server(state, port)
    bound = httpd.server_address[1]
    url = f"http://127.0.0.1:{bound}"
    print(f"BackupRestoreDrill is at {url}")
    print("It only listens on this computer. Close this window to stop it.")
    if not getattr(args, "no_browser", False) and config.open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        history.close()
    return 0


def _drill(root: Path, args) -> int:
    config, history, reports, work = _load(root)
    static_only = bool(getattr(args, "static_only", False) or config.static_only)
    kwargs = {
        "root": root,
        "history": history,
        "reports_dir": reports,
        "work_root": work,
        "static_only": static_only,
        "echo": True,
    }
    try:
        if args.all or not args.slug:
            if not args.all and not args.slug:
                print("Name a site slug, or pass --all.", file=sys.stderr)
                return 2
            results = run_all(config, **kwargs)
        else:
            results = [run_drill(config.site(args.slug), **kwargs)]
    finally:
        history.close()
    if not results:
        print("No enabled sites to drill.")
        return 1
    for result in results:
        label = "PASS" if result.passed else "FAIL"
        detail = result.error or f"{len(result.pages)} page(s), mode {result.mode}"
        print(f"{label}  {result.site_slug}  {detail}")
        if result.report_path:
            print(f"Report  {result.report_path}")
    return 0 if all(result.passed for result in results) else 1


def _schedule(root: Path, args) -> int:
    python = sys.executable
    if args.schedule_command == "install":
        print(
            "This installs a monthly drill on day 15 at 09:00. "
            "It does nothing until you run this command."
        )
        if args.platform == "linux":
            line = install_cron(python, root, _schedule_runner)
            print(f"Installed cron: {line}")
        elif args.platform == "macos":
            path = install_launchd(python, root, home=Path.home(), runner=_schedule_runner)
            print(f"Installed launchd agent: {path}")
        else:
            path = install_windows(
                python,
                root,
                xml_path=root / "data" / "BackupRestoreDrill-task.xml",
                runner=_schedule_runner,
            )
            print(f"Installed Task Scheduler task from {path}")
        return 0
    if args.platform == "linux":
        uninstall_cron(_schedule_runner)
    elif args.platform == "macos":
        uninstall_launchd(home=Path.home(), runner=_schedule_runner)
    else:
        uninstall_windows(_schedule_runner)
    print(f"Removed the monthly {args.platform} drill.")
    return 0


def _schedule_runner(args, input=None):
    import subprocess

    completed = subprocess.run(
        args,
        input=input,
        capture_output=True,
        check=False,
    )

    class _Result:
        returncode = completed.returncode
        stdout = completed.stdout.decode("utf-8", "replace")
        stderr = completed.stderr.decode("utf-8", "replace")

    return _Result()


def _hostinger(args) -> int:
    token = get_token()
    if not token:
        print(
            "No Hostinger token. Set HOSTINGER_API_TOKEN or run "
            "'python -m backuprestoredrill token set'.",
            file=sys.stderr,
        )
        return 2
    client = HostingerClient(token)
    try:
        if args.hostinger_command == "sites":
            for site in client.list_websites():
                print(f"{site.get('domain')}\t{site.get('website_type')}\tenabled={site.get('is_enabled')}")
            print(WEBSITE_BACKUP_NOTE)
            return 0
        print(WEBSITE_BACKUP_NOTE)
        if args.vps_id is not None:
            for item in client.list_vps_backups(args.vps_id):
                print(f"{item.get('id')}\t{item.get('created_at')}")
            print("Listed VPS backup metadata only. Nothing was downloaded or restored.")
        return 0
    except HostingerError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _token(args) -> int:
    if args.token_command == "clear":
        delete_token()
        print("Removed the Hostinger token from the OS keychain, if it was stored there.")
        return 0
    token = getpass.getpass("Hostinger API token (input hidden): ")
    try:
        set_token(token)
    except Exception as exc:  # noqa: BLE001 - keychain errors vary by OS
        print(f"Could not store the token in the OS keychain: {exc}", file=sys.stderr)
        print("You can still export HOSTINGER_API_TOKEN for this session.", file=sys.stderr)
        return 1
    print("Stored the token in the OS keychain. It was not written to the config file.")
    return 0


def _cleanup() -> int:
    docker = DockerCLI()
    ok, detail = docker.available()
    if not ok:
        print(detail)
        return 1
    removed = 0
    problems: list[str] = []
    for container in docker.list_sandbox_ids():
        result = docker.remove(container)
        if result.returncode == 0:
            removed += 1
        else:
            problems.append(f"container {container}: {(result.stderr or result.stdout).strip()}")
    for network in docker.list_sandbox_networks():
        result = docker.remove_network(network)
        if result.returncode != 0:
            problems.append(f"network {network}: {(result.stderr or result.stdout).strip()}")
    if problems:
        print("Cleanup incomplete; residual resources are owned by this run: " + "; ".join(problems), file=sys.stderr)
        return 1
    print(f"Removed {removed} sandbox container(s).")
    return 0
