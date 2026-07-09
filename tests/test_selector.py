import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from pistepilot import ffmpeg_tools
from pistepilot.ffmpeg_tools import ExternalCommandError
from pistepilot.grouping import apply_relative_subtitle_rule, build_relative_subtitle_rule, group_ambiguous_files
from pistepilot.gui import CHOOSER_PLACEHOLDER, OPTIONS_MENU_LABELS, PistePilotGUI
from pistepilot.i18n import DEFAULT_LANGUAGE, display_language_name, normalize_language_code
from pistepilot.mkv_editor import MediaEditor
from pistepilot.models import MediaFileReport, PlannedChange, ProfileSettings, SelectionDecision, StreamInfo, ToolInfo, Toolset
from pistepilot.profiles import DEFAULT_PROFILE
from pistepilot.selector import evaluate_report, select_subtitle_track
from pistepilot.services import analyze_directory, export_analysis_reports, restore_from_backup, summarize_reports
from pistepilot.ui import pause_before_exit_if_interactive


def make_track(
    *,
    kind: str,
    type_index: int,
    language: str | None,
    title: str | None,
    channels: int | None = None,
    default: bool = False,
    forced: bool = False,
    codec: str | None = None,
    disposition: dict | None = None,
) -> StreamInfo:
    return StreamInfo(
        kind=kind,  # type: ignore[arg-type]
        id=type_index,
        type_index=type_index,
        codec=codec or ("aac" if kind == "audio" else "subrip"),
        language=language,
        language_ietf=None,
        title=title,
        channels=channels,
        default=default,
        forced=forced,
        tags={},
        dispositions=disposition or {"default": int(default), "forced": int(forced)},
        raw={},
    )


def base_report(
    audio_tracks: list[StreamInfo],
    subtitle_tracks: list[StreamInfo],
    *,
    path: str = "sample.mkv",
    video_codec: str = "hevc",
) -> MediaFileReport:
    empty_decision = SelectionDecision(
        status="skipped",
        selected_type_index=None,
        candidate_type_indices=[],
        confidence="none",
        reason="",
    )
    return MediaFileReport(
        path=Path(path),
        container="mkv",
        analysis_tool="test",
        video_codec=video_codec,
        audio_tracks=audio_tracks,
        subtitle_tracks=subtitle_tracks,
        audio_decision=empty_decision,
        subtitle_decision=empty_decision,
        plan=PlannedChange(
            container="mkv",
            action="skip",
            tool="mkvpropedit",
            selected_audio=None,
            selected_subtitle=None,
            confidence="none",
        ),
    )


def make_toolset(*, ffprobe: bool = True, mkvmerge: bool = True, mkvpropedit: bool = True, ffmpeg: bool = True) -> Toolset:
    values = {
        "ffprobe": ffprobe,
        "mkvmerge": mkvmerge,
        "mkvpropedit": mkvpropedit,
        "ffmpeg": ffmpeg,
    }
    return Toolset(
        {
            name: ToolInfo(name=name, path=f"C:/tools/{name}.exe" if found else None, found=found, source="test" if found else None)
            for name, found in values.items()
        }
    )


def test_subtitle_auto_selects_unique_french_candidate_even_without_title() -> None:
    tracks = [
        make_track(kind="subtitle", type_index=1, language="eng", title="English"),
        make_track(kind="subtitle", type_index=2, language="fr", title=""),
        make_track(kind="subtitle", type_index=3, language="spa", title="Spanish"),
    ]
    decision = select_subtitle_track(tracks, "fr")
    assert decision.status == "selected"
    assert decision.selected_type_index == 2
    assert decision.confidence == "high"


def test_subtitle_does_not_auto_select_when_two_plausible_french_candidates_exist() -> None:
    tracks = [
        make_track(kind="subtitle", type_index=1, language="fr", title=""),
        make_track(kind="subtitle", type_index=2, language="fr", title=""),
    ]
    decision = select_subtitle_track(tracks, "fr")
    assert decision.status == "ambiguous"


