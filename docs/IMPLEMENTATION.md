# 实施文档 —— 从 clone 到可用

面向执行人的操作手册。按顺序做，每步都给了「怎么确认这步真的成了」。

依据方案 §28（macOS 实施步骤）。文中 §N 均指
[`mac-mini-m4-24gb-offline-document-translator-HYMT-llamacpp.md`](../mac-mini-m4-24gb-offline-document-translator-HYMT-llamacpp.md)。

> **验证状态**：本文的命令来自仓库里的脚本，脚本本身经过语法检查与静态断言，
> 但**尚未在真实 Mac mini M4 上端到端执行过**。首次实施时请逐步确认，
> 遇到与本文不符的地方以实际为准并回来更正本文。

---

## 0. 先想清楚：两台机器

这是最容易搞混的地方 —— 整个流程涉及**两台机器**，职责完全不同。

```text
┌─────────────────────────┐         ┌──────────────────────────┐
│  A 备料机（能联网）      │         │  B 目标机（Mac mini M4） │
│                         │  拷贝   │                          │
│  clone 仓库             │ ──────► │  clone 仓库              │
│  跑 prepare-bundle.sh   │ bundle  │  跑 install.sh           │
│  产出 offline-bundle/   │         │  完全断网运行            │
└─────────────────────────┘         └──────────────────────────┘
```

- **A 机**只做一件事：把所有联网才能拿到的东西下载下来，打成 `offline-bundle/`。
  跑完就没它的事了。A 机最好也是 Apple Silicon Mac —— llama.cpp 与 wheel 都要编成 arm64。
- **B 机**是生产机，全程不联网。

`offline-bundle/` 的传递方式随意（U 盘 / 内网共享 / AirDrop），它就是一个普通目录。

---

## 1. 阻塞项：许可证确认（§3.3）

§28.6 把这一步排在**最前**，而且明确「第 1 步未确认前不要开始第 2 步」。

原因：HY-MT1.5 采用 Tencent Hunyuan Community License，其中写明

```text
THIS LICENSE AGREEMENT DOES NOT APPLY IN THE
EUROPEAN UNION, UNITED KINGDOM AND SOUTH KOREA
```

而本系统的核心场景正是翻译**意大利语、德语**文档，强烈暗示业务涉及欧盟。

| 结论 | 接下来 |
|---|---|
| 可以用 HY-MT1.5 | 按本文默认流程走 |
| 不能用（落在 EU/UK/KR） | 改用 **Hy-MT2**（Apache 2.0，无地域限制）。改法见 §7.3，只改两个变量 |

> 备料和模型选型绑定 —— 权重下完再换模型，第 3 步要全部重做。所以这一步必须先定。

---

## 2. A 机：准备环境

### 2.1 clone

```bash
git clone https://github.com/LeonJi0545/mac-m4-doc-pdf-translate.git
```

```bash
cd mac-m4-doc-pdf-translate
```

### 2.2 A 机需要的工具

| 工具 | 用途 | 安装 |
|---|---|---|
| Python 3.12+ | 跑 docling 预热 | `brew install python@3.12` |
| `uv` | 编译依赖锁、下载 wheel | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Xcode Command Line Tools | 编译 llama.cpp | `xcode-select --install` |
| `cmake` | 同上 | `brew install cmake` |
| `hf`（huggingface-cli） | 下载模型权重 | `pip install -U "huggingface_hub[cli]"` |

**不需要**在 A 机手装 docling。第 4 步会用第 1 步刚下好的 wheelhouse 现建一个临时环境
（`.prepare-venv/`），模型就由那份 docling 下载 —— 和 B 机将来跑的是同一个版本。

> ⚠ 用哪个 `python3` 备料，B 机就得建同一个小版本的 venv：wheel 是按 `cp3XX` 打的。
> 脚本会把版本写进 `offline-bundle/python-version`，`install.sh` 照它建 venv。
> 要指定别的解释器就 `PY_BIN=/path/to/python3.12 ./scripts/prepare-bundle.sh`。

确认：

```bash
python3 --version && uv --version && cmake --version && hf version
```

