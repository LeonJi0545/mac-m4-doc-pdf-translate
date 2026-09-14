"""M0 基座测试：配置加载与离线守卫。"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import ConfigError, OfflineViolation
from app.core.offline import (
    OFFLINE_ENV,
    apply_offline_guard,
    assert_local_model_path,
    assert_loopback,
)
from tests.conftest import OutboundNetworkBlocked

# ── 配置 ──────────────────────────────────────────────────────────────────


def test_repo_config_loads() -> None:
    """仓库自带的 config/config.yaml 必须能被解析。"""
    settings = Settings.load(Path("config/config.yaml"))
    assert settings.server.host == "127.0.0.1"
    assert settings.sampling.temperature == 0.7
    assert settings.sampling.top_k == 20
    assert settings.sampling.top_p == 0.6
    assert settings.sampling.repeat_penalty == 1.05


def test_config_missing_file() -> None:
    with pytest.raises(ConfigError, match="配置文件不存在"):
        Settings.load(Path("config/does-not-exist.yaml"))


def test_config_bad_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("server: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="解析失败"):
        Settings.load(bad)


def test_config_root_must_be_mapping(tmp_path: Path) -> None:
    bad = tmp_path / "list.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="根节点必须是映射"):
        Settings.load(bad)


def test_paths_expand_user(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text('pdf:\n  docling_artifacts_path: "~/.cache/docling"\n', encoding="utf-8")
    settings = Settings.load(cfg)
    assert "~" not in settings.pdf.docling_artifacts_path


# ── 离线守卫 ──────────────────────────────────────────────────────────────


def test_apply_offline_guard_sets_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in OFFLINE_ENV:
        monkeypatch.delenv(key, raising=False)
    apply_offline_guard(artifacts_path="/tmp/docling")
    for key, value in OFFLINE_ENV.items():
        import os

        assert os.environ[key] == value


def test_apply_offline_guard_does_not_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """已有取值优先 —— 运维可能有意设成别的值，不要粗暴覆盖。"""
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    apply_offline_guard()
    import os

    assert os.environ["HF_HUB_OFFLINE"] == "0"


@pytest.mark.parametrize(
    "bad_path",
    [
        "tencent/HY-MT1.5-1.8B-GGUF:Q4_K_M",
        "tencent/HY-MT1.5-7B-GGUF:Q8_0",
        "tencent/Hy-MT2-1.8B-GGUF",
    ],
)
def test_model_path_rejects_hf_repo_id(bad_path: str) -> None:
    """方案 §4.2：-hf 会联网从 Hugging Face 拉取，必须拦下来。"""
    with pytest.raises(OfflineViolation) as exc:
        assert_local_model_path(bad_path)
    assert "-hf" in str(exc.value)
    assert "本地" in str(exc.value)


def test_model_path_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(OfflineViolation, match="模型文件不存在"):
        assert_local_model_path(str(tmp_path / "nope.gguf"))


def test_model_path_rejects_empty() -> None:
    with pytest.raises(OfflineViolation, match="未配置模型路径"):
        assert_local_model_path("")


def test_model_path_accepts_local_file(tmp_path: Path) -> None:
    f = tmp_path / "m.gguf"
    f.write_bytes(b"x")
    assert assert_local_model_path(str(f)) == f


@pytest.mark.parametrize("url", ["http://127.0.0.1:8001", "http://localhost:8001"])
def test_loopback_accepted(url: str) -> None:
    assert assert_loopback(url) == url


@pytest.mark.parametrize(
    "url", ["http://10.0.0.1:8001", "http://example.com", "https://huggingface.co"]
)
def test_non_loopback_rejected(url: str) -> None:
    with pytest.raises(OfflineViolation, match="loopback"):
        assert_loopback(url)


def test_non_http_scheme_rejected() -> None:
    with pytest.raises(OfflineViolation, match="http"):
        assert_loopback("ftp://127.0.0.1")


# ── 网络守卫本身 ──────────────────────────────────────────────────────────


def test_outbound_network_is_blocked() -> None:
    """守卫必须真的拦得住 —— 否则 AC-P4 是空的。"""
    s = socket.socket()
    try:
        with pytest.raises(OutboundNetworkBlocked):
            s.connect(("93.184.216.34", 80))
    finally:
        s.close()
