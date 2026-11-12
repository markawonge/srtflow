# SRTFlow 部署与配置笔记

整理日期：2026-11-12  
代码版本：SRTFlow 0.3.0  
适用目标：在 Ubuntu 服务器上通过 SSH 使用 AMD ROCm 转写英语、日语视频，再通过翻译 API 生成简体中文字幕。

## 从 GitHub 部署

本仓库已公开，可直接通过 HTTPS 克隆，无需 GitHub 登录：

```bash
mkdir -p "$HOME/whisper-subtitles"
git clone https://github.com/markawonge/srtflow.git "$HOME/whisper-subtitles/srtflow"
bash "$HOME/whisper-subtitles/srtflow/install.sh"
~/.local/bin/srtflow
```

新设备还需要准备 ROCm 驱动、Docker 与转写镜像，见第4、5节。仓库包含宿主机 wheel，不包含 GPU 镜像、模型权重或账户密钥。

## 1. 功能与运行方式

SRTFlow 提供三个主要功能：

1. 视频 → 原文 SRT → 简体中文 SRT。
2. 视频 → 原文 SRT，不翻译。
3. 已有原文 SRT → 简体中文 SRT。

同时支持批量选择文件、检查点续跑、失败部分重试和后台运行，不需要 WebUI，也不需要区分说话人。

| 模块 | 运行位置 | 作用 |
| --- | --- | --- |
| SRTFlow 命令与配置向导 | 宿主机 Python 虚拟环境 | 文件选择、任务调度、配置、进度、结果展示 |
| faster-whisper + Silero VAD | 原有 ROCm Docker 镜像 | GPU 语音识别、语音活动检测、原文字幕输出 |
| 翻译适配器 | 宿主机 | 调用 DeepL、百度、DeepSeek 或本地兼容 API |
| tmux | 宿主机 | SSH 断开后继续任务 |

目标语言固定为简体中文。视频不必复制到指定 input 文件夹，用户始终输入服务器上的路径，由程序自动映射到容器。

## 2. 当前设备与已记录环境

下面记录的是本次设备上的实际版本，不是新设备必须照搬的驱动安装清单。

| 项目 | 当前配置 |
| --- | --- |
| CPU / 内存 | Intel i5-13600 / 32GB |
| GPU | AMD Radeon RX 7900 XT，20GB，`gfx1100` |
| 系统 | Ubuntu 26.04.1 LTS |
| 宿主机 Python | 3.14.4；SRTFlow 要求 Python 3.11+ |
| 宿主机 ROCm | AMD SMI 显示 10.1.0 |
| HIP 组件版本 | `7.16.26385-0000000` |
| Docker | 29.9.3 |
| 转写镜像 | `whisper-subtitles:rocm` |
| 已记录镜像 ID | `sha256:600f810f7835b964f845e6594626a81557e3c9d1323b8f7187562bed2e9a45ec` |
| 镜像基础 | `rocm/dev-ubuntu-24.04:7.2.4-complete` |
| 容器 Python | 3.12.3 |
| faster-whisper / CTranslate2 | 1.2.1 / ROCm 专用 wheel 4.8.5 |
| PyAV | 18.1.0 |
| 默认模型 / 精度 | `large-v3` / `float16` |

宿主机负责 GPU 内核驱动，容器提供转写所需的 Python 和 ROCm 用户态环境。新设备仍需先安装适合其系统与显卡的驱动、ROCm 和 Docker Engine，再验证容器能访问 GPU。其他显卡或系统组合需要在新设备实测。

## 3. 部署包内容

文件名：`srtflow-deploy-0.3.zip`。

解压后包含：

| 路径 | 内容 |
| --- | --- |
| `srtflow/README.md` | 本部署与配置笔记 |
| `srtflow/install.sh` | 创建或复用宿主机虚拟环境，安装命令入口 |
| `srtflow/pyproject.toml` | Python 项目描述与依赖 |
| `srtflow/dist/` | 已构建的 SRTFlow 0.3.0 wheel |
| `srtflow/src/srtflow/` | 完整程序源码，包含容器转写 worker |
| `srtflow/tests/` | 自动化测试 |
| `srtflow/samples/` | 英语、日语 SRT 小样本 |
| `srtflow/deployment/Dockerfile` | 按本次固定版本整理的转写镜像构建配方 |
| `srtflow/deployment/config.example.json` | 无密钥的默认配置示例；供参考，不会自动覆盖配置 |
| `srtflow/tools/verify-gpu.sh` | 容器 GPU 与 VAD 检查 |
| `srtflow/tools/build-image.sh` | 按随包 Dockerfile 构建镜像 |
| `srtflow/tools/export-image.sh` | 导出当前镜像及校验文件，供新设备迁移 |

