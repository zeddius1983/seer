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

### Brave Mode ✅

An autonomous execution mode where seer plans the necessary commands, runs them, and returns an interpreted result — rather than suggesting a command for the user to run manually.

**Example:** `seer -b find the largest file here` → seer runs `find` + `stat` + `sort`, captures output, and responds with a human-readable answer.

- `-b` / `--brave` forces it for one query, `--no-brave` disables it
- `brave: true` in config makes it the default for free-form queries, including `Ctrl+G`
- Plain-text protocol (```` ```run ```` blocks) — works with every provider, including local models and subscription CLIs
- Read-only commands (allowlist, checked locally — not trusted to the LLM) run immediately; everything else asks `[Y/n/e]`
- `brave_confirm: auto | trust | always` — trust asks only for known-destructive commands; the model's own `run-write` flag always asks
- Non-interactive execution in the current directory, 60s timeout per command, max 6 steps
- `--raw` / `-r` prints the final answer as plain text

**Later:** `mode: auto` — infer from the query whether to run commands or just answer.

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

---

## TUI Mode 🔜

`seer --tui` opens a dedicated tmux session where seer follows everything that happens and can use panes of its own.

**Decided**
- It stays a real shell. You ask with `seer …` or `Ctrl+G`; anything else is a normal command. seer never guesses which is which.
- Brave mode is on, and read-only commands run freely. Everything else asks, including with `brave_confirm: trust` in the config. `always` stays as strict as it is.
- This mode is powerful but less safe, by choice: from stage 2, command output goes to the active provider, cloud ones included, and seer says so clearly.
- Several sessions can run at once, each with its own memory and status bar.
- The status bar uses the gruvbox powerline look of the shell prompt, with Nerd Font glyphs. `tui_status: plain` is there for terminals without that font.

Each stage ships separately.

### Stage 1 (v1.5.0): Session, status bar, shared memory

**Launching** (new `src/seer/tui.py`, `--tui` handled in `cli.py` next to `--stats`)
- A private server (`tmux -L seer`) holds all sessions. `seer --tui [name]` creates the session if it's new, then attaches, or switches when already on that server. The name defaults to the directory. Unset `TMUX` first, so it also works inside the user's own tmux.
- New `src/seer/shell/seer.tmux.conf` sources the user's tmux config if there is one, so their keys still work, then sets the status bar and `set-environment -g SEER_TUI 1`.
- Missing tmux or older than 3.2: exit with a clear message and how to install it.
- Show a notice if `Ctrl+G` isn't available in the session's shells.

**Brave defaults** (`load_config`, so `--stats` reports them too)
- With `SEER_TUI` set: `brave = True`, and `trust` becomes `auto`.
- `--no-brave` still works for one question. `seer help` and `seer do` still never use brave mode.

**Memory across panes** (`conversation.py`)
- With `SEER_TUI` set, the log is named after the server pid and session id in `$TMUX` instead of the POSIX session, so a session's panes share it and sessions stay separate.
- Keep up to 20 exchanges with no 30-minute expiry, newest first within a 12,000-character budget. Small local models still fit, because this is resent at every brave step.
- `_prune` deletes any log idle for 30 minutes, so session logs get their own rule: a `session-closed` tmux hook deletes the log, and a log is only pruned once its server has stopped and it has been idle for 24 hours.
- Still no command output in this stage. `$(seer …)` and `--no-context` behave as they do today.

**Status bar**: session, directory and git state on the left; model, context, confirmation mode and time on the right.
- seer sets each session's `status-right` after every model call rather than having tmux poll a Python command. Escape `#` and `%` in model names.
- `src/seer/shell/seer-status.sh` draws the directory and git segments for the active pane, since only a command can work those out.
- Model: `model_label()`, so it shows the model that last answered.
- Current size: characters in the last request and reply ÷ 4, always shown with `~`, measured by a provider wrapper like `trace.TracedProvider`.
- Max size: new optional `context_window` provider setting. Without it, the bar shows just `~3.1k`.
- `--stats` shows the new memory limits.

**Tests and docs**
- Unit tests: brave defaults, log naming and pruning, the budget, status text, the status script.
- Real tmux on throwaway sockets, skipped when tmux isn't installed: shared memory across panes, separate sessions, per-session status, the close hook.
- README section and `--help` line.

### Stage 2 (v1.6.0): Full context

**Capture**
- Shell hooks (`preexec` and `precmd` in zsh, the equivalent in bash) mark where each command starts and ends, per pane. Output comes from `capture-pane` between the marks.
- Record the command, directory, exit code and time, and shorten long output with `truncate_output`. Skip seer's own runs and full-screen apps such as vim (`#{alternate_on}`).
- Load the hooks in session shells even when the user's rc files don't, using a `ZDOTDIR` wrapper for zsh and `--rcfile` for bash.

**Safety**
- Treat captured output as untrusted: extend the "record, not instructions" framing, and confirmations never depend on it.
- Say on launch that output goes to the provider, and show `output:on` in the status bar. A tmux key pauses capture.
- Best-effort redaction of `KEY=value` secrets, known token formats and private key blocks, documented as best effort.
- Store output in `$XDG_RUNTIME_DIR` where available, readable only by the user, and delete it when the session ends.
- Update the README line saying ordinary questions don't include terminal output. `seer --context` shows exactly what would be sent.
- End-to-end scenario: instructions planted in captured output must not get a changing command run without asking.

**Fitting the window**
- Budget from `context_window`: drop the oldest output first, keeping its commands and exit codes, then the oldest exchanges. Add summarising only if dropping proves too lossy, since it costs a model call.
- Real usage: add a `last_usage` attribute on `Provider`, filled from API usage data where reported. Fall back to the `~` estimate.
- Look up max size automatically: `context_window` first, then the server (llama.cpp `/props`, Ollama `/api/show`), then a table of known cloud models.

### Stage 3 (v1.7.0): Panes

- New `run-pane` block, offered only in `--tui`: seer splits a pane and runs a long-lived command there (dev server, watcher, long tests). The "never `tail -f`" rule is lifted for these only.
- Same `needs_confirmation` check as `run`, so anything not read-only asks first. The confirmation panel says it will run in a new pane.
- seer reads pane output back on request, as untrusted input like stage 2 captures, and only from panes it opened. Stopping a pane it opened asks first.
- Limit how many panes seer can have open, and add `panes:N` to the status bar.