def test_subtitle_excludes_forced_and_sdh() -> None:
    tracks = [
        make_track(kind="subtitle", type_index=1, language="fr", title="forced", forced=True),
        make_track(kind="subtitle", type_index=2, language="fr", title="SDH"),
        make_track(kind="subtitle", type_index=3, language="fr", title="normal"),
    ]
    decision = select_subtitle_track(tracks, "fr")
    assert decision.selected_type_index == 3


def test_subtitle_prefers_classic_french_over_dubtitle() -> None:
    tracks = [
        make_track(kind="subtitle", type_index=26, language="fr", title=""),
        make_track(kind="subtitle", type_index=27, language="fre", title="Dubtitle"),
    ]
    decision = select_subtitle_track(tracks, "fr")
    assert decision.status == "selected"
    assert decision.selected_type_index == 26


def test_subtitle_dubtitle_alone_requires_confirmation() -> None:
    tracks = [make_track(kind="subtitle", type_index=27, language="fre", title="Dubtitle")]
    decision = select_subtitle_track(tracks, "fr")
    assert decision.status == "ambiguous"
    assert decision.manual_required is True


def test_subtitle_prefers_empty_classic_track_over_forced() -> None:
    tracks = [
        make_track(kind="subtitle", type_index=1, language="fr", title="forced", forced=True),
        make_track(kind="subtitle", type_index=2, language="fr", title=""),
    ]
    decision = select_subtitle_track(tracks, "fr")
    assert decision.status == "selected"
    assert decision.selected_type_index == 2


def test_subtitle_prefers_empty_classic_track_over_sdh() -> None:
    tracks = [
        make_track(kind="subtitle", type_index=1, language="fr", title="SDH"),
        make_track(kind="subtitle", type_index=2, language="fr", title=""),
    ]
    decision = select_subtitle_track(tracks, "fr")
    assert decision.status == "selected"
    assert decision.selected_type_index == 2


def test_fr_and_fre_are_treated_as_same_target_language() -> None:
    tracks = [make_track(kind="subtitle", type_index=1, language="fre", title="")]
    decision = select_subtitle_track(tracks, "fr")
    assert decision.status == "selected"
    assert decision.selected_type_index == 1


def test_relative_rule_uses_first_french_candidate_even_if_absolute_index_changes() -> None:
    source = evaluate_report(
        base_report(
            [make_track(kind="audio", type_index=1, language="fr", title="French", channels=2)],
            [
                make_track(kind="subtitle", type_index=20, language="fr", title=""),
                make_track(kind="subtitle", type_index=21, language="fr", title=""),
            ],
            path="Season1/E01.mkv",
        ),
        "fr",
        "fr",
    )
    selected_track = source.subtitle_tracks[0]
    rule = build_relative_subtitle_rule(source, selected_track, "fr")

    target = evaluate_report(
        base_report(
            [make_track(kind="audio", type_index=1, language="fr", title="French", channels=2)],
            [
                make_track(kind="subtitle", type_index=25, language="eng", title="English"),
                make_track(kind="subtitle", type_index=26, language="fr", title=""),
                make_track(kind="subtitle", type_index=27, language="fr", title=""),
            ],
            path="Season1/E02.mkv",
        ),
        "fr",
        "fr",
    )
    matched = apply_relative_subtitle_rule(target, rule)
    assert matched is not None
    assert matched.type_index == 26


def test_grouping_similar_episodes_ignores_total_subtitle_count_variation() -> None:
    report_a = evaluate_report(
        base_report(
            [make_track(kind="audio", type_index=1, language="fr", title="French", channels=2)],
            [
                make_track(kind="subtitle", type_index=10, language="fr", title=""),
                make_track(kind="subtitle", type_index=11, language="fr", title=""),
                make_track(kind="subtitle", type_index=12, language="eng", title="English"),
            ],
            path="ExampleSeason/Series.S01E01.mkv",
        ),
        "fr",
        "fr",
    )
    report_b = evaluate_report(
        base_report(
            [make_track(kind="audio", type_index=1, language="fr", title="French", channels=2)],
            [
                make_track(kind="subtitle", type_index=20, language="fr", title=""),
                make_track(kind="subtitle", type_index=21, language="fr", title=""),
                make_track(kind="subtitle", type_index=22, language="eng", title="English"),
                make_track(kind="subtitle", type_index=23, language="spa", title="Spanish"),
                make_track(kind="subtitle", type_index=24, language="de", title="German"),
            ],
            path="ExampleSeason/Series.S01E02.mkv",
        ),
        "fr",
        "fr",
    )
    groups = group_ambiguous_files([report_a, report_b], "fr")
    assert len(groups) == 1
    assert len(groups[0].reports) == 2