部署包不包含 Docker 镜像本体、模型权重、视频、用户凭据、模型缓存或历史任务。现有机器优先通过镜像导出/导入复用已经运行的依赖组合；新建镜像可使用随包配方，需要联网下载其固定版本资源。

## 4. 新设备准备

### 4.1 基础工具

在 Ubuntu 服务器安装驱动、ROCm 和 Docker Engine 后，准备宿主机工具：

```bash
sudo apt update
sudo apt install -y python3-venv tmux unzip gzip
```

检查：

```bash
python3 --version
docker --version
command -v tmux
ls -l /dev/kfd /dev/dri/render*
/opt/rocm/bin/rocminfo
```

SRTFlow 的视频转写需要 `/dev/kfd` 与 `/dev/dri`，不需要在宿主机另装 faster-whisper、CTranslate2 或 PyTorch。

### 4.2 当前用户权限

安装 Docker 后，使当前用户能直接运行 Docker：

```bash
sudo usermod -aG docker "$USER"
```

如果系统已提供 `video`、`render` 组，可同时赋予 GPU 设备组权限：

```bash
sudo usermod -aG video,render "$USER"
```

注销 SSH 并重新登录，然后检查：

```bash
id -nG
docker info --format '{{.ServerVersion}}'
```

日常运行 SRTFlow 不使用 sudo。后台任务采用同一用户权限；Docker 组授予该用户广泛的宿主机管理能力。

### 4.3 解压代码

假设压缩包已经传到服务器 `~/Downloads/`：

```bash
mkdir -p "$HOME/whisper-subtitles/cache"
unzip -o "$HOME/Downloads/srtflow-deploy-0.3.zip" \
  -d "$HOME/whisper-subtitles"
```

后续默认安装位置为：

```text
~/whisper-subtitles/srtflow/
```

如已有该项目，先结束正在运行的任务，再更新程序文件；用户配置、凭据、历史任务保存在另外的目录，不会由解压操作替换。

## 5. 准备转写 Docker 镜像

### 方案 A：从现有机器迁移镜像

在已经可以正常转写的原设备上执行：

```bash
bash "$HOME/whisper-subtitles/srtflow/tools/export-image.sh" \
  "$HOME/srtflow-migration"
```

原设备需先取得本部署包中的 `tools/export-image.sh`。也可直接使用以下等价命令：

```bash
mkdir -p "$HOME/srtflow-migration"
docker save whisper-subtitles:rocm \
  -o "$HOME/srtflow-migration/whisper-subtitles-rocm.tar"
```

辅助脚本生成的是 `.tar.gz`，直接命令生成的是 `.tar`；二者均可由 `docker load` 导入。只选择一种导出方式即可。

将导出文件传到新服务器。使用辅助脚本导出时，同时传输 `SHA256SUMS` 与 `image-id.txt`，在新设备检查并导入：

```bash
cd "$HOME/srtflow-migration"
sha256sum -c SHA256SUMS
docker load -i whisper-subtitles-rocm.tar.gz
```

若使用直接命令导出的 `.tar`：

```bash
docker load -i "$HOME/srtflow-migration/whisper-subtitles-rocm.tar"
```

检查镜像：

```bash
docker image inspect whisper-subtitles:rocm --format '{{.Id}}'
```

迁移的是同一镜像时，应与原设备当前导出的镜像 ID 一致。导入镜像不等于安装 GPU 驱动，新设备仍需完成上一节的宿主机准备。

### 方案 B：根据固定版本配方重新构建

没有现成导出镜像时，在新设备执行：

```bash
bash "$HOME/whisper-subtitles/srtflow/tools/build-image.sh"
```

该脚本使用 `deployment/Dockerfile` 构建 `whisper-subtitles:rocm`。配方固定基础镜像、faster-whisper、CTranslate2 ROCm wheel 及 PyAV 版本；依赖 Docker Hub、GitHub 和 Python 包源可访问。它不是完整离线镜像归档，也没有锁定所有间接依赖；要保留原设备的精确运行组合，使用方案 A。

