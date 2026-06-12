import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from .config import load_config, save_default_config, CONFIG_PATH, DO_SYSTEM_PROMPT
from .config import CONTEXT_FILE
from .system_info import format_for_prompt, get_system_info
from .context import get_context, read_stdin_batches, _stdin_has_data, _read_stdin_until_idle
from .providers import get_provider
console = Console()
err_console = Console(stderr=True)


def _run_shell_command(command: str) -> None:
    """Execute a shell command using the user's preferred shell."""
    shell = os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh") or "sh"
    subprocess.run([shell, "-c", command])

def _find_bash4() -> str:
    """Return path to bash 4+ (supports read -e -i), or empty string."""
    candidates = ["/opt/homebrew/bin/bash", "/usr/local/bin/bash", "bash"]
    for candidate in candidates:
        path = shutil.which(candidate)
        if not path:
            continue
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=2).stdout
            m = re.search(r"version (\d+)\.", out)
            if m and int(m.group(1)) >= 4:
                return path
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    return ""


def _edit_inline(command: str) -> str:
    """Open command for inline editing with readline pre-fill."""
    import platform, shlex, tempfile

    # bash read -e -i pre-fills the readline buffer reliably.
    # Linux always has bash 4+; macOS ships bash 3.2 (no -i support) so we
    # look for a Homebrew bash 4+ first.
    if platform.system() == "Linux":
        bash = "bash"
    elif platform.system() == "Darwin":
        bash = _find_bash4()
    else:
        bash = ""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        tmpfile = f.name
    try:
        if bash:
            script = f'read -e -i {shlex.quote(command)} -p "$ " _cmd; printf "%s" "$_cmd" > {shlex.quote(tmpfile)}'
            subprocess.run([bash, "-c", script])
        elif platform.system() == "Darwin":
            # macOS without bash 4+: use zsh vared (always available, uses ZLE)
            script = f'_v={shlex.quote(command)}; vared -p "$ " _v; printf "%s" "$_v" > {shlex.quote(tmpfile)}'
            subprocess.run(["/bin/zsh", "--no-rcs", "-i", "-c", script])
        else:
            os.unlink(tmpfile)
            tmpfile = None
            try:
                import readline
                def _hook():
                    readline.insert_text(command)
                    readline.redisplay()
                readline.set_pre_input_hook(_hook)
                try:
                    return input("$ ").strip() or command
                finally:
                    readline.set_pre_input_hook(None)
            except (ImportError, OSError):
                edited = click.edit(command)
                return edited.strip() if edited else command

        if tmpfile:
            result = Path(tmpfile).read_text().strip()
            return result if result else command
        return command
    finally:
        if tmpfile and os.path.exists(tmpfile):
            os.unlink(tmpfile)


def _unwrap_markdown_fence(text: str) -> str:
    """Strip outer ```markdown``` wrapper some models add around their entire response."""
    stripped = text.strip()
    match = re.match(r'^```(?:markdown)?\n(.*?)```\s*$', stripped, re.DOTALL)
    return match.group(1) if match else text


def _get_padded_renderable(renderable):
    """Pads a renderable left and right by 4 spaces and limits the maximum width to 90 characters."""
    term_width = console.width if console.width is not None else 80
    # pad_edge=True with (0,4,0,4) adds 8 chars around the column; subtract that from the cap
    render_width = min(82, term_width - 8) if term_width > 20 else term_width
    t = Table.grid(padding=(0, 4, 0, 4), pad_edge=True)
    t.add_column(width=render_width)
    t.add_row(renderable)
    return t


def build_prompt(question: str, context: Optional[str]) -> str:
    if context:
        return (
            f"Terminal context (recent session output):\n"
            f"```\n{context}\n```\n\n"
            f"{question}"
        )
    return question


