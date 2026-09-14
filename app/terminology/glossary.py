"""术语库（方案 §11）。

简单 YAML，不做术语管理 UI（§11 / §23）。

命中判定的两个要点：

- **最长优先**：术语按长度降序匹配，保证 "Managed Service Provider"
  先于 "Service" 命中，否则长术语永远轮不上。
- **CJK 无词边界**：ASCII 术语用 ``\\b`` 做整词匹配（避免 "Ticket" 命中 "Tickets" 之外的
  "Tick"），含 CJK 的术语退化为子串匹配。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.core.errors import ConfigError

_CJK = re.compile(r"[㐀-鿿豈-﫿぀-ヿ]")


@dataclass(frozen=True)
class Glossary:
    """不可变术语表。"""

    terms: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> Glossary:
        p = Path(path)
        if not p.is_file():
            # 术语库缺失不是致命错误 —— 没有术语表也能翻译。
            return cls({})
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"术语库解析失败 ({p}): {exc}") from exc
        terms = raw.get("terms") or {}
        if not isinstance(terms, dict):
            raise ConfigError(f"术语库的 terms 必须是映射: {p}")
        return cls({str(k): str(v) for k, v in terms.items()})

    def match(self, text: str) -> list[tuple[str, str]]:
        """返回 ``text`` 中实际命中的术语对。

        返回顺序按术语表的定义顺序（而非命中位置），保证 prompt 可复现 ——
        同样的输入永远产出同样的 prompt，测试才能做字节级断言。
        """
        if not text or not self.terms:
            return []

        # 先按长度降序找出命中的术语，并"消费"掉已匹配的区段，
        # 避免短术语在长术语内部重复命中。
        remaining = text
        hit: set[str] = set()
        for term in sorted(self.terms, key=len, reverse=True):
            pattern = self._pattern(term)
            if pattern.search(remaining):
                hit.add(term)
                remaining = pattern.sub(" ", remaining)

        return [(t, self.terms[t]) for t in self.terms if t in hit]

    @staticmethod
    def _pattern(term: str) -> re.Pattern[str]:
        escaped = re.escape(term)
        if _CJK.search(term):
            # CJK 没有词边界，\b 在这里不起作用，用子串匹配。
            return re.compile(escaped, re.IGNORECASE)
        return re.compile(rf"\b{escaped}\b", re.IGNORECASE)

    def as_dict(self) -> dict[str, str]:
        return dict(self.terms)