### 2.3 准备一份 sample.pdf

`prepare-bundle.sh` 的第 4 步要拿它做两件事：在 A 机上以**断网语义**验一遍 Docling
模型齐不齐，然后跟着 bundle 拷到 B 机，装完再验一遍。

**这份 sample.pdf 必须包含扫描页**（即图片型 PDF）—— 否则 OCR 引擎的模型不会被触发下载，
到了断网的 B 机上处理扫描件时才会失败，而且报错不明显。

放在仓库根目录，命名 `sample.pdf`。

---

## 3. A 机：备料

```bash
./scripts/prepare-bundle.sh offline-bundle
```

脚本会依次做六件事，产出：

```text
offline-bundle/
├── requirements.txt      # 依赖锁（install.sh 从这里读，不是从仓库读）
├── python-version        # 备料用的 Python 小版本，install.sh 照它建 venv
├── wheels/               # 全部 Python 依赖的 wheel
├── llama/llama-server    # 编译好的 arm64 二进制
├── models/hy-mt1.5/      # GGUF 权重
├── docling/              # Docling artifacts：一个模型一个 <org>--<repo> 子目录
├── sample.pdf            # 脚本自动放，B 机装完拿它做离线自检
├── fonts/                # 中文字体 ← 需手动放
└── libreoffice/          # LibreOffice dmg ← 需手动放
```

> `docling/` 是 **artifacts 目录**，不是 `~/.cache/docling`。
> 早先的做法是「不带 artifacts_path 跑一次解析，再把 `~/.cache/docling` 整个拷走」，
> 结果 bundle 里只有 OCR 引擎那一份，layout / tableformer 落在了 Hugging Face 缓存里，
> B 机断网后才炸（实机记录见 `tests/dev-mac/test.log`）。
> 现在脚本改成显式下载 + 当场断网验收，第 4 步跑通就说明模型是齐的。

### 3.1 手动补两样东西

脚本的第 5、6 步只打印提示，需要人工放文件。

**字体（§15）** —— 放进 `offline-bundle/fonts/`：

> ⚠ ReportLab **不支持 OTF/CFF 轮廓**，而 "Noto Sans CJK SC" 最常见的官方发布形态
> 恰好是 `.otf`。取错了要到渲染阶段才会暴露。

| 取这个 | 不要取 |
|---|---|
| `NotoSansSC-Regular.ttf`（最省事） | ❌ `NotoSansCJKsc-Regular.otf` |
| `NotoSansCJK-Regular.ttc`（需配 `subfont_index`） | |

**LibreOffice（§5.1.1）** —— macOS arm64 的 dmg 放进 `offline-bundle/libreoffice/`。
只有要处理旧版 `.doc` 才需要；不需要的话可以跳过，并在 B 机上把
`config.yaml` 的 `doc_conversion.enabled` 设为 `false`。

### 3.2 确认备料完整

```bash
ls -la offline-bundle/wheels | head && ls -lh offline-bundle/llama/llama-server && ls -lh offline-bundle/models/hy-mt1.5/
```

四项必须都有内容：

- [ ] `offline-bundle/requirements.txt` 存在
- [ ] `offline-bundle/wheels/` 里有一堆 `.whl`
- [ ] `offline-bundle/llama/llama-server` 存在且是 arm64（`file offline-bundle/llama/llama-server`）
- [ ] `offline-bundle/models/hy-mt1.5/` 里有 `.gguf`
- [ ] `offline-bundle/docling/` 里有 `docling-project--docling-layout-heron`
      这类 `<org>--<repo>` 子目录 ← 最容易漏的一项；第 4 步打印的
      「docling 离线解析通过」才是它真正的判据
- [ ] `offline-bundle/sample.pdf` 存在（脚本自动拷，B 机自检要用）
- [ ] `offline-bundle/fonts/` 里是 `.ttf` 或 `.ttc`，**不是 `.otf`**

**记下 GGUF 的实际文件名**，下一步要用：

```bash
ls offline-bundle/models/hy-mt1.5/*.gguf
```

HF 仓库的文件命名不总是 `HY-MT1.5-1.8B-Q4_K_M.gguf`，以实际为准。

