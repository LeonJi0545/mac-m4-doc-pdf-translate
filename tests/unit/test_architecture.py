"""架构约束的源码级断言。

这些是方案 §23（V1 不做清单）与 §26（验收表）里**可以静态验证**的部分。
放进测试是为了让「架构漂移」在 CI 里立刻暴露，而不是等到代码评审才发现。

注意测试的**边界要选对**，否则只会制造误报：

- `app/core/config.py` 里有 `LlamaSettings`，这是 config.yaml 的 `llama:` 段的 schema
  （方案 §4 本就规定了这一节），不算「业务代码依赖 llama.cpp」。
- `app/core/offline.py` 的报错文案里出现 `-hf` 字样，那正是**拦截**它的地方。

所以下面按真实约束来写：业务层不得 import llama 客户端；`-hf` 只在真正会被当成命令行
参数的文件（plist / shell / yaml）里扫。
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

APP = Path("app")

# 纯业务层 —— 这些包的**代码**里不该出现 llama。
#
# 刻意不含 `api`：/api/v1/health 按方案 §28.3 必须如实报告 llama-server 的就绪状态，
# 响应体里有个 `llama` 字段是需求本身，不是抽象泄漏。
BUSINESS_PACKAGES = ["document", "rendering", "jobs", "terminology"]

# 组装根。它**必须**认识具体实现 —— 总得有人把 HYMTProvider 构造出来。
# 把它排除掉不是放水：§12 约束的是「业务代码不直接依赖 llama.cpp」，
# 而组装根的职责恰恰就是选定具体实现。
COMPOSITION_ROOT = {Path("app/main.py")}


def _py_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """只返回**真实代码**，剥掉注释与字符串字面量（含 docstring）。

    这是关键：注释里写「不引入 Redis」、docstring 里说明 llama-server 的启动顺序，
    都是文档而不是依赖。按原始文本 grep 只会制造误报，反过来逼人把有价值的说明删掉。
    """
    source = path.read_text(encoding="utf-8")
    buckets: dict[int, list[str]] = {}
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type in (tokenize.COMMENT, tokenize.STRING, tokenize.FSTRING_START,
                            tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                            tokenize.DEDENT):
                continue
            buckets.setdefault(tok.start[0], []).append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:  # pragma: no cover
        raise AssertionError(f"{path} 无法解析: {exc}") from exc
    return [(line, " ".join(parts)) for line, parts in sorted(buckets.items())]


# ── AC-1.11 / AC-P3：翻译入口唯一（方案 §12）──────────────────────────────


def test_llama_client_not_imported_outside_translation() -> None:
    """业务代码不得直接依赖 llama.cpp —— 只能经 TranslationProvider 抽象。

    例外是组装根 ``app/main.py``（见 COMPOSITION_ROOT 的说明）。
    """
    offenders: list[str] = []
    for path in _py_files(APP):
        if path.parts[:2] == ("app", "translation") or path in COMPOSITION_ROOT:
            continue
        for i, code in _code_lines(path):
            if re.search(r"\b(LlamaServerClient|llama_client|HYMTProvider)\b", code):
                offenders.append(f"{path}:{i}: {code}")
    assert not offenders, (
        "llama 实现细节泄漏到 app/translation/ 之外（方案 §12 要求业务代码不直接依赖 "
        "llama.cpp）:\n" + "\n".join(offenders)
    )


def test_composition_root_is_the_only_exception() -> None:
    """守卫上面的例外不会被悄悄扩大 —— 组装根确实只有一个。"""
    importers = {
        path
        for path in _py_files(APP)
        if path.parts[:2] != ("app", "translation")
        and any("HYMTProvider" in code for _, code in _code_lines(path))
    }
    assert importers == COMPOSITION_ROOT, f"构造具体 Provider 的地方变了: {importers}"


@pytest.mark.parametrize("package", BUSINESS_PACKAGES)
def test_business_packages_never_mention_llama(package: str) -> None:
    """文档解析 / 渲染 / 调度 / 术语 / API 层的**代码**不该碰推理 Runtime。

    注释与 docstring 里解释「为什么只有一个 llama-server」是有价值的文档，不算违规。
    """
    root = APP / package
    if not _py_files(root):
        pytest.skip(f"app/{package}/ 尚无源码")
    offenders = [
        f"{path}:{i}: {code}"
        for path in _py_files(root)
        for i, code in _code_lines(path)
        if "llama" in code.lower()
    ]
    assert not offenders, f"app/{package}/ 的代码里出现 llama:\n" + "\n".join(offenders)


def test_translation_package_actually_implements_llama() -> None:
    """守卫上面两条不会因为「压根没实现」而空过。"""
    hits = [
        p
        for p in _py_files(APP / "translation")
        if "llama" in p.read_text(encoding="utf-8").lower()
    ]
    assert hits, "app/translation/ 内应当存在 llama 相关实现"


# ── AC-3.11 / G6：禁用中间件（方案 §9 / §23）──────────────────────────────

FORBIDDEN_MIDDLEWARE = re.compile(r"\b(redis|rabbitmq|pika|psycopg|postgresql|celery)\b", re.I)


def test_no_forbidden_middleware_in_app() -> None:
    """方案 §23：不做 Redis / RabbitMQ / PostgreSQL。§9 只用 SQLite + 文件系统。

    同样只扫真实代码 —— docstring 里写明「不引入 Redis」是文档，不是依赖。
    """
    offenders = [
        f"{path}:{i}: {code}"
        for path in _py_files(APP)
        for i, code in _code_lines(path)
        if FORBIDDEN_MIDDLEWARE.search(code)
    ]
    assert not offenders, "出现被 §23 排除的中间件:\n" + "\n".join(offenders)


def test_sqlite_is_the_only_database() -> None:
    """反向守卫：确认我们确实在用 SQLite，上面那条才不是空过。"""
    store = (APP / "jobs" / "store.py").read_text(encoding="utf-8")
    assert "import sqlite3" in store


# ── AC-P5 / AC-4.9：禁止 -hf（方案 §4.2 / §20 / §26）──────────────────────

# 只扫会被当成命令行参数的文件类型。Python 源码里出现 "-hf" 的地方是
# app/core/offline.py 的报错文案 —— 那正是拦截它的实现，不是违规。
_COMMAND_FILE_SUFFIXES = {".plist", ".sh", ".yaml", ".yml", ".html", ".json", ".toml"}
_HF_FLAG = re.compile(r"(^|\s)-hf(\s|$|=)")


@pytest.mark.parametrize("folder", ["config", "deploy", "scripts", "app"])
def test_no_hf_flag_in_command_files(folder: str) -> None:
    """模型必须走本地路径。``-hf`` 会联网从 Hugging Face 拉取权重。

    唯一例外是 ``scripts/prepare-bundle.sh`` —— 它按 §28.1 本就在**联网**机器上运行。
    """
    root = Path(folder)
    if not root.exists():
        pytest.skip(f"{folder}/ 尚未创建")
    offenders: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in _COMMAND_FILE_SUFFIXES:
            continue
        if path.name == "prepare-bundle.sh":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _HF_FLAG.search(line):
                offenders.append(f"{path}:{i}: {line.strip()}")
    assert not offenders, (
        "出现 -hf（会联网拉模型，违反方案 §20 的离线要求）:\n" + "\n".join(offenders)
    )