def test_grouping_twelve_episodes_same_release_with_different_subtitle_indexes() -> None:
    reports = []
    for episode in range(1, 13):
        report = evaluate_report(
            base_report(
                [make_track(kind="audio", type_index=1, language="fr", title="French", channels=2)],
                [
                    make_track(kind="subtitle", type_index=19 + episode, language="fr", title=""),
                    make_track(kind="subtitle", type_index=29 + episode, language="fr", title=""),
                    make_track(kind="subtitle", type_index=39 + episode, language="eng", title="English"),
                ],
                path=f"ExampleSeason/Series.S01E{episode:02d}.1080p.WEB-DL.mkv",
            ),
            "fr",
            "fr",
        )
        reports.append(report)

    groups = group_ambiguous_files(reports, "fr")
    assert len(groups) == 1
    assert len(groups[0].reports) == 12


def test_media_editor_handles_subprocess_failure_without_crash(monkeypatch) -> None:
    temp_dir = Path(tempfile.mkdtemp(dir=Path.cwd()))
    report = evaluate_report(
        base_report(
            [make_track(kind="audio", type_index=1, language="fr", title="French", channels=2)],
            [make_track(kind="subtitle", type_index=1, language="fr", title="normal")],
            path=str(temp_dir / "sample.mkv"),
        ),
        "fr",
        "fr",
    )
    report.path.write_text("dummy", encoding="utf-8")

    def fake_run_command(command: list[str], timeout: int = 600):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="Permission denied")

    monkeypatch.setattr("pistepilot.mkv_editor.run_command", fake_run_command)
    editor = MediaEditor(make_toolset(), logger=DummyLogger(), log_dir=temp_dir)
    try:
        success, message = editor.apply_report(report, dry_run=False)
        assert success is False
        assert "locked" in message
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_media_editor_handles_external_command_exception(monkeypatch) -> None:
    temp_dir = Path(tempfile.mkdtemp(dir=Path.cwd()))
    report = evaluate_report(
        base_report(
            [make_track(kind="audio", type_index=1, language="fr", title="French", channels=2)],
            [make_track(kind="subtitle", type_index=1, language="fr", title="normal")],
            path=str(temp_dir / "sample.mkv"),
        ),
        "fr",
        "fr",
    )
    report.path.write_text("dummy", encoding="utf-8")

    def fake_run_command(command: list[str], timeout: int = 600):
        raise ExternalCommandError(command, returncode=None, stderr="boom", message="Impossible")

    monkeypatch.setattr("pistepilot.mkv_editor.run_command", fake_run_command)
    editor = MediaEditor(make_toolset(), logger=DummyLogger(), log_dir=temp_dir)
    try:
        success, message = editor.apply_report(report, dry_run=False)
        assert success is False
        assert "Check the log" in message
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_pause_before_exit_does_not_call_input_in_gui_mode(monkeypatch) -> None:
    called = {"value": False}

    def fake_input(_prompt: str) -> str:
        called["value"] = True
        raise AssertionError("input should not be called in GUI mode")

    monkeypatch.setattr("builtins.input", fake_input)
    pause_before_exit_if_interactive(interactive_mode=True, gui_mode=True)
    assert called["value"] is False