---

## 4. B 机：安装

把整个 `offline-bundle/` 拷到 B 机，并在 B 机上 clone 同一个仓库。

```bash
git clone https://github.com/LeonJi0545/mac-m4-doc-pdf-translate.git
```

```bash
cd mac-m4-doc-pdf-translate
```

B 机只需要 `uv`（安装 Python 环境用），不需要 cmake / hf / Xcode。

### 4.1 执行安装

把上一步记下的 GGUF 文件名填进 `MODEL_FILE`：

```bash
MODEL_FILE=HY-MT1.5-1.8B-Q4_K_M.gguf ./scripts/install.sh /path/to/offline-bundle
```

安装到 `/Users/Shared/offline-translator`。要换位置就加 `INSTALL_ROOT=...`。

> 用 `/Users/Shared` 而非 `~/Documents` 是**有意的**（§17.2）：
> `~/Documents`、`~/Desktop`、`~/Downloads` 都受 TCC 保护，
> LaunchAgent 访问它们需要手动授「完全磁盘访问权限」，而授权对象是 Python 解释器，
> 授权面偏大。放 `/Users/Shared` 零授权、零弹窗、重装系统后行为一致。

脚本做八件事：拷代码与配置 → 建离线 venv → 装 llama-server 与权重 →
铺 Docling artifacts → 铺中文字体 → 渲染 launchd plist 到 `~/Library/LaunchAgents/` →
建源目录与输出目录 → **离线自检**（用 bundle 里的 sample.pdf 真跑一次 PDF 解析）。

最后那步是有意放在安装阶段的：模型缺没缺，肉眼 `ls` 看不出来，必须真解析一次才知道。
急着往下走可以 `SKIP_VERIFY=1` 跳过，但那样就得在 §6 断网验收时补回来。

重复执行是安全的：`config/` 下已存在的文件（`config.yaml`、术语库）不会被覆盖，
仓库里的新版会落成同名 `.new` 文件等你自己比对。

> ⚠ 但「保留旧配置」有个反面：新版若改了**必须跟进**的键，旧值会静静留着。
> `pdf.docling_artifacts_path` 就这么栽过一次 —— 新 artifacts 铺到了
> `$ROOT/models/docling`，服务却还照旧配置去读 `~/.cache/docling`，装完看着成功、
> 跑第一份 PDF 才炸。出现 `.new` 文件时脚本会提醒你，第 8 步的自检也会用
> **部署中的那份配置**真跑一遍，指错了在那里就会失败。

### 4.2 确认安装

```bash
ls -lh /Users/Shared/offline-translator/models/hy-mt1.5/ /Users/Shared/offline-translator/runtime/
```

```bash
/Users/Shared/offline-translator/.venv/bin/python -c "import fastapi, docx, reportlab, docling; print('deps ok')"
```

```bash
grep -c '@@' ~/Library/LaunchAgents/com.offline-translator.*.plist
```

最后一条应该输出两个 `0` —— 有非零说明占位符没被替换干净，plist 里还留着 `@@INSTALL_ROOT@@`。

### 4.3 核对配置

打开 `/Users/Shared/offline-translator/config/config.yaml`，确认四项：

```yaml
model:
  path: "/Users/Shared/offline-translator/models/hy-mt1.5/<实际的文件名>.gguf"

fonts:
  cjk_candidates:
    - name: "NotoSansSC"
      path: "/Users/Shared/offline-translator/fonts/NotoSansSC-Regular.ttf"

doc_conversion:
  enabled: true   # 不装 LibreOffice 就设 false
  soffice_path: "/Applications/LibreOffice.app/Contents/MacOS/soffice"

pdf:
  docling_artifacts_path: "/Users/Shared/offline-translator/models/docling"
```

> `pdf.docling_artifacts_path` 填的是 **artifacts 目录**——里面一个模型一个
> `<org>--<repo>` 子目录。**不要**改成 `~/.cache/docling`（Docling 的 cache 根目录），
> 那会得到 `Model 'docling-project/docling-layout-heron' not found in artifacts_path`。

