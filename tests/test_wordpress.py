from __future__ import annotations

from backuprestoredrill.wordpress import replace_site_urls, rewrite_wp_config

PRODUCTION = "https://brochure.example.test"
SANDBOX = "http://127.0.0.1:8765"
WP_CONFIG = """<?php
define( 'DB_NAME', 'wp_prod' );
define( 'DB_USER', 'wp_prod' );
define( 'DB_PASSWORD', 'prod-secret-password' );
define( 'DB_HOST', 'localhost' );
$table_prefix = 'wp_';
/* That's all, stop editing! Happy publishing. */
"""


def test_wp_config_points_at_the_sandbox() -> None:
    updated = rewrite_wp_config(
        WP_CONFIG,
        db_name="drill",
        db_user="drill",
        db_password="sandbox-pass",
        db_host="db",
        site_url=SANDBOX,
    )
    assert "prod-secret-password" not in updated
    assert "define( 'DB_HOST', 'db' );" in updated
    assert f"define( 'WP_HOME', '{SANDBOX}' );" in updated
    assert f"define( 'WP_SITEURL', '{SANDBOX}' );" in updated
    assert "$table_prefix = 'wp_';" in updated


def test_double_quoted_defines_are_rewritten() -> None:
    text = '<?php\ndefine("DB_NAME", "wp_prod");\n'
    updated = rewrite_wp_config(
        text,
        db_name="drill",
        db_user="drill",
        db_password="p",
        db_host="db",
        site_url=SANDBOX,
    )
    assert "define( 'DB_NAME', 'drill' );" in updated
    assert "WP_HOME" in updated


def test_serialized_search_replace_updates_lengths() -> None:
    old = PRODUCTION
    assert len(old.encode()) == 29
    sql = (
        "INSERT INTO `wp_options` VALUES (1,'siteurl','https://brochure.example.test','yes');\n"
        'INSERT INTO `wp_options` VALUES (2,\'widget\',\'s:29:"https://brochure.example.test";\',\'yes\');\n'
        'INSERT INTO `wp_options` VALUES (3,\'other\',\'s:5:"hello";\',\'yes\');\n'
        "Visit https://brochure.example.test/about\n"
    )
    updated = replace_site_urls(sql, old + "/", SANDBOX + "/")
    assert "prod-secret" not in updated
    assert f's:{len(SANDBOX)}:"{SANDBOX}";' in updated
    assert 's:5:"hello";' in updated
    assert f"{SANDBOX}/about" in updated
    assert PRODUCTION not in updated


def test_multibyte_serialized_length_is_bytes() -> None:
    old = "café"
    new = "tea"
    sql = 's:5:"café";'
    updated = replace_site_urls(sql, old, new)
    assert updated == 's:3:"tea";'


def test_serialized_nested_escaped_and_repeated_urls_keep_byte_lengths() -> None:
    """Synthetic dump shapes that must not produce a false recovery pass."""
    old = PRODUCTION
    values = [
        old,
        old + "/again",
        old + r'/\"quoted\"\\path',
        old + "/caf\u00e9",
    ]

    def serialized(value: str) -> str:
        return f's:{len(value.encode("utf-8"))}:"{value}";'

    sql = "a:1:{s:7:\"widgets\";a:4:{" + "".join(
        f"i:{index};{serialized(value)}" for index, value in enumerate(values)
    ) + "}}"
    updated = replace_site_urls(sql, old, SANDBOX)

    # Walk the serialized leaves independently from the implementation: every
    # declared string length must match the exact UTF-8 bytes in that leaf.
    cursor = 0
    while True:
        start = updated.find("s:", cursor)
        if start < 0:
            break
        colon = updated.find(':"', start + 2)
        assert colon > start
        declared = int(updated[start + 2 : colon])
        content_start = colon + 2
        content = updated.encode("utf-8")[len(updated[:content_start].encode("utf-8")) :]
        assert len(content[:declared]) == declared
        assert content[declared : declared + 2] == b'";'
        cursor = content_start + len(content[:declared].decode("utf-8")) + 2

    assert old not in updated
    assert updated.count(SANDBOX) == len(values)
