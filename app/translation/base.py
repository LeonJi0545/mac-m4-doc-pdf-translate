"""翻译 Provider 抽象（方案 §12）。

虽然全系统只有一个翻译模型，仍保留这层接口，好让业务代码不直接依赖 llama.cpp。
唯一实现是 ``app.translation.hy_mt.HYMTProvider``。

``translate`` 的签名逐字照抄方案 §12，不增不减参数。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderHealth:
    """Provider 就绪状态。

    ``health()`` 约定**永不抛异常** —— 一个会把自己搞崩的健康检查没有意义，
    而 ``/api/v1/health`` 正是靠它来如实反映 llama-server 是否起来（方案 §28.3）。
    """

    ready: bool
    detail: str = ""


class TranslationProvider(ABC):
    """翻译能力的唯一入口。

    业务代码（文档解析、Job 调度、API）只依赖这个抽象；
    llama.cpp 的细节全部封在 ``app/translation/`` 包内部。
    """

    @abstractmethod
    def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
        glossary: dict | None = None,
        context: str | None = None,
    ) -> str:
        """把 ``text`` 翻译成 ``target_language``。

        Args:
            text: 待翻译文本。
            source_language: 源语言代码。**注意**：模型官方的 prompt 模板里没有
                源语言槽位（方案 §13），源语言由模型自行识别 —— 这正是「混合语言
                自动识别」（§25）能成立的原因。本参数只作记录与术语方向选择之用。
            target_language: 目标语言代码，决定用哪个官方模板。
            glossary: 术语表（源词 → 目标词）。只有当前文本**实际命中**的术语
                才会被注入 prompt（§11）。
            context: 上下文。见各实现的说明 —— 官方模板无此槽位，
                V1 的 HYMTProvider 不使用它。
        """

    def translate_batch(
        self,
        texts: list[str],
        source_language: str,
        target_language: str,
        glossary: dict | None = None,
        context: str | None = None,
    ) -> list[str]:
        """批量翻译。

        默认实现是串行循环 —— 只有一个 llama-server 常驻（方案 §21），
        并发发请求不会更快，只会争抢同一块统一内存。
        """
        return [
            self.translate(t, source_language, target_language, glossary, context)
            for t in texts
        ]

    @abstractmethod
    def health(self) -> ProviderHealth:
        """返回就绪状态。实现必须吞掉所有异常，只返回结果。"""
