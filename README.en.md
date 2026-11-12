# SRTFlow

[简体中文](README.md) | **English**

Transcribe video speech and translate subtitles into Simplified Chinese from an Ubuntu terminal.

SRTFlow uses **faster-whisper + Silero VAD** for speech recognition inside an **AMD ROCm Docker container**, while translation APIs run on the host. It is designed for processing long English and Japanese videos over SSH.

The interactive CLI currently uses Chinese prompts. This document provides the English deployment and usage guide.

## Features

- **Three workflows**: transcribe and translate a video, transcribe without translation, or translate an existing SRT file.
- **Reusable defaults**: save languages, directories, transcription settings, and the translation provider; select files and confirm to start.
- **Batch processing**: accept multiple files or directories, optionally scan recursively, select files by number, and deduplicate inputs. Hidden and temporary files are excluded.
- **Multiple translation providers**: DeepL, Baidu General Text Translation, DeepSeek, and local APIs compatible with OpenAI Chat Completions.
- **Checkpoints and recovery**: save transcription intervals and validated translations, resume work, retry failed items, and handle output conflicts.
- **Background execution and live progress**: use tmux to keep tasks running after SSH disconnects and reconnect to view their status.

The current version supports English and Japanese source audio, with optional language detection for each file. The translation target is fixed to **Simplified Chinese**. Original and translated subtitles are saved separately. Translation preserves subtitle numbers and timestamps.

## Requirements

| Use case | Requirements |
| --- | --- |
| CLI and translation | Ubuntu, Python 3.11+, venv, and access to the selected API service |
| Background execution | tmux |
| Video transcription | Docker Engine, a working AMD GPU / ROCm driver environment, and a transcription image |
| Included Dockerfile | Linux x86_64; the GPU and host driver must be compatible with the container |

**Translating existing SRT files does not require a GPU, ROCm, or a transcription image.**

The host runs the interactive CLI, task scheduler, and translation adapters. Transcription dependencies run inside Docker. Install ROCm drivers and Docker Engine appropriate for your device, and ensure `/dev/kfd` and `/dev/dri` are available. The project does not install GPU drivers automatically.

## Deployment

The following steps use `~/whisper-subtitles/srtflow` as the installation directory.

### 1. Install host tools

```bash
sudo apt update
sudo apt install -y git python3-venv tmux gzip
```

Video transcription requires Docker access without sudo. After installing Docker Engine, run:

```bash
sudo usermod -aG docker "$USER"
```

If your ROCm installation provides the `video` and `render` groups, add your account to them:

```bash
sudo usermod -aG video,render "$USER"
```

Log out of SSH and reconnect for group membership changes to take effect. Then check:

```bash
docker info --format '{{.ServerVersion}}'
ls -l /dev/kfd /dev/dri/render*
```

The Docker group grants broad administrative access to the host. Run SRTFlow as your regular user, without sudo.

### 2. Clone and install

```bash
mkdir -p "$HOME/whisper-subtitles"
git clone https://github.com/markawonge/srtflow.git \
  "$HOME/whisper-subtitles/srtflow"

bash "$HOME/whisper-subtitles/srtflow/install.sh"
```

The installer creates or reuses the project's `.venv`, installs the included wheel, and creates the `~/.local/bin/srtflow` launcher. The first installation needs internet access to download Rich and its dependencies.

Start the application using its full path:

```bash
~/.local/bin/srtflow
```

Or add the command directory to PATH:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

To make this permanent, add the export line to your shell configuration, such as `~/.bashrc` or `~/.zshrc`.

### 3. Prepare the transcription image

Skip this section and the next if you only need to translate existing SRT files.

The default image name is `whisper-subtitles:rocm`. Reuse an existing working image with that name, or choose one of the following methods.

**Option A: import an image from another device**

On the original device, export the image using the repository's helper script:

```bash
bash "$HOME/whisper-subtitles/srtflow/tools/export-image.sh" \
  "$HOME/srtflow-migration"
```