随包 Dockerfile 来自本次部署配方；本次整理没有重新下载镜像、构建 GPU 环境或在新显卡上执行推理。

### 检查容器 GPU 与 VAD

```bash
bash "$HOME/whisper-subtitles/srtflow/tools/verify-gpu.sh"
```

检查结果应包含：

- GPU 数量至少为 1。
- 支持 `float16`。
- Silero VAD 加载成功。

工具会动态读取设备组 ID，不写死用户 UID 或 render 组编号。AMD ROCm 版 CTranslate2 的 Python 接口仍使用 `device="cuda"`。

## 6. 安装 SRTFlow 命令

```bash
bash "$HOME/whisper-subtitles/srtflow/install.sh"
```

安装器会：

1. 创建或复用 `srtflow/.venv`。
2. 安装随包提供的 SRTFlow wheel。
3. 创建 `~/.local/bin/srtflow` 入口。

首次安装需要联网取得 Rich 依赖；已有可用 Rich 环境时，安装器直接使用随包 wheel，不重复下载依赖。

启动：

```bash
~/.local/bin/srtflow
```

如果希望当前终端直接输入 `srtflow`：

```bash
export PATH="$HOME/.local/bin:$PATH"
srtflow
```

需要永久生效时，将该 export 行加入实际使用的 Shell 配置文件，例如 Bash 的 `~/.bashrc` 或 Zsh 的 `~/.zshrc`。

## 7. 首次配置

没有 `config.json` 时，输入 `srtflow` 自动进入配置向导。也可随时运行：

```bash
srtflow --config
```

### 7.1 翻译引擎

可分别保存多个引擎，但每个任务使用明确选定的一个引擎；不会自动在后台切换。

| 引擎 | 配置内容 |
| --- | --- |
| DeepL | Free / Pro 套餐、API Key、可选原生 glossary_id |
| 百度 | 通用文本翻译 API 的 APPID 与密钥 |
| DeepSeek | API 基础地址、账户实际可用模型、API Key、输出 token 上限 |
| 本地 API | OpenAI Chat Completions 兼容地址、服务实际提供的模型、可选密钥、输出 token 上限、JSON 模式开关 |

配置后会实际翻译一句固定短文本并校验中文返回，可能产生少量用量。配置成功并保存后，日常任务不再重复实测。

**本次使用 DeepL Free：**

- 套餐选 Free。
- 端点自动设为 `https://api-free.deepl.com`。
- 填写 DeepL API Key。
- 没有原生 glossary_id 时留空；该字段是术语表 ID。
- 完成实际测试。

本地 API 由宿主机调用，基础地址例如 `http://127.0.0.1:8000/v1`，程序追加 `/chat/completions`。此处 `127.0.0.1` 指 Ubuntu 服务器本身。工具仅接入已经存在的服务，不部署本地翻译模型；HTTP 地址明文传输，远程服务可使用 HTTPS。

### 7.2 默认目录与语言

| 设置 | 本次使用方式 |
| --- | --- |
| 默认输入目录 | 例如 `~/Videos`，按新设备实际目录填写 |
| 默认输出位置 | 视频相同目录；翻译已有 SRT 时为原文 SRT 相同目录 |
| 默认输出目录 | 选择集中输出时填写 |
| 递归扫描 | 根据目录组织选择开启或关闭 |
| 默认源语言 | 日语、英语，或逐文件自动检测英/日 |
| 目标语言 | 固定简体中文 |
| 默认翻译引擎 | DeepL |

翻译已有 SRT 时，优先读取 `.jp.srt`、`.en.srt` 后缀；无语言标记时参考默认语言。源语言设为自动且文件也无标记时，会请用户确认。

### 7.3 默认转写参数

默认优先采用已经使用的配置：

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| 模型 | `large-v3` | 常规质量配置 |
| 精度 | `float16` | 当前 GPU 已验证的计算类型 |
| VAD | 开启 | 跳过没有检测到语音的区间 |
| VAD 阈值 | `0.6` | 检测语音的门槛 |
| 最短语音 | 250ms | 过滤过短的候选片段 |
| 分段静音间隔 | 700ms | 组织相邻语音区间 |
| 语音边界留白 | 300ms | 保留语音边缘 |
| 单区间最长时间 | 25秒 | 控制独立转写区间长度 |
| beam_size | 5 | 解码搜索宽度 |
| 镜像 | `whisper-subtitles:rocm` | 现有 GPU 转写镜像 |
| 缓存目录 | `~/whisper-subtitles/cache` | 模型持久化存储 |

