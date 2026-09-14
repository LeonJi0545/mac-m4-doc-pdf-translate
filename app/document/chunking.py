"""Chunking（方案 §10）。

**不逐句翻译。** 按文档结构分组：Heading + 其后若干 Paragraph 构成一个 chunk，
受字符预算约束。

回填的可靠性是这里的关键：模型**不保证**保留我们的分隔结构。所以拆回来的段数
一旦与请求段数不符，就自动降级为逐 Block 单独翻译并记警告 ——
降级路径仍是**段落级**，不是逐句，不违反 §10。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.document.model import Block, BlockType, QAWarning
from app.translation.base import TranslationProvider

log = get_logger(__name__)

SEPARATOR = "\n\n"


@dataclass
class TranslationChunk:
    block_ids: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return SEPARATOR.join(self.texts)

    def __len__(self) -> int:
        return len(self.block_ids)


def build_chunks(blocks: list[Block], max_chars: int = 1200) -> list[TranslationChunk]:
    """把 Block 序列切成翻译 chunk。

    规则：
    - 空文本 Block（图片等）不进 chunk，但它在序列里的位置不受影响
    - 遇到 HEADING 开新 chunk（标题与其正文同组，给模型上下文）
    - 表格单元格**单独成 chunk** —— 单元格多是短语，混进正文会被模型当句子续写
    """
    chunks: list[TranslationChunk] = []
    current = TranslationChunk()
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if current.block_ids:
            chunks.append(current)
        current = TranslationChunk()
        current_chars = 0

    for block in blocks:
        if not block.translatable:
            continue

        if block.type is BlockType.TABLE_CELL:
            flush()
            chunks.append(TranslationChunk([block.id], [block.text]))
            continue

        if block.type is BlockType.HEADING:
            flush()

        text_len = len(block.text)
        if current.block_ids and current_chars + text_len > max_chars:
            flush()

        current.block_ids.append(block.id)
        current.texts.append(block.text)
        current_chars += text_len

    flush()
    return chunks


def split_response(response: str, expected: int) -> list[str] | None:
    """把模型返回拆回逐 Block 的译文。段数不符时返回 None（交给调用方降级）。"""
    if expected <= 1:
        return [response.strip()]
    parts = [p.strip() for p in response.split(SEPARATOR)]
    parts = [p for p in parts if p]
    if len(parts) != expected:
        return None
    return parts


def translate_blocks(
    blocks: list[Block],
    provider: TranslationProvider,
    *,
    source_language: str,
    target_language: str,
    max_chars: int = 1200,
    glossary: dict[str, str] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[QAWarning]:
    """按 chunk 翻译并把译文回填进 ``block.translated``。

    返回降级等情况产生的告警。**不抛异常**给上层的做法是刻意的：翻译失败会直接
    向上抛（由 Job runner 记成单文件失败），而「分隔结构没对上」只是质量问题，
    降级重试即可，不该让整个文件失败。
    """
    index = {b.id: b for b in blocks}
    chunks = build_chunks(blocks, max_chars)
    warnings: list[QAWarning] = []
    done = 0
    total = sum(len(c) for c in chunks)

    for chunk in chunks:
        parts: list[str] | None = None
        if len(chunk) > 1:
            raw = provider.translate(
                chunk.text, source_language, target_language, glossary, None
            )
            parts = split_response(raw, len(chunk))
            if parts is None:
                warnings.append(
                    QAWarning(
                        code="chunk_split_mismatch",
                        detail=(
                            f"模型未保留分隔结构（期望 {len(chunk)} 段），"
                            "已降级为逐段落单独翻译"
                        ),
                        block_id=chunk.block_ids[0],
                    )
                )
                log.warning("chunk 回填失败，降级逐段翻译: %s", chunk.block_ids)

        if parts is None:
            parts = [
                provider.translate(t, source_language, target_language, glossary, None)
                for t in chunk.texts
            ]

        for block_id, translated in zip(chunk.block_ids, parts, strict=True):
            index[block_id].translated = translated

        done += len(chunk)
        if on_progress is not None:
            on_progress(done, total)

    return warnings
