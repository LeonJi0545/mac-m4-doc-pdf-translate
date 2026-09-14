"""旧版 .doc → .docx 转换（方案 §5.1.1）。

python-docx 只支持 OOXML 的 ``.docx``，不认 Word 97-2003 的二进制 ``.doc``，
所以必须先转一步。转换只是**前置步骤**，转完仍走标准 DOCX 链路 ——
「不要把 DOCX 转成纯文本」的原则不受影响。

候选链：

1. ``soffice --headless --convert-to docx`` —— 主方案。保真度高（段落样式 / 表格 /
   页眉页脚 / 内嵌图片都在）、完全离线、跨平台。
2. ``textutil -convert docx`` —— 兜底。macOS 自带、零安装零体积，
   但复杂表格与图片的保真度低于 LibreOffice。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import DocConversionSettings
from app.core.errors import ConversionError
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class _Attempt:
    command: str
    reason: str


@dataclass
class DocConverter:
    settings: DocConversionSettings
    attempts: list[_Attempt] = field(default_factory=list)

    def convert(self, src: Path, out_dir: Path) -> Path:
        """把 ``src`` 转成 .docx 放进 ``out_dir``，返回产物路径。"""
        if not self.settings.enabled:
            raise ConversionError(
                f"遇到旧版 .doc 文件（{src.name}），但 config.yaml 的 "
                "doc_conversion.enabled 为 false。"
                "要么开启它并配好 LibreOffice，要么把 .doc 移出处理范围。"
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        expected = out_dir / f"{src.stem}.docx"
        self.attempts = []

        if self._try_soffice(src, out_dir, expected):
            return expected
        if self.settings.fallback_textutil and self._try_textutil(src, expected):
            return expected

        detail = "\n".join(f"  - {a.command}\n    → {a.reason}" for a in self.attempts)
        raise ConversionError(
            f"无法转换旧版 .doc 文件: {src}\n"
            f"已尝试：\n{detail}\n"
            "处理方式（三选一）：\n"
            "  1. 安装 LibreOffice，并在 config.yaml 的 doc_conversion.soffice_path "
            "填写 soffice 可执行文件路径\n"
            "     （macOS 默认 /Applications/LibreOffice.app/Contents/MacOS/soffice）\n"
            "  2. 在 macOS 上确认内置的 textutil 可用，并保持 "
            "doc_conversion.fallback_textutil 为 true\n"
            "  3. 把 doc_conversion.enabled 设为 false，扫描时直接忽略 .doc 文件"
        )

    # ── 候选 1：LibreOffice ───────────────────────────────────────────────

    def _try_soffice(self, src: Path, out_dir: Path, expected: Path) -> bool:
        exe = self._resolve_soffice()
        if exe is None:
            self.attempts.append(
                _Attempt(
                    command=f"soffice ({self.settings.soffice_path})",
                    reason="可执行文件不存在，且 PATH 上也找不到 soffice",
                )
            )
            return False

        # 并发调用同一个 user profile 会互相阻塞，给每次转换一个独立 profile
        with tempfile.TemporaryDirectory(prefix="soffice-profile-") as profile:
            cmd = [
                exe,
                f"-env:UserInstallation=file://{Path(profile).as_posix()}",
                "--headless",
                "--norestore",
                "--convert-to",
                "docx",
                "--outdir",
                str(out_dir),
                str(src),
            ]
            ok, reason = self._run(cmd, expected)
        self.attempts.append(_Attempt(command=" ".join(cmd[:1] + cmd[2:]), reason=reason))
        return ok

    def _resolve_soffice(self) -> str | None:
        configured = Path(self.settings.soffice_path)
        if configured.is_file():
            return str(configured)
        found = shutil.which("soffice") or shutil.which("libreoffice")
        return found

    # ── 候选 2：macOS textutil ────────────────────────────────────────────

    def _try_textutil(self, src: Path, expected: Path) -> bool:
        exe = shutil.which("textutil")
        if exe is None:
            self.attempts.append(
                _Attempt(command="textutil", reason="不在 PATH 上（仅 macOS 自带）")
            )
            return False
        cmd = [exe, "-convert", "docx", "-output", str(expected), str(src)]
        ok, reason = self._run(cmd, expected)
        self.attempts.append(_Attempt(command=" ".join(cmd), reason=reason))
        return ok

    # ── 公共执行逻辑 ──────────────────────────────────────────────────────

    def _run(self, cmd: list[str], expected: Path) -> tuple[bool, str]:
        try:
            proc = subprocess.run(  # noqa: S603 —— 命令来自配置，不含用户输入拼接
                cmd,
                capture_output=True,
                text=True,
                timeout=self.settings.timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return False, "命令不存在"
        except subprocess.TimeoutExpired:
            return False, f"超时（{self.settings.timeout_seconds}s）"
        except OSError as exc:
            return False, f"调用失败: {exc}"

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()[:300]
            return False, f"退出码 {proc.returncode}: {stderr or '(无 stderr)'}"

        # LibreOffice 有「退出码 0 但不产出文件」的已知行为，只看 returncode 会误判成功。
        if not expected.is_file():
            return False, f"退出码 0 但未生成 {expected.name}"

        log.info("已转换 %s", expected.name)
        return True, "成功"
