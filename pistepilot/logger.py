from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from rich.logging import RichHandler


def get_runtime_root() -> Path:
    return Path.cwd()


def cleanup_old_logs(log_dir: Path, *, keep: int = 20) -> None:
    log_files = sorted(log_dir.glob("pistepilot_*.log"))
    if len(log_files) <= keep:
        return
    for stale_file in log_files[:-keep]:
        try:
            stale_file.unlink()
        except OSError:
            pass


def setup_logging(*, verbose: bool = False) -> tuple[logging.Logger, Path, Path]:
    root = get_runtime_root()
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"pistepilot_{timestamp}.log"

    logger = logging.getLogger("pistepilot")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if logger.handlers:
        logger.handlers.clear()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )

    rich_handler = RichHandler(
        markup=True,
        rich_tracebacks=True,
        show_level=True,
        show_path=False,
    )
    rich_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    rich_handler.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(rich_handler)
    logger.addHandler(file_handler)
    cleanup_old_logs(log_dir)
    logger.info("Logs written to %s", log_file)
    return logger, log_dir, log_file


def latest_log_file(log_dir: Path) -> Path | None:
    log_files = sorted(log_dir.glob("pistepilot_*.log"))
    if not log_files:
        return None
    return log_files[-1]


def set_console_verbose(logger: logging.Logger, verbose: bool) -> None:
    for handler in logger.handlers:
        if isinstance(handler, RichHandler):
            handler.setLevel(logging.DEBUG if verbose else logging.INFO)
