# Mac mini M4 24GB 完全离线文档翻译系统

内部自用的单机离线文档翻译工具。意大利语 / 德语 / 英语 → 中文，中文 → 英语，
支持 DOC / DOCX / PDF（含扫描件 OCR）。

实现依据：[`mac-mini-m4-24gb-offline-document-translator-HYMT-llamacpp.md`](mac-mini-m4-24gb-offline-document-translator-HYMT-llamacpp.md)（以下 §N 均指该方案的章节）。

> **要动手部署，看 [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md)** —— 从 clone 到可用的完整操作手册，含排障与卸载。本文只讲架构与验收状态。

> **一个模型、一个推理 Runtime、一个业务服务、一个本地数据库。**
> **目录 → 解析 → HY-MT1.5 → 输出。**

---

## 架构

```text
Web UI ──> FastAPI (127.0.0.1:8000) ──> JobRunner ──> pipeline
                                            │              │
                                       SQLite + FS     ┌────┴────┐
                                                       │         │
                                                    DOCX        PDF
                                                python-docx  Docling/OCR
                                                       │         │
                                                       └────┬────┘
                                                   TranslationProvider
                                                            │
                                                    llama-server (8001)
                                                            │
                                                        HY-MT1.5
```

| 层 | 位置 | 职责 |
|---|---|---|
| API / UI | `app/api/` `app/web/` | §18 的三个接口 + 一个只读目录浏览；§6 的极简界面 |
| Job | `app/jobs/` | 扫描结果 → 串行执行 → SQLite 状态与计数 |
| 文档 | `app/document/` | DOCX / PDF 解析、`.doc` 转换、chunking、QA |
| 渲染 | `app/rendering/` | ReportLab + 中文字体 |
| 翻译 | `app/translation/` | Provider 抽象 + llama-server 客户端 |
| 术语 | `app/terminology/` | YAML 术语库与命中 |
| 基座 | `app/core/` | 配置、离线守卫、异常、日志 |

三条跨层契约：

- `app/document/model.py` 的 `Block` —— DOCX 与 PDF 两条链路收敛到同一模型，
  下游的 chunking / 翻译 / QA 对文档格式无感知
- `app/translation/base.py` 的 `TranslationProvider` —— 业务代码的唯一翻译入口
- `app/jobs/pipeline.py` 的 `translate_file()` —— Job 层与文档层的接缝

---

## 部署（macOS，完全离线）

> 下面是流程概览。**逐步操作手册见 [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md)。**

严格按 §28.6 的顺序：

```text
1. 法务确认许可证（§3.3）   ← 可能推翻模型选型，放最前
2. 联网备料 offline-bundle   scripts/prepare-bundle.sh
3. 目标 Mac 离线安装         scripts/install.sh
4. launchd + 自动登录        scripts/start.sh
5. 断网重启验收              scripts/offline-check.sh
6. 跑 §25 测试集 + §26 验收表
```

> 第 1 步未确认前不要开始第 2 步 —— 备料和模型选型绑定，换模型意味着权重要全部重下。

### 备料（在**能联网**的机器上，只做一次）

```bash
./scripts/prepare-bundle.sh offline-bundle
```

这是全仓**唯一**允许联网的脚本。两个最容易漏的点脚本里都有提示：

- **Docling 的模型产物**必须提前固化。它首次运行时会静默联网拉模型，
  不提前落盘就会在断网环境直接失败，且报错不明显（§28.1 第 4 步）。
- **中文字体要取 TTF 或 TTC，不能只拿 OTF**。ReportLab 不支持 OTF(CFF) 轮廓，
  而 Noto Sans CJK SC 最常见的发布形态恰好是 `.otf`。

### 安装

```bash
./scripts/install.sh offline-bundle
```

`uv pip install` 带了 `--offline --no-index` —— 不带的话 uv 会静默回落到 PyPI，
那就不是离线安装了（§28.2）。

### 启动与检查

```bash
./scripts/start.sh
```

```bash
./scripts/health.sh
```

launchd **不保证** api 与 llama-server 的启动顺序。依赖处理放在 API 侧：
`/api/v1/health` 在 llama-server 未就绪时返回 503，服务本身不退出（§28.3）。

用的是 **LaunchAgent 而非 LaunchDaemon**（§17.1）—— 本系统要读写用户目录，
而 LaunchDaemon 在现代 macOS 上访问 TCC 保护目录基本只能靠 MDM 下发 PPPC 描述文件。
要满足「开机自动启动」还需**开启自动登录**（系统设置 → 用户与群组）。

默认路径用 `/Users/Shared/...` 而非 `~/Documents`，避开 TCC（§17.2）。

---

## 配置

全部在 [`config/config.yaml`](config/config.yaml)。几个要点：

| 项 | 说明 |
|---|---|
| `model.path` | **必须是本地 .gguf 绝对路径**。填成 `org/repo:QUANT` 这类 HF repo id 会被启动期守卫拒绝 —— 那是 `-hf` 的写法，会联网拉取（§4.2 / §20） |
| `sampling.*` | 官方推荐值 `0.7 / 20 / 0.6 / 1.05`（§4.2），非必要不要改 |
| `doc_conversion.enabled` | 关掉则扫描阶段直接忽略 `.doc` |
| `fonts.cjk_candidates` | 按顺序尝试，**优先 TTF**，TTC 需同时配 `subfont_index` |
| `pdf.docling_artifacts_path` | 指向本地 **artifacts 目录**（一个模型一个 `<org>--<repo>` 子目录），杜绝运行期联网。填成 Docling 的 cache 根目录（`~/.cache/docling`）会在解析时报 `layout-heron not found` —— 实机记录见 `tests/dev-mac/test.log` |