| 调整目的 | 可尝试设置 |
| --- | --- |
| 较轻对白需要更宽松检测 | VAD 阈值 `0.5` |
| 更严格检测候选语音 | VAD 阈值 `0.65`，可能漏掉对白 |
| 尝试提高速度 | 模型改为 `turbo`，需自行比较准确率 |

`turbo` 是模型选择，不是另一个加速开关。VAD 不保证将所有叹息、呻吟等非对白人声排除，阈值应结合小样本选择。

程序按 VAD 区间独立识别，再映射回原视频时间轴；固定 `word_timestamps=False`、`condition_on_previous_text=False`、`temperature=0`。不把远距离音频拼接后推算时间轴。

### 7.4 高级设置

| 设置 | 默认值 |
| --- | --- |
| 翻译每批最大条数 | 20；百度实际按单条请求 |
| 每批原文字符预算 | 4000 |
| 相邻上下文 | 3条 |
| 请求超时 | 60秒 |
| 额外重试 | 3次 |
| 请求后间隔 | 1秒 |
| 并发 | 1 |
| 已有中文结果 | 跳过 |
| 已有原文字幕 | 遇到时确认 |
| 执行方式 | 后台运行并自动显示进度 |
| 本地术语表 | 可选 JSON 路径，适用于 DeepSeek / 本地 API |

已有输出可改为“发布前备份后重做”。输入字幕不会被中文输出覆盖。修改全局默认配置只影响后续新任务；历史任务仍使用其快照。

## 8. 日常使用

```bash
srtflow
```

正常流程为：

1. 展示已保存配置；“是否修改”默认选否。
2. 选择视频转写并翻译、仅转写或翻译已有 SRT。
3. 选择当前目录、默认输入目录，或逐行粘贴多个文件/目录。
4. 从扫描清单选择编号、多选或回车全选。
5. 查看输入与输出路径、主要设置，回车开始。
6. 自动显示当前阶段与实际进度，完成后列出结果路径。

当前目录是启动命令时所在目录。路径支持空格、中文和日文。扫描会排除隐藏目录、隐藏文件、`._` 文件和临时文件，并自动去重。

默认复用保存的语言、模型、VAD、目录和后台设置；不再逐项询问。遇到同名输出、需要创建目录或无法安全推断文件名等情况，会单独提示处理。

### 字幕命名

| 输入 | 输出 |
| --- | --- |
| `Movie.2024.mkv`，日语 | 原文 `Movie.2024.jp.srt` |
| `Movie.2024.mkv`，英语 | 原文 `Movie.2024.en.srt` |
| 视频转写并翻译 | 中文 `Movie.2024.srt` |
| 原文 `Movie.2024.jp.srt` | 中文 `Movie.2024.srt` |

日语文件后缀使用 `jp`，转写参数内部使用 `ja`。仅去掉视频最后一个扩展名。翻译保留原文编号和时间轴，不合并、拆分或重新定时。

首次使用所选模型会下载权重，之后复用缓存。转写容器按文件执行并自动删除，空闲时不需要单独 stop 常驻转写服务。

## 9. 后台运行、进度与续跑

默认后台使用 tmux。SSH 断开后任务继续；服务器重启后需手动恢复。

| 命令 | 功能 |
| --- | --- |
| `srtflow` | 正常交互流程，也可选择继续任务或重试失败部分 |
| `srtflow --config` | 修改默认配置 |
| `srtflow --status 任务ID` | 查看任务摘要 |
| `srtflow --watch 任务ID` | 查看实时进度 |
| `srtflow --resume` | 选择并继续未完成任务 |
| `srtflow --retry` | 选择任务，仅重试失败部分 |

进度界面的 Ctrl+C 只退出查看，后台任务继续。重新登录后可再次 `--watch`。

转写百分比以已处理的 VAD 保留语音时间为依据；翻译以已校验并保存的字幕条数为依据。音频解码、VAD 扫描和模型加载没有可靠百分比时，显示阶段、活动指示与计时，不使用固定计时模拟进度。

任务会保存转写区间检查点和已完成翻译。继续任务时已完成转写或译文不会重复调用推理/API；音频准备、VAD 重算和输入校验仍可能再次执行。