Transfer the generated directory to the new device, then verify and import the image:

```bash
cd "$HOME/srtflow-migration"
sha256sum -c SHA256SUMS
docker load -i whisper-subtitles-rocm.tar.gz
```

This preserves the dependencies in the original image. The new device still needs a compatible GPU driver.

**Option B: build from the Dockerfile**

```bash
bash "$HOME/whisper-subtitles/srtflow/tools/build-image.sh"
```

The build recipe in [deployment/Dockerfile](deployment/Dockerfile) pins faster-whisper 1.2.1, the CTranslate2 ROCm wheel 4.8.5, and PyAV 18.1.0. Building requires access to the container registry, GitHub, and Python package repositories.

### 4. Verify GPU access and VAD

```bash
bash "$HOME/whisper-subtitles/srtflow/tools/verify-gpu.sh"
```

The output should report at least one GPU, support for `float16`, and successful loading of Silero VAD.

### 5. Complete initial configuration

```bash
srtflow
```

The first run opens the configuration wizard automatically. You can also open it at any time:

```bash
srtflow --config
```

## Configuration

The wizard provides the following settings:

| Setting | Contents |
| --- | --- |
| Translation provider | Service address, credentials, plan or model settings, followed by a real translation test |
| Default directories | Input directory, output location, optional shared output directory, and recursive scanning |
| Languages | Japanese, English, or automatic detection; the target is fixed to Simplified Chinese |
| Transcription | Model, VAD, compute type, decoding settings, image name, and cache directory |
| Advanced settings | Translation batch limits, context, timeout, retries, request interval, existing output policy, execution mode, and glossary |

### Translation providers

| Provider | Required settings |
| --- | --- |
| DeepL | API key and Free / Pro plan; optional native glossary ID |
| Baidu | APPID and secret key for the **General Text Translation API** |
| DeepSeek | API base URL, an available model name, API key, and output token limit |
| Local API | Compatible API base URL, model name, optional key, output token limit, and JSON mode |

DeepL Free uses `https://api-free.deepl.com`; Pro uses `https://api.deepl.com`.

After configuring or modifying a provider, SRTFlow translates a short fixed sample and validates the result. This may incur a small amount of usage. Providers that have passed the test are not retested for every task. Keys are entered with hidden input and are not shown in configuration summaries.

A **local API** requires an existing service compatible with OpenAI Chat Completions, for example at `http://127.0.0.1:8000/v1`. SRTFlow sends requests to `/chat/completions` under that base URL and validates the structured translations it receives. Requests run on the host, so `127.0.0.1` refers to the Ubuntu server itself. The project does not install or run a local translation model.

### Common transcription settings

| Setting | Default | Notes |
| --- | --- | --- |
| Model | `large-v3` | Select `turbo` to compare speed and accuracy; turbo is a model choice |
| Compute type | `float16` | Validate other compute types on your GPU |
| VAD | Enabled | Detect speech intervals and skip sections where no speech is detected |
| VAD threshold | `0.6` | `0.5` is less restrictive; `0.65` is stricter and may miss dialogue |
| Minimum speech duration | 250ms | Filter very short candidate intervals |
| Silence interval for splitting | 700ms | Control separation between speech intervals |
| Speech padding | 300ms | Preserve audio around speech boundaries |
| Maximum speech interval | 25 seconds | Limit the length of each independent transcription interval |
| beam_size | 5 | Decoding search width |
| Model cache | `~/whisper-subtitles/cache` | Download on first use and reuse later |

VAD does not guarantee that all non-dialogue vocalizations, such as sighs or moans, will be excluded. Use a short sample to choose suitable settings.

Existing Chinese subtitles are skipped by default. You can instead regenerate them and back up existing output before publishing. Existing original subtitles can be reused after confirmation, backed up and regenerated, or skipped. Output conflicts never silently overwrite input subtitles.

## Usage

```bash
srtflow
```

After saving your defaults, the normal workflow is:

