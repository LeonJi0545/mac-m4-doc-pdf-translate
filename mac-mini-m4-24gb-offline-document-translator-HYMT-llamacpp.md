# Mac mini M4 24GB 完全离线文档翻译系统落地实施方案

# 1. 项目定位

内部自用的单机离线文档翻译工具。

运行环境：

- Mac mini M4
- 24GB Unified Memory
- 512GB SSD
- macOS
- 完全断网运行

支持：

- DOC
- DOCX
- PDF
- 扫描 PDF（OCR）
- 意大利语 → 中文
- 德语 → 中文
- 英语 → 中文
- 中文 → 英语

核心原则：

> **单模型、少中间件、少功能、易维护、完全离线。**

---

# 2. 最终架构

```text
                    ┌──────────────────────┐
                    │        Web UI        │
                    │  源目录 / 输出目录   │
                    └──────────┬───────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │       FastAPI        │
                    │   Job + Translation  │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
              ▼                ▼                ▼
        Directory Scan    Document Engine    SQLite + FS
                              │
                         ┌────┴────┐
                         │         │
                         ▼         ▼
                       DOCX       PDF
                    python-docx  Docling/OCR
                                   │
                                   ▼
                            Translation Engine
                                   │
                                   ▼
                               llama.cpp
                                   │
                                   ▼
                              HY-MT1.5
                                   │
                                   ▼
                            Output Documents
```

整个系统只使用一个翻译模型：

```text
HY-MT1.5
```

（尺寸 1.8B / 7B 的取舍见 §3.2，不影响架构。）

---

# 3. 模型

## 3.1 唯一翻译模型

使用腾讯官方发布的 GGUF 量化权重：

```text
tencent/HY-MT1.5-7B-GGUF
```

官方提供的量化档位与体积：

| 量化 | 体积 |
|---|---|
| **Q4_K_M** | **4.62 GB** ← 默认 |
| Q6_K | 6.16 GB |
| Q8_0 | 7.98 GB |

建议：

- Q4_K_M：默认
- 不部署其他翻译模型

> 注：早期版本写的 `HY-MT1.5-7B-4bit/` 是 MLX 社区的命名习惯，
> 与 GGUF 的 `Q4_K_M` 不是同一套体系。本系统统一使用 GGUF 命名。

语言覆盖：官方支持 36 语种互译，本需求所需的
`it` / `de` / `en` / `zh` 全部在列。

定位：

> **整个系统唯一的翻译引擎。**

---

## 3.2 尺寸选择：先试 1.8B

官方同时发布了 `tencent/HY-MT1.5-1.8B-GGUF`：

| 量化 | 体积 |
|---|---|
| Q4_K_M | 1.13 GB |
| Q6_K | 1.47 GB |
| Q8_0 | 1.91 GB |

官方说法：参数量不到 7B 的三分之一，翻译质量与 7B **相当**
（"delivers translation performance comparable to its larger counterpart"）。

为什么值得先试：

- CPU / 统一内存下生成速度**大致与模型体积成反比**
- 1.13GB vs 4.62GB ≈ **4 倍**体积差 → 批量翻译整目录的耗时可能压到约 1/4
- 切换成本≈改一个 `-m` 路径，架构完全不变，不违反 §23 的克制原则

> **该结论来自厂商自述，尚未在本项目验证。**
> 做法：V1 用 1.8B 起步，拿 §25 测试集与中文参考件做 A/B；
> 质量够就留 1.8B，不够再换 7B。

---

## 3.3 许可证与合规（须法务确认）

HY-MT1.5 采用 **Tencent Hunyuan Community License**，关键条款：

```text
THIS LICENSE AGREEMENT DOES NOT APPLY IN THE
EUROPEAN UNION, UNITED KINGDOM AND SOUTH KOREA
```

- **该许可证不适用于欧盟、英国、韩国**
- 月活 > 1 亿需另行向腾讯申请授权（内部自用不触及）
- 禁止用模型输出训练其他 AI 模型
- 另有 Acceptable Use Policy，含 20+ 项禁止用途