API 临时错误先按设置重试。需要用户处理时保存进度，提示重新配置并实测；通过后可继续未完成部分。SSH 未连接时不等待键盘输入，重新登录后用 `--resume` 处理。显式更换翻译引擎或模型会记录配置修订及译文来源。

## 10. 配置、缓存与任务文件位置

| 位置 | 内容 |
| --- | --- |
| `~/whisper-subtitles/srtflow/` | 程序、宿主机虚拟环境和辅助脚本 |
| `~/.local/bin/srtflow` | 命令入口 |
| `~/.config/srtflow/config.json` | 默认配置 |
| `~/.config/srtflow/secrets.json` | API 凭据，权限600 |
| `~/whisper-subtitles/cache/` | 默认模型缓存 |
| `~/.local/state/srtflow/tasks/任务ID/` | 任务状态与检查点 |
| 任务目录中的 `worker.log` | 执行日志 |
| 任务目录中的 `events.jsonl` | 进度事件 |
| 任务目录中的 `config-revisions/` | 用户确认过的翻译配置修订 |

示例路径使用默认 XDG 位置；设置了 `XDG_CONFIG_HOME` 或 `XDG_STATE_HOME` 时，以相应环境变量为准。

## 11. 新设备可选迁移内容

### 模型缓存

无需重新下载权重时，在旧设备打包默认缓存：

```bash
mkdir -p "$HOME/srtflow-migration"
tar -czf "$HOME/srtflow-migration/model-cache.tar.gz" \
  -C "$HOME/whisper-subtitles" cache
```

新设备恢复：

```bash
mkdir -p "$HOME/whisper-subtitles"
tar -xzf "$HOME/srtflow-migration/model-cache.tar.gz" \
  -C "$HOME/whisper-subtitles"
```

自定义缓存路径时改为实际目录。恢复后的目录需由当前用户读写。

### 默认配置与凭据

可以在新设备重新运行配置向导。需要迁移账户配置时，单独通过自己的安全传输渠道复制配置目录；不要将凭据合并到公开代码包。

默认位置下可在旧设备打包：

```bash
mkdir -p "$HOME/srtflow-migration"
tar -czf "$HOME/srtflow-migration/user-config.private.tar.gz" \
  -C "$HOME/.config" srtflow
chmod 600 "$HOME/srtflow-migration/user-config.private.tar.gz"
```

新设备恢复后确认权限：

```bash
mkdir -p "$HOME/.config"
tar -xzf "$HOME/srtflow-migration/user-config.private.tar.gz" \
  -C "$HOME/.config"
chmod 700 "$HOME/.config/srtflow"
chmod 600 "$HOME/.config/srtflow/config.json" \
  "$HOME/.config/srtflow/secrets.json"
```

迁移后检查默认输入、输出、缓存和术语表路径是否适合新设备，并对账户引擎做一次实际测试。

### 历史任务

仅需新建任务时，不迁移状态目录。需要恢复历史任务时，可在停止相关任务后，单独迁移 `~/.local/state/srtflow/`，并保留输入视频、原文及已发布输出。

历史任务保存绝对路径、输入指纹、字幕指纹和转写镜像 ID。可靠续跑要求这些内容仍可核对，且原镜像可用；新用户名、不同目录或重新构建镜像可能使旧任务不兼容。不要手工修改任务快照绕过校验；无法保持原路径和运行组合时，在新设备创建新任务。

Python 虚拟环境不要从旧设备直接复制，使用安装器重建。

## 12. 部署验收

按以下顺序确认：

1. `docker info` 可由当前用户直接运行。
2. `tools/verify-gpu.sh` 检查 GPU、float16 和 Silero VAD。
3. `srtflow --config` 完成一个翻译引擎的小文本实测。
4. 使用 `samples/` 中一个 SRT 完成中文导出。
5. 使用一小段英语或日语视频，确认原文、中文路径与时间轴。
6. 开始后台任务后重新登录，用 `--status` 或 `--watch` 查看状态。

源码测试：

```bash
cd "$HOME/whisper-subtitles/srtflow"
.venv/bin/python -m unittest discover -s tests -q
```

当前 0.3 代码已有63项自动化测试，使用模拟 API、小样本及本机 HTTP 服务覆盖主流程，不消耗付费翻译额度。镜像导出、Dockerfile 构建、新设备 GPU 推理和实际 SSH 断开需在目标设备验收。
