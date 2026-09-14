"""全局测试夹具。

两件事：

1. **网络守卫** —— 方案 §20 / §22 要求运行期零出站连接。测试层面把它变成硬断言：
   任何指向非 loopback 地址的 socket 连接直接抛异常。这样「不小心引入了联网行为」
   会在 CI 里立刻暴露，而不是等到断网验收（§28.5）才发现。

2. **FakeProvider** —— 无真实模型时的确定性翻译实现。
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from app.core.config import Settings
from app.translation.base import ProviderHealth, TranslationProvider

_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


class OutboundNetworkBlocked(AssertionError):
    """测试期试图连接非 loopback 地址。"""


def _host_of(address: object) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return ""


@pytest.fixture(autouse=True, scope="session")
def _block_outbound_network() -> None:
    """禁止一切非 loopback 的出站连接。"""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address, *args, **kwargs):  # type: ignore[no-untyped-def]
        host = _host_of(address)
        if host not in _ALLOWED_HOSTS:
            raise OutboundNetworkBlocked(
                f"测试期禁止出站网络连接（尝试连接 {host}）。"
                "本系统要求完全离线运行，见方案 §20 / §22。"
            )
        return real_connect(self, address, *args, **kwargs)

    def guarded_connect_ex(self, address, *args, **kwargs):  # type: ignore[no-untyped-def]
        host = _host_of(address)
        if host not in _ALLOWED_HOSTS:
            raise OutboundNetworkBlocked(
                f"测试期禁止出站网络连接（尝试连接 {host}）。"
            )
        return real_connect_ex(self, address, *args, **kwargs)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]


class FakeProvider(TranslationProvider):
    """确定性的假 Provider。

    行为刻意设计成能被 QA 检查通过：**原样保留**数字、URL、邮箱、日期、货币，
    只在前面加一个语言标记。这样 QA 的「源有译文无」告警只会在真出问题时触发，
    而不是被假数据本身触发。
    """

    def __init__(self, prefix: str = "[zh]") -> None:
        self.prefix = prefix
        self.calls: list[dict[str, object]] = []

    def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
        glossary: dict | None = None,
        context: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "text": text,
                "source_language": source_language,
                "target_language": target_language,
                "glossary": glossary,
                "context": context,
            }
        )
        if not text.strip():
            return text
        return f"{self.prefix}{text}"

    def health(self) -> ProviderHealth:
        return ProviderHealth(ready=True, detail="fake")


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    """指向临时目录的 Settings，避免测试污染真实 data/ logs/。"""
    model_file = tmp_path / "model.gguf"
    model_file.write_bytes(b"GGUF-stub")
    glossary_file = tmp_path / "terms.yaml"
    glossary_file.write_text("terms:\n  Tenant: 租户\n", encoding="utf-8")
    return Settings.model_validate(
        {
            "model": {"path": str(model_file)},
            "glossary": {"path": str(glossary_file)},
            "paths": {
                "install_root": str(tmp_path),
                "data_dir": str(tmp_path / "data"),
                "temp_dir": str(tmp_path / "data" / "temp"),
                "processing_dir": str(tmp_path / "data" / "processing"),
                "logs_dir": str(tmp_path / "logs"),
                "db_path": str(tmp_path / "data" / "jobs.db"),
            },
            "logging": {"file": str(tmp_path / "logs" / "app.log")},
        }
    )