1. Review the configuration summary. The default answer to modifying settings is no.
2. Choose video transcription and translation, transcription only, or translation of an existing SRT.
3. Select the current directory, the default input directory, or enter multiple file/directory paths on separate lines.
4. Choose files from the list: use `1,3-5` for multiple selections, `a` for all, or `q` to cancel.
5. Review inputs, outputs, and key settings, then press Enter to start.
6. Watch progress and view the generated subtitle paths when the task finishes.

The current directory is the working directory from which you launch the command. Files can be anywhere the server can access, including paths containing spaces, Chinese, or Japanese characters. You do not need to copy files into an input folder. Recursive scanning follows the saved setting, excludes hidden directories, hidden files, `._` files, and temporary files, and deduplicates inputs.

By default, output is saved in the **same directory as the video**. When translating an existing SRT, output is saved beside that original SRT. You can configure a separate default output directory.

### Output naming

For an input named `Movie.2024.mkv`:

| Content | Filename |
| --- | --- |
| Japanese original subtitles | `Movie.2024.jp.srt` |
| English original subtitles | `Movie.2024.en.srt` |
| Simplified Chinese subtitles | `Movie.2024.srt` |

Translating `Movie.2024.jp.srt` or `Movie.2024.en.srt` also produces `Movie.2024.srt`. Only the video's final extension is removed. Japanese subtitle filenames use `jp`, while the internal recognition language code is `ja`.

### Background execution and recovery

Tasks run in the background through tmux by default, with live progress displayed automatically. They continue after SSH disconnects. **Ctrl+C in the progress viewer only exits the viewer.** After a server reboot, resume tasks manually.

| Command | Purpose |
| --- | --- |
| `srtflow` | Start a task, or choose resume/retry from the menu |
| `srtflow --config` | Edit default settings |
| `srtflow --status TASK_ID` | Show a task summary |
| `srtflow --watch TASK_ID` | Watch live progress |
| `srtflow --resume` | Select and continue an unfinished task |
| `srtflow --retry` | Select a task and retry failed items only |

Checkpoints are saved for transcription intervals and translation batches. Recovery skips completed recognition and saved translations, although audio preparation and input validation may run again. Translation failures do not require repeating completed transcription.

Temporary API failures are retried according to your settings. If further action is needed, progress is saved and you can reconfigure the provider, test it, and continue. After an SSH disconnect, reconnect and use `--resume`. Changing a translation provider or model requires explicit confirmation; configuration changes and translation provenance are recorded.

Transcription progress is based on processed speech time retained by VAD. Translation progress is based on validated and saved subtitle entries. Decoding, VAD scanning, and model loading show the stage, activity indicator, and elapsed time when no reliable percentage is available.

## Configuration and data locations

| Default location | Contents |
| --- | --- |
| `~/whisper-subtitles/srtflow/` | Application and host `.venv` |
| `~/.local/bin/srtflow` | Command launcher |
| `~/.config/srtflow/config.json` | Default configuration |
| `~/.config/srtflow/secrets.json` | API credentials, with file permissions set to 600 |
| `~/whisper-subtitles/cache/` | Model cache |
| `~/.local/state/srtflow/tasks/TASK_ID/` | Status, checkpoints, `worker.log`, and `events.jsonl` |

If `XDG_CONFIG_HOME` or `XDG_STATE_HOME` is set, the corresponding environment variable takes precedence. The model cache directory is configurable.

Changes to defaults apply only to new tasks. Existing tasks use their saved configuration snapshots. Recovery checks input files, original subtitles, and image identifiers; do not manually edit task snapshots to bypass validation.

## Updating

Wait for active tasks to finish, then run:

```bash
cd "$HOME/whisper-subtitles/srtflow"
git pull --ff-only
bash install.sh
```

The installer reuses the existing virtual environment. Configuration, credentials, model cache, and task records are stored separately and are not replaced by a normal code update. The transcription image is not rebuilt automatically.