def stream_response(system: str, prompt: str, cfg, raw: bool = False) -> None:
    provider = get_provider(cfg.get_active_provider())
    buffer = ""

    if raw:
        try:
            for chunk in provider.stream(system, prompt):
                print(chunk, end="", flush=True)
        except KeyboardInterrupt:
            pass
        print()
    else:
        # Buffered rich markdown rendering with dynamic spinner
        try:
            console.print()
            with Live(
                Text("  thinking…", style="dim"),
                console=console,
                refresh_per_second=12,
                transient=True,
            ) as live:
                for chunk in provider.stream(system, prompt):
                    buffer += chunk
                    word_count = len(buffer.split())
                    live.update(Text(f"  thinking… ({word_count} words)", style="dim"))
        except KeyboardInterrupt:
            pass

        if buffer:
            console.print(_get_padded_renderable(Markdown(buffer)))
        else:
            err_console.print("[yellow]No response received from provider.[/yellow]")


@click.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "allow_interspersed_args": False,  # flags must come before the query
    }
)
@click.argument("query", nargs=-1)
@click.option("--no-context", is_flag=True, help="Do not attach terminal context.")
@click.option("--raw", "-r", is_flag=True, help="Stream raw text, disabling glow and rich rendering.")
@click.option("--provider", "-p", default=None, help="Override the active provider.")
@click.option("--model", "-m", default=None, help="Override the model.")
@click.option("--stats", is_flag=True, help="Show provider, model, context size, and system info.")
@click.option("--context", "show_context", is_flag=True, help="Show the full system prompt and captured terminal context.")
@click.option(
    "--shell-path",
    type=click.Choice(["bash", "zsh"]),
    default=None,
    help="Print path to shell integration script (for sourcing).",
)
@click.pass_context
def main(ctx, query, no_context, raw, provider, model, stats, show_context, shell_path):
    """seer — Shell Enhanced Execution & Reasoning.

    \b
    Examples:
      seer help                    # explain the last error in your terminal
      seer how do I list open ports
      git pull-request 2>&1 | seer # pipe any output as context
      seer config                  # show/init config file
      seer --stats                 # show provider, model, and system info
    """
    # --shell-path: print path to the integration script
    if shell_path:
        script = Path(__file__).parent / "shell" / f"seer.{shell_path}"
        click.echo(str(script.resolve()))
        return

    if stats:
        _cmd_stats(provider, model)
        return

    if show_context:
        _cmd_context(provider, model)
        return

    args = list(query)

    if args and args[0].startswith("--"):
        err_console.print(f"[red]Unknown option:[/red] {args[0]}")
        err_console.print("Run [bold]seer --help[/bold] to see available options.")
        sys.exit(1)

    # config subcommand
    if args and args[0] == "config":
        _cmd_config()
        return

    try:
        cfg = load_config()
    except Exception as e:
        err_console.print(f"[red]Config error:[/red] {e}")
        err_console.print(f"Run [bold]seer config[/bold] to generate a default config.")
        sys.exit(1)

    # Apply CLI overrides
    if provider:
        cfg.provider = provider
    if model:
        if cfg.provider in cfg.providers:
            cfg.providers[cfg.provider]["model"] = model

    # Special sub-command: do
    if args and args[0] == "do":
        task = " ".join(args[1:])
        if not task:
            err_console.print("[red]Usage:[/red] seer do <task description>")
            sys.exit(1)
        try:
            system = DO_SYSTEM_PROMPT + "\n\n" + format_for_prompt()
            llm = get_provider(cfg.get_active_provider())
            buffer = ""
            console.print()
            with Live(
                Text("  thinking…", style="dim"),
                console=console,
                refresh_per_second=12,
                transient=True,
            ) as live:
                for chunk in llm.stream(system, task):
                    buffer += chunk
                    live.update(Text(f"  thinking… ({len(buffer.split())} words)", style="dim"))

            # Extract bash command and explanation from response
            cleaned = textwrap.dedent(_unwrap_markdown_fence(buffer))
            match = re.search(r'```bash\n(.*?)```', cleaned, re.DOTALL)
            if not match:
                console.print(_get_padded_renderable(Markdown(cleaned)))
                return
            command = match.group(1).strip()
            explanation = cleaned[:match.start()].strip()

            if explanation:
                console.print(_get_padded_renderable(Markdown(explanation)))
            console.print(_get_padded_renderable(Panel(Syntax(command, "bash", theme="ansi_dark"), border_style="cyan")))

            while True:
                choice = click.prompt("Run this command? [Y/n/e]", default="y").strip().lower()
                if choice in ("y", ""):
                    console.print()
                    _run_shell_command(command)
                    break
                elif choice == "n":
                    break
                elif choice == "e":
                    command = _edit_inline(command)
                    console.print(_get_padded_renderable(Panel(Syntax(command, "bash", theme="ansi_dark"), border_style="cyan")))
                else:
                    console.print("[dim]Enter y, n, or e[/dim]")
        except Exception as e:
            err_console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)
        return

    # Determine mode: help (analyse context for errors) vs question (answer directly)
    is_help = args in ([], ["help"])

    if is_help:
        question = "What went wrong in my terminal session above? How do I fix it?"
    else:
        question = " ".join(args)

    # Detect streaming stdin (tail -f etc.) before consuming it
    if not no_context and not sys.stdin.isatty() and _stdin_has_data():
        first_batch, got_eof = _read_stdin_until_idle(idle_timeout=1.0)
        if not got_eof:
            # Pipe is still open — enter watch mode
            focus = " ".join(args) if args and args not in (["help"], []) else None
            _cmd_watch(first_batch, focus, cfg, raw)
            return
        # Regular pipe — use what we already read as context
        context = first_batch.strip() or None
    # Gather context — always for help mode, skip for plain questions unless piped
    elif no_context:
        context = None
    elif is_help:
        context = get_context(cfg.context_lines)
    else:
        context = None

    if context is None and not no_context and is_help:
        err_console.print(
            "[yellow]No terminal context found.[/yellow] "
            "Source the seer shell integration or run inside tmux.\n"
            "Tip: pipe output directly with [bold]cmd 2>&1 | seer[/bold]"
        )

    prompt = build_prompt(question, context)

    try:
        system = cfg.system_prompt + "\n\n" + format_for_prompt()
        stream_response(system, prompt, cfg, raw=raw)
    except Exception as e:
        err_console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