> ⚠ **本系统的核心场景是翻译意大利语、德语文档，强烈暗示业务涉及欧盟。**
> 若部署主体或使用地落在 EU / UK / 韩国，该许可证不适用。
> **这是合规问题，不是技术问题，须法务确认后再定稿。**

### 备选：Hy-MT2（Apache 2.0）

2026-05-21 腾讯发布了 `Hy-MT2`（1.8B / 7B / 30B-A3B MoE），
**采用 Apache License 2.0**，无地域限制、无 MAU 门槛、无输出限制，
同样提供 GGUF、同样支持 it / de / en / zh。

若 3.3 的地域限制构成障碍，切换到 Hy-MT2 可直接消除该风险，
且因同样走 llama.cpp + GGUF，**切换成本仅为更换模型路径**。

> Hy-MT2 与 HY-MT1.5 的质量对比官方未给出直接数据，**未经本项目验证**。
> 是否换代由需求方决策。

---

# 4. Runtime

只使用：

```text
llama.cpp
```

架构：

```text
FastAPI
   ↓
127.0.0.1:8001
   ↓
llama-server
   ↓
HY-MT1.5
```

不使用：

- Ollama
- oMLX
- MLX-LM
- 其他模型 Runtime

原因：

> 只有一个翻译模型，没有必要增加模型管理层。

## 4.1 为什么不复用已装的 oMLX

即使机器上已经装了 oMLX，本系统仍然不使用它。理由：

1. **官方只发 GGUF，不发 MLX。**
   `tencent/HY-MT1.5-7B-GGUF` 是腾讯官方产物，model card 直接给出 `llama-cli` 用法示例；
   官方仓库全无 MLX 相关内容。走 MLX 需要自行转换或依赖第三方社区量化，
   **反而多一步、多一份质量不确定性**。
2. **模型管理层在本系统里收益为零。**
   oMLX 的价值在于管理多个模型（拉取 / 切换 / 版本）。
   §23 已明确不做第二个模型、不做模型切换，这层只剩成本。
3. **多一层就多一处离线审计点。**
   模型管理层普遍带自动拉取行为，而 §20 明令禁止自动下载模型。
   `llama-server` 只接受本地 `-m` 路径，不配置就不会联网，审计面更小。
4. **省下的只是一次性安装，付出的是持续维护成本。**

> llama.cpp 在 Apple Silicon 上走 Metal 后端，对 M4 统一内存有原生支持，
> 性能与 MLX 在同一量级，不存在「为简化而牺牲性能」的问题。

## 4.2 离线启动注意

官方示例用的是 `-hf` 参数：

```text
llama-cli -hf tencent/HY-MT1.5-7B-GGUF:Q8_0 ...
```

**`-hf` 会联网从 Hugging Face 拉取，违反 §20。**
正式部署必须改为本地路径：

```text
llama-server -m models/hy-mt1.5/HY-MT1.5-7B-Q4_K_M.gguf --host 127.0.0.1 --port 8001
```

官方推荐采样参数：

```text
--temp 0.7 --top-k 20 --top-p 0.6 --repeat-penalty 1.05
```

模型**没有默认 system prompt**，不要自行注入。

---

# 5. 文档解析

## 5.1 DOCX

使用：

```text
python-docx
```

流程：

```text
DOCX
 ↓
结构化解析
 ↓
提取可翻译文本
 ↓
HY-MT1.5
 ↓
写回原结构
```

尽量保留：

- paragraph
- heading
- table
- list
- hyperlink
- header/footer
- image
- 基础样式

不要把 DOCX 全部转成纯文本后重新生成。

### 5.1.1 旧版 .doc（Word 97-2003 二进制格式）

**python-docx 只支持 OOXML 的 `.docx`，不支持二进制 `.doc`。**
§8 要求处理 `.doc`，因此必须先做一步格式转换。

主方案 —— LibreOffice headless：

