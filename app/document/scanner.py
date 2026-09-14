"""目录扫描与输出路径映射（方案 §7 / §8）。

递归扫描源目录，只收 ``.doc`` / ``.docx`` / ``.pdf``，输出保持相对目录结构，
命名加 ``_zh`` 后缀，目标已存在则跳过。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.errors import ConfigError
from app.core.logging import get_logger

log = get_logger(__name__)

SUPPORTED_SUFFIXES = {".doc", ".docx", ".pdf"}
LEGACY_DOC_SUFFIX = ".doc"


@dataclass(frozen=True)
class ScanItem:
    source: Path
    output: Path
    relative: Path
    will_skip: bool


def map_output(src: Path, root: Path, out_root: Path) -> Path:
    """``Source/German/x.pdf`` → ``Translated/German/x_zh.pdf``。

    后缀判断用小写，但**输出保留原后缀的原始写法**（``.PDF`` → ``_zh.PDF``），
    避免在大小写不敏感的 macOS 文件系统上产生意外的重名判定。

    ``.doc`` 的输出统一为 ``.docx``（方案 §8）—— 它要先被转换成 .docx 才能处理。
    """
    relative = src.relative_to(root)
    suffix = ".docx" if src.suffix.lower() == LEGACY_DOC_SUFFIX else src.suffix
    return out_root / relative.parent / f"{src.stem}_zh{suffix}"


def assert_not_overlapping(root: Path, out_root: Path) -> None:
    """源目录与输出目录不得重叠。

    重叠的后果不是「有点乱」而是**无限增殖**：输出文件落在扫描范围里，
    下一轮会被当成输入再翻一遍，产出 ``x_zh_zh.docx``、``x_zh_zh_zh.docx``……
    """
    src = root.resolve()
    out = out_root.resolve()

    if src == out:
        raise ConfigError(f"源目录与输出目录不能是同一个目录: {src}")
    if src in out.parents:
        raise ConfigError(
            f"输出目录 {out} 在源目录 {src} 之内。"
            "输出文件会被下一轮扫描当成输入，导致重复翻译。请换一个输出目录。"
        )
    if out in src.parents:
        raise ConfigError(
            f"源目录 {src} 在输出目录 {out} 之内。"
            "输出目录会包含源文件，容易互相覆盖。请换一个输出目录。"
        )


def _is_candidate(path: Path, *, include_doc: bool) -> bool:
    name = path.name
    if name.startswith("~$"):
        # Word 打开文件时生成的锁文件，把它当输入会解析失败
        return False
    if name.startswith("."):
        return False
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        return False
    if suffix == LEGACY_DOC_SUFFIX and not include_doc:
        return False
    return True


def scan(root: Path, out_root: Path, *, include_doc: bool = True) -> list[ScanItem]:
    """递归扫描源目录。

    Args:
        include_doc: 为 False 时忽略 ``.doc``（对应 config.yaml 的
            ``doc_conversion.enabled: false``）。

    Returns:
        按路径排序的扫描结果 —— **顺序必须可复现**，测试与进度显示都依赖它。
    """
    root = Path(root)
    out_root = Path(out_root)
    if not root.is_dir():
        raise ConfigError(f"源目录不存在或不是目录: {root}")

    assert_not_overlapping(root, out_root)

    items: list[ScanItem] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not _is_candidate(path, include_doc=include_doc):
            continue
        output = map_output(path, root, out_root)
        items.append(
            ScanItem(
                source=path,
                output=output,
                relative=path.relative_to(root),
                will_skip=output.exists(),
            )
        )

    log.info("扫描 %s：%d 个文件，其中 %d 个已有输出将跳过",
             root, len(items), sum(1 for i in items if i.will_skip))
    return items
