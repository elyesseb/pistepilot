from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import Callable

from pistepilot.analyzer import MediaAnalyzer
from pistepilot.grouping import apply_relative_subtitle_rule, build_relative_subtitle_rule, group_ambiguous_files
from pistepilot.mkv_editor import MediaEditor
from pistepilot.models import (
    AmbiguityGroup,
    BatchSummary,
    MediaFileReport,
    ProfileSettings,
    SelectionDecision,
    StreamInfo,
    Toolset,
)
from pistepilot.scanner import scan_video_files
from pistepilot.selector import apply_manual_selection, apply_session_rules, evaluate_report, subtitle_candidates_for_language


ProgressCallback = Callable[[str, int, int, str], None]


def make_default_decision(reason: str = "No selection available.") -> SelectionDecision:
    return SelectionDecision(
        status="skipped",
        selected_type_index=None,
        candidate_type_indices=[],
        confidence="none",
        reason=reason,
    )


def summarize_reports(reports: list[MediaFileReport]) -> BatchSummary:
    summary = BatchSummary(total_files=len(reports))
    for report in reports:
        if report.plan.action == "apply":
            summary.auto_applicable += 1
        elif report.plan.action == "review":
            summary.needs_review += 1
        elif report.plan.action == "skip":
            summary.skipped += 1
        elif report.plan.action == "error":
            summary.errors += 1
    return summary


def analyze_directory(
    folder: Path,
    *,
    profile: ProfileSettings,
    recursive: bool,
    toolset: Toolset,
    logger,
    session_rules=None,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
) -> list[MediaFileReport]:
    files = scan_video_files(folder, recursive=recursive)
    analyzer = MediaAnalyzer(toolset, logger)
    reports: list[MediaFileReport] = []

    if not files:
        logger.warning("No video file was found in %s", folder)
        return reports

    for index, file_path in enumerate(files, start=1):
        if cancel_event and cancel_event.is_set():
            logger.warning("Analysis cancelled by the user.")
            break
        if progress_callback is not None:
            progress_callback("analysis", index, len(files), file_path.name)
        try:
            report = analyzer.analyze_file(file_path)
            report = evaluate_report(
                report,
                profile.audio_language,
                profile.subtitle_language,
                prefer_srt_over_pgs=profile.prefer_srt_over_pgs,
                auto_apply_unique_candidate=profile.auto_apply_unique_candidate,
            )
            if session_rules and report.needs_manual_review():
                apply_session_rules(report, session_rules, logger=logger)
            reports.append(report)
        except Exception as exc:
            logger.exception("Error while analyzing %s", file_path)
            reports.append(
                MediaFileReport(
                    path=file_path,
                    container=file_path.suffix.lower().lstrip("."),
                    analysis_tool="none",
                    video_codec=None,
                    audio_tracks=[],
                    subtitle_tracks=[],
                    audio_decision=make_default_decision(),
                    subtitle_decision=make_default_decision(),
                    plan=report_error_plan(file_path, str(exc)),
                    errors=[str(exc)],
                )
            )
    return reports


def report_error_plan(file_path: Path, message: str):
    from pistepilot.models import PlannedChange

    return PlannedChange(
        container=file_path.suffix.lower().lstrip("."),
        action="error",
        tool="mkvpropedit" if file_path.suffix.lower() == ".mkv" else "ffmpeg",
        selected_audio=None,
        selected_subtitle=None,
        confidence="none",
        notes=[message],
        error=message,
    )


def resolve_groups(reports: list[MediaFileReport], target_language: str) -> list[AmbiguityGroup]:
    return group_ambiguous_files(reports, target_language)


def apply_group_recommendation(group: AmbiguityGroup, target_language: str) -> int:
    resolved = 0
    for report in group.reports:
        _, plausible_tracks = subtitle_candidates_for_language(report.subtitle_tracks, target_language)
        if len(plausible_tracks) != 1:
            continue
        track = plausible_tracks[0]
        apply_manual_selection(report, report.audio_decision.selected_type_index, track.type_index)
        report.resolution_source = "group_recommendation"
        resolved += 1
    return resolved


def apply_group_relative_choice(
    group: AmbiguityGroup,
    reference_report: MediaFileReport,
    selected_track: StreamInfo,
    target_language: str,
) -> int:
    rule = build_relative_subtitle_rule(reference_report, selected_track, target_language)
    group.relative_rule = rule
    resolved = 0
    for report in group.reports:
        match = apply_relative_subtitle_rule(report, rule)
        if match is None:
            continue
        apply_manual_selection(report, report.audio_decision.selected_type_index, match.type_index)
        report.resolution_source = "group_relative_rule"
        resolved += 1
    return resolved


def apply_changes(
    reports: list[MediaFileReport],
    *,
    toolset: Toolset,
    logger,
    log_dir: Path,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
) -> tuple[BatchSummary, Path | None]:
    summary = summarize_reports(reports)
    if summary.auto_applicable == 0:
        logger.warning("No file is ready to be updated.")
        return summary, None

    editor = MediaEditor(toolset, logger, log_dir)
    backup_path = editor.write_original_state_backup(reports)
    if not backup_path.exists():
        raise RuntimeError("Le backup metadata n'a pas pu etre cree avant application.")
    ready_reports = [report for report in reports if report.is_ready()]

    for index, report in enumerate(ready_reports, start=1):
        if cancel_event and cancel_event.is_set():
            logger.warning("Apply cancelled by the user.")
            break
        success, message = editor.apply_report(report, dry_run=False)
        if success:
            summary.applied += 1
            logger.info("[%s/%s] %s : OK", index, len(ready_reports), report.path.name)
        else:
            summary.errors += 1
            report.plan.action = "error"
            report.plan.error = message
            logger.error("[%s/%s] %s : Error", index, len(ready_reports), report.path.name)
        if progress_callback is not None:
            progress_callback("apply", index, len(ready_reports), f"{report.path.name} : {'OK' if success else 'Error'}")

    return summary, backup_path


def export_analysis_reports(
    reports: list[MediaFileReport],
    *,
    toolset: Toolset,
    logger,
    log_dir: Path,
) -> tuple[Path, Path]:
    editor = MediaEditor(toolset, logger, log_dir)
    json_path = editor.write_analysis_report(reports)
    csv_path = editor.write_analysis_csv(reports)
    return json_path, csv_path


def restore_from_backup(
    backup_path: Path,
    *,
    toolset: Toolset,
    logger,
    log_dir: Path,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
) -> tuple[int, int]:
    import json

    payload = json.loads(backup_path.read_text(encoding="utf-8"))
    files = payload.get("files", [])
    editor = MediaEditor(toolset, logger, log_dir)
    restored = 0
    errors = 0

    for index, file_state in enumerate(files, start=1):
        if cancel_event and cancel_event.is_set():
            logger.warning("Restore cancelled by the user.")
            break
        file_path = Path(file_state["path"])
        success, message = editor.restore_report_from_backup_state(file_path, file_state)
        if success:
            restored += 1
            logger.info("[%s/%s] %s : restauration OK", index, len(files), file_path.name)
        else:
            errors += 1
            logger.error("[%s/%s] %s : restauration en erreur | %s", index, len(files), file_path.name, message)
        if progress_callback is not None:
            progress_callback("restore", index, len(files), f"{file_path.name} : {'OK' if success else 'Error'}")

    return restored, errors


def validate_reports_before_apply(reports: list[MediaFileReport]) -> list[MediaFileReport]:
    return [report for report in reports if report.is_ready()]
