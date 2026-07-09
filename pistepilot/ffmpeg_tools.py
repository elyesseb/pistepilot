from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from pistepilot.models import ToolInfo, Toolset


TOOL_NAMES = ("mkvmerge", "mkvpropedit", "ffprobe", "ffmpeg")
_HIDE_SUBPROCESS_WINDOWS = False


class MissingToolError(RuntimeError):
    """Raised when a required external tool cannot be found."""


class ExternalCommandError(RuntimeError):
    def __init__(
        self,
        command: list[str],
        *,
        returncode: int | None,
        stdout: str = "",
        stderr: str = "",
        message: str = "External command failed.",
    ) -> None:
        super().__init__(message)
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def format_details(self) -> str:
        return (
            f"Command: {format_command(self.command)}\n"
            f"Exit code: {self.returncode}\n"
            f"Stdout:\n{self.stdout or '-'}\n"
            f"Stderr:\n{self.stderr or '-'}"
        )


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def get_executable_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def get_runtime_root() -> Path:
    return get_executable_dir()


def get_local_bin_dir(create: bool = True) -> Path:
    if is_frozen():
        bin_dir = get_executable_dir() / "bin"
    else:
        bin_dir = Path(__file__).resolve().parents[1] / "bin"

    if create:
        bin_dir.mkdir(parents=True, exist_ok=True)
    return bin_dir


def _candidate_locations(name: str) -> list[tuple[Path, str]]:
    suffixes = [".exe", ".cmd", ".bat", ""] if sys.platform.startswith("win") else [""]
    locations: list[tuple[Path, str]] = []

    for suffix in suffixes:
        locations.append((get_local_bin_dir(create=True) / f"{name}{suffix}", "bin/ local"))

    executable_dir = get_executable_dir()
    for suffix in suffixes:
        locations.append((executable_dir / f"{name}{suffix}", "dossier executable"))

    return locations


def _resolve_tool(name: str) -> ToolInfo:
    seen: set[Path] = set()
    for candidate, source in _candidate_locations(name):
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.exists():
            return ToolInfo(name=name, path=str(resolved), found=True, source=source)

    path_value = shutil.which(name)
    if path_value:
        return ToolInfo(name=name, path=path_value, found=True, source="PATH")

    return ToolInfo(name=name, path=None, found=False, source=None)


def detect_tools() -> Toolset:
    return Toolset({name: _resolve_tool(name) for name in TOOL_NAMES})


def format_missing_tools_help(missing_names: list[str]) -> str:
    joined = ", ".join(missing_names)
    return (
        f"Missing tools: {joined}\n"
        "Install FFmpeg and MKVToolNix and add their executables to PATH,\n"
        "or copy the binaries into the local 'bin' folder next to PistePilot."
    )


def ensure_tools(toolset: Toolset, names: list[str]) -> None:
    missing = toolset.missing(names)
    if missing:
        raise MissingToolError(format_missing_tools_help(missing))


def format_command(command: list[str]) -> str:
    def quote(part: str) -> str:
        return f'"{part}"' if " " in part else part

    return " ".join(quote(part) for part in command)


def set_hide_subprocess_windows(hide: bool) -> None:
    global _HIDE_SUBPROCESS_WINDOWS
    _HIDE_SUBPROCESS_WINDOWS = hide


def should_hide_subprocess_windows() -> bool:
    return _HIDE_SUBPROCESS_WINDOWS


def _subprocess_creationflags(*, hide_console: bool | None = None) -> int:
    if sys.platform != "win32":
        return 0
    hide = _HIDE_SUBPROCESS_WINDOWS if hide_console is None else hide_console
    if not hide:
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_command(
    command: list[str],
    timeout: int = 120,
    *,
    hide_console: bool | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_subprocess_creationflags(hide_console=hide_console),
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalCommandError(
            command,
            returncode=None,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            message="The external command timed out.",
        ) from exc
    except OSError as exc:
        raise ExternalCommandError(
            command,
            returncode=None,
            stdout="",
            stderr=str(exc),
            message="Unable to start the external command.",
        ) from exc


def run_json_command(
    command: list[str],
    timeout: int = 120,
    *,
    hide_console: bool | None = None,
) -> dict[str, Any]:
    result = run_command(command, timeout=timeout, hide_console=hide_console)
    if result.returncode != 0:
        raise ExternalCommandError(
            command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            message="The external command returned an error.",
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExternalCommandError(
            command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            message="The external command returned invalid JSON.",
        ) from exc
