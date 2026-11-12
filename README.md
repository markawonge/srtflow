# SRTFlow

**简体中文** | [English](README.en.md)

在 Ubuntu 终端中完成视频语音转写与简体中文字幕翻译。

SRTFlow 使用 **faster-whisper + Silero VAD** 识别语音，通过 **AMD ROCm Docker 容器**运行转写，并在宿主机调用翻译 API。适合通过 SSH 处理英语、日语长视频。

## 功能

- **三种任务**：视频转写并翻译、视频仅转写、翻译已有原文 SRT。
- **配置一次，日常复用**：保存语言、目录、转写参数和翻译引擎，选完文件后确认开始。
- **批量处理**：支持多个文件或目录、可选递归扫描、编号多选及输入去重，排除隐藏和临时文件。
- **多种翻译接口**：DeepL、百度通用文本翻译、DeepSeek，以及 OpenAI Chat Completions 兼容的本地 API。
- **检查点与恢复**：保存转写区间和已校验译文，支持续跑、失败重试及输出冲突处理。
- **后台执行与实时进度**：使用 tmux，SSH 断开后继续运行，可重新连接查看状态。

当前版本支持英语、日语原音及逐文件英/日自动检测，翻译目标固定为**简体中文**。原文字幕与中文字幕分别保存，翻译保留原字幕编号和时间轴。

## 环境要求

| 场景 | 要求 |
| --- | --- |
| 运行命令与翻译 | Ubuntu、Python 3.11+、venv；API 服务可访问 |
| 后台运行 | tmux |
| 视频转写 | Docker Engine、可用的 AMD GPU / ROCm 驱动环境、转写镜像 |
| 随仓库 Dockerfile | Linux x86_64；显卡与宿主机驱动需兼容该容器 |

**只翻译已有 SRT 时，不需要 GPU、ROCm 或转写镜像。**

宿主机运行交互、调度与翻译模块；转写依赖安装在 Docker 容器中。请先安装适合目标设备的 ROCm 驱动和 Docker Engine，确认 `/dev/kfd` 与 `/dev/dri` 可用。项目不自动安装驱动。

## 部署

以下步骤使用默认安装目录 `~/whisper-subtitles/srtflow`。

### 1. 安装宿主机工具

```bash
sudo apt update
sudo apt install -y git python3-venv tmux gzip
```

视频转写还需要当前用户能够直接调用 Docker。安装 Docker Engine 后执行：

```bash
sudo usermod -aG docker "$USER"
```

如已安装的 ROCm 环境提供 `video` 和 `render` 组，可加入这些设备组：

```bash
sudo usermod -aG video,render "$USER"
```

重新登录 SSH，使组权限生效，并检查：

```bash
docker info --format '{{.ServerVersion}}'
ls -l /dev/kfd /dev/dri/render*
```

Docker 组拥有广泛的宿主机管理权限。日常运行 SRTFlow 使用普通用户，不加 sudo。

### 2. 获取代码并安装

```bash
mkdir -p "$HOME/whisper-subtitles"
git clone https://github.com/markawonge/srtflow.git \
  "$HOME/whisper-subtitles/srtflow"

bash "$HOME/whisper-subtitles/srtflow/install.sh"
```

安装器会创建或复用项目中的 `.venv`，安装随仓库提供的 wheel，并生成 `~/.local/bin/srtflow` 命令入口。首次安装需联网下载 Rich 等宿主机依赖。

可以直接使用完整路径启动：

```bash
~/.local/bin/srtflow
```

或将命令目录加入 PATH：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

需要永久生效时，将该 export 行加入实际使用的 Shell 配置文件，例如 `~/.bashrc` 或 `~/.zshrc`。

### 3. 准备转写镜像

只翻译已有 SRT 时，跳过本节与下一节。

默认镜像名为 `whisper-subtitles:rocm`。已有同名可用镜像时直接复用；否则选择一种方式准备。

**方式一：从已有设备导入**

在原设备使用仓库中的脚本导出镜像：

```bash
bash "$HOME/whisper-subtitles/srtflow/tools/export-image.sh" \
  "$HOME/srtflow-migration"
```

