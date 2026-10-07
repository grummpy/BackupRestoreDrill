"""Extract a backup archive or copy a folder, blocking path traversal."""

from __future__ import annotations

import os
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

DEFAULT_MAX_FILES = 20_000
DEFAULT_MAX_BYTES = 2_000_000_000


class ArchiveError(Exception):
    """The backup could not be restored safely."""


def extract_backup(
    source: Path,
    dest: Path,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Path:
    """Restore ``source`` into ``dest`` and return the content root.

    ``source`` may be a directory, ``.zip``, ``.tar``, ``.tar.gz``, or ``.tgz``.
    Members that would write outside ``dest`` are rejected.
    """
    source = source.expanduser().resolve()
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        raise ArchiveError(f"Backup not found: {source}")
    if source.is_dir():
        _copy_tree(source, dest, max_files=max_files, max_bytes=max_bytes)
    elif zipfile.is_zipfile(source):
        _extract_zip(source, dest, max_files=max_files, max_bytes=max_bytes)
    elif _is_tar(source):
        _extract_tar(source, dest, max_files=max_files, max_bytes=max_bytes)
    else:
        raise ArchiveError(
            f"Unsupported backup type for {source.name}. "
            "Use a .zip, .tar, .tar.gz, .tgz, or a folder."
        )
    return content_root(dest)


def content_root(dest: Path) -> Path:
    """Use the single top-level folder when the archive is wrapped in one."""
    entries = [
        path
        for path in dest.iterdir()
        if path.name not in {"__MACOSX"} and not path.name.startswith(".")
    ]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest


def safe_member_path(dest: Path, name: str) -> Path | None:
    """Return the destination for an archive member, or None for a directory.

    Raises ArchiveError for absolute paths, drive paths, and ``..`` segments.
    """
    name = name.replace("\\", "/").strip()
    if not name:
        raise ArchiveError("Blocked empty path in archive.")
    is_dir = name.endswith("/")
    if name.startswith("/") or name.startswith("~"):
        raise ArchiveError(f"Blocked absolute path in archive: {name}")
    parts = PurePosixPath(name).parts
    if any(part in {"..", ".", ""} for part in parts):
        raise ArchiveError(f"Blocked path traversal in archive: {name}")
    if parts and ":" in parts[0]:
        raise ArchiveError(f"Blocked drive path in archive: {name}")
    if is_dir:
        return None
    return dest.joinpath(*parts)


def _reject_symlink_escape(dest: Path, parts: tuple[str, ...]) -> Path:
    current = dest.resolve()
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ArchiveError(f"Blocked symlink while restoring: {current.name}")
    try:
        common = os.path.commonpath([str(dest.resolve()), str(current)])
    except ValueError as exc:
        raise ArchiveError("Blocked path that escapes the restore folder.") from exc
    if common != str(dest.resolve()):
        raise ArchiveError("Blocked path that escapes the restore folder.")
    return current


def _write_file(dest: Path, parts: tuple[str, ...], data: bytes, total: int, max_bytes: int) -> int:
    target = _reject_symlink_escape(dest, parts)
    if len(data) > max_bytes or total + len(data) > max_bytes:
        raise ArchiveError("Archive is too large to restore safely.")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ArchiveError(f"Blocked symlink while restoring: {target.name}")
    target.write_bytes(data)
    return total + len(data)


def _extract_zip(source: Path, dest: Path, *, max_files: int, max_bytes: int) -> None:
    files = 0
    total = 0
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            name = info.filename
            if name.startswith("__MACOSX/") or name.endswith("/"):
                if name.endswith("/"):
                    safe_member_path(dest, name)
                continue
            target = safe_member_path(dest, name)
            if target is None:
                continue
            if info.file_size > max_bytes:
                raise ArchiveError("Archive is too large to restore safely.")
            files += 1
            if files > max_files:
                raise ArchiveError("Archive has too many files to restore safely.")
            parts = PurePosixPath(name.replace("\\", "/")).parts
            total = _write_file(dest, parts, archive.read(info), total, max_bytes)


def _extract_tar(source: Path, dest: Path, *, max_files: int, max_bytes: int) -> None:
    files = 0
    total = 0
    with tarfile.open(source) as archive:
        for member in archive.getmembers():
            if member.issym() or member.islnk():
                raise ArchiveError(f"Blocked link in archive: {member.name}")
            if member.isdir():
                safe_member_path(dest, member.name if member.name.endswith("/") else member.name + "/")
                continue
            if not member.isfile():
                raise ArchiveError(f"Blocked special file in archive: {member.name}")
            safe_member_path(dest, member.name)
            if member.size > max_bytes:
                raise ArchiveError("Archive is too large to restore safely.")
            files += 1
            if files > max_files:
                raise ArchiveError("Archive has too many files to restore safely.")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ArchiveError(f"Could not read {member.name} from the archive.")
            data = extracted.read()
            parts = PurePosixPath(member.name.replace("\\", "/")).parts
            total = _write_file(dest, parts, data, total, max_bytes)


def _copy_tree(source: Path, dest: Path, *, max_files: int, max_bytes: int) -> None:
    files = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(dirpath)
        kept: list[str] = []
        for dirname in dirnames:
            candidate = current / dirname
            if candidate.is_symlink():
                _require_inside(candidate, source)
                continue
            kept.append(dirname)
        dirnames[:] = kept
        relative = current.relative_to(source)
        for filename in filenames:
            candidate = current / filename
            if candidate.is_symlink():
                _require_inside(candidate, source)
                continue
            files += 1
            if files > max_files:
                raise ArchiveError("Backup folder has too many files to restore safely.")
            data = candidate.read_bytes()
            parts = (relative / filename).parts if relative.parts else (filename,)
            parts = tuple(part for part in parts if part not in {".", ""})
            total = _write_file(dest, parts, data, total, max_bytes)


def _require_inside(path: Path, root: Path) -> None:
    try:
        resolved = path.resolve()
        common = os.path.commonpath([str(root.resolve()), str(resolved)])
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"Blocked symlink that escapes the backup folder: {path}") from exc
    if common != str(root.resolve()):
        raise ArchiveError(f"Blocked symlink that escapes the backup folder: {path}")


def _is_tar(path: Path) -> bool:
    try:
        return tarfile.is_tarfile(path)
    except (OSError, tarfile.TarError):
        return False
