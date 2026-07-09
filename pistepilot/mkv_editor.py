from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from pistepilot.ffmpeg_tools import ExternalCommandError, format_command, run_command
from pistepilot.models import MediaFileReport, StreamInfo, Toolset


class MediaEditor:
    def __init__(self, toolset: Toolset, logger, log_dir: Path) -> None:
        self.toolset = toolset
        self.logger = logger
        self.log_dir = log_dir

    def write_original_state_backup(self, reports: list[MediaFileReport]) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self.log_dir / f"original_state_{timestamp}.json"
        payload = {
            "created_at": timestamp,
            "files": [report.to_dict() for report in reports if report.is_ready()],
        }
        backup_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        self.logger.info("Metadata backup created: %s", backup_path)
        return backup_path

    def write_analysis_report(self, reports: list[MediaFileReport]) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = self.log_dir / f"analysis_report_{timestamp}.json"
        payload = {
            "created_at": timestamp,
            "files": [report.to_dict() for report in reports],
        }
        report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        self.logger.info("Analysis report saved: %s", report_path)
        return report_path

    def write_analysis_csv(self, reports: list[MediaFileReport]) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = self.log_dir / f"analysis_report_{timestamp}.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "path",
                    "status",
                    "audio_selected",
                    "subtitle_selected",
                    "confidence",
                    "message",
                ],
            )
            writer.writeheader()
            for report in reports:
                writer.writerow(
                    {
                        "path": str(report.path),
                        "status": report.plan.action,
                        "audio_selected": report.plan.selected_audio or "",
                        "subtitle_selected": report.plan.selected_subtitle or "",
                        "confidence": report.plan.confidence,
                        "message": report.plan.error or " | ".join(report.plan.notes),
                    }
                )
        self.logger.info("CSV report saved: %s", csv_path)
        return csv_path

    def apply_report(self, report: MediaFileReport, dry_run: bool = False) -> tuple[bool, str]:
        if not report.is_ready():
            return False, "File is not ready to be updated."

        if report.container == "mkv":
            command = self._build_mkvpropedit_command(report)
            temp_output: Path | None = None
        else:
            command = self._build_ffmpeg_command(report)
            temp_output = Path(command[-1])

        preview = format_command(command)
        if dry_run:
            return True, preview

        self.logger.info("Applying changes to %s", report.path)
        self.logger.debug("Prepared command: %s", preview)

        try:
            result = run_command(command, timeout=600)
        except ExternalCommandError as exc:
            self.logger.exception("Unable to run the external command for %s", report.path)
            self.logger.error(exc.format_details())
            return False, self._user_facing_error_message(str(report.path), exc.stderr or str(exc))

        if result.returncode != 0:
            self.logger.error(
                "Update failed for %s\nCommand: %s\nExit code: %s\nStdout:\n%s\nStderr:\n%s",
                report.path,
                preview,
                result.returncode,
                result.stdout or "-",
                result.stderr or "-",
            )
            return False, self._user_facing_error_message(str(report.path), result.stderr or result.stdout)

        if temp_output is not None:
            try:
                temp_output.replace(report.path)
            except OSError as exc:
                self.logger.exception("Unable to replace the final file for %s", report.path)
                self._cleanup_temp_file(temp_output)
                return False, self._user_facing_error_message(str(report.path), str(exc))

        return True, preview

    def restore_report_from_backup_state(self, file_path: Path, file_state: dict) -> tuple[bool, str]:
        container = file_state.get("container") or file_path.suffix.lower().lstrip(".")
        audio_tracks = file_state.get("audio_tracks", [])
        subtitle_tracks = file_state.get("subtitle_tracks", [])

        if container == "mkv":
            command = self._build_restore_mkv_command(file_path, audio_tracks, subtitle_tracks)
            temp_output: Path | None = None
        else:
            command = self._build_restore_ffmpeg_command(file_path, audio_tracks, subtitle_tracks)
            temp_output = Path(command[-1])

        preview = format_command(command)
        self.logger.debug("Prepared restore command: %s", preview)
        try:
            result = run_command(command, timeout=600)
        except ExternalCommandError as exc:
            self.logger.exception("Unable to run restore for %s", file_path)
            self.logger.error(exc.format_details())
            return False, self._user_facing_error_message(str(file_path), exc.stderr or str(exc))

        if result.returncode != 0:
            self.logger.error(
                "Restore failed for %s\nCommand: %s\nExit code: %s\nStdout:\n%s\nStderr:\n%s",
                file_path,
                preview,
                result.returncode,
                result.stdout or "-",
                result.stderr or "-",
            )
            return False, self._user_facing_error_message(str(file_path), result.stderr or result.stdout)

        if temp_output is not None:
            try:
                temp_output.replace(file_path)
            except OSError as exc:
                self.logger.exception("Unable to move the restored file back into place for %s", file_path)
                self._cleanup_temp_file(temp_output)
                return False, self._user_facing_error_message(str(file_path), str(exc))

        return True, preview

    def _build_mkvpropedit_command(self, report: MediaFileReport) -> list[str]:
        executable = self.toolset.path_for("mkvpropedit") or "mkvpropedit"
        command = [executable, str(report.path)]

        for track in report.audio_tracks:
            command.extend(["--edit", f"track:a{track.type_index}", "--set", "flag-default=0"])
        if report.plan.selected_audio is not None:
            command.extend(["--edit", f"track:a{report.plan.selected_audio}", "--set", "flag-default=1"])

        for track in report.subtitle_tracks:
            command.extend(["--edit", f"track:s{track.type_index}", "--set", "flag-default=0"])
        if report.plan.selected_subtitle is not None:
            command.extend(
                [
                    "--edit",
                    f"track:s{report.plan.selected_subtitle}",
                    "--set",
                    "flag-default=1",
                    "--set",
                    "flag-forced=0",
                ]
            )

        return command

    def _build_ffmpeg_command(self, report: MediaFileReport) -> list[str]:
        executable = self.toolset.path_for("ffmpeg") or "ffmpeg"
        temp_output = report.path.with_name(f"{report.path.stem}.pistepilot_tmp{report.path.suffix}")

        command = [
            executable,
            "-y",
            "-i",
            str(report.path),
            "-map",
            "0",
            "-c",
            "copy",
        ]

        for track in report.audio_tracks:
            zero_based_index = (track.type_index or 1) - 1
            selected = report.plan.selected_audio == track.type_index
            command.extend(
                [
                    f"-disposition:a:{zero_based_index}",
                    self._build_ffmpeg_disposition(track, selected=selected, clear_forced=False),
                ]
            )

        for track in report.subtitle_tracks:
            zero_based_index = (track.type_index or 1) - 1
            selected = report.plan.selected_subtitle == track.type_index
            command.extend(
                [
                    f"-disposition:s:{zero_based_index}",
                    self._build_ffmpeg_disposition(track, selected=selected, clear_forced=selected),
                ]
            )

        command.append(str(temp_output))
        return command

    def _build_restore_mkv_command(
        self,
        file_path: Path,
        audio_tracks: list[dict],
        subtitle_tracks: list[dict],
    ) -> list[str]:
        executable = self.toolset.path_for("mkvpropedit") or "mkvpropedit"
        command = [executable, str(file_path)]

        for track in audio_tracks:
            command.extend(
                [
                    "--edit",
                    f"track:a{track.get('type_index')}",
                    "--set",
                    f"flag-default={1 if track.get('default') else 0}",
                ]
            )

        for track in subtitle_tracks:
            command.extend(
                [
                    "--edit",
                    f"track:s{track.get('type_index')}",
                    "--set",
                    f"flag-default={1 if track.get('default') else 0}",
                    "--set",
                    f"flag-forced={1 if track.get('forced') else 0}",
                ]
            )
        return command

    def _build_restore_ffmpeg_command(
        self,
        file_path: Path,
        audio_tracks: list[dict],
        subtitle_tracks: list[dict],
    ) -> list[str]:
        executable = self.toolset.path_for("ffmpeg") or "ffmpeg"
        temp_output = file_path.with_name(f"{file_path.stem}.pistepilot_restore_tmp{file_path.suffix}")
        command = [
            executable,
            "-y",
            "-i",
            str(file_path),
            "-map",
            "0",
            "-c",
            "copy",
        ]

        for track in audio_tracks:
            zero_based_index = (track.get("type_index") or 1) - 1
            command.extend(
                [
                    f"-disposition:a:{zero_based_index}",
                    self._build_disposition_from_saved_track(track, clear_forced=False),
                ]
            )

        for track in subtitle_tracks:
            zero_based_index = (track.get("type_index") or 1) - 1
            command.extend(
                [
                    f"-disposition:s:{zero_based_index}",
                    self._build_disposition_from_saved_track(track, clear_forced=False),
                ]
            )

        command.append(str(temp_output))
        return command

    def _build_ffmpeg_disposition(
        self,
        track: StreamInfo,
        *,
        selected: bool,
        clear_forced: bool,
    ) -> str:
        flags = {key for key, value in track.dispositions.items() if value}
        flags.discard("default")
        if clear_forced:
            flags.discard("forced")
        if selected:
            flags.add("default")
        return "+".join(sorted(flags)) if flags else "0"

    def _build_disposition_from_saved_track(self, track_state: dict, *, clear_forced: bool) -> str:
        flags = {key for key, value in (track_state.get("dispositions") or {}).items() if value}
        if track_state.get("default"):
            flags.add("default")
        else:
            flags.discard("default")
        if track_state.get("forced"):
            flags.add("forced")
        elif clear_forced:
            flags.discard("forced")
        return "+".join(sorted(flags)) if flags else "0"

    def _cleanup_temp_file(self, path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            self.logger.warning("Unable to delete temporary file %s", path)

    def _user_facing_error_message(self, file_path: str, details: str) -> str:
        lowered = details.lower()
        if any(term in lowered for term in ("permission denied", "used by another process", "access is denied", "device or resource busy")):
            return f"File is probably open or locked: unable to update ({file_path})"
        return f"Error while updating {file_path}. Check the log for full details."
