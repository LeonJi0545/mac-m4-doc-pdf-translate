"""M1 测试：Provider 签名、官方模板、术语干预、离线守卫、重试。

全部走 httpx.MockTransport —— 配合 conftest 的网络守卫，物理上不可能联网（AC-1.12）。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import OfflineViolation, TranslationError
from app.terminology.glossary import Glossary
from app.translation.base import TranslationProvider
from app.translation.hy_mt import HYMTProvider
from app.translation.llama_client import LlamaServerClient, SamplingParams
from app.translation.prompts import (
    GLOSSARY_LINE,
    TEMPLATE_TO_OTHER,
    TEMPLATE_TO_ZH,
    build_prompt,
)

# ── AC-1.1 接口签名逐字符合方案 §12 ──────────────────────────────────────


def test_provider_signature_matches_spec() -> None:
    params = list(inspect.signature(TranslationProvider.translate).parameters)
    assert params == [
        "self",
        "text",
        "source_language",
        "target_language",
        "glossary",
        "context",
    ]


# ── AC-1.2 官方模板 ───────────────────────────────────────────────────────


def test_templates_are_verbatim() -> None:
    """模板必须与方案 §13 逐字一致，注意全角标点。"""
    assert TEMPLATE_TO_ZH == (
        "将以下文本翻译为{target_language}，注意只需要输出翻译后的结果\n\n{source_text}"
    )
    assert TEMPLATE_TO_OTHER == (
        "Translate the following segment into {target_language}, "
        "without additional explanation.\n\n{source_text}"
    )
    assert GLOSSARY_LINE == "参考下面的翻译：{source_term} 翻译成 {target_term}"
    # 全角标点的码点断言 —— 半角逗号/冒号会偏离官方模板
    assert "，" in TEMPLATE_TO_ZH
    assert "：" in GLOSSARY_LINE


def test_to_zh_uses_chinese_template() -> None:
    p = build_prompt("Hello world", "zh")
    assert p == "将以下文本翻译为中文，注意只需要输出翻译后的结果\n\nHello world"


def test_to_en_uses_english_template() -> None:
    p = build_prompt("你好世界", "en")
    assert p == (
        "Translate the following segment into English, "
        "without additional explanation.\n\n你好世界"
    )


def test_no_system_message_in_payload(tmp_settings: Settings) -> None:
    """方案 §4.2：模型没有默认 system prompt，不要自行注入。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "译文"}}]})

    provider = HYMTProvider(
        tmp_settings, client=LlamaServerClient("http://127.0.0.1:8001",
                                               transport=httpx.MockTransport(handler))
    )
    provider.translate("Hello", "auto", "zh")

    assert len(captured["messages"]) == 1
    assert captured["messages"][0]["role"] == "user"


# ── AC-1.3 源语言不得污染模板 ─────────────────────────────────────────────


def test_source_language_is_not_a_prompt_parameter() -> None:
    """结构性保证：build_prompt 根本不收 source_language。"""
    assert "source_language" not in inspect.signature(build_prompt).parameters