### 换模型只改一行

1.8B ↔ 7B ↔ Hy-MT2 之间切换只需改 `model.path`，代码零改动（§3.2）。

---

## 开发

```bash
py -3 -m venv .venv && ./.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

```bash
./.venv/Scripts/python.exe -m pytest tests -q
```

测试**零网络、零模型、零外部进程**：

- `tests/conftest.py` 的网络守卫会让任何非 loopback 连接直接抛异常
- llama-server 走 `httpx.MockTransport`
- Docling 走 `tests/fixtures/fake_docling.py` 的鸭子类型假对象（**不安装 docling**）
- LibreOffice / textutil 的 `subprocess` 全部 monkeypatch

---

## 验收状态

### 本次已覆盖（CODE，有测试支撑）

- [x] 目录递归扫描、只收 `.doc`/`.docx`/`.pdf`、忽略隐藏文件与 Office 锁文件
- [x] 输出保持相对目录结构、`_zh` 命名、`.doc` 输出转 `.docx`
- [x] 目标文件已存在则跳过（执行时二次判定）
- [x] 源目录与输出目录重叠时拒绝创建 Job
- [x] 原文件永不修改（sha256 前后比对）
- [x] DOCX 结构化解析与写回原结构，段落/表格/标题/图片计数一致
- [x] 图片保真（`w:drawing` 与旧版 VML `w:pict` 都不碰）
- [x] 超链接 URL 不变，显示文字被翻译且不重复
- [x] `.doc` → `.docx` 转换适配层（LibreOffice + textutil 兜底，含「退出码 0 但无产物」判定）
- [x] PDF 经 Docling 解析，item 类型映射、页码、表格坐标；未知类型降级不失败
- [x] ReportLab + 中文字体渲染 PDF，字体嵌入、CJK 换行、HTML 转义
- [x] 按结构 chunking（不逐句），分隔结构丢失时自动降级逐段落
- [x] QA：数字 / URL / Email / 货币 / 日期 / 变量占位符；DOCX 计数；PDF 页数/空白页/字体嵌入
- [x] QA 告警不使文件失败
- [x] SQLite Job 仓储、§9 状态机、非法跃迁拒绝、重启后历史仍在
- [x] 单文件失败隔离，Completed / Skipped / Failed 三项计数
- [x] 官方 prompt 模板逐字落地，无 system prompt，源语言不进模板
- [x] 术语干预用官方格式，只注入实际命中的术语；未命中时 prompt 字节一致
- [x] 官方采样参数默认值与配置覆盖
- [x] 离线守卫：拒绝 `-hf` 形态模型标识、拒绝非 loopback、校验模型文件存在
- [x] 三个必需接口 + 受控扩展的目录浏览；llama 未就绪时 `/health` 返回 503 而非 500
- [x] 界面只有 §6 的 6 个元素，零外部资源
- [x] 两份 plist 合法，含 `-ngl 99` / `RunAtLoad` / `KeepAlive` / 日志路径，且无 `-hf`
- [x] 七个脚本语法合法、含 `set -euo pipefail`；除备料脚本外零联网命令
- [x] 仅一个翻译模型、不依赖 Redis/RabbitMQ/PostgreSQL（源码级断言）
- [x] 全仓测试零出站网络连接

### 待实机验证（DEPLOY，需在 Mac mini M4 上断网执行）

以下各项**本次未验证**，原因是无模型权重、非 macOS 环境。不得据本仓代码声称通过。

- [ ] 完全断网仍可启动（§26）
- [ ] EN → ZH 翻译质量
- [ ] ZH → EN 翻译质量
- [ ] DE → ZH 翻译质量
- [ ] IT → ZH 翻译质量
- [ ] 混合语言自动识别（§25）
- [ ] Native PDF 真实保真度
- [ ] 扫描 PDF 的 OCR 效果
- [ ] 真实 LibreOffice 对 `.doc` 的转换保真度
- [ ] 术语干预在译文中**确实生效**（格式已验，效果未验）
- [ ] Mac 重启后 launchd 自动拉起 api 与 llama 两个服务
- [ ] 自动登录配置生效
- [ ] 中文 PDF 在真实 Noto CJK 字体下的排版效果
- [ ] §25 的四份测试文档与中文参考件比对
- [ ] 整个业务流程零出站连接（`lsof -i -P`，§28.5 第 5 步）

验证方式见 `scripts/offline-check.sh`。

### 范围外

- [ ] **许可证合规确认（§3.3）—— 法务事项，不在本仓范围**

  HY-MT1.5 采用 Tencent Hunyuan Community License，其中明确写明
  **不适用于欧盟、英国、韩国**。而本系统的核心场景正是翻译意大利语、德语文档，
  强烈暗示业务涉及欧盟。若部署主体或使用地落在这些地区，该许可证不适用。

  备选方案：**Hy-MT2**（Apache 2.0，无地域限制、无 MAU 门槛、无输出限制，
  同样提供 GGUF、同样支持 it/de/en/zh）。因同样走 llama.cpp + GGUF，
  切换成本仅为更换 `config.yaml` 里的模型路径。

  两者的质量对比官方未给出直接数据，**未经本项目验证**。是否换代由需求方决策。

---

## V1 不做（§23）

第二个翻译模型 · 模型切换 · oMLX · Ollama · Redis · RabbitMQ · PostgreSQL ·
Docker · Kubernetes · 用户系统 · 权限系统 · 云同步 · 在线翻译 API ·
自动 ensemble · AI Quality Scoring · 复杂术语 UI · 文件在线编辑 ·
PDF 像素级排版 · 多机推理
