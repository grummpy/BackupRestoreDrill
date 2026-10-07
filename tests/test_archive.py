from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from backuprestoredrill.archive import ArchiveError, extract_backup


def test_zip_round_trip_and_single_root(tmp_path: Path) -> None:
    source = tmp_path / "site.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("brochure/index.html", "<h1>Hello</h1>")
        archive.writestr("brochure/about.html", "about")
    dest = tmp_path / "out"
    root = extract_backup(source, dest)
    assert root.name == "brochure"
    assert (root / "index.html").read_text(encoding="utf-8") == "<h1>Hello</h1>"


def test_zip_slip_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "evil.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("../escaped.txt", "nope")
        archive.writestr("/tmp/escaped.txt", "nope")
        archive.writestr("good/../../escaped.txt", "nope")
    dest = tmp_path / "out"
    with pytest.raises(ArchiveError):
        extract_backup(source, dest)
    assert not (tmp_path / "escaped.txt").exists()
    assert not Path("/tmp/escaped.txt").exists() or Path("/tmp/escaped.txt").read_text(
        encoding="utf-8", errors="replace"
    ) != "nope"


def test_tar_slip_and_symlink_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "evil.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        data = b"nope"
        info = tarfile.TarInfo("../escaped.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    with pytest.raises(ArchiveError):
        extract_backup(source, tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()

    linked = tmp_path / "linked.tar"
    with tarfile.open(linked, "w") as archive:
        info = tarfile.TarInfo("alias")
        info.type = tarfile.SYMTYPE
        info.linkname = "../outside"
        archive.addfile(info)
    with pytest.raises(ArchiveError, match="link"):
        extract_backup(linked, tmp_path / "out2")


def test_directory_copy_blocks_escaping_symlink(tmp_path: Path) -> None:
    source = tmp_path / "backup"
    source.mkdir()
    (source / "index.html").write_text("ok", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (source / "leak").symlink_to(outside)
    with pytest.raises(ArchiveError, match="symlink"):
        extract_backup(source, tmp_path / "out")


def test_directory_copy_and_size_limit(tmp_path: Path) -> None:
    source = tmp_path / "backup"
    nested = source / "public_html"
    nested.mkdir(parents=True)
    (nested / "index.html").write_text("home", encoding="utf-8")
    (source / "notes.txt").write_text("keep", encoding="utf-8")
    root = extract_backup(source, tmp_path / "out")
    assert (root / "public_html" / "index.html").read_text(encoding="utf-8") == "home"
    assert (root / "notes.txt").read_text(encoding="utf-8") == "keep"

    with pytest.raises(ArchiveError, match="too many"):
        extract_backup(source, tmp_path / "limited", max_files=1)


def test_unsupported_file(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("hello", encoding="utf-8")
    with pytest.raises(ArchiveError, match="Unsupported"):
        extract_backup(source, tmp_path / "out")