def test_run_command_uses_create_no_window_in_gui_windows_mode(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(ffmpeg_tools.subprocess, "run", fake_run)
    monkeypatch.setattr(ffmpeg_tools.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(ffmpeg_tools.sys, "platform", "win32", raising=False)
    ffmpeg_tools.set_hide_subprocess_windows(True)
    try:
        ffmpeg_tools.run_command(["echo", "ok"])
    finally:
        ffmpeg_tools.set_hide_subprocess_windows(False)
    assert captured["creationflags"] == 0x08000000


def test_recursive_checkbox_defaults_to_unchecked() -> None:
    assert PistePilotGUI.default_recursive_value({}) is False
    assert PistePilotGUI.default_recursive_value({"recursive": True}) is True


def test_language_code_mapping_uses_readable_names() -> None:
    assert PistePilotGUI.friendly_language_name("fre") == "French"
    assert PistePilotGUI.friendly_language_name("es-419") == "Spanish"
    assert PistePilotGUI.friendly_language_name("ara") == "Arabic"
    assert PistePilotGUI.friendly_language_name("fil") == "Filipino"
    assert PistePilotGUI.friendly_language_name("hi") == "Hindi"


def test_language_normalization_collapses_variants() -> None:
    assert normalize_language_code("es-ES") == "es"
    assert normalize_language_code("es-419") == "es"
    assert normalize_language_code("fil") == "fil"
    assert normalize_language_code("hi") == "hi"


def test_dynamic_language_menus_include_detected_languages() -> None:
    gui = PistePilotGUI.__new__(PistePilotGUI)
    gui.detected_audio_codes = {"fr", "fre", "en", "ara"}
    gui.detected_subtitle_codes = {"fr", "fra", "de", "es-ES"}
    audio_values = gui._dynamic_audio_values()
    subtitle_values = gui._dynamic_subtitle_values()
    assert audio_values[:3] == ["French", "English", "Arabic"]
    assert "Arabic" in audio_values
    assert "French regular subtitles" in subtitle_values
    assert "German regular subtitles" in subtitle_values
    assert subtitle_values.count("French regular subtitles") == 1
    assert "Spanish regular subtitles" in subtitle_values


def test_user_friendly_labels_hide_track_ids_in_normal_mode() -> None:
    gui = PistePilotGUI.__new__(PistePilotGUI)
    gui.verbose_var = DummyVar(False)
    report = evaluate_report(
        base_report(
            [make_track(kind="audio", type_index=8, language="fr", title="")],
            [make_track(kind="subtitle", type_index=25, language="fr", title="")],
        ),
        "fr",
        "fr",
    )
    assert gui._selected_track_label(report, "audio") == "French"
    assert gui._selected_track_label(report, "subtitle") == "French regular subtitles"


def test_technical_labels_visible_only_in_verbose_mode() -> None:
    gui = PistePilotGUI.__new__(PistePilotGUI)
    gui.verbose_var = DummyVar(True)
    report = evaluate_report(
        base_report(
            [make_track(kind="audio", type_index=8, language="fr", title="")],
            [make_track(kind="subtitle", type_index=25, language="fr", title="")],
        ),
        "fr",
        "fr",
    )
    assert "a8" in gui._selected_track_label(report, "audio")
    assert "s25" in gui._selected_track_label(report, "subtitle")


def test_options_menu_labels_cover_logs_tools_bin_export_and_restore() -> None:
    for label in [
        "Check tools",
        "Open logs folder",
        "Open bin folder",
        "Export analysis report",
        "Restore from backup",
        "Show technical details",
        "Advanced preferences",
        "Reset app state",
        "About",
    ]:
        assert label in OPTIONS_MENU_LABELS


def test_prescan_runs_in_background_without_using_main_worker(monkeypatch) -> None:
    gui = PistePilotGUI.__new__(PistePilotGUI)
    gui.toolset = make_toolset()
    gui.logger = DummyLogger()
    gui.reports = []
    gui.task_queue = DummyQueue()
    gui.folder_var = DummyVar(str(Path.cwd()))
    gui.recursive_var = DummyVar(False)
    gui.prescan_token = 0
    gui.prescan_thread = None
    gui._is_busy = lambda: False
    started = {"value": False}

    class DummyThread:
        def __init__(self, target=None, daemon=None):
            self.target = target

        def start(self):
            started["value"] = True

    monkeypatch.setattr("pistepilot.gui.threading.Thread", DummyThread)
    gui.available_tracks_var = DummyVar("")
    gui.warning_var = DummyVar("")
    gui.start_prescan()
    assert started["value"] is True


def test_default_language_is_english() -> None:
    assert DEFAULT_LANGUAGE == "en"
    assert display_language_name("fr") == "French"


def test_package_version_is_public_beta() -> None:
    from pistepilot import __version__

    assert __version__ == "0.1.0-beta"


def test_gui_starts_with_placeholder_selector_text() -> None:
    assert CHOOSER_PLACEHOLDER == "Choose a folder first"


def test_license_exists_and_is_mit() -> None:
    content = Path("LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in content
    assert "PistePilot contributors" in content


def test_readme_is_public_release_ready() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "# PistePilot" in readme
    assert "MIT" in readme
    assert "FFmpeg" in readme
    assert "MKVToolNix" in readme
    assert "No media is re-encoded" in readme
    assert "No tracks are deleted" in readme


def test_profile_save_and_load(monkeypatch) -> None:
    temp_dir = Path(tempfile.mkdtemp(dir=Path.cwd()))
    monkeypatch.setattr("pistepilot.profiles.get_runtime_root", lambda: temp_dir)
    from pistepilot.profiles import ensure_profiles_file, load_profiles, save_profiles

    try:
        ensure_profiles_file()
        profile = ProfileSettings(**DEFAULT_PROFILE.to_dict())
        profile.name = "Test"
        profile.audio_language = "en"
        save_profiles([profile], last_used="Test")
        profiles, last_used = load_profiles()
        assert profiles[0].name == "Test"
        assert profiles[0].audio_language == "en"
        assert last_used == "Test"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_gui_action_states_disable_apply_while_confirmations_remain() -> None:
    gui = PistePilotGUI.__new__(PistePilotGUI)
    review_report = evaluate_report(
        base_report(
            [make_track(kind="audio", type_index=1, language="fr", title="French", channels=2)],
            [
                make_track(kind="subtitle", type_index=20, language="fr", title=""),
                make_track(kind="subtitle", type_index=21, language="fr", title=""),
            ],
        ),
        "fr",
        "fr",
    )
    gui.reports = [review_report]
    gui.apply_button = DummyButton()
    gui.resolve_button = DummyButton()
    gui.analyze_button = DummyButton()
    gui.audio_combo = DummyCombo()
    gui.subtitle_combo = DummyCombo()
    gui.tool_status_var = DummyVar()
    gui.folder_var = DummyVar(str(Path.cwd()))
    gui.audio_options = [DummyOption("fr", "French")]
    gui.subtitle_options = [DummyOption("fr", "French regular subtitles")]
    gui.view_languages_button = DummyButton()
    gui.detected_audio_codes = {"fr"}
    gui.detected_subtitle_codes = {"fr"}
    gui._is_busy = lambda: False
    gui._can_apply_with_current_tools = lambda: (True, None)
    gui._base_tool_status = lambda: "Tools: OK"
    gui._update_banner = lambda: None
    PistePilotGUI._refresh_action_states(gui)
    assert gui.apply_button.state == "disabled"
    assert gui.resolve_button.state == "normal"


def test_gui_action_states_disable_confirm_button_when_no_review_needed() -> None:
    gui = PistePilotGUI.__new__(PistePilotGUI)
    ready_report = evaluate_report(
        base_report(
            [make_track(kind="audio", type_index=1, language="fr", title="French", channels=2)],
            [make_track(kind="subtitle", type_index=1, language="fr", title="normal")],
        ),
        "fr",
        "fr",
    )
    gui.reports = [ready_report]
    gui.apply_button = DummyButton()
    gui.resolve_button = DummyButton()
    gui.analyze_button = DummyButton()
    gui.audio_combo = DummyCombo()
    gui.subtitle_combo = DummyCombo()
    gui.tool_status_var = DummyVar()
    gui.folder_var = DummyVar(str(Path.cwd()))
    gui.audio_options = [DummyOption("fr", "French")]
    gui.subtitle_options = [DummyOption("fr", "French regular subtitles")]
    gui.view_languages_button = DummyButton()
    gui.detected_audio_codes = {"fr"}
    gui.detected_subtitle_codes = {"fr"}
    gui._is_busy = lambda: False
    gui._can_apply_with_current_tools = lambda: (True, None)
    gui._base_tool_status = lambda: "Tools: OK"
    gui._update_banner = lambda: None
    PistePilotGUI._refresh_action_states(gui)
    assert gui.resolve_button.state == "disabled"
    assert gui.apply_button.state == "normal"


def test_gui_action_states_disable_selectors_before_folder_is_chosen() -> None:
    gui = PistePilotGUI.__new__(PistePilotGUI)
    gui.reports = []
    gui.apply_button = DummyButton()
    gui.resolve_button = DummyButton()
    gui.analyze_button = DummyButton()
    gui.audio_combo = DummyCombo()
    gui.subtitle_combo = DummyCombo()
    gui.tool_status_var = DummyVar()
    gui.folder_var = DummyVar("")
    gui.audio_options = []
    gui.subtitle_options = []
    gui.view_languages_button = DummyButton()
    gui._is_busy = lambda: False
    gui._can_apply_with_current_tools = lambda: (False, "No ready report is available.")
    gui._base_tool_status = lambda: "Tools: OK"
    gui._update_banner = lambda: None
    gui.detected_audio_codes = set()
    gui.detected_subtitle_codes = set()
    PistePilotGUI._refresh_action_states(gui)
    assert gui.analyze_button.state == "disabled"
    assert gui.audio_combo.state == "disabled"
    assert gui.subtitle_combo.state == "disabled"


def test_reset_app_state_clears_folder_reports_and_detected_languages() -> None:
    gui = PistePilotGUI.__new__(PistePilotGUI)
    gui.folder_var = DummyVar("D:/Videos")
    gui.warning_var = DummyVar("warning")
    gui.available_tracks_var = DummyVar("available")
    gui.progress_var = DummyVar("Working")
    gui.progress_value = DummyVar(50)
    gui.summary_vars = {name: DummyVar("1") for name in ["total", "ready", "review", "skipped", "errors"]}
    gui.detected_audio_codes = {"fr", "en"}
    gui.detected_subtitle_codes = {"fr"}
    gui.audio_options = [DummyOption("fr", "French")]
    gui.subtitle_options = [DummyOption("fr", "French regular subtitles")]
    gui.reports = [object()]
    gui.audio_combo = DummyCombo()
    gui.subtitle_combo = DummyCombo()
    gui.audio_var = DummyVar("French")
    gui.subtitle_var = DummyVar("French regular subtitles")
    gui.prescan_token = 0
    gui.tree = DummyTree()
    gui._refresh_tree = lambda: None
    gui._refresh_action_states = lambda: None
    PistePilotGUI._reset_analysis_state(gui, clear_folder=True)
    assert gui.folder_var.get() == ""
    assert gui.reports == []
    assert gui.detected_audio_codes == set()
    assert gui.detected_subtitle_codes == set()
    assert gui.audio_var.get() == CHOOSER_PLACEHOLDER
    assert gui.subtitle_var.get() == CHOOSER_PLACEHOLDER


def test_gui_state_save_does_not_persist_last_folder(monkeypatch) -> None:
    temp_dir = Path(tempfile.mkdtemp(dir=Path.cwd()))
    gui = PistePilotGUI.__new__(PistePilotGUI)
    gui.folder_var = DummyVar("D:/Videos")
    gui.recursive_var = DummyVar(True)
    monkeypatch.setattr(gui, "_gui_state_path", lambda: temp_dir / "gui_state.json")
    try:
        PistePilotGUI._save_gui_state(gui)
        payload = json.loads((temp_dir / "gui_state.json").read_text(encoding="utf-8"))
        assert "last_folder" not in payload
        assert payload["recursive"] is True
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_service_analyze_directory_without_gui(monkeypatch) -> None:
    temp_dir = Path(tempfile.mkdtemp(dir=Path.cwd()))
    media_file = temp_dir / "episode01.mkv"
    media_file.write_text("dummy", encoding="utf-8")

    analyzed_report = evaluate_report(
        base_report(
            [make_track(kind="audio", type_index=1, language="fr", title="French", channels=2)],
            [make_track(kind="subtitle", type_index=1, language="fr", title="normal")],
            path=str(media_file),
        ),
        "fr",
        "fr",
    )

    class FakeAnalyzer:
        def __init__(self, toolset, logger):
            self.toolset = toolset
            self.logger = logger

        def analyze_file(self, file_path: Path) -> MediaFileReport:
            return analyzed_report

    monkeypatch.setattr("pistepilot.services.MediaAnalyzer", FakeAnalyzer)
    try:
        reports = analyze_directory(
            temp_dir,
            profile=DEFAULT_PROFILE,
            recursive=True,
            toolset=make_toolset(),
            logger=DummyLogger(),
        )
        assert len(reports) == 1
        assert reports[0].plan.action == "apply"
        assert summarize_reports(reports).auto_applicable == 1
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_service_analyze_directory_returns_empty_for_empty_folder() -> None:
    temp_dir = Path(tempfile.mkdtemp(dir=Path.cwd()))
    try:
        reports = analyze_directory(
            temp_dir,
            profile=DEFAULT_PROFILE,
            recursive=True,
            toolset=make_toolset(),
            logger=DummyLogger(),
        )
        assert reports == []
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_service_analyze_directory_returns_empty_when_no_video_files() -> None:
    temp_dir = Path(tempfile.mkdtemp(dir=Path.cwd()))
    try:
        (temp_dir / "readme.txt").write_text("no video here", encoding="utf-8")
        reports = analyze_directory(
            temp_dir,
            profile=DEFAULT_PROFILE,
            recursive=True,
            toolset=make_toolset(),
            logger=DummyLogger(),
        )
        assert reports == []
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_export_analysis_report_json_and_csv() -> None:
    temp_dir = Path(tempfile.mkdtemp(dir=Path.cwd()))
    try:
        report = evaluate_report(
            base_report(
                [make_track(kind="audio", type_index=1, language="fr", title="French", channels=2)],
                [make_track(kind="subtitle", type_index=1, language="fr", title="normal")],
                path=str(temp_dir / "episode01.mkv"),
            ),
            "fr",
            "fr",
        )
        json_path, csv_path = export_analysis_reports([report], toolset=make_toolset(), logger=DummyLogger(), log_dir=temp_dir)
        assert json_path.exists()
        assert csv_path.exists()
        assert "episode01.mkv" in json_path.read_text(encoding="utf-8")
        assert "episode01.mkv" in csv_path.read_text(encoding="utf-8-sig")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_restore_from_backup_metadata(monkeypatch) -> None:
    temp_dir = Path(tempfile.mkdtemp(dir=Path.cwd()))
    backup_path = temp_dir / "original_state_test.json"
    video_path = temp_dir / "episode01.mkv"
    video_path.write_text("dummy", encoding="utf-8")
    backup_path.write_text(
        """
        {
          "created_at": "20260708_000000",
          "files": [
            {
              "path": "%s",
              "container": "mkv",
              "audio_tracks": [{"type_index": 1, "default": true, "forced": false, "dispositions": {"default": 1}}],
              "subtitle_tracks": [{"type_index": 1, "default": false, "forced": false, "dispositions": {"default": 0, "forced": 0}}]
            }
          ]
        }
        """ % str(video_path).replace("\\", "\\\\"),
        encoding="utf-8",
    )

    def fake_restore(self, file_path: Path, file_state: dict):
        return True, "ok"

    monkeypatch.setattr("pistepilot.mkv_editor.MediaEditor.restore_report_from_backup_state", fake_restore)
    try:
        restored, errors = restore_from_backup(
            backup_path,
            toolset=make_toolset(),
            logger=DummyLogger(),
            log_dir=temp_dir,
        )
        assert restored == 1
        assert errors == 0
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


class DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def exception(self, *args, **kwargs):
        return None

    def debug(self, *args, **kwargs):
        return None


class DummyButton:
    def __init__(self) -> None:
        self.state = None
        self.text = None

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]
        if "text" in kwargs:
            self.text = kwargs["text"]


class DummyCombo:
    def __init__(self) -> None:
        self.state = None
        self.values = []

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]
        if "values" in kwargs:
            self.values = kwargs["values"]


class DummyOption:
    def __init__(self, code: str, label: str) -> None:
        self.code = code
        self.label = label


class DummyTree:
    def get_children(self):
        return []

    def delete(self, _item):
        return None


class DummyVar:
    def __init__(self, value="") -> None:
        self.value = value

    def set(self, value):
        self.value = value

    def get(self):
        return self.value


class DummyQueue:
    def __init__(self) -> None:
        self.items = []

    def put(self, item):
        self.items.append(item)
