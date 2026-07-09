from __future__ import annotations

from pathlib import Path

SUPPORTED_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm"}


def scan_video_files(folder: Path, recursive: bool = True) -> list[Path]:
    if not folder.exists():
        raise FileNotFoundError(f"Folder not found: {folder}")
    if not folder.is_dir():
        raise NotADirectoryError(f"Invalid path, expected a folder: {folder}")

    iterator = folder.rglob("*") if recursive else folder.glob("*")
    files = [path for path in iterator if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS]
    return sorted(files)
