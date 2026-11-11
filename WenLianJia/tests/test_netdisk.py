"""Baidu client configuration, path safety and error contract tests.

These tests never call Baidu.  Live OAuth/upload/share remains unverified until
the owner supplies application credentials.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from core.baidu_netdisk import BaiduNetdisk, NetdiskError, block_md5s, md5_bytes


def _client(app) -> BaiduNetdisk:
    app.settings.set("netdisk.app_key", "test-app-key")
    app.settings.set("netdisk.app_name", "wlj-test")
    app.settings.set("netdisk.root_dir", "/爱做题的大肥鱼_商品库")
    app.secrets.set("baidu_app_secret", "test-secret")
    return BaiduNetdisk(app.settings, app.secrets, db=app.db, paths=app.paths)


def test_configuration_and_effective_app_root(app) -> None:
    netdisk = _client(app)
    assert netdisk.configured() is True
    assert netdisk.effective_root == "/apps/wlj-test/爱做题的大肥鱼_商品库"
    assert netdisk.guard_path(netdisk.effective_root + "/P001_测试/01_习题详解") == (
        "/apps/wlj-test/爱做题的大肥鱼_商品库/P001_测试/01_习题详解"
    )


def test_path_outside_app_root_is_rejected(app) -> None:
    netdisk = _client(app)
    with pytest.raises(NetdiskError, match="根目录之外"):
        netdisk.guard_path("/别的地方/文件.pdf")


def test_authorize_url_uses_app_key_and_netdisk_scope(app) -> None:
    netdisk = _client(app)
    query = parse_qs(urlparse(netdisk.authorize_url(state="abc")).query)
    assert query["client_id"] == ["test-app-key"]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == ["oob"]
    assert query["state"] == ["abc"]
    assert "netdisk" in query["scope"][0]


def test_errno_zero_string_is_success_and_auth_error_is_actionable() -> None:
    assert BaiduNetdisk._check({"errno": "0"}, "测试")["errno"] == "0"
    assert BaiduNetdisk._check({"errno": -8}, "创建目录", allow_exists=True)["errno"] == -8
    with pytest.raises(NetdiskError) as caught:
        BaiduNetdisk._check({"errno": -6, "errmsg": "bad token"}, "列出目录")
    assert caught.value.needs_auth is True
    assert caught.value.errno == -6


def test_share_permission_error_is_marked_manual() -> None:
    with pytest.raises(NetdiskError) as caught:
        BaiduNetdisk._check({"errno": 42000}, "创建分享")
    assert caught.value.needs_manual is True


def test_block_hashes_cover_every_four_megabyte_part(tmp_path) -> None:
    path = tmp_path / "blob.bin"
    path.write_bytes(b"a" * (4 * 1024 * 1024) + b"tail")
    hashes = block_md5s(path)
    assert len(hashes) == 2
    assert hashes[0] == md5_bytes(b"a" * (4 * 1024 * 1024))
    assert hashes[1] == md5_bytes(b"tail")


def test_configured_requires_all_three_values(app) -> None:
    netdisk = _client(app)
    app.secrets.delete("baidu_app_secret")
    assert netdisk.configured() is False
    status = netdisk.status()
    assert status.configured is False
    assert "AppKey" in status.message


def test_token_roundtrip_is_encrypted_on_disk(app) -> None:
    netdisk = _client(app)
    app.secrets.set("baidu_token", '{"access_token":"tok","expires_at":9999999999}')
    assert netdisk.has_token() is True
    assert netdisk.access_token() == "tok"
    raw = app.paths.secrets.read_text(encoding="utf-8")
    assert "tok" not in raw
