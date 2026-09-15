"""PDF 解析（方案 §5.2）。

用 Docling 负责 layout / reading order / heading / paragraph / table / OCR。
Native PDF 与扫描件走同一条链路，OCR 由 pipeline option 开关。

**延迟导入**：``docling`` 体积大、且首次运行会尝试联网拉模型。把 import 放进函数体内，
好处是缺 docling 时整个包仍可导入、其余测试照常跑；``converter`` 参数则是测试注入点。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.errors import ParseError
from app.core.logging import get_logger
from app.document.model import Block, BlockType, ParsedDocument, TableRef

log = get_logger(__name__)

# Docling 的 item 类名 → 我们的 BlockType。
# 用类名字符串而非 isinstance，是为了不在模块层 import docling。
_TYPE_MAP = {
    "TitleItem": BlockType.HEADING,
    "SectionHeaderItem": BlockType.HEADING,
    "ListItem": BlockType.LIST_ITEM,
    "PictureItem": BlockType.IMAGE,
    "CaptionItem": BlockType.CAPTION,
    "TableItem": BlockType.TABLE_CELL,
    "TextItem": BlockType.PARAGRAPH,
}

# label 兜底（不同 Docling 版本的 item 类可能合并成 TextItem + label）
_LABEL_MAP = {
    "title": BlockType.HEADING,
    "section_header": BlockType.HEADING,
    "list_item": BlockType.LIST_ITEM,
    "caption": BlockType.CAPTION,
    "picture": BlockType.IMAGE,
    "table": BlockType.TABLE_CELL,
}


def _resolve_artifacts_path(settings: Settings) -> str:
    """校验 Docling 的 artifacts 目录，返回给 pipeline option 用。

    ⚠ 目录不存在时**不能**放任 ``artifacts_path`` 为空。那等于回落到 Docling 的
    「首次运行时联网下载」，在断网机上得到的是一条跟真正原因无关的报错
    （实机记录见 ``tests/dev-mac/test.log``）。这里直接拦下，把问题指回备料/安装环节。

    artifacts 目录的形态是「一个模型一个 ``<org>--<repo>`` 子目录」，
    **不是** Docling 的 cache 根目录（``~/.cache/docling``）。两者填反的报错是
    ``Model 'docling-project/docling-layout-heron' not found in artifacts_path``。
    """
    artifacts = Path(settings.pdf.docling_artifacts_path)
    if not artifacts.is_dir() or not any(artifacts.iterdir()):
        raise ParseError(
            f"Docling 模型目录不存在或为空: {artifacts}。"
            "本系统不会在运行期联网下载模型（方案 §20）。"
            "请按 §28.1 第 4 步把模型下载进 offline-bundle/docling/，再按 §28.2 安装；"
            "config.yaml 的 pdf.docling_artifacts_path 要指向存放各模型子目录的 "
            "artifacts 目录本身，而不是 Docling 的 cache 根目录。"
        )
    return str(artifacts)


def _build_converter(settings: Settings) -> Any:
    """构造真实的 Docling converter。只在没有注入时才走这里。"""
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:  # pragma: no cover —— 开发机不装 docling
        raise ParseError(
            "未安装 docling，无法解析 PDF。"
            "请按方案 §28.1 第 4 步把 docling 及其模型产物备进 offline-bundle，"
            "再按 §28.2 离线安装。"
        ) from exc

    options = PdfPipelineOptions()
    options.do_ocr = settings.pdf.ocr
    options.do_table_structure = True
    # 指向本地 artifacts，杜绝运行期联网拉模型（方案 §20 / §28.1 的警告）
    options.artifacts_path = _resolve_artifacts_path(settings)

    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )


def _classify(item: Any) -> tuple[BlockType, int | None]:
    """把 Docling 的 item 映射成 BlockType。

    未知类型降级为 PARAGRAPH 并记日志，**不抛异常** ——
    Docling 升级会引入新 item 类型，不能因此让整份文档失败。
    """
    class_name = type(item).__name__
    block_type = _TYPE_MAP.get(class_name)

    if block_type is None:
        label = getattr(item, "label", None)
        label_str = str(getattr(label, "value", label) or "").lower()
        block_type = _LABEL_MAP.get(label_str)

    if block_type is None:
        log.debug("未知的 Docling item 类型 %s，按段落处理", class_name)
        block_type = BlockType.PARAGRAPH

    level = None
    if block_type is BlockType.HEADING:
        level = getattr(item, "level", None) or (1 if class_name == "TitleItem" else 2)

    return block_type, level


def _page_of(item: Any) -> int | None:
    prov = getattr(item, "prov", None)
    if not prov:
        return None
    try:
        return int(getattr(prov[0], "page_no", None))
    except (TypeError, ValueError, IndexError):
        return None


def _item_text(item: Any) -> str:
    for attr in ("text", "orig"):
        value = getattr(item, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _table_cells(item: Any, table_id: str, page: int | None) -> list[Block]:
    """把一个 TableItem 展开成逐单元格的 Block。"""
    blocks: list[Block] = []
    data = getattr(item, "data", None)
    cells = getattr(data, "table_cells", None) or getattr(item, "table_cells", None) or []
    for idx, cell in enumerate(cells):
        text = getattr(cell, "text", "") or ""
        row = getattr(cell, "start_row_offset_idx", None)
        col = getattr(cell, "start_col_offset_idx", None)
        blocks.append(
            Block(
                id=f"{table_id}#c{idx}",
                type=BlockType.TABLE_CELL,
                text=text,
                page=page,
                table=TableRef(
                    table_id=table_id,
                    row=int(row) if row is not None else idx,
                    col=int(col) if col is not None else 0,
                ),
                anchor={"kind": "pdf", "table_id": table_id, "cell_index": idx},
            )
        )
    return blocks


def parse_pdf(path: Path, settings: Settings, *, converter: Any | None = None) -> ParsedDocument:
    """解析 PDF（含扫描件 OCR）。

    Args:
        converter: 测试注入点。传 None 时才会真正构造 Docling。
    """
    conv = converter if converter is not None else _build_converter(settings)

    try:
        result = conv.convert(str(path))
    except Exception as exc:
        raise ParseError(f"PDF 解析失败 ({path}): {exc}") from exc

    document = getattr(result, "document", result)
    blocks: list[Block] = []
    pages: set[int] = set()
    image_count = 0

    for idx, entry in enumerate(_iter_items(document)):
        item = entry[0] if isinstance(entry, tuple) else entry
        page = _page_of(item)
        if page is not None:
            pages.add(page)

        block_type, level = _classify(item)

        if block_type is BlockType.TABLE_CELL:
            blocks.extend(_table_cells(item, f"pdf-t{idx}", page))
            continue

        if block_type is BlockType.IMAGE:
            image_count += 1
            blocks.append(
                Block(
                    id=f"pdf-{idx}",
                    type=BlockType.IMAGE,
                    text="",
                    page=page,
                    anchor={"kind": "pdf", "item_index": idx},
                )
            )
            continue

        text = _item_text(item)
        if not text.strip():
            continue
        blocks.append(
            Block(
                id=f"pdf-{idx}",
                type=block_type,
                text=text,
                level=level,
                page=page,
                anchor={"kind": "pdf", "item_index": idx},
            )
        )

    return ParsedDocument(
        source_path=path,
        kind="pdf",
        blocks=blocks,
        meta={"page_count": max(pages) if pages else 0, "image_count": image_count},
    )


def _iter_items(document: Any):
    """兼容 Docling 的两种遍历接口。"""
    if hasattr(document, "iterate_items"):
        return document.iterate_items()
    return getattr(document, "texts", []) or []
