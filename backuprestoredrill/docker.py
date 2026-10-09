"""A thin Docker CLI wrapper. The sandbox is the only caller."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


class DockerError(Exception):
    """Docker could not complete a sandbox command."""


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


DOCKER_SETUP = (
    "Docker was not found, so container sandboxes are unavailable. "
    "Install Docker Desktop from https://docs.docker.com/get-docker/ and start it, "
    "then run the drill again. Static sites can still be checked in static-only mode, "
    "which serves the restored files with Python's built-in web server on 127.0.0.1. "
    "WordPress drills need Docker (php-apache and MariaDB)."
)


class DockerCLI:
    """Run ``docker`` subcommands. Ports must be published on 127.0.0.1 only."""

    def __init__(self, runner=None):
        self._runner = runner or _subprocess_run

    def available(self) -> tuple[bool, str]:
        try:
            result = self._runner(["version", "--format", "{{.Server.Version}}"], input=None, timeout=8)
        except FileNotFoundError:
            return False, DOCKER_SETUP
        except subprocess.TimeoutExpired:
            return False, "Docker did not answer. Start Docker Desktop and try the drill again."
        except OSError as exc:
            return False, f"{DOCKER_SETUP} ({exc})"
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            return False, (
                "Docker is installed but the engine is not running. "
                "Start Docker Desktop and try the drill again. "
                + detail
            )
        version = result.stdout.strip() or "unknown"
        return True, f"Docker {version}"

    def create_network(self, name: str) -> None:
        result = self._runner(["network", "create", "--label", "backuprestoredrill=1", name], input=None, timeout=30)
        if result.returncode != 0 and "already exists" not in (result.stderr or ""):
            raise DockerError(result.stderr.strip() or "Could not create the sandbox network.")

    def remove_network(self, name: str) -> RunResult:
        return self._runner(["network", "rm", name], input=None, timeout=30)

    def run(
        self,
        *,
        name: str,
        image: str,
        network: str,
        publish: str | None,
        volumes: list[tuple[str, str]],
        env: dict[str, str],
        command: list[str] | None = None,
        aliases: list[str] | None = None,
        extra_hosts: list[str] | None = None,
    ) -> str:
        if publish is not None and not publish.startswith("127.0.0.1:"):
            raise DockerError("Sandbox ports must bind to 127.0.0.1 only.")
        args = ["run", "-d", "--name", name, "--label", "backuprestoredrill=1", "--network", network]
        for alias in aliases or []:
            args += ["--network-alias", alias]
        for host in extra_hosts or []:
            args += ["--add-host", host]
        if publish:
            args += ["-p", publish]
        for host_path, container_path in volumes:
            args += ["-v", f"{host_path}:{container_path}"]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        args.append(image)
        if command:
            args.extend(command)
        result = self._runner(args, input=None, timeout=180)
        if result.returncode != 0:
            raise DockerError(result.stderr.strip() or f"docker run {image} failed")
        container_id = result.stdout.strip()
        if not container_id:
            raise DockerError(f"docker run {image} did not return a container id")
        return container_id

    def exec(
        self,
        container: str,
        command: list[str],
        *,
        input_bytes: bytes | None = None,
    ) -> RunResult:
        return self._runner(
            ["exec", "-i", container, *command],
            input=input_bytes,
            timeout=180,
        )

    def logs(self, container: str) -> str:
        result = self._runner(["logs", "--tail", "80", container], input=None, timeout=30)
        return (result.stdout + result.stderr)[-4000:]

    def remove(self, container: str) -> RunResult:
        return self._runner(["rm", "-f", container], input=None, timeout=60)

    def list_sandbox_ids(self) -> list[str]:
        result = self._runner(
            ["ps", "-aq", "--filter", "label=backuprestoredrill=1"],
            input=None,
            timeout=30,
        )
        if result.returncode != 0:
            raise DockerError(result.stderr.strip() or "Could not discover owned sandbox containers.")
        return [line for line in result.stdout.split() if line]

    def list_sandbox_networks(self) -> list[str]:
        result = self._runner(
            ["network", "ls", "--filter", "label=backuprestoredrill=1", "--format", "{{.Name}}"],
            input=None,
            timeout=30,
        )
        if result.returncode != 0:
            raise DockerError(result.stderr.strip() or "Could not discover owned sandbox networks.")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _subprocess_run(args: list[str], *, input: bytes | None, timeout: int) -> RunResult:
    completed = subprocess.run(
        ["docker", *args],
        input=input,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return RunResult(
        returncode=completed.returncode,
        stdout=completed.stdout.decode("utf-8", "replace"),
        stderr=completed.stderr.decode("utf-8", "replace"),
    )