```text
.doc
 ↓
soffice --headless --convert-to docx
 ↓
.docx
 ↓
（进入上面的 DOCX 链路）
```

- 保真度高，保留段落样式 / 表格 / 页眉页脚 / 内嵌图片
- 完全离线，无需 Office 授权
- 跨平台，开发机与生产机行为一致
- 代价：offline-bundle 体积增加约 700MB

备选 —— macOS 内置 `textutil`：

```text
textutil -convert docx source.doc
```

- 零安装、零体积，macOS 自带
- 但**仅 macOS 可用**，且复杂表格 / 图片保真度低于 LibreOffice

> 转换只是前置步骤，**不改变**「不要把 DOCX 转成纯文本」的原则：
> 转换后仍走结构化解析 → 翻译 → 写回原结构。
>
> 若实际待翻译文件中确认没有 `.doc`，也可以把 `.doc` 移出 V1 范围，
> 从 §8 的处理列表中删除 —— 这更符合 §23 的克制原则。

---

## 5.2 PDF

使用：

```text
Docling
```

负责：

- PDF layout
- reading order
- heading
- paragraph
- table
- OCR pipeline

Native PDF：

```text
PDF
 ↓
Docling
 ↓
Structured Blocks
 ↓
HY-MT1.5
 ↓
PDF Renderer
```

Scan PDF：

```text
PDF
 ↓
OCR
 ↓
Docling
 ↓
HY-MT1.5
 ↓
PDF Renderer
```

V1 不要求 PDF 像素级还原。

优先保证：

- 页面顺序
- 标题
- 段落
- 表格
- 图片
- 基本布局

---

# 6. 页面功能

内部自用，页面保持极简：

```text
┌────────────────────────────────────────┐
│ Offline Document Translator            │
├────────────────────────────────────────┤
│                                        │
│ 原文件目录                             │
│ [/Users/leon/Documents/Source ] [选择] │
│                                        │
│ 翻译后目录                             │
│ [/Users/leon/Documents/Translated] [选择]
│                                        │
│ 源语言 [Auto ▼]                        │
│ 目标语言 [中文 ▼]                      │
│                                        │
│ [开始翻译]                             │
│                                        │
│ 状态：12 / 37                          │
└────────────────────────────────────────┘
```

只保留：

1. 原文件目录
2. 翻译后目录
3. 源语言
4. 目标语言
5. 开始翻译
6. 当前进度

不做：

- 模型选择
- Quality Mode
- 模型切换
- 高级推理参数页面
- 复杂任务中心

---

# 7. 目录翻译模式

用户选择：

```text
原文件目录：
/Users/leon/Documents/Source
```

输出：

```text
翻译后目录：
/Users/leon/Documents/Translated
```

递归扫描所有子目录。

例如：

```text
Source/
├── English/
│   ├── manual.docx
│   └── guide.pdf
├── German/
│   └── contract.pdf
└── Italian/
    └── invoice.docx
```

输出保持相对目录结构：

```text
Translated/
├── English/
│   ├── manual_zh.docx
│   └── guide_zh.pdf
├── German/
│   └── contract_zh.pdf
└── Italian/
    └── invoice_zh.docx
```

---

# 8. 文件规则

只处理：

```text
.doc
.docx
.pdf
```

其他文件忽略。

递归扫描子目录。

输出文件名：

```text
original.docx
    ↓
original_zh.docx
```

```text
contract.pdf
    ↓
contract_zh.pdf
```

`.doc` 经 §5.1.1 转换后处理，**输出统一为 `.docx`**：

```text
legacy.doc
    ↓
legacy_zh.docx
```

目标文件已经存在时：

```text
跳过
```

原文件永远不修改。

---

# 9. Job 设计

不需要 Redis / RabbitMQ。

使用：

```text
SQLite
+
Filesystem
```

状态：

```text
queued
 ↓
parsing
 ↓
translating
 ↓
rendering
 ↓
completed
```

失败：

```text
failed
```

SQLite 只记录：

