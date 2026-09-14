"""异常层级。

全部业务异常都继承 ``TranslatorError``，好让调用方可以用一个 except 兜住
自家的错误、而不会顺手吞掉 ``KeyboardInterrupt`` 之类的系统异常。

Job runner 对单文件失败的宽捕获（见 app/jobs/runner.py）依赖这一点：
它捕获的是 ``Exception``，而 ``KeyboardInterrupt`` / ``SystemExit``
继承自 ``BaseException``，不会被吞。
"""

from __future__ import annotations


class TranslatorError(Exception):
    """本项目所有业务异常的基类。"""


class ConfigError(TranslatorError):
    """配置文件缺失、格式错误或取值非法。"""


class OfflineViolation(TranslatorError):
    """配置或运行时行为违反了离线约束（方案 §20 / §22）。

    典型场景：
    - 模型路径写成了 ``-hf`` 形态的 HF repo id
    - llama-server 的 base_url 指向非 loopback 地址
    - 模型文件在本地不存在
    """


class ConversionError(TranslatorError):
    """旧版 .doc → .docx 转换失败（方案 §5.1.1）。"""


class ParseError(TranslatorError):
    """文档解析失败。"""


class RenderError(TranslatorError):
    """文档渲染 / 写回失败。"""


class TranslationError(TranslatorError):
    """翻译请求失败（llama-server 不可达、重试耗尽、响应格式异常）。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class IllegalTransition(TranslatorError):
    """Job 状态机的非法跃迁。"""

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"非法状态跃迁: {current} -> {target}")
        self.current = current
        self.target = target