> `model.path` **必须是本地绝对路径**。填成 `tencent/HY-MT1.5-1.8B-GGUF:Q4_K_M`
> 这类写法会被启动期的离线守卫直接拒绝 —— 那是 `-hf` 的形态，会联网拉取（§4.2 / §20）。

---

## 5. B 机：启动

### 5.1 开启自动登录

方案 §17 要求「开机自动启动」，但本系统用的是 **LaunchAgent 而非 LaunchDaemon**（§17.1）——
LaunchAgent 要用户登录后才启动。所以需要：

**系统设置 → 用户与群组 → 自动以此用户身份登录 → 选定用户**

> ⚠ 自动登录会降低物理安全性（开机即进入桌面）。内部自用、机器在可控环境下可接受；
> 若不可接受，就退而要求「重启后人工登录一次」，并在验收标准里写明这一条。

为什么不能用 LaunchDaemon：它以 root 运行，在 macOS 14/15 上访问 TCC 保护目录
基本只能靠 MDM 下发 PPPC 描述文件 —— 这条路对内部自用场景走不通。

### 5.2 加载服务

```bash
./scripts/start.sh
```

`start.sh` 是幂等的**重新加载**：已加载的先 `bootout` 再 `bootstrap`，
所以改完 plist 直接再跑一次就生效，不会静默沿用旧配置。
加载完它会自己轮询 `health.sh`（默认最多等 60 秒，`STARTUP_TIMEOUT=N` 可调），
等不到就**以非零码退出**并告诉你去看哪个日志 —— 不会像旧版那样吞掉错误还宣布成功。

### 5.3 确认

```bash
./scripts/health.sh
```

期望输出 `both services OK`。

如果看到 `api DOWN` 或 `llama-server DOWN`，**先别急着重启** —— 看日志：

```bash
tail -40 /Users/Shared/offline-translator/logs/llama.err.log /Users/Shared/offline-translator/logs/api.err.log
```

> launchd **不保证** api 与 llama-server 的启动顺序，api 可能先起来。
> 这是预期行为：`/api/v1/health` 会如实返回 503 直到 llama-server 就绪，
> api 进程本身不退出，所以 `KeepAlive` 也不会反复重启它。稍等即可。

### 5.4 打开界面

```bash
open http://127.0.0.1:8000/
```

---

## 6. B 机：断网验收（§28.5）

**这一步不能省。** 联网环境下的「跑通」不能证明离线可用 ——
Docling、OCR、tokenizer 都可能在首次使用**某个功能**时才尝试联网。

```bash
./scripts/offline-check.sh
```

脚本会打印清单并检查当前状态。完整验收要人工走完五步：

1. **物理断网**（关 Wi-Fi / 拔网线，不要只靠防火墙规则）

   ```bash
   networksetup -setairportpower en0 off
   ```

2. **重启**，验证自动登录 + 两个服务自起

   ```bash
   sudo reboot
   ```

3. 重启后确认

   ```bash
   launchctl list | grep offline-translator && ./scripts/health.sh
   ```

4. **跑 §25 的四份测试文档**，覆盖 EN / DE / IT / ZH 四个方向。
   必须跑**完整**业务流程（含扫描件 OCR）才算验过 —— 这是第 4 步的意义所在。

5. **确认零出站连接**

   ```bash
   lsof -i -P | grep -v '127.0.0.1' | grep -iE 'llama|python'
   ```

   应该没有输出。有输出就是违反 §20，需要排查。