```text
job_id
source_path
output_path
source_language
target_language
status
progress
created_at
started_at
completed_at
error
```

---

# 10. Chunking

不要逐句翻译。

按文档结构切分：

```text
Heading
+
Paragraph
+
Paragraph
```

形成 translation chunk。

流程：

```text
Document
 ↓
Blocks
 ↓
Context Grouping
 ↓
Glossary
 ↓
HY-MT1.5
 ↓
Write Back
```

---

# 11. 术语库

简单 YAML：

```text
config/glossary/terms.yaml
```

示例：

```yaml
terms:
  Tenant: 租户
  Workspace: 工作区
  Ticket: 工单
  Billing: 计费
  Managed Service Provider: 托管服务提供商
```

翻译前按**模型官方的术语干预格式**拼入 prompt —— 不要自创格式。

HY-MT1.5 原生支持术语干预，官方模板：

```text
参考下面的翻译：{source_term} 翻译成 {target_term}
...
将以下文本翻译为{target_language}
```

即：把命中的术语对逐条列在正文之前，再接翻译指令。
只注入**当前 chunk 实际命中**的术语，不要把整个术语表塞进去。

不做术语管理 UI。

---

# 12. Translation Provider

虽然只有一个模型，仍保留一个简单 Provider 接口，方便未来替换：

```python
class TranslationProvider:
    def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
        glossary: dict | None = None,
        context: str | None = None,
    ) -> str:
        ...
```

唯一实现：

```text
HYMTProvider
```

Provider 内部调用：

```text
llama-server
```

业务代码不直接依赖 llama.cpp。

---

# 13. 翻译 Prompt

**使用模型官方的 prompt 模板，不要自拟。**
自拟 prompt 会偏离模型训练时的指令分布，通常降低翻译质量。

模型**没有默认 system prompt**，全部内容以 user role 经
`apply_chat_template()` 传入。

译成中文（IT/DE/EN → ZH）：

```text
将以下文本翻译为{target_language}，注意只需要输出翻译后的结果

{source_text}
```

译成其他语言（ZH → EN）：

```text
Translate the following segment into {target_language}, without additional explanation.

{source_text}
```

带术语干预时，按 §11 的格式在正文前追加术语对。

覆盖的方向：

```text
English → Chinese
German → Chinese
Italian → Chinese
Chinese → English
```

只修改 target language，模板本身不动。

采样参数按 §4.2 的官方推荐值。

---

# 14. QA

翻译完成后检查：

```text
Numbers
URLs
Emails
Currency
Dates
Variables
```

DOCX：

```text
Paragraph count
Table count
Heading count
Image count
```

PDF：

```text
Page count
Blank page
Missing glyph
Font embedding
```

V1 不做 AI 自动质量评分。

---

# 15. 中文 PDF 字体

提前安装：

```text
Noto Sans CJK SC
Noto Serif CJK SC
```

PDF renderer 显式指定中文字体。

---

# 16. 项目目录

```text
offline-translator/

├── app/
│   ├── api/
│   ├── core/
│   ├── document/
│   ├── translation/
│   │   ├── base.py
│   │   └── hy_mt.py
│   ├── rendering/
│   ├── terminology/
│   ├── jobs/
│   └── main.py
│
├── models/
│   ├── hy-mt1.5/
│   │   └── HY-MT1.5-7B-Q4_K_M.gguf
│   └── docling/
│
├── data/
│   ├── processing/
│   ├── output/
│   └── temp/
│
├── config/
│   ├── config.yaml
│   └── glossary/
│       └── terms.yaml
│
├── logs/
│
├── runtime/
│   └── llama.cpp/          # llama-server 二进制
│
└── scripts/
    ├── install.sh
    ├── offline-check.sh
    ├── start.sh
    ├── stop.sh
    ├── backup.sh
    └── health.sh
```

---

# 17. macOS 服务

使用：

```text
launchd
```

服务：

```text
com.offline-translator.api.plist
com.offline-translator.llama.plist
```

要求：

