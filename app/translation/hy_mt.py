"""HY-MT1.5 Provider —— ``TranslationProvider`` 的唯一实现（方案 §12）。

系统只有一个翻译模型、一个推理 Runtime（§23 / §27），
所有 llama.cpp 的细节到此为止，不向业务层泄漏。
"""

from __future__ import annotations

import re

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.offline import assert_local_model_path, assert_loopback
from app.terminology.glossary import Glossary
from app.translation.base import ProviderHealth, TranslationProvider
from app.translation.llama_client import LlamaServerClient, SamplingParams
from app.translation.prompts import build_prompt

log = get_logger(__name__)

# 模型偶尔会带上「译文：」之类的前缀或整体加引号，剥掉即可。
# 官方模板已经要求「只需要输出翻译后的结果」，所以这里只做最保守的清洗。
_LEADING_LABEL = re.compile(r"^\s*(译文|翻译结果|Translation)\s*[:：]\s*")


class HYMTProvider(TranslationProvider):
    def __init__(
        self,
        settings: Settings,
        *,
        client: LlamaServerClient | None = None,
        glossary: Glossary | None = None,
    ) -> None:
        # ── 离线守卫：三项都在构造期查，不给运行期留坑（方案 §20 / §26）──
        assert_loopback(settings.llama.base_url, what="llama-server 地址")
        assert_local_model_path(settings.model.path)

        self.settings = settings
        self.model_path = settings.model.path
        self.sampling = SamplingParams(
            temperature=settings.sampling.temperature,
            top_k=settings.sampling.top_k,
            top_p=settings.sampling.top_p,
            repeat_penalty=settings.sampling.repeat_penalty,
        )
        self.client = client or LlamaServerClient(
            settings.llama.base_url,
            timeout=settings.llama.timeout_seconds,
            max_retries=settings.llama.max_retries,
        )
        self.glossary = glossary if glossary is not None else Glossary.load(settings.glossary.path)

    def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
        glossary: dict | None = None,
        context: str | None = None,
    ) -> str:
        """翻译一段文本。

        Args:
            source_language: **不进 prompt**。官方模板（§13）只有 target language
                槽位，源语言由模型自行识别 —— 这也是「混合语言自动识别」（§25）
                的实现方式。本参数只作记录之用。
            context: **当前实现忽略**。官方模板没有上下文槽位，硬塞进去会破坏模板，
                而 §13 明确要求「模板本身不动」。保留该参数是为了满足 §12 规定的接口
                签名，供未来替换的 Provider 使用。
        """
        if not text or not text.strip():
            return text

        table = Glossary(glossary) if glossary is not None else self.glossary
        hits = table.match(text)
        prompt = build_prompt(text, target_language, hits)

        if hits:
            log.debug("术语命中 %d 条: %s", len(hits), [h[0] for h in hits])

        raw = self.client.chat(prompt, self.sampling)
        return self._clean(raw)

    @staticmethod
    def _clean(raw: str) -> str:
        out = raw.strip()
        out = _LEADING_LABEL.sub("", out)
        # 整体被引号包裹时剥掉（只剥成对的，避免误伤正文里的引号）
        for lq, rq in (('"', '"'), ("“", "”"), ("'", "'"), ("「", "」")):
            if len(out) >= 2 and out.startswith(lq) and out.endswith(rq):
                inner = out[1:-1]
                if lq not in inner and rq not in inner:
                    out = inner
                break
        return out.strip()

    def health(self) -> ProviderHealth:
        """永不抛异常（见 base.ProviderHealth 的说明）。"""
        try:
            ready = self.client.health()
        except Exception as exc:  # noqa: BLE001 —— 探活必须永不抛出
            return ProviderHealth(ready=False, detail=f"探活异常: {exc}")
        if ready:
            return ProviderHealth(ready=True, detail=f"model={self.model_path}")
        return ProviderHealth(
            ready=False,
            detail=(
                f"llama-server 未就绪（{self.settings.llama.base_url}）。"
                "launchd 不保证启动顺序，稍候会自行恢复；"
                "若持续未就绪请检查 com.offline-translator.llama 服务。"
            ),
        )