验收清单见 [README 的「待实机验证」一节](../README.md#待实机验证deploy需在-mac-mini-m4-上断网执行)。

---

## 7. 日常运维

### 7.1 常用命令

| 做什么 | 命令 |
|---|---|
| 启动 | `./scripts/start.sh` |
| 停止 | `./scripts/stop.sh` |
| 健康检查 | `./scripts/health.sh` |
| 备份 | `./scripts/backup.sh` |
| 看日志 | `tail -f /Users/Shared/offline-translator/logs/api.out.log` |

备份只备 `config`、`data/output` 和数据库 —— 模型与 runtime 可按 §28.1/§28.2 重建，不进备份。

### 7.2 改配置后重启

```bash
./scripts/stop.sh && ./scripts/start.sh
```

### 7.3 换模型（1.8B ↔ 7B ↔ Hy-MT2）

方案 §3.2 建议先用 1.8B：官方称其翻译质量与 7B 相当，而体积约 1/4，
CPU / 统一内存下速度大致与体积成反比。质量不够再换。

**A 机**重新下权重：

```bash
MODEL_REPO=tencent/HY-MT1.5-7B-GGUF ./scripts/prepare-bundle.sh offline-bundle-7b
```

**B 机**改两处：

1. `config.yaml` 的 `model.path` 指向新的 `.gguf`
2. `~/Library/LaunchAgents/com.offline-translator.llama.plist` 里 `-m` 后面的路径

然后 `./scripts/stop.sh && ./scripts/start.sh`。

**代码零改动** —— 架构对模型无感知。换 Hy-MT2（Apache 2.0）同理，
把 `MODEL_REPO` 换成 `tencent/Hy-MT2-1.8B-GGUF` 即可。

### 7.4 术语库

编辑 `/Users/Shared/offline-translator/config/glossary/terms.yaml`：

```yaml
terms:
  Tenant: 租户
  Managed Service Provider: 托管服务提供商
```

改完重启 api 生效。只有当前文本**实际命中**的术语才会被注入 prompt（§11），
整张表不会被塞进去。不做术语管理界面（§23）。

---

## 8. 排障

按现象查。

### 启动期就报错

| 报错关键字 | 原因 | 处理 |
|---|---|---|
| `model.path 看起来是 Hugging Face repo id` | 填成了 `-hf` 的写法 | 改成本地 `.gguf` 绝对路径 |
| `模型文件不存在` | 路径拼错，或 GGUF 文件名与默认值不符 | `ls` 确认实际文件名后改 `config.yaml` |
| `llama-server 地址 必须指向本机 loopback` | `base_url` 被改成了外网地址 | 改回 `http://127.0.0.1:8001` |
| `配置文件不存在` | `OFFLINE_TRANSLATOR_CONFIG` 指错了 | 检查 api plist 里的该环境变量 |
| `ModuleNotFoundError: No module named 'docling'`（跑备料脚本时） | 旧版第 4 步直接使唤系统 `python3`，而 A 机未必装了 docling | 已修：现在从 wheelhouse 现建临时环境。若仍看到，说明跑的是旧脚本 |
| `No solution found` / 找不到匹配的 wheel（B 机安装时） | venv 的 Python 小版本和 wheelhouse 对不上 | 看 `cat offline-bundle/python-version`，确认 B 机装了该小版本的 Python；旧 bundle 没有这个文件，重跑备料即可 |
| `Load failed: 5: Input/output error` | 旧版 `start.sh` 用的 `launchctl load`，服务已加载时就报这个，**而且仍返回 0** | 已修：现在用 `bootstrap` / `bootout`。若仍看到，说明跑的是旧脚本 |
| `Bootstrap failed: 37: Operation already in progress` | 上一次 `bootout` 还没落地 | `start.sh` 已会等待；手工操作时 `launchctl print gui/$(id -u)/com.offline-translator.llama` 确认消失后再来 |

### 翻译时报错

| 现象 | 原因 | 处理 |
|---|---|---|
| 译文 PDF 中文是空白/方块 | 字体没注册上 | 看日志里的字体错误；**多半是拿了 `.otf`**，换 TTF/TTC |
| `找不到可用的中文字体` | 候选路径全不存在 | 错误信息会列出试过哪些路径，照着补 |
| `.doc` 文件全部失败 | LibreOffice 没装或路径不对 | 看错误信息里给的三条路；不需要就把 `doc_conversion.enabled` 设 `false` |
| PDF 全部失败，提示 `未安装 docling` | bundle 的 docling 没装上 | 回到 §4.2 确认依赖完整 |
| `Docling 模型目录不存在或为空` | `pdf.docling_artifacts_path` 指向的目录没铺上 | 按 §4.3 核对路径；确认 `ls /Users/Shared/offline-translator/models/docling` 有 `<org>--<repo>` 子目录 |
| `Model 'docling-project/docling-layout-heron' not found in artifacts_path`<br>`Available models in ...: RapidOcr` | 路径填成了 Docling 的 cache 根目录，或备料时只拷了缓存没显式下载 | 改 §4.3 的路径；若 bundle 本身就缺，回 A 机重跑 `prepare-bundle.sh` 第 4 步（新版会当场验收） |
| `Image processor config not found: .../preprocessor_config.json` | **目录形状不对，不是文件损坏。** `<org>--<repo>/` 底下是 `blobs/ refs/ snapshots/ trees/`(HF 的对象存储)，而 docling 要的是平铺的 `config.json` / `preprocessor_config.json` / 权重。这正是 `docling-tools models download-hf-repo`(docling 报错里给的第 1 条建议)的产出 —— **照它的提示修，修不好** | 用 `docling-tools models download -o <artifacts 目录>` 重下；另查 `pdf.docling_artifacts_path` 是否还指着 `~/.cache/docling`。新版在备料、安装、运行三处都会拦下这个形状 |
| 扫描件 PDF 失败但普通 PDF 正常 | OCR 模型没备进 bundle | A 机上用**含扫描页**的 sample.pdf 重跑第 4 步 |

### 跑着跑着停了 / 结果不对

| 现象 | 说明 |
|---|---|
| 某个文件失败，其他正常 | **这是设计行为**（§26）。失败原因在界面的「N 个文件失败」里展开可见 |
| 完成数比文件数少 | 差额是 Skipped —— 输出目录已有同名 `_zh` 文件时会跳过（§8）。要重翻就先删输出 |
| 创建任务报「源目录与输出目录不能是同一个目录」 | 两者重叠会让输出被下一轮当成输入，无限增殖。换个输出目录 |
| 界面一直显示 `0 / N` | 看 api 日志；多半是 llama-server 没起来 |
| QA 告警很多但文件是成功的 | **告警不使文件失败**（§14）。它提示译文里丢了数字/URL/日期之类，需要人工判断 |

### 服务反复重启

`KeepAlive` 为 true 时 launchd 会不停拉起崩溃的进程。先停掉再看日志，
否则日志会被反复覆盖的启动信息淹没：

```bash
./scripts/stop.sh && tail -60 /Users/Shared/offline-translator/logs/llama.err.log
```

### 翻译很慢

- 确认 plist 里有 `-ngl 99` —— 漏了会退化成纯 CPU，慢一个量级
- 确认在跑 1.8B 而不是 7B（§3.2：体积差约 4 倍，耗时大致成反比）
- 文件是**串行**处理的，这是有意的：只有一个 llama-server 常驻（§21），
  并发只会争抢同一块统一内存

---

## 9. 卸载

```bash
./scripts/stop.sh
```

```bash
rm ~/Library/LaunchAgents/com.offline-translator.llama.plist ~/Library/LaunchAgents/com.offline-translator.api.plist
```

```bash
rm -rf /Users/Shared/offline-translator
```

> 删之前先确认 `/Users/Shared/offline-translator/data/output` 里没有还需要的译文。
> 源目录 `/Users/Shared/translator/Source` 不在删除范围内，原文件从头到尾没被改过。

---

## 10. 开发者：在本机改代码

不需要模型也能跑全部测试。

```bash
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
```

```bash
./.venv/bin/python -m pytest tests -q
```

测试**零网络、零模型、零外部进程**：llama-server 走 `httpx.MockTransport`，
Docling 走鸭子类型的假对象（**不需要安装 docling**），LibreOffice 的 subprocess 全部 mock，
`tests/conftest.py` 里的守卫会让任何非 loopback 连接直接抛异常。

```bash
./.venv/bin/python -m ruff check app tests
```

改完提交前建议再跑一遍架构约束测试 —— 它会检查翻译入口是否仍然唯一、
有没有混进 §23 排除的中间件、plist 与脚本里有没有冒出 `-hf`：

```bash
./.venv/bin/python -m pytest tests/unit/test_architecture.py tests/unit/test_deploy_assets.py -q
```
