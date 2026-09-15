"""配置加载。

``config/config.yaml`` 是**唯一**配置入口 —— 任何模块都不得自己读环境变量或写死路径。
需要新配置项时加在这里，不要在使用处 ``os.environ.get`` 兜底。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from app.core.errors import ConfigError

DEFAULT_CONFIG_PATH = Path("config/config.yaml")


def _expand(value: str) -> str:
    """展开 ``~`` 与 ``$VAR``，让配置在 macOS 与开发机上都能写得自然。"""
    return os.path.expandvars(os.path.expanduser(value))


class ServerSettings(BaseModel):
    # 绑定 loopback 是方案 §22 的硬要求，不提供改成 0.0.0.0 的途径。
    host: str = "127.0.0.1"
    port: int = 8000


class LlamaSettings(BaseModel):
    base_url: str = "http://127.0.0.1:8001"
    timeout_seconds: float = 300.0
    max_retries: int = 3
    # 启动时等待 llama-server 就绪的轮询上限（方案 §28.3：launchd 不保证启动顺序）
    startup_probe_seconds: float = 0.0


class ModelSettings(BaseModel):
    """模型权重位置。

    必须是**本地路径**。方案 §4.2 明确禁止官方示例里的 ``-hf`` 参数，
    因为它会联网从 Hugging Face 拉取，违反 §20。
    """

    path: str = ""

    @field_validator("path")
    @classmethod
    def _expand_path(cls, v: str) -> str:
        return _expand(v) if v else v


class SamplingSettings(BaseModel):
    """官方推荐采样参数（方案 §4.2）。非必要不要改。"""

    temperature: float = 0.7
    top_k: int = 20
    top_p: float = 0.6
    repeat_penalty: float = 1.05


class GlossarySettings(BaseModel):
    path: str = "config/glossary/terms.yaml"

    @field_validator("path")
    @classmethod
    def _expand_path(cls, v: str) -> str:
        return _expand(v)


class ChunkingSettings(BaseModel):
    # 一个 chunk 的字符预算。方案 §10 要求按结构切分、不逐句翻译。
    max_chars: int = 1200


class DocConversionSettings(BaseModel):
    """旧版 .doc 转换（方案 §5.1.1）。"""

    enabled: bool = True
    soffice_path: str = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    fallback_textutil: bool = True
    timeout_seconds: float = 180.0

    @field_validator("soffice_path")
    @classmethod
    def _expand_path(cls, v: str) -> str:
        return _expand(v)


class PdfSettings(BaseModel):
    ocr: bool = True
    # Docling 默认首次运行时联网拉模型（方案 §28.1 第 4 步的警告），必须指向本地目录。
    # 填的是 **artifacts 目录**（一个模型一个 `<org>--<repo>` 子目录），
    # 不是 Docling 的 cache 根目录 —— 两者填反的实机报错见 tests/dev-mac/test.log。
    docling_artifacts_path: str = "/Users/Shared/offline-translator/models/docling"

    @field_validator("docling_artifacts_path")
    @classmethod
    def _expand_path(cls, v: str) -> str:
        return _expand(v)


class FontSettings(BaseModel):
    """中文字体候选（方案 §15）。

    ReportLab 的 TTFont **只支持 TrueType 轮廓**，不支持 OTF/CFF。
    而 "Noto Sans CJK SC" 最常见的发布形态恰好是 .otf —— 直接喂进去会失败。
    所以候选表优先 .ttf，其次 .ttc（需要 subfont_index）。
    """

    cjk_candidates: list[dict[str, Any]] = Field(default_factory=list)


class PathSettings(BaseModel):
    install_root: str = "/Users/Shared/offline-translator"
    data_dir: str = "data"
    temp_dir: str = "data/temp"
    processing_dir: str = "data/processing"
    logs_dir: str = "logs"
    db_path: str = "data/jobs.db"

    @field_validator("*")
    @classmethod
    def _expand_path(cls, v: str) -> str:
        return _expand(v)


class DefaultsSettings(BaseModel):
    """UI 的默认取值。用 /Users/Shared 而非 ~/Documents 是为了避开 TCC（方案 §17.2）。"""

    source_dir: str = "/Users/Shared/translator/Source"
    output_dir: str = "/Users/Shared/translator/Translated"
    source_language: str = "auto"
    target_language: str = "zh"


class LoggingSettings(BaseModel):
    level: str = "INFO"
    # 只写本地文件。方案 §22 禁止 remote logging。
    file: str = "logs/app.log"

    @field_validator("file")
    @classmethod
    def _expand_path(cls, v: str) -> str:
        return _expand(v)


class Settings(BaseModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    llama: LlamaSettings = Field(default_factory=LlamaSettings)
    model: ModelSettings = Field(default_factory=ModelSettings)
    sampling: SamplingSettings = Field(default_factory=SamplingSettings)
    glossary: GlossarySettings = Field(default_factory=GlossarySettings)
    chunking: ChunkingSettings = Field(default_factory=ChunkingSettings)
    doc_conversion: DocConversionSettings = Field(default_factory=DocConversionSettings)
    pdf: PdfSettings = Field(default_factory=PdfSettings)
    fonts: FontSettings = Field(default_factory=FontSettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    defaults: DefaultsSettings = Field(default_factory=DefaultsSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    # pydantic v2 默认把 `model_` 开头的字段名视为保留前缀，会对 `model` 字段发警告。
    model_config = {"protected_namespaces": ()}

    @classmethod
    def load(cls, path: str | Path | None = None) -> Settings:
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not cfg_path.is_file():
            raise ConfigError(f"配置文件不存在: {cfg_path}")
        try:
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"配置文件解析失败 ({cfg_path}): {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"配置文件根节点必须是映射: {cfg_path}")
        try:
            return cls.model_validate(raw)
        except Exception as exc:  # pydantic ValidationError
            raise ConfigError(f"配置校验失败 ({cfg_path}): {exc}") from exc
