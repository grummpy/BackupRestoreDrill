"""Point a restored WordPress tree at the sandbox database and URL."""

from __future__ import annotations

import os
import re
from pathlib import Path

_DEFINE_LINE = re.compile(
    r"""^(?P<indent>\s*)define\(\s*(?P<q>['\"])(?P<name>[A-Za-z0-9_]+)(?P=q)\s*,\s*(?P<vq>['\"]).*?(?P=vq)\s*\)\s*;(?P<trail>[^\n]*)$""",
    re.IGNORECASE,
)
STOP_EDITING = "/* That's all, stop editing!"


class WordPressError(Exception):
    """The WordPress files could not be prepared for the sandbox."""


def find_wp_config(root: Path) -> Path | None:
    matches: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        relative = Path(dirpath).relative_to(root)
        if len(relative.parts) > 4:
            dirnames.clear()
            continue
        if "wp-config.php" in filenames:
            matches.append(Path(dirpath) / "wp-config.php")
    if not matches:
        return None
    matches.sort(key=lambda path: (len(path.parts), str(path)))
    return matches[0]


def rewrite_wp_config(
    text: str,
    *,
    db_name: str,
    db_user: str,
    db_password: str,
    db_host: str,
    site_url: str,
) -> str:
    """Replace database defines and force WP_HOME / WP_SITEURL to the sandbox."""
    values = {
        "DB_NAME": db_name,
        "DB_USER": db_user,
        "DB_PASSWORD": db_password,
        "DB_HOST": db_host,
        "WP_HOME": site_url,
        "WP_SITEURL": site_url,
    }
    seen: set[str] = set()
    lines: list[str] = []
    newline = "\n" if text.endswith("\n") or "\n" in text else ""
    raw_lines = text.splitlines()
    for line in raw_lines:
        match = _DEFINE_LINE.match(line)
        if match and match.group("name").upper() in values:
            name = match.group("name").upper()
            seen.add(name)
            lines.append(
                f"{match.group('indent')}define( '{name}', {_php_quote(values[name])} );"
                f"{match.group('trail')}"
            )
        else:
            lines.append(line)
    updated = "\n".join(lines)
    if text.endswith("\n"):
        updated += "\n"
    elif newline and not updated.endswith("\n"):
        updated += newline
    missing = [name for name in ("WP_HOME", "WP_SITEURL") if name not in seen]
    if missing:
        extra = "".join(
            f"define( '{name}', {_php_quote(values[name])} );\n" for name in missing
        )
        extra += "define( 'WP_ENVIRONMENT_TYPE', 'local' );\n"
        if STOP_EDITING in updated:
            updated = updated.replace(STOP_EDITING, extra + STOP_EDITING, 1)
        else:
            if not updated.endswith("\n"):
                updated += "\n"
            updated += extra
    return updated


def replace_site_urls(sql: str, production_url: str, sandbox_url: str) -> str:
    """Replace the production URL, including PHP serialized string lengths."""
    old = production_url.rstrip("/")
    new = sandbox_url.rstrip("/")
    if not old or old == new:
        return sql
    return _replace_serialized(sql, old, new)


def _replace_serialized(sql: str, old: str, new: str) -> str:
    data = sql.encode("utf-8")
    old_b = old.encode("utf-8")
    new_b = new.encode("utf-8")
    out = bytearray()
    index = 0
    length = len(data)
    while index < length:
        if data[index : index + 2] == b"s:" and index + 2 < length and 48 <= data[index + 2] <= 57:
            cursor = index + 2
            while cursor < length and 48 <= data[cursor] <= 57:
                cursor += 1
            if cursor + 1 < length and data[cursor : cursor + 2] == b':"':
                declared = int(data[index + 2 : cursor])
                start = cursor + 2
                end = start + declared
                if 0 <= declared and end + 1 < length and data[end : end + 2] == b'";':
                    content = bytes(data[start:end])
                    if old_b in content:
                        content = content.replace(old_b, new_b)
                        out += b"s:" + str(len(content)).encode("ascii") + b':"' + content + b'";'
                    else:
                        out += data[index : end + 2]
                    index = end + 2
                    continue
        out.append(data[index])
        index += 1
    return bytes(out).replace(old_b, new_b).decode("utf-8")


def _php_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"