- 开机自动启动
- 异常自动重启
- 日志本地保存

## 17.1 用 LaunchAgent，不用 LaunchDaemon

这是 macOS 上一个容易踩的坑，必须先定下来。

| | LaunchDaemon | LaunchAgent |
|---|---|---|
| 启动时机 | 开机（无需登录） | **用户登录后** |
| 运行身份 | root（或 `UserName` 指定） | 当前用户 |
| 访问 `~/Documents` 等 TCC 保护目录 | ❌ **macOS 14/15 上基本只能靠 MDM 下发 PPPC 描述文件** | ✅ 可在「系统设置 → 隐私与安全性 → 完全磁盘访问权限」手动授予 |

本系统要读写用户目录下的源文件与输出目录，**LaunchDaemon 这条路在现代 macOS 上走不通**。

采用方案：

```text
LaunchAgent（~/Library/LaunchAgents/）
        +
开启自动登录（系统设置 → 用户与群组 → 自动以此用户身份登录）
```

自动登录补上了 LaunchAgent「需要登录才启动」的缺口，
合起来满足 §17 的「开机自动启动」。

> ⚠️ 自动登录会降低物理安全性（开机即进入桌面）。
> 内部自用、机器在可控环境下可接受；若不可接受，
> 则退而要求「重启后人工登录一次」，并在验收标准中写明。

## 17.2 避开 TCC 保护目录

`~/Documents`、`~/Desktop`、`~/Downloads` 都受 TCC 保护。
§6 界面示例里的 `/Users/leon/Documents/Source` 正好落在保护区内。

两个选择：

1. **推荐**：把源目录与输出目录放在非保护路径，例如
   `/Users/Shared/translator/Source` 与 `/Users/Shared/translator/Translated`，
   零授权、零弹窗、重装系统后行为一致
2. 或者：保留 `~/Documents` 路径，但需手动给 LaunchAgent 实际执行的
   二进制（Python 解释器）授予「完全磁盘访问权限」
   —— 授权对象是解释器，**授权面偏大**，不推荐

---

# 18. API

只提供必要接口。

## 创建翻译任务

```http
POST /api/v1/jobs
```

```json
{
  "source_dir": "/Users/leon/Documents/Source",
  "output_dir": "/Users/leon/Documents/Translated",
  "source_language": "auto",
  "target_language": "zh"
}
```

## 获取任务状态

```http
GET /api/v1/jobs/{job_id}
```

## 健康检查

```http
GET /api/v1/health
```

不提供复杂公开 API。

---

# 19. GUI 行为

点击：

```text
开始翻译
```

执行：

```text
选择源目录
 ↓
扫描子目录
 ↓
过滤 DOC/DOCX/PDF
 ↓
跳过已存在输出
 ↓
建立 Job
 ↓
逐文件处理
 ↓
显示进度
 ↓
完成
```

完成显示：

```text
Completed: 34
Skipped: 2
Failed: 1
```

---

# 20. Offline 要求

联网安装阶段一次性准备：

```text
Python runtime
Python packages
llama.cpp
Docling
OCR dependencies
HY-MT1.5 GGUF 权重
Docling model artifacts
LibreOffice（.doc 转换，见 §5.1.1）
Fonts
PDF renderer dependencies
```

保存：

```text
offline-bundle/
```

正式运行阶段：

- 不访问 Hugging Face
- 不访问云端 API
- 不自动下载模型
- 不上传日志
- 不启用 telemetry

模型必须使用本地路径。

---

# 21. 内存策略

24GB Unified Memory 下只运行一个翻译模型，不需要模型切换。

权重常驻占用（Q4_K_M）：

| 模型 | 权重 | 加上 KV cache 与运行开销的大致常驻 |
|---|---|---|
| HY-MT1.5-1.8B | 1.13 GB | 约 2–3 GB |
| HY-MT1.5-7B | 4.62 GB | 约 6–8 GB |

两者在 24GB 下都宽裕，**容量不是选型依据**；
真正的差别是速度（见 §3.2）。

