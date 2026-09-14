"""官方 prompt 模板（方案 §13）。

**不要自拟 prompt。** 自拟会偏离模型训练时的指令分布，通常降低翻译质量。
下面两个模板逐字取自方案 §13，术语干预格式逐字取自 §11。

两个关键约束：

1. **模型没有默认 system prompt**，全部内容以 user role 传入（§4.2 / §13）。
2. **官方模板里没有源语言槽位** —— 只有 ``{target_language}``。
   源语言由模型自行识别，这正是「混合语言自动识别」（§25）能成立的原因。
   所以 ``build_prompt`` 的签名里**故意不收** source_language：
   从源头上杜绝它污染模板，而不是靠测试事后兜。
"""

from __future__ import annotations

# ── 官方模板（逐字，勿改）─────────────────────────────────────────────────
# 注意全角标点：「，」是 U+FF0C，「：」是 U+FF1A。

TEMPLATE_TO_ZH = "将以下文本翻译为{target_language}，注意只需要输出翻译后的结果\n\n{source_text}"

TEMPLATE_TO_OTHER = (
    "Translate the following segment into {target_language}, "
    "without additional explanation.\n\n{source_text}"
)

GLOSSARY_LINE = "参考下面的翻译：{source_term} 翻译成 {target_term}"

# ── 目标语言名称 ──────────────────────────────────────────────────────────
# 中文模板里填中文名，英文模板里填英文名。

LANGUAGE_NAMES: dict[str, dict[str, str]] = {
    "zh": {"zh": "中文", "en": "Chinese"},
    "en": {"zh": "英文", "en": "English"},
    "de": {"zh": "德文", "en": "German"},
    "it": {"zh": "意大利文", "en": "Italian"},
}

# 方案 §3.1：官方支持 36 语种，本需求所需的 it/de/en/zh 全部在列。
SUPPORTED_TARGETS = frozenset(LANGUAGE_NAMES)


def language_name(code: str, *, in_language: str) -> str:
    """取语言的可读名称。未知代码原样返回，不抛异常。"""
    entry = LANGUAGE_NAMES.get(code.lower())
    if entry is None:
        return code
    return entry.get(in_language, entry["en"])


def build_prompt(
    text: str,
    target_language: str,
    glossary_hits: list[tuple[str, str]] | None = None,
) -> str:
    """按官方模板组装 prompt。

    Args:
        text: 待翻译正文。
        target_language: 目标语言代码。``zh`` 走中文模板，其余走英文模板 ——
            这是模板选择的**唯一**依据（§13：只修改 target language，模板本身不动）。
        glossary_hits: 当前文本**实际命中**的术语对，按 §11 的官方格式拼在正文之前。
            只传命中的，不要把整张术语表塞进去。

    Returns:
        完整的 user message 内容。调用方不得再包 system prompt。
    """
    target = target_language.lower()
    if target == "zh":
        body = TEMPLATE_TO_ZH.format(
            target_language=language_name("zh", in_language="zh"),
            source_text=text,
        )
    else:
        body = TEMPLATE_TO_OTHER.format(
            target_language=language_name(target, in_language="en"),
            source_text=text,
        )

    if not glossary_hits:
        return body

    lines = [
        GLOSSARY_LINE.format(source_term=src, target_term=dst) for src, dst in glossary_hits
    ]
    return "\n".join(lines) + "\n\n" + body