将生成的目录传到新设备，在新设备校验并导入：

```bash
cd "$HOME/srtflow-migration"
sha256sum -c SHA256SUMS
docker load -i whisper-subtitles-rocm.tar.gz
```

此方式保留原镜像中的依赖组合。新设备仍需具备兼容的 GPU 驱动。

**方式二：从 Dockerfile 构建**

```bash
bash "$HOME/whisper-subtitles/srtflow/tools/build-image.sh"
```

构建配方位于 [deployment/Dockerfile](deployment/Dockerfile)，固定使用 faster-whisper 1.2.1、CTranslate2 ROCm wheel 4.8.5 和 PyAV 18.1.0。构建需要访问镜像仓库、GitHub 和 Python 包源。

### 4. 验证 GPU 与 VAD

```bash
bash "$HOME/whisper-subtitles/srtflow/tools/verify-gpu.sh"
```

结果应显示 GPU 数量至少为 1、支持 `float16`，以及 Silero VAD 加载成功。

### 5. 完成首次配置

```bash
srtflow
```

首次运行会自动进入配置向导。也可随时手动打开：

```bash
srtflow --config
```

## 配置

配置向导提供以下内容：

| 配置项 | 内容 |
| --- | --- |
| 翻译引擎 | 服务地址、凭据、套餐或模型等参数，配置后实际测试 |
| 默认目录 | 输入目录、输出位置、可选集中输出目录、递归扫描 |
| 语言 | 日语、英语或自动检测；目标固定简体中文 |
| 转写参数 | 模型、VAD、计算精度、解码参数、镜像名和缓存路径 |
| 高级设置 | 翻译批次、上下文、超时、重试、请求间隔、已有结果策略、执行方式及术语表 |

### 翻译引擎

| 引擎 | 需要填写 |
| --- | --- |
| DeepL | API Key、Free / Pro 套餐；可选原生术语表 ID |
| 百度 | **通用文本翻译 API** 的 APPID 和密钥 |
| DeepSeek | API 基础地址、实际可用模型名称、API Key、输出 token 上限 |
| 本地 API | 兼容接口的基础地址、模型名称、可选密钥、输出 token 上限及 JSON 模式 |

DeepL Free 自动使用 `https://api-free.deepl.com`，Pro 使用 `https://api.deepl.com`。

配置或修改引擎后，会实际翻译一句固定短文本并校验结果，可能产生少量用量。已通过测试的配置，日常任务不重复测试。密钥采用隐藏输入，配置摘要不展示密钥。

**本地 API** 需要已有的 OpenAI Chat Completions 兼容服务，例如基础地址 `http://127.0.0.1:8000/v1`。程序会请求该地址下的 `/chat/completions`，并校验返回的结构化译文。接口由宿主机调用，`127.0.0.1` 指 Ubuntu 服务器自身；项目不安装或运行本地翻译模型。

### 常用转写设置

| 设置 | 默认值 | 调整说明 |
| --- | --- | --- |
| 模型 | `large-v3` | 可改为 `turbo` 比较速度和准确率；turbo 是模型选择 |
| 计算精度 | `float16` | 其他精度需在目标 GPU 上验证 |
| VAD | 开启 | 检测语音区间，跳过未检测到语音的部分 |
| VAD 阈值 | `0.6` | `0.5` 更宽松；`0.65` 更严格，也可能漏对白 |
| 最短语音 | 250ms | 过滤过短的候选片段 |
| 静音分段间隔 | 700ms | 控制相邻语音的分段 |
| 语音边界留白 | 300ms | 保留语音边缘 |
| 最长语音区间 | 25秒 | 控制独立转写区间长度 |
| beam_size | 5 | 解码搜索宽度 |
| 模型缓存 | `~/whisper-subtitles/cache` | 首次下载，后续复用 |

VAD 不能保证排除所有叹息、呻吟等非对白人声，可先用小样本选择合适参数。

默认已有中文字幕时跳过；也可设为发布前备份后重做。已有原文字幕可选择确认后复用、备份后重新转写或跳过。输出冲突不会静默覆盖输入字幕。

