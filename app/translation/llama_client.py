"""llama-server HTTP 客户端（方案 §4）。

用 OpenAI 兼容的 ``/v1/chat/completions`` 端点而非 ``/completion``：
方案 §13 要求内容经 ``apply_chat_template()`` 传入，chat 端点由 llama-server 侧
完成模板套用；我们自己拼 ChatML 只会引入偏差。

请求体里 ``messages`` **恒为长度 1 的 user 列表** —— 落实 §4.2 的
「模型没有默认 system prompt，不要自行注入」。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

import httpx

from app.core.errors import TranslationError
from app.core.logging import get_logger

log = get_logger(__name__)

# 重试退避序列（秒）。仅用于连接错误与 5xx。
_BACKOFF = (0.5, 1.0, 2.0)


@dataclass(frozen=True)
class SamplingParams:
    """官方推荐采样参数（方案 §4.2）。"""

    temperature: float = 0.7
    top_k: int = 20
    top_p: float = 0.6
    repeat_penalty: float = 1.05

    def as_payload(self) -> dict[str, float | int]:
        return asdict(self)


class LlamaServerClient:
    """极薄的传输层：只管发请求、重试、判健康，不懂任何业务。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 300.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, transport=self._transport)

    def chat(self, prompt: str, sampling: SamplingParams) -> str:
        """发一次翻译请求，返回模型输出的文本。"""
        payload = {
            # 恒为单条 user message —— 不注入 system prompt（§4.2）。
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            **sampling.as_payload(),
        }
        url = f"{self.base_url}/v1/chat/completions"

        last_error: Exception | None = None
        last_status: int | None = None

        for attempt in range(self.max_retries + 1):
            try:
                with self._client() as client:
                    resp = client.post(url, json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
                last_status = None
            else:
                if resp.status_code < 400:
                    return self._extract(resp)
                last_status = resp.status_code
                last_error = TranslationError(
                    f"llama-server 返回 {resp.status_code}: {resp.text[:200]}",
                    status_code=resp.status_code,
                )
                # 4xx 是我们的请求有问题，重试没有意义。
                if resp.status_code < 500:
                    break

            if attempt < self.max_retries:
                delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
                log.warning(
                    "llama-server 请求失败（第 %d/%d 次），%.1fs 后重试: %s",
                    attempt + 1,
                    self.max_retries + 1,
                    delay,
                    last_error,
                )
                time.sleep(delay)

        raise TranslationError(
            f"llama-server 请求失败，已重试 {self.max_retries} 次: {last_error}",
            status_code=last_status,
        )

    @staticmethod
    def _extract(resp: httpx.Response) -> str:
        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise TranslationError(
                f"llama-server 响应格式异常: {resp.text[:200]}"
            ) from exc

    def health(self) -> bool:
        """探活。

        **吞掉所有异常** —— 健康检查不能把调用方搞崩（方案 §28.3：
        llama-server 未就绪时 /api/v1/health 只需返回非健康）。
        """
        try:
            with self._client() as client:
                resp = client.get(f"{self.base_url}/health", timeout=5.0)
            return resp.status_code < 400
        except Exception:  # noqa: BLE001 —— 见上：探活必须永不抛出
            return False