建议翻译服务长期运行：

```text
llama-server
      │
   HY-MT1.5
```

文档解析和 OCR 在需要时运行。Docling + OCR 的峰值内存需单独预留，
在无独立显存的机器上它们与 llama-server 争抢同一块内存。

---

# 22. 数据安全

默认：

```text
FastAPI → 127.0.0.1
```

所有文件：

```text
data/processing/
data/output/
```

只保存在本机。

禁止：

- Cloud upload
- Telemetry
- Remote logging
- Automatic model download

原始文件不修改。

---

# 23. V1 不做的功能

明确控制范围：

- 不做第二个翻译模型
- 不做模型切换
- 不做 oMLX
- 不做 Ollama
- 不做 Redis
- 不做 RabbitMQ
- 不做 PostgreSQL
- 不做 Docker
- 不做 Kubernetes
- 不做用户系统
- 不做权限系统
- 不做云同步
- 不做在线翻译 API
- 不做自动 ensemble
- 不做 AI Quality Scoring
- 不做复杂术语 UI
- 不做文件在线编辑
- 不做 PDF 像素级排版
- 不做多机推理

---

# 24. 开发顺序

## M1：翻译 Runtime

完成：

```text
llama.cpp（llama-server）
HY-MT1.5 GGUF（先 1.8B，见 §3.2）
```

验证：

- EN → ZH
- DE → ZH
- IT → ZH
- ZH → EN
- 完全断网启动（确认未使用 `-hf`，模型走本地路径）

## M2：文档

完成：

```text
DOCX
PDF
OCR
```

## M3：目录任务

完成：

```text
Source Directory
Output Directory
Recursive Scan
Skip Existing
Progress
```

## M4：GUI + launchd

完成：

```text
Web UI
FastAPI
SQLite
launchd
```

---

# 25. 测试集

使用之前准备的测试文档：

```text
01_German_Business_Operations_Test.pdf
02_Italian_Operations_Test.docx
03_Mixed_Languages_Translation_Test.docx
04_Mixed_Languages_Incident_Test.pdf
```

中文参考文件：

```text
01_German_Business_Operations_Test_ZH_REFERENCE.pdf
02_Italian_Operations_Test_ZH_REFERENCE.docx
03_Mixed_Languages_Translation_Test_ZH_REFERENCE.docx
04_Mixed_Languages_Incident_Test_ZH_REFERENCE.pdf
```

重点测试：

- 德语 → 中文
- 意大利语 → 中文
- 英语 → 中文
- 混合语言自动识别
- DOCX 保真
- PDF 解析
- OCR
- 数字 / 日期 / 金额
- 专有名词
- 术语一致性

---

# 26. V1 验收标准

- [ ] 完全断网仍可启动
- [ ] EN → ZH
- [ ] ZH → EN
- [ ] DE → ZH
- [ ] IT → ZH
- [ ] DOCX
- [ ] Native PDF
- [ ] Scan PDF OCR
- [ ] 目录递归扫描
- [ ] 自定义输出目录
- [ ] 跳过已有输出
- [ ] 术语库
- [ ] 进度显示
- [ ] 失败文件不影响其他文件
- [ ] DOCX 输出
- [ ] PDF 输出
- [ ] Mac 重启自动恢复（launchd 拉起 api 与 llama 两个服务）
- [ ] 仅使用 HY-MT1.5 一个模型
- [ ] 通过 llama.cpp 加载官方 GGUF 权重（本地路径，非 `-hf`）
- [ ] 不依赖 Redis/RabbitMQ/PostgreSQL
- [ ] 不依赖云端 API
- [ ] `.doc` 可处理（或已明确移出 V1 范围）
- [ ] 术语干预使用官方格式，术语在译文中生效
- [ ] 许可证合规已确认（见 §3.3）

---

# 27. 最终技术栈

