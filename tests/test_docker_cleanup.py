import pytest

from backuprestoredrill.docker import DockerCLI, DockerError, RunResult


def test_discovery_failure_is_not_an_empty_success():
    docker = DockerCLI(runner=lambda *args, **kwargs: RunResult(1, "", "daemon unavailable"))
    with pytest.raises(DockerError, match="daemon unavailable"):
        docker.list_sandbox_ids()


def test_preexisting_network_is_refused():
    docker = DockerCLI(runner=lambda *args, **kwargs: RunResult(1, "", "network brd-x already exists"))
    with pytest.raises(DockerError, match="already exists"):
        docker.create_network("brd-x")
