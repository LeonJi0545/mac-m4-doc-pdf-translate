"""统一文档模型 —— 跨里程碑的核心契约。

DOCX 与 PDF 两条解析链路**必须**收敛到这里，否则 chunking / 翻译 / QA 就得
分别写两套。这是全项目回滚代价最高的一个模块，改它要连带改两个 reader、
两个 writer、chunking 和 qa。

关键约定：``Block.anchor`` 是**不透明字段** —— 只有产出它的 reader 和配对的
writer 能解释它。chunking / qa / 翻译层一律不得读它（有测试守这条）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal


class BlockType(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE_CELL = "table_cell"
    CAPTION = "caption"
    IMAGE = "image"


class JobPhase(StrEnum):
    """单文件处理阶段（方案 §9 的状态机用词）。"""

    PARSING = "parsing"
    TRANSLATING = "translating"
    RENDERING = "rendering"


@dataclass(frozen=True)
class TableRef:
    """表格单元格的位置。

    放在 Block 的一级字段而不是 anchor 里，因为渲染器需要按它把单元格重新
    聚合成表格 —— 这是**格式无关**的结构信息，两条链路都有。
    """

    table_id: str
    row: int
    col: int


@dataclass
class Block:
    """一个可翻译（或占位）的文档块。"""

    id: str
    type: BlockType
    text: str
    level: int | None = None
    page: int | None = None
    table: TableRef | None = None
    # 不透明的 writer 回指信息。只有产出它的 reader / writer 对能解释。
    anchor: dict[str, Any] = field(default_factory=dict)
    translated: str | None = None

    @property
    def translatable(self) -> bool:
        """图片等空文本块不进翻译，但要保留在序列里（计数类 QA 与写回顺序都依赖）。"""
        return bool(self.text and self.text.strip())

    @property
    def output_text(self) -> str:
        """译文优先，没有译文就退回原文。"""
        return self.translated if self.translated is not None else self.text


@dataclass
class ParsedDocument:
    source_path: Path
    kind: Literal["docx", "pdf"]
    blocks: list[Block] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def translatable_blocks(self) -> list[Block]:
        return [b for b in self.blocks if b.translatable]


@dataclass
class QAWarning:
    """QA 告警。

    告警**不使文件失败**（方案 §14 只要求「检查」，V1 不做 AI 质量评分）。
    """

    code: str
    detail: str
    block_id: str | None = None

    def __str__(self) -> str:
        where = f" [{self.block_id}]" if self.block_id else ""
        return f"{self.code}{where}: {self.detail}"


@dataclass
class FileResult:
    """单文件处理结果（契约 C 的返回值）。"""

    source: Path
    output: Path
    status: Literal["completed", "skipped", "failed"]
    warnings: list[QAWarning] = field(default_factory=list)
    error: str | None = None
    blocks_total: int = 0
    blocks_translated: int = 0