```text
Mac mini M4 24GB
        │
        ├── macOS
        │
        ├── Python + uv
        │
        ├── FastAPI
        │
        ├── SQLite
        │
        ├── Filesystem
        │
        ├── Docling
        │
        ├── OCR
        │
        ├── python-docx
        │
        ├── llama.cpp
        │     └── HY-MT1.5
        │
        ├── PDF Renderer
        │
        └── launchd
```

## 核心数据流

```text
原文件目录
   ↓
递归扫描
   ↓
DOCX / PDF / OCR
   ↓
Structured Blocks
   ↓
Chunking + Glossary
   ↓
HY-MT1.5
   ↓
QA
   ↓
输出目录
```

## 最终原则

> **一个模型、一个推理 Runtime、一个业务服务、一个本地数据库。**

> **目录 → 解析 → HY-MT1.5 → 输出。**

内部自用场景不为未来可能的需求提前引入额外中间件。

---

# 28. macOS 实施步骤

§16 列了 6 个脚本、§17 列了 plist、§20 提了 offline-bundle，
本节给出它们的**实际内容与执行顺序**。

## 28.0 路径约定

```text
安装根目录：/Users/Shared/offline-translator
源目录：    /Users/Shared/translator/Source
输出目录：  /Users/Shared/translator/Translated
```

用 `/Users/Shared` 而非 `~/Documents`，理由见 §17.2（避开 TCC）。

## 28.1 阶段一：联网备料（只做一次）

在**能联网**的机器上准备 `offline-bundle/`，之后整包拷到目标 Mac。

```bash
mkdir -p offline-bundle/{wheels,llama,models,fonts,docling}

# 1. Python 依赖 -> wheelhouse
uv pip compile requirements.in -o requirements.txt
uv pip download -r requirements.txt -d offline-bundle/wheels

# 2. llama.cpp（Apple Silicon arm64）
#    方式 A：取官方 release 的 macos-arm64 包
#    方式 B：源码构建（需 Xcode Command Line Tools）
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp && cmake -B build && cmake --build build --config Release -j
cp build/bin/llama-server ../offline-bundle/llama/

# 3. 模型权重（先 1.8B，见 §3.2）
hf download tencent/HY-MT1.5-1.8B-GGUF \
  --local-dir offline-bundle/models/hy-mt1.5

# 4. Docling / OCR 模型产物
#    关键：Docling 默认首次运行时联网拉模型，必须提前落盘
#    跑一次完整解析，把缓存目录整个拷进 bundle
python -c "from docling.document_converter import DocumentConverter; DocumentConverter().convert('sample.pdf')"
cp -R ~/.cache/docling offline-bundle/docling/

# 5. 中文字体
#    Noto Sans CJK SC / Noto Serif CJK SC -> offline-bundle/fonts/

# 6. LibreOffice（.doc 转换，见 §5.1.1）
#    下载 macOS arm64 dmg -> offline-bundle/
```

> ⚠️ 第 4 步最容易被漏。Docling、OCR 引擎这类库普遍在首次运行时静默联网拉模型，
> **不提前固化就会在断网环境下直接失败**，而且报错通常不明显。
> 备料完成后务必按 28.5 断网验证一遍。

## 28.2 阶段二：离线安装

```bash
# scripts/install.sh
set -euo pipefail
ROOT=/Users/Shared/offline-translator
mkdir -p "$ROOT"/{app,models,data/{processing,output,temp},config,logs,runtime}

# Python 环境（全程离线）
uv venv "$ROOT/.venv"
uv pip install --offline --no-index \
  --find-links offline-bundle/wheels -r requirements.txt

cp offline-bundle/llama/llama-server "$ROOT/runtime/"
cp -R offline-bundle/models/*          "$ROOT/models/"
cp -R offline-bundle/docling/*         "$HOME/.cache/docling/"
cp offline-bundle/fonts/*.otf          "$HOME/Library/Fonts/"
chmod +x "$ROOT/runtime/llama-server"
```

关键点：`uv pip install` 必须带 `--offline --no-index`，
否则它会静默回落到 PyPI —— 那就不是离线安装了。

