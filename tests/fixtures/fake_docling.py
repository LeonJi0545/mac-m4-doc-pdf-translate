"""假的 Docling 文档 —— 鸭子类型，只实现 pdf_reader 真正用到的属性。

这样 PDF 解析链路可以在**不安装 docling、不下载模型**的前提下被完整测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeProv:
    page_no: int = 1


@dataclass
class FakeLabel:
    value: str


class TitleItem:
    def __init__(self, text: str, page: int = 1) -> None:
        self.text = text
        self.prov = [FakeProv(page)]
        self.level = 1


class SectionHeaderItem:
    def __init__(self, text: str, level: int = 2, page: int = 1) -> None:
        self.text = text
        self.level = level
        self.prov = [FakeProv(page)]


class TextItem:
    def __init__(self, text: str, page: int = 1, label: str = "text") -> None:
        self.text = text
        self.prov = [FakeProv(page)]
        self.label = FakeLabel(label)


class ListItem:
    def __init__(self, text: str, page: int = 1) -> None:
        self.text = text
        self.prov = [FakeProv(page)]


class PictureItem:
    def __init__(self, page: int = 1) -> None:
        self.prov = [FakeProv(page)]


class CaptionItem:
    def __init__(self, text: str, page: int = 1) -> None:
        self.text = text
        self.prov = [FakeProv(page)]


class UnknownFutureItem:
    """模拟 Docling 升级后引入的新类型 —— 不该让整份文档失败。"""

    def __init__(self, text: str, page: int = 1) -> None:
        self.text = text
        self.prov = [FakeProv(page)]


@dataclass
class FakeCell:
    text: str
    start_row_offset_idx: int
    start_col_offset_idx: int


@dataclass
class FakeTableData:
    table_cells: list[FakeCell] = field(default_factory=list)


class TableItem:
    def __init__(self, rows: list[list[str]], page: int = 1) -> None:
        cells = [
            FakeCell(text=value, start_row_offset_idx=r, start_col_offset_idx=c)
            for r, row in enumerate(rows)
            for c, value in enumerate(row)
        ]
        self.data = FakeTableData(cells)
        self.prov = [FakeProv(page)]


@dataclass
class FakeDoclingDocument:
    items: list[Any] = field(default_factory=list)

    def iterate_items(self):
        return iter(self.items)


@dataclass
class FakeConvertResult:
    document: FakeDoclingDocument


class FakeConverter:
    """替代 docling.document_converter.DocumentConverter。"""

    def __init__(self, document: FakeDoclingDocument) -> None:
        self.document = document
        self.converted: list[str] = []

    def convert(self, path: str) -> FakeConvertResult:
        self.converted.append(path)
        return FakeConvertResult(self.document)


def build_sample_document() -> FakeDoclingDocument:
    """一份覆盖全部 item 类型的样例。"""
    return FakeDoclingDocument(
        items=[
            TitleItem("Geschäftsbericht 2024", page=1),
            TextItem("Die Rechnung beträgt EUR 1.250,00.", page=1),
            SectionHeaderItem("Einzelheiten", level=2, page=1),
            ListItem("Erster Punkt", page=1),
            PictureItem(page=1),
            CaptionItem("Abbildung 1", page=1),
            TableItem([["Region", "Umsatz"], ["Lombardei", "EUR 980,00"]], page=2),
            TextItem("Kontakt: info@example.com", page=2),
            UnknownFutureItem("Ein neuer Blocktyp", page=2),
        ]
    )
