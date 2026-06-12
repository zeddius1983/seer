"""Collect local system information to inject into LLM prompts."""

import os
import platform
import shutil
import subprocess
from functools import lru_cache


@lru_cache(maxsize=1)
def get_system_info() -> dict:
    return {
        "os":              _os_name(),
        "arch":            platform.machine(),
        "shell":           _shell(),
        "memory":          _memory(),
        "package_manager": _package_manager(),
    }


def format_for_prompt() -> str:
    i = get_system_info()
    lines = [
        "## User system",
        f"- **OS:** {i['os']}",
        f"- **Architecture:** {i['arch']}",
        f"- **Shell:** {i['shell']}",
        f"- **Memory:** {i['memory']}",
    ]
    if i["package_manager"]:
        lines.append(f"- **Package manager:** {i['package_manager']}")
    return "\n".join(lines)


def _os_name() -> str:
    system = platform.system()
    if system == "Darwin":
        mac_ver = platform.mac_ver()[0]
        return f"macOS {mac_ver}" if mac_ver else "macOS"
    if system == "Linux":
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return "Linux"
    return system


def _shell() -> str:
    shell = os.environ.get("SHELL", "")
    if not shell:
        return "unknown"
    try:
        result = subprocess.run(
            [shell, "--version"],
            capture_output=True, text=True, timeout=2
        )
        first_line = (result.stdout or result.stderr).splitlines()[0]
        return first_line.strip()
    except Exception:
        return os.path.basename(shell)


def _memory() -> str:
    system = platform.system()
    try:
        if system == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=2
            )
            total = int(result.stdout.strip())
            return f"{total // (1024 ** 3)} GB"
        if system == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return f"{kb // (1024 ** 2)} GB"
    except Exception:
        pass
    return "unknown"


def _package_manager() -> str:
    managers = ["brew", "apt", "dnf", "pacman", "zypper", "apk"]
    found = [m for m in managers if shutil.which(m)]
    return ", ".join(found) if found else ""
