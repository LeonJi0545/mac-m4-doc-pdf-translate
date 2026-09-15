"""M4 测试：launchd plist、shell 脚本、UI 范围、README（AC-4.7 ~ AC-4.14）。

这些都是**静态可验证**的部分。macOS 上的实际行为（launchctl 真的拉起服务、
断网重启、真实 curl）属 DEPLOY 项，本次不声称通过。
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path

import pytest

PLIST_DIR = Path("deploy/launchd")
SCRIPTS_DIR = Path("scripts")
UI = Path("app/web/index.html")
README = Path("README.md")

PLISTS = ["com.offline-translator.llama.plist", "com.offline-translator.api.plist"]


def _load(name: str) -> dict:
    with (PLIST_DIR / name).open("rb") as fh:
        return plistlib.load(fh)


# ── AC-4.8 ~ AC-4.10 launchd ──────────────────────────────────────────────


@pytest.mark.parametrize("name", PLISTS)
def test_plist_parses(name: str) -> None:
    assert isinstance(_load(name), dict)


@pytest.mark.parametrize("name", PLISTS)
def test_label_matches_filename(name: str) -> None:
    assert _load(name)["Label"] == name.removesuffix(".plist")


@pytest.mark.parametrize("name", PLISTS)
def test_required_keys_present(name: str) -> None:
    data = _load(name)
    assert data["RunAtLoad"] is True      # §17 开机自动启动
    assert data["KeepAlive"] is True      # §17 异常自动重启
    assert data["StandardOutPath"]        # §17 日志本地保存
    assert data["StandardErrorPath"]
    assert data["WorkingDirectory"]


def _render(name: str, root: str = "/Users/Shared/offline-translator",
            model_file: str = "HY-MT1.5-1.8B-Q4_K_M.gguf") -> dict:
    """按 install.sh 的方式渲染模板，再解析 —— 验的是**实际下发的内容**。"""
    raw = (PLIST_DIR / name).read_text(encoding="utf-8")
    rendered = raw.replace("@@INSTALL_ROOT@@", root).replace("@@MODEL_FILE@@", model_file)
    return plistlib.loads(rendered.encode("utf-8"))


def test_llama_plist_has_ngl_and_local_model() -> None:
    args = _render("com.offline-translator.llama.plist")["ProgramArguments"]

    # -ngl 99：把全部层交给 Metal。漏了就退化成纯 CPU。
    assert "-ngl" in args and args[args.index("-ngl") + 1] == "99"

    # 模型走本地绝对路径，而不是 HF repo id（那是 -hf 的写法）
    assert "-m" in args
    model_arg = args[args.index("-m") + 1]
    assert model_arg.startswith("/") and model_arg.endswith(".gguf")
    assert not re.match(r"^[\w.-]+/[\w.-]+(:[\w.]+)?$", model_arg)

    # 只监听 loopback
    assert args[args.index("--host") + 1] == "127.0.0.1"


def test_rendered_paths_are_under_shared() -> None:
    """渲染后所有路径都在 /Users/Shared 下，避开 TCC（§17.2）。"""
    for name in PLISTS:
        data = _render(name)
        assert data["WorkingDirectory"].startswith("/Users/Shared/")
        assert data["StandardOutPath"].startswith("/Users/Shared/")
        assert data["StandardErrorPath"].startswith("/Users/Shared/")


@pytest.mark.parametrize("name", PLISTS)
def test_no_hf_flag_in_plist(name: str) -> None:
    """方案 §4.2 / §26：-hf 会联网从 Hugging Face 拉取。"""
    raw = (PLIST_DIR / name).read_text(encoding="utf-8")
    assert not re.search(r"(^|\s|>)-hf(\s|<|$)", raw)
    args = _load(name)["ProgramArguments"]
    assert "-hf" not in args


def test_api_plist_runs_module_entrypoint() -> None:
    args = _load("com.offline-translator.api.plist")["ProgramArguments"]
    assert args[-2:] == ["-m", "app.main"]
    assert args[0].endswith(".venv/bin/python")


@pytest.mark.parametrize("name", PLISTS)
def test_paths_are_templated_to_shared(name: str) -> None:
    """默认路径落在 /Users/Shared，避开 TCC 保护目录（§17.2）。"""
    raw = (PLIST_DIR / name).read_text(encoding="utf-8")
    assert "@@INSTALL_ROOT@@" in raw, "路径应是占位符，由 install.sh 渲染"
    assert "~/Documents" not in raw and "/Users/leon/Documents" not in raw


# ── AC-4.11 ~ AC-4.13 脚本 ────────────────────────────────────────────────

EXPECTED_SCRIPTS = {
    "prepare-bundle.sh", "install.sh", "start.sh", "stop.sh",
    "health.sh", "backup.sh", "offline-check.sh",
}


def test_all_scripts_present() -> None:
    assert {p.name for p in SCRIPTS_DIR.glob("*.sh")} == EXPECTED_SCRIPTS


@pytest.mark.parametrize("name", sorted(EXPECTED_SCRIPTS))
def test_script_syntax_is_valid(name: str) -> None:
    proc = subprocess.run(
        ["bash", "-n", str(SCRIPTS_DIR / name)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("name", sorted(EXPECTED_SCRIPTS))
def test_script_has_strict_mode(name: str) -> None:
    text = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text


@pytest.mark.parametrize(
    ("script", "modern", "legacy"),
    [("start.sh", "launchctl bootstrap", "launchctl load"),
     ("stop.sh", "launchctl bootout", "launchctl unload")],
)
def test_launchctl_uses_modern_api(script: str, modern: str, legacy: str) -> None:
    """实机回归（typescript / tests/dev-mac）。

    `launchctl load -w` 在服务已加载时只打 "Load failed: 5: Input/output error"
    却仍返回 0 —— start.sh 照样打印「已加载」，报错被吞掉、脚本谎报成功。
    """
    code = "\n".join(
        ln for ln in (SCRIPTS_DIR / script).read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert modern in code
    assert legacy not in code


def test_start_script_verifies_instead_of_claiming_success() -> None:
    """start.sh 必须真的去确认服务起来了，而不是 bootstrap 完就宣布成功。"""
    code = (SCRIPTS_DIR / "start.sh").read_text(encoding="utf-8")
    assert "health.sh" in code


def test_install_materialises_docling_artifacts() -> None:
    """§28.2 第 4 步：artifacts 必须铺到 config 指向的那个目录。"""
    import yaml

    install = (SCRIPTS_DIR / "install.sh").read_text(encoding="utf-8")
    configured = yaml.safe_load(
        Path("config/config.yaml").read_text(encoding="utf-8")
    )["pdf"]["docling_artifacts_path"]
    tail = configured.replace("/Users/Shared/offline-translator/", "")
    assert f'"$ROOT/{tail}/"' in install, f"install.sh 没有把 docling artifacts 铺到 {configured}"


def test_prepare_bundle_downloads_artifacts_explicitly() -> None:
    """不能再靠「跑一次 convert 捡缓存」—— 那次捡到的缓存里没有 layout 模型。"""
    text = (SCRIPTS_DIR / "prepare-bundle.sh").read_text(encoding="utf-8")
    assert "docling-tools models download" in text or "download_models" in text
    # 带 artifacts_path + HF 离线开关的验收跑，是「模型齐不齐」的唯一硬判据
    assert "HF_HUB_OFFLINE=1" in text
    assert "artifacts_path" in text


def test_wheelhouse_python_version_handoff_is_closed() -> None:
    """wheel 是按 cp3XX 打的，两边的 Python 小版本必须对上。

    备料写 python-version、安装照它建 venv。不钉的话 uv 会在 B 机上自己挑一个，
    挑到别的小版本就是 --no-index 下「找不到匹配 wheel」，而那台机器没法上网补。
    A/B 同机时这个坑碰巧不发作，换台真 B 机就翻车。
    """
    prepare = (SCRIPTS_DIR / "prepare-bundle.sh").read_text(encoding="utf-8")
    install = (SCRIPTS_DIR / "install.sh").read_text(encoding="utf-8")
    assert '"$BUNDLE/python-version"' in prepare, "备料脚本应把 Python 版本写进 bundle"
    assert '"$BUNDLE/python-version"' in install, "安装脚本应从 bundle 读 Python 版本"
    assert 'uv venv --python "$PY_VERSION"' in install


def test_prepare_bundle_does_not_depend_on_system_docling() -> None:
    """实机回归：A 机的系统 python3 没装 docling，第 4 步当场 ModuleNotFoundError。

    改成用第 1 步刚下好的 wheelhouse 现建一个环境 —— 既不挑 A 机装了什么，
    下模型的 docling 版本也和 B 机跑的那份完全一致。
    """
    text = (SCRIPTS_DIR / "prepare-bundle.sh").read_text(encoding="utf-8")
    block = text[text.index("[4/6]"):text.index("[5/6]")]
    assert "$TOOLS_VENV" in block
    assert '--find-links "$BUNDLE/wheels"' in block
    # 第 4 步不得再直接使唤系统 python3
    code = "\n".join(
        ln for ln in block.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "python3 -" not in code


def test_install_uses_offline_flags() -> None:
    """方案 §28.2：不带 --offline --no-index 的话 uv 会静默回落到 PyPI。"""
    text = (SCRIPTS_DIR / "install.sh").read_text(encoding="utf-8")
    assert "uv pip install" in text
    install_cmd = text[text.index("uv pip install"):text.index("uv pip install") + 400]
    assert "--offline" in install_cmd
    assert "--no-index" in install_cmd
    assert "--find-links" in install_cmd


_NETWORK_CMDS = re.compile(
    r"\b(wget|git\s+clone|hf\s+download|huggingface-cli|pip\s+download|"
    r"uv\s+pip\s+download|npm\s+install|brew\s+install)\b"
)


@pytest.mark.parametrize(
    "name", sorted(EXPECTED_SCRIPTS - {"prepare-bundle.sh"})
)
def test_no_network_commands_outside_bundle_prep(name: str) -> None:
    """只有 prepare-bundle.sh 允许联网（§28.1 它就在联网机器上跑）。"""
    text = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
    code = "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )
    assert not _NETWORK_CMDS.search(code), f"{name} 出现联网命令"
    # curl 只允许打 127.0.0.1
    for m in re.finditer(r"curl\s[^\n]*", code):
        assert "127.0.0.1" in m.group(0), f"{name} 的 curl 指向了非本机: {m.group(0)}"


def test_prepare_bundle_declares_itself_as_the_online_one() -> None:
    """反向守卫：上面那条排除法要有依据。"""
    text = (SCRIPTS_DIR / "prepare-bundle.sh").read_text(encoding="utf-8")
    assert "唯一允许联网" in text
    assert _NETWORK_CMDS.search(text), "备料脚本本就该有联网命令"


def test_prepare_bundle_warns_about_docling_and_fonts() -> None:
    """§28.1 第 4 步最容易被漏；第 5 步的 OTF 陷阱同样要写明。"""
    text = (SCRIPTS_DIR / "prepare-bundle.sh").read_text(encoding="utf-8")
    assert "静默联网" in text
    assert "OTF" in text


# ── AC-4.7 UI 范围 ────────────────────────────────────────────────────────


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _ui_markup() -> str:
    """剥掉 HTML 注释后的界面源码。

    注释里写明「刻意不做：模型选择、Quality Mode……」是有价值的文档，
    按原始文本 grep 会把这条说明本身判成违规，反过来逼人删掉它。
    """
    return _HTML_COMMENT.sub("", UI.read_text(encoding="utf-8"))


def test_ui_has_exactly_the_six_elements() -> None:
    """方案 §6：只保留 6 个元素。"""
    html = _ui_markup()
    for needed in ("原文件目录", "翻译后目录", "源语言", "目标语言", "开始翻译", "状态"):
        assert needed in html, f"缺少界面元素: {needed}"


@pytest.mark.parametrize(
    "forbidden", ["模型选择", "Quality Mode", "quality_mode", "模型切换",
                  "temperature", "top_k", "repeat_penalty", "任务中心"]
)
def test_ui_omits_out_of_scope_controls(forbidden: str) -> None:
    """§6 明列的不做项 —— 高级推理参数页、模型切换等一律不出现在界面上。"""
    assert forbidden not in _ui_markup()


def test_ui_documents_what_it_deliberately_omits() -> None:
    """反向守卫：那些不做项应当在注释里写明缘由，而不是无声消失。"""
    raw = UI.read_text(encoding="utf-8")
    assert "刻意不做" in raw


def test_ui_has_no_external_resources() -> None:
    """断网环境下任何外链都会挂（§20）。"""
    html = _ui_markup()
    externals = re.findall(r'(?:src|href)\s*=\s*["\'](https?://|//)[^"\']*', html)
    assert not externals, f"界面引用了外部资源: {externals}"
    assert "cdn" not in html.lower()
    assert "fonts.googleapis" not in html


def test_ui_polls_job_status() -> None:
    html = UI.read_text(encoding="utf-8")
    assert "/api/v1/jobs/" in html
    assert "setInterval" in html


# ── AC-4.14 README ────────────────────────────────────────────────────────


def test_readme_has_both_checklists() -> None:
    text = README.read_text(encoding="utf-8")
    assert "本次已覆盖" in text
    assert "待实机验证" in text


def test_readme_deploy_items_are_not_ticked() -> None:
    """DEPLOY 项**不得**打勾 —— 未在 macOS 实机验证过的不能声称通过。"""
    text = README.read_text(encoding="utf-8")
    deploy_section = text[text.index("待实机验证"):]
    end = deploy_section.find("\n## ")
    if end != -1:
        deploy_section = deploy_section[:end]
    assert "- [x]" not in deploy_section.lower(), "DEPLOY 清单里出现了已勾选项"
    assert "- [ ]" in deploy_section


def test_readme_records_license_risk() -> None:
    """§3.3 的地域限制是需要 Leon 决策的事项，不能只留在方案里。"""
    text = README.read_text(encoding="utf-8")
    assert "许可证" in text
    assert "欧盟" in text
