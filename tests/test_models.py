import base64

import pytest
from astrbot_plugin_magnet.config import Settings
from astrbot_plugin_magnet.models import BTError, normalize_magnet, safe_file, safe_rel, select_indices


def test_magnet_canonical_and_ignores_untrusted_urls():
    h = "ab" * 20
    b32 = base64.b32encode(bytes.fromhex(h)).decode()
    actual, uri = normalize_magnet(
        "magnet:?xt=urn:btih:" + b32 + "&ws=http://127.0.0.1/secret&tr=http://internal/"
    )
    assert actual == h
    assert "internal" not in uri and "secret" not in uri


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/a",
        "magnet:?xt=urn:btih:xxx",
        "magnet:?xt=urn:btmh:1220abc",
        "magnet:?xt=urn:btih:" + "a" * 40 + "&xt=urn:btih:" + "b" * 40,
    ],
)
def test_invalid_magnets(uri):
    with pytest.raises(BTError):
        normalize_magnet(uri)


@pytest.mark.parametrize(
    "expr,expected", [("1,3", [0, 2]), ("2-4", [1, 2, 3]), ("1,1,2", [0, 1]), ("all", [0, 1, 2, 3])]
)
def test_selection(expr, expected):
    assert select_indices(expr, 4, 4) == expected


@pytest.mark.parametrize("expr", ["0", "1-999999999999", "5", "3-1", "", "1;rm", "1,,2"])
def test_invalid_selection(expr):
    with pytest.raises(BTError):
        select_indices(expr, 4, 4)


def test_selection_limit():
    with pytest.raises(BTError):
        select_indices("all", 100, 20)


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "x/../../y", "a\\b", "C:/hello", "a\x00b", "./a"])
def test_unsafe_paths(path):
    with pytest.raises(BTError):
        safe_rel(path)


def test_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(tmp_path)
    with pytest.raises(BTError):
        safe_file(root, "link/file")


def test_defaults_schema_and_settings():
    assert Settings.from_mapping({}) == Settings()
    with pytest.raises(ValueError):
        Settings.from_mapping({"max_concurrent": 0})
    with pytest.raises(ValueError):
        Settings.from_mapping({"local_root": "/"})
    with pytest.raises(ValueError):
        Settings.from_mapping({"failure_retention": 1})
