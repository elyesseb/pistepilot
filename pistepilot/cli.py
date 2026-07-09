from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.traceback import install as install_rich_traceback

from pistepilot import __version__
from pistepilot.ffmpeg_tools import MissingToolError, detect_tools, format_missing_tools_help, get_local_bin_dir, is_frozen
from pistepilot.grouping import build_relative_subtitle_rule
from pistepilot.logger import latest_log_file, set_console_verbose, setup_logging
from pistepilot.models import AmbiguityGroup, BatchSummary, MediaFileReport, ProfileSettings, StreamInfo, ToolInfo, Toolset
from pistepilot.profiles import DEFAULT_PROFILE, get_profile_by_name, load_profiles, save_last_used_profile, upsert_profile
from pistepilot.selector import language_profile_for, subtitle_candidates_for_language
from pistepilot.services import (
    analyze_directory,
    apply_changes,
    apply_group_recommendation,
    apply_group_relative_choice,
    resolve_groups,
    restore_from_backup,
    summarize_reports,
    validate_reports_before_apply,
)
from pistepilot.ui import open_path_with_system, pause_before_exit_if_interactive, prompt_directory_manually, select_directory_interactive

try:
    import questionary
except Exception:  # pragma: no cover - fallback only
    questionary = None


LANGUAGE_CHOICES = [
    ("French", "fr"),
    ("English", "en"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
    ("Spanish", "es"),
    ("German", "de"),
    ("Italian", "it"),
    ("Custom language code", "__custom__"),
]


@dataclass(slots=True)
class InteractiveState:
    profile: ProfileSettings = field(default_factory=lambda: get_profile_by_name(None))
    selected_folder: Path = field(default_factory=Path.cwd)
    recursive: bool = True
    reports: list[MediaFileReport] = field(default_factory=list)
    last_backup_path: Path | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pistepilot", description="Batch-select preferred audio and subtitle tracks.")
    parser.add_argument("--version", action="version", version=f"PistePilot {__version__}")
    parser.add_argument("--verbose", action="store_true", help="Show technical details in the terminal")

    subparsers = parser.add_subparsers(dest="command")

    for name in ("analyze", "apply"):
        sub = subparsers.add_parser(name)
        sub.add_argument("folder", type=Path, help="Folder to analyze")
        sub.add_argument("--audio", required=True, help="Target audio language code, for example: fr")
        sub.add_argument("--subs", required=True, help="Target subtitle language code, for example: fr")
        sub.add_argument("--recursive", action="store_true", default=True, help="Include subfolders")
        sub.add_argument("--no-recursive", dest="recursive", action="store_false", help="Disable subfolders")
        sub.add_argument("--verbose", action="store_true", help="Show technical details in the terminal")
        if name == "apply":
            sub.add_argument("--yes", action="store_true", help="Apply without final confirmation")

    tools = subparsers.add_parser("tools", help="Check external tools")
    tools.add_argument("--verbose", action="store_true", help="Show technical details in the terminal")
    restore = subparsers.add_parser("restore", help="Restore from a metadata backup JSON file")
    restore.add_argument("backup", type=Path, help="original_state_*.json file")
    restore.add_argument("--yes", action="store_true", help="Restore without an extra confirmation")
    restore.add_argument("--verbose", action="store_true", help="Show technical details in the terminal")
    return parser


def language_label(code: str) -> str:
    return language_profile_for(code).label


def prompt_select(message: str, choices: list[str], *, default: str | None = None) -> str:
    if questionary is not None:
        return str(questionary.select(message, choices=choices, default=default or choices[0]).ask())

    print(message)
    for index, choice in enumerate(choices, start=1):
        marker = " (default)" if default and choice == default else ""
        print(f"{index}. {choice}{marker}")
    raw = input("Number: ").strip()
    if not raw and default:
        return default
    return choices[int(raw) - 1]


def prompt_confirm(message: str, *, default: bool = False) -> bool:
    if questionary is not None:
        return bool(questionary.confirm(message, default=default).ask())

    raw = input(f"{message} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes", "o", "oui"}


def prompt_text(message: str, *, default: str = "") -> str:
    if questionary is not None:
        return (questionary.text(message, default=default).ask() or "").strip()
    suffix = f" [{default}]" if default else ""
    return (input(f"{message}{suffix}: ").strip() or default).strip()


def format_tool_status(info: ToolInfo) -> str:
    return "[green]OK[/green]" if info.found else "[red]Missing[/red]"


def show_tool_diagnostic(console: Console, toolset: Toolset) -> None:
    ffmpeg_table = Table(title="FFmpeg")
    ffmpeg_table.add_column("Tool")
    ffmpeg_table.add_column("Status")
    ffmpeg_table.add_column("Source")
    ffmpeg_table.add_column("Path")

    mkvtoolnix_table = Table(title="MKVToolNix")
    mkvtoolnix_table.add_column("Tool")
    mkvtoolnix_table.add_column("Status")
    mkvtoolnix_table.add_column("Source")
    mkvtoolnix_table.add_column("Path")

    for name in ("ffmpeg", "ffprobe"):
        info = toolset.get(name)
        ffmpeg_table.add_row(name, format_tool_status(info), info.source or "-", info.path or "-")
    for name in ("mkvmerge", "mkvpropedit"):
        info = toolset.get(name)
        mkvtoolnix_table.add_row(name, format_tool_status(info), info.source or "-", info.path or "-")

    console.print(Panel("External tools", title="Diagnostic"))
    console.print(ffmpeg_table)
    console.print(mkvtoolnix_table)
    console.print(
        Panel(
            "Impact :\n"
            "- Basic analysis can work with ffprobe.\n"
            "- Fast and safe MKV updates require MKVToolNix.\n"
            "- Without MKVToolNix, PistePilot will not be able to apply MKV changes correctly.",
            title="Impact",
        )
    )
    console.print(
        Panel(
            "Solutions:\n"
            "1. Install MKVToolNix and add it to PATH\n"
            "2. Copy mkvmerge.exe and mkvpropedit.exe into the bin folder next to PistePilot.exe",
            title="How to fix",
        )
    )
    console.print(Panel(f"Local bin folder: {get_local_bin_dir(create=True)}", title="Local bin"))


def display_report(console: Console, report: MediaFileReport) -> None:
    console.rule(str(report.path))
    table = Table(title="File summary")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Status", report.plan.action)
    table.add_row("Selected audio", f"a{report.plan.selected_audio}" if report.plan.selected_audio else "-")
    table.add_row("Selected subtitle", f"s{report.plan.selected_subtitle}" if report.plan.selected_subtitle else "-")
    table.add_row("Confidence", report.plan.confidence)
    table.add_row("Message", report.plan.error or ", ".join(report.plan.notes) or "-")
    console.print(table)


def display_analysis_summary(console: Console, summary: BatchSummary, *, folder: Path, profile: ProfileSettings) -> None:
    table = Table(title="Analysis summary")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Video files found", str(summary.total_files))
    table.add_row("Ready to update automatically", str(summary.auto_applicable))
    table.add_row("Needs confirmation", str(summary.needs_review))
    table.add_row("Skipped", str(summary.skipped))
    table.add_row("Errors", str(summary.errors))
    table.add_row("Requested audio", language_label(profile.audio_language))
    table.add_row("Requested subtitles", language_label(profile.subtitle_language))
    table.add_row("Folder", str(folder))
    console.print(table)

    if summary.needs_review:
        message = f"Resolve the {summary.needs_review} confirmation case(s), then apply the changes."
    elif summary.auto_applicable:
        message = "Everything is ready. You can apply the changes."
    else:
        message = "No automatic change is ready at the moment."
    console.print(Panel(message, title="Recommended next step"))


def display_apply_summary(console: Console, summary: BatchSummary, *, log_file: Path, backup_path: Path | None) -> None:
    table = Table(title="Done")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Updated successfully", str(summary.applied))
    table.add_row("Skipped", str(summary.skipped))
    table.add_row("Errors", str(summary.errors))
    table.add_row("Logs", str(log_file))
    table.add_row("Metadata backup", str(backup_path) if backup_path else "-")
    console.print(table)

    if summary.errors:
        console.print(
            Panel(
                "Some files were not updated.\n"
                "Possible causes:\n"
                "- file open in a media player\n"
                "- insufficient permissions\n"
                "- corrupted file\n"
                "- missing external tool\n"
                "Check the log for more details.",
                title="Warning",
                border_style="yellow",
            )
        )


def display_apply_preview(console: Console, reports: list[MediaFileReport]) -> None:
    preview = Table(title="Files that will be updated")
    preview.add_column("File")
    preview.add_column("Audio")
    preview.add_column("Subtitle")
    for report in validate_reports_before_apply(reports):
        preview.add_row(
            report.path.name,
            f"a{report.plan.selected_audio}" if report.plan.selected_audio else "-",
            f"s{report.plan.selected_subtitle}" if report.plan.selected_subtitle else "-",
        )
    console.print(preview)


def ensure_analysis_prerequisites(toolset: Toolset) -> None:
    if not toolset.is_available("mkvmerge") and not toolset.is_available("ffprobe"):
        raise MissingToolError(format_missing_tools_help(["mkvmerge", "ffprobe"]))


def ensure_apply_prerequisites(toolset: Toolset, reports: list[MediaFileReport]) -> None:
    missing_tools: list[str] = []
    if any(report.container == "mkv" and report.is_ready() for report in reports) and not toolset.is_available("mkvpropedit"):
        missing_tools.append("mkvpropedit")
    if any(report.container != "mkv" and report.is_ready() for report in reports) and not toolset.is_available("ffmpeg"):
        missing_tools.append("ffmpeg")
    if missing_tools:
        raise MissingToolError(format_missing_tools_help(missing_tools))


def choose_language(current_code: str, prompt_message: str) -> str:
    default_label = next((label for label, code in LANGUAGE_CHOICES if code == current_code), LANGUAGE_CHOICES[0][0])
    selection = prompt_select(prompt_message, [label for label, _ in LANGUAGE_CHOICES], default=default_label)
    for label, code in LANGUAGE_CHOICES:
        if label == selection:
            if code == "__custom__":
                return prompt_text("Enter the custom language code")
            return code
    return current_code


def edit_profile_interactive(profile: ProfileSettings) -> ProfileSettings:
    name = prompt_text("Preset name", default=profile.name)
    audio_language = choose_language(profile.audio_language, "Choose the preferred audio language")
    subtitle_language = choose_language(profile.subtitle_language, "Choose the preferred subtitle language")
    auto_apply_unique_candidate = prompt_confirm("Automatically select a single plausible track when possible?", default=profile.auto_apply_unique_candidate)
    auto_group_series = prompt_confirm("Group similar episodes automatically?", default=profile.auto_group_series)
    prefer_srt_over_pgs = prompt_confirm("Prefer SRT over PGS?", default=profile.prefer_srt_over_pgs)
    return ProfileSettings(
        name=name,
        audio_language=audio_language,
        subtitle_language=subtitle_language,
        subtitle_policy=profile.subtitle_policy,
        auto_apply_unique_candidate=auto_apply_unique_candidate,
        auto_group_series=auto_group_series,
        prefer_srt_over_pgs=prefer_srt_over_pgs,
        exclude_forced=profile.exclude_forced,
        exclude_sdh=profile.exclude_sdh,
        exclude_commentary=profile.exclude_commentary,
        exclude_dubtitle=profile.exclude_dubtitle,
    )


def choose_profile_interactive(console: Console, current_profile: ProfileSettings) -> ProfileSettings:
    profiles, last_used = load_profiles()
    names = [profile.name for profile in profiles] or [DEFAULT_PROFILE.name]
    default_name = current_profile.name if current_profile.name in names else last_used
    selection = prompt_select(
        "Choose a preset",
        names + ["Edit the current preset", "Create a new preset"],
        default=default_name if default_name in names else names[0],
    )

    if selection == "Edit the current preset":
        updated = edit_profile_interactive(current_profile)
        upsert_profile(updated)
        save_last_used_profile(updated.name)
        console.print(f"[green]Preset updated: {updated.name}[/green]")
        return updated
    if selection == "Create a new preset":
        created = edit_profile_interactive(ProfileSettings(**DEFAULT_PROFILE.to_dict()))
        upsert_profile(created)
        save_last_used_profile(created.name)
        console.print(f"[green]Preset created: {created.name}[/green]")
        return created

    selected = get_profile_by_name(selection)
    save_last_used_profile(selected.name)
    return selected


def choose_directory_with_options(state: InteractiveState) -> Path | None:
    choice = prompt_select(
        "Folder selection",
        [
            "Open the Windows folder picker",
            "Enter the path manually",
            "Back",
        ],
        default="Open the Windows folder picker",
    )
    if choice == "Back":
        return None
    if choice == "Enter the path manually":
        return prompt_directory_manually(state.selected_folder)
    return select_directory_interactive()


def _show_group_details(console: Console, group: AmbiguityGroup, target_language: str) -> None:
    for report in group.reports:
        language_tracks, plausible_tracks = subtitle_candidates_for_language(report.subtitle_tracks, target_language)
        details = Table(title=report.path.name)
        details.add_column("Track")
        details.add_column("Language")
        details.add_column("Title")
        details.add_column("Codec")
        details.add_column("Plausible")
        for track in language_tracks:
            details.add_row(
                track.short_label(),
                track.language_ietf or track.language or "und",
                track.title or "-",
                track.codec or "-",
                "Yes" if any(candidate.type_index == track.type_index for candidate in plausible_tracks) else "No",
            )
        console.print(details)


def _resolve_group_file_by_file(console: Console, group: AmbiguityGroup, profile: ProfileSettings) -> None:
    for report in group.reports:
        language_tracks, plausible_tracks = subtitle_candidates_for_language(report.subtitle_tracks, profile.subtitle_language)
        candidates = plausible_tracks or language_tracks
        if not candidates:
            continue
        labels = [f"{track.short_label()} | {track.language or track.language_ietf or 'und'} | {track.codec or '-'} | {track.title or 'nom vide'}" for track in candidates]
        selected = prompt_select(
            f"Choose the correct subtitle for {report.path.name}",
            labels,
            default=labels[0],
        )
        chosen_track = candidates[labels.index(selected)]
        apply_group_relative_choice(group, report, chosen_track, profile.subtitle_language)


def resolve_ambiguity_groups(console: Console, reports: list[MediaFileReport], profile: ProfileSettings) -> None:
    groups = resolve_groups(reports, profile.subtitle_language)
    if not groups:
        return

    console.print(Panel("Grouped confirmation cases", title="Resolution"))
    for index, group in enumerate(groups, start=1):
        unresolved_reports = [report for report in group.reports if report.needs_manual_review()]
        if not unresolved_reports:
            continue

        representative = unresolved_reports[0]
        _, plausible_tracks = subtitle_candidates_for_language(representative.subtitle_tracks, profile.subtitle_language)
        candidate_counts = []
        for report in unresolved_reports:
            _, plausible = subtitle_candidates_for_language(report.subtitle_tracks, profile.subtitle_language)
            candidate_counts.append(len(plausible))

        default_choice = (
            "Apply the automatically recommended subtitle to the group"
            if candidate_counts and all(count == 1 for count in candidate_counts)
            else "Choose a reference file and apply the same relative track to the other files"
        )
        console.print(
            Panel(
                f"Group {index}\n"
                f"Folder: {group.folder}\n"
                f"Files affected: {len(unresolved_reports)}\n"
                f"Recommended audio: {language_label(profile.audio_language)}\n"
                f"Detected {language_label(profile.subtitle_language)} subtitle candidates: {', '.join(str(count) for count in candidate_counts[:3])}"
                f"{'...' if len(candidate_counts) > 3 else ''}\n"
                f"Reason: {group.reason}",
                title=f"Group {index}",
            )
        )

        while True:
            action = prompt_select(
                "What do you want to do?",
                [
                    "Apply the automatically recommended subtitle to the group",
                    "Choose a reference file and apply the same relative track to the other files",
                    "Show track details",
                    "Handle one file at a time",
                    "Ignore this group",
                ],
                default=default_choice,
            )

            if action == "Show track details":
                _show_group_details(console, group, profile.subtitle_language)
                continue

            if action == "Ignore this group":
                break

            if action == "Apply the automatically recommended subtitle to the group":
                resolved = apply_group_recommendation(group, profile.subtitle_language)
                console.print(f"[green]{resolved} file(s) resolved automatically in this group.[/green]")
                break

            if action == "Handle one file at a time":
                _resolve_group_file_by_file(console, group, profile)
                break

            reference_labels = [report.path.name for report in unresolved_reports]
            reference_name = prompt_select(
                "Choose a reference file",
                reference_labels,
                default=reference_labels[0],
            )
            reference_report = unresolved_reports[reference_labels.index(reference_name)]
            language_tracks, plausible = subtitle_candidates_for_language(reference_report.subtitle_tracks, profile.subtitle_language)
            candidates = plausible or language_tracks
            labels = [
                f"{track.short_label()} | {track.language or track.language_ietf or 'und'} | {track.codec or '-'} | {track.title or 'empty title'}"
                for track in candidates
            ]
            selected = prompt_select(
                "Choose the correct subtitle on the reference file",
                labels,
                default=labels[0],
            )
            chosen_track = candidates[labels.index(selected)]
            rule = build_relative_subtitle_rule(reference_report, chosen_track, profile.subtitle_language)
            if prompt_confirm(f"Apply this logical choice to the {len(unresolved_reports)} files in the group?", default=True):
                resolved = apply_group_relative_choice(group, reference_report, chosen_track, profile.subtitle_language)
                console.print(
                    Panel(
                        f"{resolved} file(s) resolved via a relative rule.\n"
                        f"Rule: choose candidate #{rule.candidate_rank} among the {rule.target_language} subtitle candidates.",
                        title="Group resolution",
                    )
                )
                break


def run_interactive_analysis(console: Console, state: InteractiveState, toolset: Toolset, logger) -> BatchSummary:
    console.print(Panel("Simulation analysis\nNo video file will be changed at this step.", title="Simulation"))
    state.reports = analyze_directory(
        state.selected_folder,
        profile=state.profile,
        recursive=state.recursive,
        toolset=toolset,
        logger=logger,
    )
    summary = summarize_reports(state.reports)
    display_analysis_summary(console, summary, folder=state.selected_folder, profile=state.profile)
    return summary


def run_apply_flow(console: Console, state: InteractiveState, toolset: Toolset, logger, log_dir: Path, log_file: Path, *, auto_confirm: bool) -> None:
    if not state.reports:
        console.print("[yellow]No recommendation is currently loaded. Run an analysis first.[/yellow]")
        return
    if any(report.needs_manual_review() for report in state.reports):
        resolve_ambiguity_groups(console, state.reports, state.profile)
    ensure_apply_prerequisites(toolset, state.reports)
    ready_reports = validate_reports_before_apply(state.reports)
    if not ready_reports:
        console.print("[yellow]No valid analysis report is available.[/yellow]")
        return
    display_apply_preview(console, state.reports)

    if not auto_confirm:
        if not prompt_confirm(
            "You are about to update the prepared files. No track will be removed. Continue?",
            default=False,
        ):
            console.print("[yellow]Apply cancelled.[/yellow]")
            return

    summary, state.last_backup_path = apply_changes(state.reports, toolset=toolset, logger=logger, log_dir=log_dir)
    display_apply_summary(console, summary, log_file=log_file, backup_path=state.last_backup_path)


def open_last_log(console: Console, log_file: Path, log_dir: Path) -> None:
    path = latest_log_file(log_dir) or log_file
    open_path_with_system(path)
    console.print(f"[green]Opened log file: {path}[/green]")


def open_logs_folder(console: Console, log_dir: Path) -> None:
    open_path_with_system(log_dir)
    console.print(f"[green]Opened logs folder: {log_dir}[/green]")


def open_local_bin_folder(console: Console) -> None:
    bin_dir = get_local_bin_dir(create=True)
    open_path_with_system(bin_dir)
    console.print(f"[green]Opened bin folder: {bin_dir}[/green]")


def advanced_menu(console: Console, state: InteractiveState, toolset: Toolset, logger, log_dir: Path, log_file: Path) -> None:
    while True:
        console.print(
            Panel(
                f"Preset: {state.profile.name}\n"
                f"Folder: {state.selected_folder}\n"
                f"Requested audio: {language_label(state.profile.audio_language)}\n"
                f"Requested subtitles: {language_label(state.profile.subtitle_language)}\n"
                f"Include subfolders: {'Yes' if state.recursive else 'No'}",
                title="Advanced settings",
            )
        )
        choice = prompt_select(
            "Advanced settings",
            [
                "Choose a folder",
                "Choose or edit a preset",
                "Toggle subfolder scanning",
                "Run a simulation analysis",
                "Resolve grouped confirmation cases",
                "Apply the current recommendations",
                "Check external tools",
                "Open the local bin folder",
                "Open the latest log file",
                "Back",
            ],
            default="Choose a folder",
        )

        if choice == "Choose a folder":
            selected = choose_directory_with_options(state)
            if selected is not None:
                state.selected_folder = selected
        elif choice == "Choose or edit a preset":
            state.profile = choose_profile_interactive(console, state.profile)
        elif choice == "Toggle subfolder scanning":
            state.recursive = not state.recursive
        elif choice == "Run a simulation analysis":
            ensure_analysis_prerequisites(toolset)
            run_interactive_analysis(console, state, toolset, logger)
        elif choice == "Resolve grouped confirmation cases":
            if not state.reports:
                console.print("[yellow]No analysis is available yet.[/yellow]")
            else:
                resolve_ambiguity_groups(console, state.reports, state.profile)
        elif choice == "Apply the current recommendations":
            run_apply_flow(console, state, toolset, logger, log_dir, log_file, auto_confirm=False)
        elif choice == "Check external tools":
            show_tool_diagnostic(console, toolset)
        elif choice == "Open the local bin folder":
            open_local_bin_folder(console)
        elif choice == "Open the latest log file":
            open_last_log(console, log_file, log_dir)
        elif choice == "Back":
            return


def run_quick_assistant(console: Console, state: InteractiveState, toolset: Toolset, logger, log_dir: Path, log_file: Path) -> None:
    ensure_analysis_prerequisites(toolset)
    console.print(Panel(f"Preset: {state.profile.name}", title="Quick assistant"))
    if prompt_confirm("Change the current preset?", default=False):
        state.profile = choose_profile_interactive(console, state.profile)

    selected = select_directory_interactive()
    if selected is None:
        console.print("[yellow]No folder was selected. Returning to the main menu.[/yellow]")
        return
    state.selected_folder = selected
    state.recursive = prompt_confirm("Include subfolders?", default=False)

    summary = run_interactive_analysis(console, state, toolset, logger)
    if summary.needs_review:
        resolve_ambiguity_groups(console, state.reports, state.profile)
        summary = summarize_reports(state.reports)
        display_analysis_summary(console, summary, folder=state.selected_folder, profile=state.profile)

    action = prompt_select(
        "Apply the changes now?",
        ["Yes", "No", "Save the report and quit"],
        default="Yes" if summary.auto_applicable else "No",
    )
    if action == "Save the report and quit":
        from pistepilot.mkv_editor import MediaEditor

        editor = MediaEditor(toolset, logger, log_dir)
        report_path = editor.write_analysis_report(state.reports)
        console.print(f"[green]Saved report: {report_path}[/green]")
        return
    if action == "No":
        return
    run_apply_flow(console, state, toolset, logger, log_dir, log_file, auto_confirm=True)


def interactive_menu(console: Console, toolset: Toolset, logger, log_dir: Path, log_file: Path) -> int:
    state = InteractiveState()
    save_last_used_profile(state.profile.name)

    while True:
        console.print(Panel("Welcome to PistePilot", title="PistePilot"))
        choice = prompt_select(
            "What would you like to do?",
            [
                "Quick assistant: analyze a folder and apply the recommended tracks",
                "Analyze a folder only",
                "Apply the latest recommendations",
                "Advanced settings",
                "Check external tools",
                "Open the logs folder",
                "Quit",
            ],
            default="Quick assistant: analyze a folder and apply the recommended tracks",
        )
        try:
            if choice == "Quick assistant: analyze a folder and apply the recommended tracks":
                run_quick_assistant(console, state, toolset, logger, log_dir, log_file)
            elif choice == "Analyze a folder only":
                ensure_analysis_prerequisites(toolset)
                selected = select_directory_interactive()
                if selected is None:
                    console.print("[yellow]Selection cancelled.[/yellow]")
                    continue
                state.selected_folder = selected
                run_interactive_analysis(console, state, toolset, logger)
            elif choice == "Apply the latest recommendations":
                run_apply_flow(console, state, toolset, logger, log_dir, log_file, auto_confirm=False)
            elif choice == "Advanced settings":
                advanced_menu(console, state, toolset, logger, log_dir, log_file)
            elif choice == "Check external tools":
                show_tool_diagnostic(console, toolset)
            elif choice == "Open the logs folder":
                open_logs_folder(console, log_dir)
            elif choice == "Quit":
                return 0
        except MissingToolError as exc:
            console.print(Panel(str(exc), title="Required tools", border_style="yellow"))
        except Exception as exc:
            logger.exception("Interactive flow error")
            console.print(Panel(f"Error: {exc}\nCheck the log: {log_file}", title="Error", border_style="red"))


def run_cli_command(args, console: Console, toolset: Toolset, logger, log_dir: Path, log_file: Path) -> int:
    if args.command == "tools":
        show_tool_diagnostic(console, toolset)
        return 0
    if args.command == "restore":
        if not args.yes and not prompt_confirm(
            "Restore should be tested on copies first. Continue?",
            default=False,
        ):
            return 0
        restored, errors = restore_from_backup(args.backup, toolset=toolset, logger=logger, log_dir=log_dir)
        console.print(
            Panel(
                f"Restore complete\nRestored: {restored}\nErrors: {errors}\nLog: {log_file}",
                title="Restore",
            )
        )
        return 0

    ensure_analysis_prerequisites(toolset)
    profile = ProfileSettings(
        name="CLI",
        audio_language=args.audio,
        subtitle_language=args.subs,
        subtitle_policy=DEFAULT_PROFILE.subtitle_policy,
        auto_apply_unique_candidate=DEFAULT_PROFILE.auto_apply_unique_candidate,
        auto_group_series=DEFAULT_PROFILE.auto_group_series,
        prefer_srt_over_pgs=DEFAULT_PROFILE.prefer_srt_over_pgs,
        exclude_forced=DEFAULT_PROFILE.exclude_forced,
        exclude_sdh=DEFAULT_PROFILE.exclude_sdh,
        exclude_commentary=DEFAULT_PROFILE.exclude_commentary,
        exclude_dubtitle=DEFAULT_PROFILE.exclude_dubtitle,
    )
    reports = analyze_directory(args.folder, profile=profile, recursive=args.recursive, toolset=toolset, logger=logger)
    for report in reports:
        display_report(console, report)
    summary = summarize_reports(reports)
    display_analysis_summary(console, summary, folder=args.folder, profile=profile)

    if args.command == "analyze":
        return 0

    if summary.needs_review:
        resolve_ambiguity_groups(console, reports, profile)
        summary = summarize_reports(reports)
        display_analysis_summary(console, summary, folder=args.folder, profile=profile)

    ensure_apply_prerequisites(toolset, reports)
    ready_reports = validate_reports_before_apply(reports)
    if not ready_reports:
        console.print("[yellow]No valid analysis report is available.[/yellow]")
        return 0
    display_apply_preview(console, reports)
    if not args.yes and not prompt_confirm("Apply the changes now?", default=False):
        return 0
    summary, backup_path = apply_changes(reports, toolset=toolset, logger=logger, log_dir=log_dir)
    display_apply_summary(console, summary, log_file=log_file, backup_path=backup_path)
    return 0


def _run(argv: list[str] | None, *, console: Console, logger, log_dir: Path, log_file: Path) -> int:
    install_rich_traceback(show_locals=False)
    parser = build_parser()
    args = parser.parse_args(argv)
    set_console_verbose(logger, getattr(args, "verbose", False))
    toolset = detect_tools()

    if args.command is None:
        return interactive_menu(console, toolset, logger, log_dir, log_file)
    return run_cli_command(args, console, toolset, logger, log_dir, log_file)


def main(argv: list[str] | None = None) -> int:
    interactive_mode = argv is None and len(sys.argv) == 1
    parser = build_parser()
    preview_args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    console = Console()
    logger, log_dir, log_file = setup_logging(verbose=getattr(preview_args, "verbose", False))

    try:
        return _run(argv, console=console, logger=logger, log_dir=log_dir, log_file=log_file)
    except KeyboardInterrupt:
        logger.warning("User interruption.")
        console.print(Panel("User interruption.", title="Stopped", border_style="yellow"))
        return 130
    except MissingToolError as exc:
        logger.error(str(exc))
        console.print(Panel(str(exc), title="Required tools", border_style="yellow"))
        return 2
    except Exception as exc:
        logger.exception("Unexpected PistePilot error")
        console.print(
            Panel(
                f"An unexpected error occurred: {exc}\n"
                f"Log file: {log_file}\n"
                "Copy this log file to help diagnose the problem.",
                title="Error",
                border_style="red",
            )
        )
        return 1
    finally:
        pause_before_exit_if_interactive(interactive_mode=interactive_mode or is_frozen())


if __name__ == "__main__":
    raise SystemExit(main())
