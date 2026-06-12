# SEER - Shell Enhanced Execution & Reasoning

![Built with AI assistance](https://img.shields.io/badge/Built%20with-AI%20assistance-blueviolet?logo=openai&logoColor=white)

LLM-powered assistant for your terminal. Get contextual help from your local or cloud LLM based on what just happened in your shell — errors, failed commands, unexpected output.

Inspired by [PEEL](https://github.com/lemonade-sdk/peel) for PowerShell.

**Supported platforms:** macOS, Linux  
**Supported shells:** zsh, bash

```
$ git pull-request
git: 'pull-request' is not a git command. See 'git --help'.

$ seer help
The command `git pull-request` is not valid. Use `git pull` to fetch and
merge, or install the `gh` CLI and run `gh pr create` to open a pull request.
```

---

## How it works

Shell hooks (`PROMPT_COMMAND` / `precmd`) save your terminal's scrollback after every command. When you run `seer help`, the saved context is sent to your LLM of choice. No copy-pasting, no switching windows.

With tmux, the full screen output (including stderr) is captured. Without tmux, the last command and exit code are saved as fallback.

---

## Installation

### Method 1: Installer (Recommended)

Run directly from GitHub — installs `seer`, sets up shell integration and implicit mode:

```bash
curl -fsSL https://raw.githubusercontent.com/zeddius1983/seer/main/install.sh | bash
```

`uv` is installed automatically if not already present. To install a specific version or branch:

```bash
# By version tag
curl -fsSL https://raw.githubusercontent.com/zeddius1983/seer/main/install.sh | bash -s -- --version v1.1.0

# By branch
curl -fsSL https://raw.githubusercontent.com/zeddius1983/seer/main/install.sh | bash -s -- --version feature/v1.1.0
```

`--version` accepts any git ref (tag, branch, or SHA). It can be combined with other flags:

```bash
curl -fsSL https://raw.githubusercontent.com/zeddius1983/seer/main/install.sh | bash -s -- --version v1.1.0 --all
```

> **Note:** Whenever `~/.zshrc` is modified, a backup is created first as `~/.zshrc.YYYYMMDD_HHMMSS.bak`.

### Method 2: Build from source

```bash
git clone https://github.com/zeddius1983/seer
cd seer
uv tool install .
```

---

## Uninstallation

If you installed via the **installer** (Method 1):
```bash
curl -fsSL https://raw.githubusercontent.com/zeddius1983/seer/main/install.sh | bash -s -- --uninstall
```

---

## Usage

### Implicit Mode (Ctrl+Space)

Implicit mode is installed automatically with seer. Type any question directly into your terminal and press `Ctrl+Space`:

```bash
find the largest file in this folder[Press Ctrl+Space]
# Instantly expands and runs: seer find the largest file in this folder
```

### Standard Commands

```bash
# Explain what just went wrong
seer help

# Ask anything shell-related
seer how to list all datasets in a ZFS pool
seer what does SIGKILL mean
seer how do I find which process is using port 8080

# Let seer do it — generates a command, shows it, asks before running
seer do find the largest file in ~/Downloads
seer do show disk usage by folder in /var
seer do list all listening ports

# Tip: quote the task if it contains apostrophes or special characters
seer do "show all files modified in the last week"
seer do "find the largest file and show it's size in MB"

# Pipe output directly (no shell hook needed)
kubectl get pods 2>&1 | seer
journalctl -xe | seer why is nginx failing

# Skip context, ask a clean question
seer --no-context explain the difference between hard and soft links

# Raw output — no glow or rich rendering
seer --raw how do I list open ports
seer -r help

# Use a specific provider or model for one query
seer -p anthropic help
seer -p openai -m gpt-4o how do I list listening ports
```

### Subcommands

| Command | Description |
|---|---|
| `seer help` | Analyse your last terminal output and explain errors |
| `seer do <task>` | Generate a shell command, preview it, confirm before running |
| `seer config` | Show the active config file, or create a default one |
| `seer --context` | Show the full system prompt and captured terminal context |
| `seer --stats` | Show provider, model, context size, and system info |

### Flags

| Flag | Short | Description |
|---|---|---|
| `--no-context` | | Skip attaching terminal context |
| `--raw` | `-r` | Disable glow and rich rendering, stream plain text |
| `--provider <name>` | `-p` | Override the active provider for this query |
| `--model <name>` | `-m` | Override the model for this query |

---

## Configuration

Generate the default config file:
```bash
seer config
```

This creates `~/.config/seer/config.yaml`:

```yaml
provider: lmstudio        # active provider

providers:
  lmstudio:               # LM Studio (or any OpenAI-compatible local server)
    type: openai
    base_url: http://localhost:1234/v1
    api_key: lmstudio
    model: google/gemma-3-4b

  ollama:
    type: openai
    base_url: http://localhost:11434/v1
    api_key: ollama
    model: llama3.2

  openai:
    type: openai
    model: gpt-4o
    api_key: sk-...       # or set OPENAI_API_KEY env var

  anthropic:
    type: anthropic
    model: claude-sonnet-4-6
    api_key: sk-ant-...   # or set ANTHROPIC_API_KEY env var

  # Any OpenAI-compatible endpoint (vLLM, llama.cpp, etc.)
  custom:
    type: openai
    base_url: http://myserver:8080/v1
    api_key: none
    model: my-model
```

---

## Supported providers

| Provider | `type` | Notes |
|---|---|---|
| [LM Studio](https://lmstudio.ai) | `openai` | Default. Set `base_url: http://localhost:1234/v1` |
| [Ollama](https://ollama.com) | `openai` | Set `base_url: http://localhost:11434/v1` |
| [OpenAI](https://platform.openai.com) | `openai` | Set `OPENAI_API_KEY` |
| [Anthropic](https://anthropic.com) | `anthropic` | Set `ANTHROPIC_API_KEY` |
| llama.cpp / vLLM / any OpenAI-compatible | `openai` | Set `base_url` to your server |

---

## Development

```bash
git clone https://github.com/zeddius1983/seer
cd seer

# Install dev environment
uv sync

# Run directly without installing
.venv/bin/seer --help

# Build a wheel
uv build
# → dist/seer-1.0.0-py3-none-any.whl
```


