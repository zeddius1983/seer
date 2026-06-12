# SEER Roadmap

---

## v1.0.0 — Initial Release ✅

**Shell Integration**
- zsh and bash support via shell hooks (`precmd` / `PROMPT_COMMAND`)
- Implicit mode: type a question at the prompt and press `Ctrl+Space` to invoke seer
- Installer script with automatic `uv` setup and shell integration
- Uninstall / setup menu

**Context Capture**
- tmux pane capture (full scrollback, takes priority)
- Hook-based fallback: last command + exit code
- Shell history fallback
- Piped input support (`cmd 2>&1 | seer`)

**Error Analysis Mode (`seer help`)**
- Automatic error detection from terminal context
- System info auto-population (OS, CPU arch, shell, package managers)
- Responses tailored to the user's platform

**Command Generation Mode (`seer do`)**
- Natural language → shell command with syntax highlighting
- Safety warnings for destructive or privileged commands
- macOS vs Linux compatibility warnings
- Inline edit before execution (`e` key)
- Confirmation prompt (`[Y/n/e]`) before running

**Output Rendering**
- `glow` integration for rendered Markdown (host-side)
- `rich` Live streaming fallback
- `--raw` / `-r` flag for plain text output

**Multi-Provider Support**
- LM Studio (OpenAI-compatible, local)
- Ollama (OpenAI-compatible, local)
- OpenAI
- Anthropic
- Custom OpenAI-compatible endpoints

**Diagnostics**
- `seer info` — active provider, model, context stats, system specs
- `seer config` — print config path and contents

---

## v1.1.0 — Provider Autoconfig 🔜

Introduce `auto` as a valid value for both `provider` and `model`, removing the need for manual configuration on machines running local LLM servers.

**`provider: auto`**
- At startup, probe each configured provider in order
- Select the first provider that responds successfully
- Probe method: attempt to list models via the provider's API

**`model: auto`**
- When a provider's model is set to `auto`, query its model list endpoint
- Select the first available model returned

**Updated default config**
- Ships with llama.cpp, LM Studio, and Ollama pre-configured
- All three default to `model: auto`
- Top-level `provider` defaults to `auto`

```yaml
provider: auto
context_lines: 100
providers:
  llamacpp:
    type: openai
    base_url: http://127.0.0.1:8080/v1
    api_key: llamacpp
    model: auto
  lmstudio:
    type: openai
    base_url: http://127.0.0.1:1234/v1
    api_key: lmstudio
    model: auto
  ollama:
    type: openai
    base_url: http://localhost:11434/v1
    api_key: ollama
    model: auto
```

---

## v1.2.0 — Brave Mode & Pipe-Aware Output 🔜

### Brave Mode

An autonomous execution mode where seer plans the necessary commands, runs them, and returns an interpreted result — rather than suggesting a command for the user to run manually.

**Example:** `seer find the largest file here` → seer runs `find` + `du` + `sort`, captures output, and responds with a human-readable answer.

**Mode system**

A new top-level `mode` config key controls the default behaviour:

| Value | Behaviour |
|---|---|
| `default` | Current behaviour — suggest commands, never execute autonomously |
| `brave` | Always use brave mode — plan, execute, interpret |
| `auto` | seer infers the desired mode from the query (e.g. questions → brave, `do`-style tasks → default) |

```yaml
mode: auto   # default | brave | auto
```

**Per-invocation override**

`--brave` / `-b` flag forces brave mode for a single query regardless of config:

```
seer --brave find the largest file here
seer -b what is eating my disk space
```

**Execution model**
- LLM receives the query and generates a plan (sequence of shell commands)
- seer executes each command and feeds stdout/stderr back to the LLM
- Before any write or destructive command, seer pauses and prompts for confirmation (`[Y/n/e]`)
- Read-only commands run without interruption
- LLM produces a final human-readable summary
- `--raw` / `-r` outputs plain text (no Markdown formatting)

---

### Pipe-Aware Output

When seer's stdout is not a TTY (i.e. the output is being piped), automatically switch to plain text mode:
- Disable glow and rich rendering
- Instruct the LLM to respond with factual, unformatted output suitable for further processing
- Applies to all modes, including brave mode

```
seer what docker containers are running | grep local
seer --brave find the largest log file | xargs wc -l
```
