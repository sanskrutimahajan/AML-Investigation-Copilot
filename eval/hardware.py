"""The machine an evaluation ran on, recorded in result files next to the LLM provider and model,
so latency figures can be read in context. Only the OS, chip, core count and memory are recorded:
no host name, user name or path."""

import os
import platform
import subprocess
from typing import Any


def _sysctl(key: str) -> str | None:
    try:
        return subprocess.run(["sysctl", "-n", key], capture_output=True, text=True,
                              check=True, timeout=5).stdout.strip() or None  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None


def _memory_gb() -> float | None:
    if platform.system() == "Darwin":
        raw = _sysctl("hw.memsize")
        return round(int(raw) / 2**30, 1) if raw else None
    try:
        with open("/proc/meminfo") as meminfo:
            kb = int(next(line for line in meminfo if line.startswith("MemTotal")).split()[1])
        return round(kb / 2**20, 1)
    except (OSError, StopIteration, ValueError):
        return None


def describe() -> dict[str, Any]:
    chip = _sysctl("machdep.cpu.brand_string") if platform.system() == "Darwin" else None
    return {
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "chip": chip or platform.processor() or None,
        "cpu_cores": os.cpu_count(),
        "memory_gb": _memory_gb(),
        "python": platform.python_version(),
    }