## 28.3 阶段三：launchd 配置

两个 LaunchAgent，放在 `~/Library/LaunchAgents/`。

**`com.offline-translator.llama.plist`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.offline-translator.llama</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/Shared/offline-translator/runtime/llama-server</string>
    <string>-m</string>
    <string>/Users/Shared/offline-translator/models/hy-mt1.5/HY-MT1.5-1.8B-Q4_K_M.gguf</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8001</string>
    <string>-c</string><string>4096</string>
    <string>-ngl</string><string>99</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key>
  <string>/Users/Shared/offline-translator</string>
  <key>StandardOutPath</key>
  <string>/Users/Shared/offline-translator/logs/llama.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/Shared/offline-translator/logs/llama.err.log</string>
</dict>
</plist>
```

- `-ngl 99`：把全部层交给 Metal，Apple Silicon 上必加，漏了就退化成纯 CPU
- `KeepAlive` → §17 的「异常自动重启」
- `RunAtLoad` + 自动登录 → §17 的「开机自动启动」
- `StandardOutPath` / `StandardErrorPath` → §17 的「日志本地保存」
- **不出现 `-hf`**，模型走本地路径（§4.2、§26）

**`com.offline-translator.api.plist`** 同构，`ProgramArguments` 换成：

```text
/Users/Shared/offline-translator/.venv/bin/python -m app.main
```

加载：

```bash
launchctl load -w ~/Library/LaunchAgents/com.offline-translator.llama.plist
launchctl load -w ~/Library/LaunchAgents/com.offline-translator.api.plist
```

> launchd **不保证启动顺序**，API 可能先于 llama-server 起来。
> 不要试图用 launchd 表达依赖 —— 在 API 侧做重试：
> 启动时轮询 `127.0.0.1:8001` 直到就绪，未就绪时 `/api/v1/health` 返回非健康。

## 28.4 阶段四:启动、停止、健康检查

```bash
# scripts/start.sh
launchctl load -w ~/Library/LaunchAgents/com.offline-translator.{llama,api}.plist

# scripts/stop.sh
launchctl unload ~/Library/LaunchAgents/com.offline-translator.{api,llama}.plist

# scripts/health.sh
curl -sf http://127.0.0.1:8001/health  || echo "llama-server DOWN"
curl -sf http://127.0.0.1:8000/api/v1/health || echo "api DOWN"

# scripts/backup.sh —— 只需备份状态，模型可重建
tar czf "backup-$(date +%F).tgz" \
  /Users/Shared/offline-translator/{config,data/output} \
  /Users/Shared/offline-translator/*.db
```

## 28.5 阶段五:断网验收

**这一步不能省。** 联网环境下的「跑通」不能证明离线可用。

```bash
# scripts/offline-check.sh
# 1. 物理断网(拔网线 / 关 Wi-Fi),不要只靠防火墙规则
networksetup -setairportpower en0 off

# 2. 重启 Mac,验证自动登录 + 两个服务自起
sudo reboot

# 3. 重启后确认
launchctl list | grep offline-translator
./scripts/health.sh

# 4. 跑 §25 的四份测试文档,覆盖 EN/DE/IT/ZH 四个方向

# 5. 确认整个过程没有任何出站连接
lsof -i -P | grep -v '127.0.0.1' | grep -i llama
```

第 5 步是关键:Docling、OCR、tokenizer 都可能在首次使用某功能时才尝试联网,
**必须跑完整业务流程**(含扫描件 OCR)才算验过。

## 28.6 实施顺序小结

```text
1. 法务确认许可证(§3.3)      ← 可能推翻模型选型,放最前
2. 联网备料 offline-bundle    (28.1)
3. 目标 Mac 离线安装          (28.2)
4. launchd + 自动登录         (28.3)
5. 断网重启验收               (28.5)
6. 跑 §25 测试集 + §26 验收表
```

> 第 1 步未确认前不要开始第 2 步 —— 备料和模型选型绑定，
> 换模型意味着 28.1 第 3 步全部重做。