WATCH_SYSTEM_PROMPT = (
    "You are monitoring a live stream of log lines. "
    "For each batch, report anything notable: errors, warnings, unusual patterns, "
    "repeated failures, or signs of misconfiguration. Be concise. "
    "If the batch looks normal, say so in one line."
)

WATCH_INTERVAL = 15.0


def _cmd_watch(first_batch: str, focus: Optional[str], cfg, raw: bool) -> None:
    """Watch mode: process stdin in batches, sending each to the LLM."""
    system = WATCH_SYSTEM_PROMPT + "\n\n" + format_for_prompt()
    if focus:
        system += f"\n\nUser focus: {focus}"

    err_console.print("[dim]Watch mode — press Ctrl+C to stop.[/dim]\n")

    def process_batch(batch: str, n: int) -> None:
        err_console.print(f"[dim]─── batch {n} ({'%.0f' % WATCH_INTERVAL}s window) ───[/dim]")
        prompt = f"Log lines:\n```\n{batch.strip()}\n```"
        try:
            stream_response(system, prompt, cfg, raw=raw)
        except Exception as e:
            err_console.print(f"[red]Error:[/red] {e}")

    try:
        batch_num = 1
        if first_batch.strip():
            process_batch(first_batch, batch_num)
            batch_num += 1
        for batch in read_stdin_batches(WATCH_INTERVAL):
            process_batch(batch, batch_num)
            batch_num += 1
    except KeyboardInterrupt:
        err_console.print("\n[dim]Watch mode ended.[/dim]")


