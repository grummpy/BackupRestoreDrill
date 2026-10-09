from backuprestoredrill.docker import DockerError, RunResult


def test_cleanup_returns_nonzero_when_owned_removal_fails(monkeypatch):
    import backuprestoredrill.cli as cli

    class Docker:
        def available(self): return True, "ok"
        def list_sandbox_ids(self): return ["owned-id"]
        def list_sandbox_networks(self): return []
        def remove(self, name): return RunResult(1, "", "denied")

    monkeypatch.setattr(cli, "DockerCLI", Docker)
    assert cli._cleanup() == 1


def test_cleanup_does_not_hide_discovery_failure(monkeypatch):
    import backuprestoredrill.cli as cli

    class Docker:
        def available(self): return True, "ok"
        def list_sandbox_ids(self): raise DockerError("discovery failed")

    monkeypatch.setattr(cli, "DockerCLI", Docker)
    try:
        cli._cleanup()
    except DockerError as exc:
        assert "discovery failed" in str(exc)
    else:
        raise AssertionError("discovery failure was hidden")