## 使用

```bash
srtflow
```

保存默认配置后，日常流程为：

1. 查看配置摘要，“是否修改”默认选择否。
2. 选择视频转写并翻译、仅转写或翻译已有 SRT。
3. 选择输入：当前目录、默认目录，或逐行输入多个文件/目录。
4. 从清单选择文件；可输入 `1,3-5` 多选，`a` 全选，`q` 取消。
5. 查看输入、输出和主要参数，回车开始。
6. 查看实时进度，完成后获取字幕文件路径。

“当前目录”是启动命令时的工作目录。文件可放在任意可访问的服务器目录，支持空格、中文和日文路径，无需复制到 input 文件夹。递归扫描使用已保存设置，自动排除隐藏目录、隐藏文件、`._` 文件及临时文件，并对输入去重。

默认输出到**视频相同目录**；翻译已有 SRT 时输出到原文 SRT 相同目录。也可在配置中指定默认输出目录。

### 输出命名

以 `Movie.2024.mkv` 为例：

| 内容 | 文件名 |
| --- | --- |
| 日语原文字幕 | `Movie.2024.jp.srt` |
| 英语原文字幕 | `Movie.2024.en.srt` |
| 简体中文字幕 | `Movie.2024.srt` |

翻译 `Movie.2024.jp.srt` 或 `Movie.2024.en.srt` 时，中文输出同样为 `Movie.2024.srt`。只移除视频最后一个扩展名；日语文件后缀使用 `jp`，内部识别代码使用 `ja`。

### 后台与任务恢复

默认通过 tmux 在后台执行，同时自动显示进度。SSH 断开后任务继续；进度界面的 **Ctrl+C 只退出查看**。服务器重启后需手动恢复任务。

| 命令 | 功能 |
| --- | --- |
| `srtflow` | 开始新任务，或从菜单选择继续、重试 |
| `srtflow --config` | 修改默认配置 |
| `srtflow --status 任务ID` | 查看任务摘要 |
| `srtflow --watch 任务ID` | 查看实时进度 |
| `srtflow --resume` | 选择并继续未完成任务 |
| `srtflow --retry` | 选择任务，仅重试失败部分 |

任务按转写区间和翻译批次保存进度。恢复时跳过已完成识别和已保存译文，音频准备及输入校验仍可能再次执行。翻译失败不会要求重做已经完成的转写。

API 临时错误会按设置重试；仍需处理时保存进度，并提供重新配置、实测和继续的入口。SSH 已断开时，重新登录后使用 `--resume`。更换翻译引擎或模型需要明确确认，并记录实际配置及译文来源。

转写进度按已处理的 VAD 保留语音时间计算，翻译按已校验并保存的字幕条数计算。解码、VAD 扫描和模型加载没有可靠百分比时，仅显示阶段、活动指示和计时。

## 配置与数据位置

| 默认位置 | 内容 |
| --- | --- |
| `~/whisper-subtitles/srtflow/` | 程序与宿主机 `.venv` |
| `~/.local/bin/srtflow` | 命令入口 |
| `~/.config/srtflow/config.json` | 默认配置 |
| `~/.config/srtflow/secrets.json` | API 凭据，权限600 |
| `~/whisper-subtitles/cache/` | 模型缓存 |
| `~/.local/state/srtflow/tasks/任务ID/` | 状态、检查点、`worker.log` 和 `events.jsonl` |

设置了 `XDG_CONFIG_HOME` 或 `XDG_STATE_HOME` 时，以相应环境变量为准。模型缓存路径可通过配置修改。

更新默认配置仅影响后续新任务；历史任务使用保存的配置快照。恢复任务时会检查输入文件、原文字幕和镜像标识，不应手工修改任务快照绕过校验。

## 更新

等待当前任务完成后，在项目目录执行：

```bash
cd "$HOME/whisper-subtitles/srtflow"
git pull --ff-only
bash install.sh
```

安装器沿用现有虚拟环境。配置、凭据、模型缓存和任务记录保存在独立位置，不会被普通代码更新替换；转写镜像也不会自动重建。
