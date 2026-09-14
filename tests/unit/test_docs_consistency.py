"""实施文档与实际代码的一致性。

运维手册最容易腐化：脚本改名、配置项重命名、端口变了，文档还停在旧版本，
而执行人是照着文档敲命令的 —— 错一处就卡在半路。

所以把「文档里提到的东西必须真实存在」写成测试。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

DOC = Path("docs/IMPLEMENTATION.md")
README = Path("README.md")
CONFIG = Path("config/config.yaml")


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def test_doc_exists() -> None:
    assert DOC.is_file()


def test_readme_links_to_it() -> None:
    assert "docs/IMPLEMENTATION.md" in README.read_text(encoding="utf-8")


# ── 文档提到的脚本必须存在 ────────────────────────────────────────────────


def test_referenced_scripts_exist() -> None:
    referenced = set(re.findall(r"\./scripts/([\w-]+\.sh)", _doc()))
    assert referenced, "文档里应当出现脚本调用"
    missing = {s for s in referenced if not (Path("scripts") / s).is_file()}
    assert not missing, f"文档引用了不存在的脚本: {sorted(missing)}"


def test_all_scripts_are_documented() -> None:
    """反向：每个脚本都该在手册里出现过，否则执行人不知道它干什么。"""
    referenced = set(re.findall(r"\./scripts/([\w-]+\.sh)", _doc()))
    actual = {p.name for p in Path("scripts").glob("*.sh")}
    assert actual - referenced == set(), f"这些脚本没写进手册: {sorted(actual - referenced)}"


# ── 文档提到的配置项必须存在 ──────────────────────────────────────────────


DOCUMENTED_CONFIG_KEYS = [
    ("model", "path"),
    ("fonts", "cjk_candidates"),
    ("doc_conversion", "enabled"),
    ("doc_conversion", "soffice_path"),
    ("server", "port"),
    ("llama", "base_url"),
]


@pytest.mark.parametrize(("section", "key"), DOCUMENTED_CONFIG_KEYS)
def test_documented_config_keys_exist(section: str, key: str) -> None:
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert section in cfg, f"config.yaml 缺少 {section} 节"
    assert key in cfg[section], f"config.yaml 的 {section} 缺少 {key}"


# ── 文档提到的环境变量必须被代码真正读取 ──────────────────────────────────


@pytest.mark.parametrize(
    ("env_var", "where"),
    [
        ("MODEL_FILE", "scripts/install.sh"),
        ("INSTALL_ROOT", "scripts/install.sh"),
        ("MODEL_REPO", "scripts/prepare-bundle.sh"),
        ("OFFLINE_TRANSLATOR_CONFIG", "app/main.py"),
    ],
)
def test_documented_env_vars_are_read(env_var: str, where: str) -> None:
    assert env_var in _doc(), f"手册里应当说明 {env_var}"
    assert env_var in Path(where).read_text(encoding="utf-8"), (
        f"{where} 并没有读取 {env_var}"
    )


# ── 端口必须与配置一致 ────────────────────────────────────────────────────


def test_documented_ports_match_config() -> None:
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    api_port = cfg["server"]["port"]
    assert f"127.0.0.1:{api_port}" in _doc(), f"手册里的 API 端口与配置（{api_port}）不符"
    assert cfg["llama"]["base_url"].endswith(":8001")
    assert "8001" in _doc()


# ── 锁文件的交接必须闭环 ──────────────────────────────────────────────────


def test_requirements_lock_handoff_is_closed() -> None:
    """prepare-bundle 产出、install 消费，两边必须指向 bundle 内的同一路径。

    这里曾经断过：prepare-bundle 把锁文件编译到仓库根目录，
    而 install.sh 从 $ROOT 找，离线机上根本没有这个文件。
    """
    prepare = Path("scripts/prepare-bundle.sh").read_text(encoding="utf-8")
    install = Path("scripts/install.sh").read_text(encoding="utf-8")
    assert '"$BUNDLE/requirements.txt"' in prepare, "备料脚本应把锁文件写进 bundle"
    assert '"$BUNDLE/requirements.txt"' in install, "安装脚本应从 bundle 读锁文件"


# ── 手册必须覆盖关键前提 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "topic",
    [
        "许可证",        # §3.3 阻塞项
        "自动登录",      # §17.1 LaunchAgent 的前提
        "LaunchDaemon",  # 为什么不用它
        "TCC",           # §17.2 路径选择理由
        "OTF",           # 字体陷阱
        "扫描页",        # docling OCR 模型预热
        "lsof",          # §28.5 第 5 步
    ],
)
def test_doc_covers_critical_prerequisites(topic: str) -> None:
    assert topic in _doc(), f"实施手册未覆盖关键前提: {topic}"


def test_doc_states_its_verification_status() -> None:
    """手册没在真机上跑过，必须自己说清楚，不能让人误以为已验证。"""
    doc = _doc()
    assert "尚未在真实 Mac mini M4 上端到端执行过" in doc