def _cmd_config():
    if CONFIG_PATH.exists():
        console.print(f"[bold]Config file:[/bold] {CONFIG_PATH}\n")
        console.print(CONFIG_PATH.read_text())
    else:
        save_default_config()
        console.print(f"[green]Created default config:[/green] {CONFIG_PATH}")
        console.print("\nEdit it to add your API keys and preferred provider.")


def _cmd_context(provider_override, model_override):
    """Print the full system prompt + terminal context that would be sent to the LLM."""
    try:
        cfg = load_config()
    except Exception as e:
        err_console.print(f"[red]Config error:[/red] {e}")
        sys.exit(1)
    if provider_override:
        cfg.provider = provider_override
    if model_override and cfg.provider in cfg.providers:
        cfg.providers[cfg.provider]["model"] = model_override

    system = cfg.system_prompt + "\n\n" + format_for_prompt()
    context = get_context(cfg.context_lines)

    console.print(Panel(Markdown(system), title="[bold cyan]System Prompt[/bold cyan]", border_style="cyan"))
    console.print()
    if context:
        console.print(Panel(
            Syntax(context, "text", theme="ansi_dark", word_wrap=True),
            title="[bold yellow]Terminal Context[/bold yellow]",
            border_style="yellow",
        ))
        est_tokens = len((system + context).split()) * 4 // 3
        console.print(f"\n[dim]~{len(context.splitlines())} lines · ~{est_tokens} tokens estimated[/dim]")
    else:
        console.print(Panel("[dim]No context captured yet.[/dim]",
                            title="[bold yellow]Terminal Context[/bold yellow]",
                            border_style="yellow"))


def _cmd_stats(provider_override, model_override):
    """Print provider, model, context, and system info stats."""
    try:
        cfg = load_config()
    except Exception as e:
        err_console.print(f"[red]Config error:[/red] {e}")
        sys.exit(1)
    if provider_override:
        cfg.provider = provider_override
    if model_override and cfg.provider in cfg.providers:
        cfg.providers[cfg.provider]["model"] = model_override

    try:
        pcfg = cfg.get_active_provider()
    except ValueError as e:
        err_console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    sys_info = get_system_info()
    context = get_context(cfg.context_lines)
    context_lines_actual = len(context.splitlines()) if context else 0
    context_chars = len(context) if context else 0
    est_tokens = context_chars * 4 // 15  # rough estimate

    system_prompt = cfg.system_prompt + "\n\n" + format_for_prompt()
    system_tokens = len(system_prompt.split()) * 4 // 3

    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="bold cyan", no_wrap=True)
    t.add_column()

    provider_display = (
        f"auto → {pcfg.name}" if cfg.provider == "auto" else cfg.provider
    )
    model_display = (
        f"auto → {pcfg.model}" if pcfg.model_was_auto else pcfg.model
    )
    t.add_row("Provider", provider_display)
    t.add_row("Type", pcfg.type)
    t.add_row("Model", model_display)
    t.add_row("Base URL", pcfg.base_url or "[dim]default[/dim]")
    t.add_row("", "")
    t.add_row("Context limit", f"{cfg.context_lines} lines (max)")
    t.add_row("Context captured", f"{context_lines_actual} lines · {context_chars} chars · ~{est_tokens} tokens")
    t.add_row("System prompt", f"~{system_tokens} tokens")
    t.add_row("Context file", str(CONTEXT_FILE))
    t.add_row("Config file", str(CONFIG_PATH))
    t.add_row("", "")
    t.add_row("OS", sys_info["os"])
    t.add_row("Architecture", sys_info["arch"])
    t.add_row("Shell", sys_info["shell"])
    t.add_row("Memory", sys_info["memory"])
    t.add_row("Package manager", sys_info["package_manager"] or "[dim]none detected[/dim]")

    console.print(Panel(t, title="[bold green]seer stats[/bold green]", border_style="green"))
