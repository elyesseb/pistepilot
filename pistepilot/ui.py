from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from pistepilot.i18n import t


DIRECTORY_DIALOG_TITLE = t("directory_dialog_title")


def choose_directory_gui() -> Path | None:
    if sys.platform != "win32":
        raise RuntimeError("The native folder picker is only available on Windows.")

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - depend de l'environnement
        raise RuntimeError("tkinter is not available.") from exc

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    try:
        selected = filedialog.askdirectory(title=DIRECTORY_DIALOG_TITLE, mustexist=True)
    finally:
        root.destroy()

    if not selected:
        return None
    return Path(selected)


def select_directory_interactive() -> Path | None:
    if sys.platform == "win32":
        try:
            return choose_directory_gui()
        except Exception:
            pass

    return prompt_directory_manually()


def prompt_directory_manually(default: Path | None = None) -> Path | None:
    raw = input(f"{t('manual_folder_prompt')} [{default or ''}]: ").strip()
    if not raw and default is not None:
        return default
    if not raw:
        return None
    return Path(raw).expanduser()


def pause_before_exit_if_interactive(*, interactive_mode: bool, gui_mode: bool = False) -> None:
    if gui_mode:
        return
    if not (interactive_mode or getattr(sys, "frozen", False)):
        return

    stdin = getattr(sys, "stdin", None)
    if stdin is None:
        return
    if getattr(stdin, "closed", False):
        return
    if hasattr(stdin, "isatty"):
        try:
            if not stdin.isatty():
                return
        except Exception:
            return

    try:
        input(t("press_enter_to_close"))
    except (EOFError, RuntimeError, OSError, ValueError):
        return


def open_path_with_system(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
        return
    subprocess.Popen(["xdg-open", str(path)])