@pytest.mark.parametrize("src", ["auto", "de", "it", "en", "zh"])
def test_prompt_identical_across_source_languages(tmp_settings: Settings, src: str) -> None:
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        prompts.append(json.loads(request.content)["messages"][0]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    provider = HYMTProvider(
        tmp_settings,
        client=LlamaServerClient("http://127.0.0.1:8001",
                                 transport=httpx.MockTransport(handler)),
        glossary=Glossary({}),
    )
    provider.translate("Die Rechnung ist fällig.", src, "zh")
    assert prompts[0] == (
        "将以下文本翻译为中文，注意只需要输出翻译后的结果\n\nDie Rechnung ist fällig."
    )


# ── AC-1.4 / AC-1.5 术语干预 ──────────────────────────────────────────────


FIVE_TERMS = {
    "Tenant": "租户",
    "Workspace": "工作区",
    "Ticket": "工单",
    "Billing": "计费",
    "Managed Service Provider": "托管服务提供商",
}


def test_only_hit_terms_are_injected() -> None:
    g = Glossary(FIVE_TERMS)
    hits = g.match("The Tenant opened a Ticket about storage.")
    assert [h[0] for h in hits] == ["Tenant", "Ticket"]

    prompt = build_prompt("The Tenant opened a Ticket about storage.", "zh", hits)
    lines = [ln for ln in prompt.splitlines() if ln.startswith("参考下面的翻译：")]
    assert len(lines) == 2
    assert lines[0] == "参考下面的翻译：Tenant 翻译成 租户"
    assert lines[1] == "参考下面的翻译：Ticket 翻译成 工单"


def test_glossary_lines_precede_body() -> None:
    hits = Glossary(FIVE_TERMS).match("Billing question")
    prompt = build_prompt("Billing question", "zh", hits)
    assert prompt.startswith("参考下面的翻译：Billing 翻译成 计费\n\n将以下文本翻译为中文")


def test_no_hit_means_byte_identical_prompt() -> None:
    """AC-1.5：术语未命中时，prompt 与无术语表时字节一致。"""
    text = "Nothing relevant here."
    with_table = build_prompt(text, "zh", Glossary(FIVE_TERMS).match(text))
    without_table = build_prompt(text, "zh", None)
    assert with_table == without_table


def test_longest_term_wins() -> None:
    """"Managed Service Provider" 必须整体命中，而不是被更短的术语切碎。"""
    g = Glossary({**FIVE_TERMS, "Service": "服务"})
    hits = dict(g.match("A Managed Service Provider handles it."))
    assert "Managed Service Provider" in hits
    assert "Service" not in hits


def test_word_boundary_for_ascii_terms() -> None:
    g = Glossary({"Ticket": "工单"})
    assert g.match("Ticketing system") == []
    assert g.match("a ticket here") == [("Ticket", "工单")]


def test_cjk_term_uses_substring_match() -> None:
    g = Glossary({"工单": "Ticket"})
    assert g.match("这是一个工单问题") == [("工单", "Ticket")]


def test_glossary_load_missing_file_is_empty(tmp_path: Path) -> None:
    assert Glossary.load(tmp_path / "none.yaml").terms == {}


# ── AC-1.6 采样参数 ───────────────────────────────────────────────────────


def test_sampling_defaults_are_official() -> None:
    s = SamplingParams()
    assert (s.temperature, s.top_k, s.top_p, s.repeat_penalty) == (0.7, 20, 0.6, 1.05)


def test_sampling_sent_in_payload(tmp_settings: Settings) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    provider = HYMTProvider(
        tmp_settings,
        client=LlamaServerClient("http://127.0.0.1:8001",
                                 transport=httpx.MockTransport(handler)),
    )
    provider.translate("hi", "auto", "zh")
    assert captured["temperature"] == 0.7
    assert captured["top_k"] == 20
    assert captured["top_p"] == 0.6
    assert captured["repeat_penalty"] == 1.05


def test_sampling_override_from_config(tmp_settings: Settings) -> None:
    tuned = tmp_settings.model_copy(deep=True)
    tuned.sampling.temperature = 0.2
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    HYMTProvider(
        tuned,
        client=LlamaServerClient("http://127.0.0.1:8001",
                                 transport=httpx.MockTransport(handler)),
    ).translate("hi", "auto", "zh")
    assert captured["temperature"] == 0.2


# ── AC-1.7 / AC-1.8 离线守卫 ──────────────────────────────────────────────


def test_non_loopback_base_url_rejected(tmp_settings: Settings) -> None:
    bad = tmp_settings.model_copy(deep=True)
    bad.llama.base_url = "http://10.0.0.1:8001"
    with pytest.raises(OfflineViolation, match="loopback"):
        HYMTProvider(bad)


def test_hf_style_model_path_rejected(tmp_settings: Settings) -> None:
    bad = tmp_settings.model_copy(deep=True)
    bad.model.path = "tencent/HY-MT1.5-1.8B-GGUF:Q4_K_M"
    with pytest.raises(OfflineViolation) as exc:
        HYMTProvider(bad)
    assert "-hf" in str(exc.value)
    assert "本地" in str(exc.value)


# ── AC-1.9 / AC-1.10 重试与健康 ───────────────────────────────────────────


def test_retries_on_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.translation.llama_client.time.sleep", lambda _: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="loading model")
        return httpx.Response(200, json={"choices": [{"message": {"content": "好"}}]})

    client = LlamaServerClient(
        "http://127.0.0.1:8001", max_retries=3, transport=httpx.MockTransport(handler)
    )
    assert client.chat("p", SamplingParams()) == "好"
    assert calls["n"] == 3


def test_retry_exhausted_raises_with_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.translation.llama_client.time.sleep", lambda _: None)
    client = LlamaServerClient(
        "http://127.0.0.1:8001",
        max_retries=2,
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")),
    )
    with pytest.raises(TranslationError) as exc:
        client.chat("p", SamplingParams())
    assert exc.value.status_code == 500


def test_4xx_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.translation.llama_client.time.sleep", lambda _: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    client = LlamaServerClient(
        "http://127.0.0.1:8001", max_retries=3, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(TranslationError):
        client.chat("p", SamplingParams())
    assert calls["n"] == 1


def test_health_false_when_unreachable(tmp_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    provider = HYMTProvider(
        tmp_settings,
        client=LlamaServerClient("http://127.0.0.1:8001",
                                 transport=httpx.MockTransport(handler)),
    )
    health = provider.health()
    assert health.ready is False
    assert "llama-server 未就绪" in health.detail


def test_health_true_when_up(tmp_settings: Settings) -> None:
    provider = HYMTProvider(
        tmp_settings,
        client=LlamaServerClient(
            "http://127.0.0.1:8001",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "ok"})),
        ),
    )
    assert provider.health().ready is True


def test_malformed_response_raises(tmp_settings: Settings) -> None:
    client = LlamaServerClient(
        "http://127.0.0.1:8001",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="not json")),
    )
    with pytest.raises(TranslationError, match="响应格式异常"):
        client.chat("p", SamplingParams())


# ── 输出清洗 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  你好  ", "你好"),
        ("译文：你好", "你好"),
        ("Translation: hello", "hello"),
        ('"你好"', "你好"),
        ("“你好”", "你好"),
        ('他说"是"的时候', '他说"是"的时候'),
    ],
)
def test_output_cleaning(raw: str, expected: str) -> None:
    assert HYMTProvider._clean(raw) == expected


def test_empty_text_short_circuits(tmp_settings: Settings) -> None:
    """空文本不该发请求 —— 白白占用 llama-server。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("空文本不应发起请求")

    provider = HYMTProvider(
        tmp_settings,
        client=LlamaServerClient("http://127.0.0.1:8001",
                                 transport=httpx.MockTransport(handler)),
    )
    assert provider.translate("   ", "auto", "zh") == "   "
