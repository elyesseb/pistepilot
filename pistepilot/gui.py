from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from threading import Event
from tkinter import filedialog, messagebox, simpledialog, ttk

from pistepilot.analyzer import MediaAnalyzer
from pistepilot.ffmpeg_tools import detect_tools, get_local_bin_dir, set_hide_subprocess_windows
from pistepilot.i18n import language_display_name, normalize_language_code, sort_language_codes, t
from pistepilot.logger import setup_logging
from pistepilot.models import AmbiguityGroup, LanguageOption, MediaFileReport, ProfileSettings
from pistepilot.profiles import get_config_dir, get_profile_by_name, load_profiles, save_last_used_profile, upsert_profile
from pistepilot.scanner import scan_video_files
from pistepilot.selector import (
    is_subtitle_commentary_like,
    is_subtitle_dubtitle_like,
    is_subtitle_forced_like,
    is_subtitle_sdh_like,
    language_profile_for,
    normalize_text,
    subtitle_candidates_for_language,
)
from pistepilot.services import (
    analyze_directory,
    apply_changes,
    apply_group_relative_choice,
    export_analysis_reports,
    resolve_groups,
    restore_from_backup,
    summarize_reports,
    validate_reports_before_apply,
)
from pistepilot.ui import open_path_with_system, pause_before_exit_if_interactive, select_directory_interactive


DEFAULT_AUDIO_CHOICES = [
    ("French", "fr"),
    ("English", "en"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
    ("Spanish", "es"),
    ("German", "de"),
    ("Italian", "it"),
    ("Arabic", "ar"),
    ("Portuguese", "pt"),
    ("Chinese", "zh"),
    ("Other...", "__custom__"),
]
OPTIONS_MENU_LABELS = [
    "Check tools",
    "Open logs folder",
    "Open bin folder",
    "Export analysis report",
    "Restore from backup",
    "Show technical details",
    "Advanced preferences",
    "Reset app state",
    "About",
]

CHOOSER_PLACEHOLDER = "Choose a folder first"
AVAILABLE_TRACKS_IDLE = ""
PRIORITY_AUDIO_CODE = "fr"
PRIORITY_SUBTITLE_CODE = "fr"


class PistePilotGUI:
    def __init__(self) -> None:
        set_hide_subprocess_windows(True)
        self.root = tk.Tk()
        self.root.title("PistePilot")
        self.root.minsize(1100, 720)
        self.root.geometry("1180x780")
        self._configure_style()

        self.logger, self.log_dir, self.log_file = setup_logging(verbose=False)
        self.toolset = detect_tools()
        self.profiles, self.last_profile_name = load_profiles()
        self.profile = get_profile_by_name(self.last_profile_name)
        self.reports: list[MediaFileReport] = []
        self.audio_options: list[LanguageOption] = []
        self.subtitle_options: list[LanguageOption] = []
        self.task_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event: Event | None = None
        self.prescan_thread: threading.Thread | None = None
        self.prescan_token = 0
        self.log_lines: list[str] = []
        self.max_log_lines = 5
        self.detected_audio_codes: set[str] = set()
        self.detected_subtitle_codes: set[str] = set()

        gui_state = self._load_gui_state()
        self.folder_var = tk.StringVar(value="")
        self.recursive_var = tk.BooleanVar(value=self.default_recursive_value(gui_state))
        self.audio_var = tk.StringVar(value=CHOOSER_PLACEHOLDER)
        self.subtitle_var = tk.StringVar(value=CHOOSER_PLACEHOLDER)
        self.profile_var = tk.StringVar(value=self.profile.name)
        self.verbose_var = tk.BooleanVar(value=False)
        self.progress_var = tk.StringVar(value="Ready")
        self.progress_value = tk.DoubleVar(value=0)
        self.tool_status_var = tk.StringVar(value="")
        self.available_tracks_var = tk.StringVar(value=AVAILABLE_TRACKS_IDLE)
        self.warning_var = tk.StringVar(value="")
        self.banner_var = tk.StringVar(value="Choose a video folder, then start the analysis.")

        self.summary_vars = {
            "total": tk.StringVar(value="0"),
            "ready": tk.StringVar(value="0"),
            "review": tk.StringVar(value="0"),
            "skipped": tk.StringVar(value="0"),
            "errors": tk.StringVar(value="0"),
        }

        self._build_layout()
        self._configure_choice_values()
        self._refresh_profile_list()
        self._refresh_tool_status()
        self._refresh_action_states()
        self.verbose_var.trace_add("write", lambda *_args: self._refresh_tree())
        self.recursive_var.trace_add("write", lambda *_args: self._save_gui_state())
        self._poll_queue()

    def _build_layout(self) -> None:
        self.root.configure(background="#f6f7fb")
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(4, weight=1)
        main.rowconfigure(5, weight=0)

        top = ttk.LabelFrame(main, text="Video folder")
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(1, weight=1)
        self.choose_folder_button = ttk.Button(top, text="Choose folder", command=self.choose_folder, style="Primary.TButton")
        self.choose_folder_button.grid(row=0, column=0, padx=8, pady=8)
        self.folder_entry = ttk.Entry(top, textvariable=self.folder_var)
        self.folder_entry.grid(row=0, column=1, sticky="ew", padx=8, pady=8)
        ttk.Checkbutton(top, text="Include subfolders", variable=self.recursive_var).grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))

        params = ttk.LabelFrame(main, text="Track preferences")
        params.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        for col in range(2):
            params.columnconfigure(col, weight=1)
        ttk.Label(params, text="Audio").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.audio_combo = ttk.Combobox(params, state="disabled", textvariable=self.audio_var)
        self.audio_combo.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        self.audio_combo.bind("<<ComboboxSelected>>", lambda _event: self.on_audio_selected())
        ttk.Label(params, text="Subtitles").grid(row=0, column=1, sticky="w", padx=8, pady=4)
        self.subtitle_combo = ttk.Combobox(params, state="disabled", textvariable=self.subtitle_var)
        self.subtitle_combo.grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        self.subtitle_combo.bind("<<ComboboxSelected>>", lambda _event: self.on_subtitle_selected())
        available_frame = ttk.Frame(params)
        available_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 4))
        available_frame.columnconfigure(0, weight=1)
        self.available_tracks_label = ttk.Label(available_frame, textvariable=self.available_tracks_var, justify="left")
        self.available_tracks_label.grid(row=0, column=0, sticky="w")
        self.view_languages_button = ttk.Button(
            available_frame,
            text="View all languages",
            command=self.show_all_languages,
            state="disabled",
        )
        self.view_languages_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.warning_label = ttk.Label(params, textvariable=self.warning_var, wraplength=900, justify="left", foreground="#a55b00")
        self.warning_label.grid(row=3, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))

        actions = ttk.LabelFrame(main, text="Actions")
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        for col in range(4):
            actions.columnconfigure(col, weight=1)
        self.analyze_button = ttk.Button(
            actions,
            text="Analyze folder",
            command=self.start_analysis,
            style="Primary.TButton",
            state="disabled",
        )
        self.analyze_button.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        self.resolve_button = ttk.Button(actions, text="Confirm subtitles", command=self.open_resolver, state="disabled")
        self.resolve_button.grid(row=0, column=1, sticky="ew", padx=6, pady=6)
        self.apply_button = ttk.Button(actions, text="Apply", command=self.start_apply, state="disabled")
        self.apply_button.grid(row=0, column=2, sticky="ew", padx=6, pady=6)
        options_button = ttk.Menubutton(actions, text="Options")
        options_button.grid(row=0, column=3, sticky="ew", padx=6, pady=6)
        options_menu = tk.Menu(options_button, tearoff=False)
        options_menu.add_command(label="Check tools", command=self.show_tools)
        options_menu.add_command(label="Open logs folder", command=self.open_logs_folder)
        options_menu.add_command(label="Open bin folder", command=self.open_bin_folder)
        options_menu.add_command(label="Export analysis report", command=self.export_reports)
        options_menu.add_command(label="Restore from backup", command=self.restore_backup)
        options_menu.add_separator()
        options_menu.add_checkbutton(label="Show technical details", variable=self.verbose_var)
        options_menu.add_command(label="Advanced preferences", command=self.save_profile)
        options_menu.add_command(label="Reset app state", command=self.reset_app_state)
        options_menu.add_command(label="About", command=self.show_about)
        options_button["menu"] = options_menu
        self.options_button = options_button
        self.options_menu = options_menu
        ttk.Label(actions, textvariable=self.tool_status_var, anchor="w", justify="left").grid(row=1, column=0, columnspan=4, sticky="ew", padx=6, pady=(0, 6))

        summary = ttk.LabelFrame(main, text="Summary")
        summary.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        summary.columnconfigure(0, weight=1)
        self.banner_label = tk.Label(
            summary,
            textvariable=self.banner_var,
            anchor="w",
            justify="left",
            bg="#e8f5e9",
            fg="#1b5e20",
            padx=12,
            pady=10,
            font=("Segoe UI", 10, "bold"),
        )
        self.banner_label.grid(row=0, column=0, columnspan=5, sticky="ew", padx=8, pady=(0, 8))
        for col in range(5):
            summary.columnconfigure(col, weight=1)
        self._summary_card(summary, 0, "Files found", self.summary_vars["total"], row=1)
        self._summary_card(summary, 1, "Ready", self.summary_vars["ready"], row=1)
        self._summary_card(summary, 2, "To confirm", self.summary_vars["review"], row=1)
        self._summary_card(summary, 3, "Skipped", self.summary_vars["skipped"], row=1)
        self._summary_card(summary, 4, "Errors", self.summary_vars["errors"], row=1)

        table_frame = ttk.LabelFrame(main, text="Files")
        table_frame.grid(row=4, column=0, sticky="nsew", pady=(0, 8))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("status", "file", "audio", "subtitle", "message")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        for column, label, width in [
            ("status", "Status", 100),
            ("file", "File", 380),
            ("audio", "Audio", 120),
            ("subtitle", "Subtitles", 170),
            ("message", "Result", 220),
        ]:
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", self.show_selected_report_details)
        tree_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tree_scroll.set)

        logs = ttk.LabelFrame(main, text="Recent activity")
        logs.grid(row=5, column=0, sticky="nsew")
        logs.columnconfigure(0, weight=1)
        ttk.Button(logs, text="Open logs folder", command=self.open_logs_folder).grid(row=0, column=1, padx=(8, 0), pady=(0, 6), sticky="e")
        self.log_text = tk.Text(logs, height=3, state="disabled", wrap="word", relief="flat", background="#ffffff")
        self.log_text.grid(row=0, column=0, sticky="nsew")

        status_frame = ttk.Frame(main)
        status_frame.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        status_frame.columnconfigure(0, weight=1)
        status_frame.columnconfigure(1, weight=0)
        ttk.Label(status_frame, textvariable=self.progress_var).grid(row=0, column=0, sticky="w")
        self.cancel_button = ttk.Button(status_frame, text="Cancel", command=self.cancel_current_task, state="disabled")
        self.cancel_button.grid(row=0, column=1, sticky="e")
        self.progress = ttk.Progressbar(status_frame, mode="determinate", variable=self.progress_value, maximum=100)
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))

    def _summary_card(self, parent: ttk.LabelFrame, column: int, title: str, variable: tk.StringVar, *, row: int = 0) -> None:
        frame = ttk.Frame(parent, padding=8)
        frame.grid(row=row, column=column, sticky="ew")
        ttk.Label(frame, text=title).pack(anchor="center")
        ttk.Label(frame, textvariable=variable, font=("Segoe UI", 14, "bold")).pack(anchor="center")

    def _configure_style(self) -> None:
        self.root.option_add("*Font", "{Segoe UI} 10")
        style = ttk.Style(self.root)
        try:
            if "vista" in style.theme_names():
                style.theme_use("vista")
            elif "clam" in style.theme_names():
                style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("TFrame", background="#f6f7fb")
        style.configure("TLabelframe", background="#f6f7fb", padding=10)
        style.configure("TLabelframe.Label", font=("Segoe UI", 11, "bold"))
        style.configure("TLabel", background="#f6f7fb")
        style.configure("TButton", padding=(12, 8))
        style.configure("Primary.TButton", padding=(14, 9))
        style.configure("Treeview", rowheight=28)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    def _gui_state_path(self) -> Path:
        return get_config_dir() / "gui_state.json"

    def _load_gui_state(self) -> dict:
        path = self._gui_state_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def default_recursive_value(gui_state: dict) -> bool:
        return bool(gui_state.get("recursive", False))

    def _save_gui_state(self) -> None:
        payload = {
            "recursive": bool(self.recursive_var.get()),
        }
        self._gui_state_path().write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def friendly_language_name(code: str) -> str:
        return language_display_name(code)

    @staticmethod
    def canonical_language_code(code: str) -> str:
        return normalize_language_code(code)

    @staticmethod
    def _sorted_language_codes(codes: set[str] | list[str] | tuple[str, ...]) -> list[str]:
        return sort_language_codes(codes)

    @classmethod
    def _default_audio_values(cls) -> list[str]:
        return [label for label, _code in DEFAULT_AUDIO_CHOICES]

    @classmethod
    def _default_subtitle_values(cls) -> list[str]:
        return [f"{label} regular subtitles" for label, code in DEFAULT_AUDIO_CHOICES if code != "__custom__"] + ["Other..."]

    def _audio_label_for_code(self, code: str) -> str:
        return self.friendly_language_name(code)

    def _subtitle_label_for_code(self, code: str) -> str:
        return f"{self.friendly_language_name(code)} regular subtitles"

    def _code_for_audio_label(self, label: str) -> str:
        option = self._find_option_by_label(self.audio_options, label)
        if option is not None:
            return option.code
        if label == "Other...":
            return PRIORITY_AUDIO_CODE
        return self.canonical_language_code(label) or PRIORITY_AUDIO_CODE

    def _code_for_subtitle_label(self, label: str) -> str:
        option = self._find_option_by_label(self.subtitle_options, label)
        if option is not None:
            return option.code
        if label == "Other...":
            return PRIORITY_SUBTITLE_CODE
        base = normalize_text(label.replace("regular subtitles", "").strip())
        return self.canonical_language_code(base) or PRIORITY_SUBTITLE_CODE

    def _configure_choice_values(self) -> None:
        self._apply_empty_selector_state()

    def _dynamic_audio_values(self) -> list[str]:
        return [option.label for option in self._build_audio_options()]

    def _dynamic_subtitle_values(self) -> list[str]:
        return [option.label for option in self._build_subtitle_options()]

    def _refresh_language_choices(self) -> None:
        self.audio_options = self._build_audio_options()
        self.subtitle_options = self._build_subtitle_options()
        if not self.folder_var.get().strip():
            self._apply_empty_selector_state()
            return

        audio_values = [option.label for option in self.audio_options]
        subtitle_values = [option.label for option in self.subtitle_options]
        self.audio_combo.configure(values=audio_values, state="readonly")
        self.subtitle_combo.configure(values=subtitle_values, state="readonly")

        selected_audio_code = self._preferred_audio_code()
        selected_subtitle_code = self._preferred_subtitle_code()
        self.audio_var.set(self._label_for_code(self.audio_options, selected_audio_code) or audio_values[0])
        self.subtitle_var.set(self._label_for_code(self.subtitle_options, selected_subtitle_code) or subtitle_values[0])

    def _available_tracks_label(self) -> str:
        if not self.folder_var.get().strip():
            return AVAILABLE_TRACKS_IDLE
        if not self.detected_audio_codes and not self.detected_subtitle_codes:
            return AVAILABLE_TRACKS_IDLE
        audio_summary = self._compact_language_summary(self.detected_audio_codes)
        subtitle_summary = self._compact_language_summary(self.detected_subtitle_codes)
        return f"Available in this folder: Audio: {audio_summary} | Subtitles: {subtitle_summary}"

    @staticmethod
    def _compact_language_summary(codes: set[str]) -> str:
        ordered = PistePilotGUI._sorted_language_codes(codes)
        if not ordered:
            return "-"
        labels = [PistePilotGUI.friendly_language_name(code) for code in ordered]
        if len(labels) <= 4:
            return ", ".join(labels)
        return f"{', '.join(labels[:4])} + {len(labels) - 4} more"

    @staticmethod
    def _find_option_by_label(options: list[LanguageOption], label: str) -> LanguageOption | None:
        normalized = normalize_text(label)
        for option in options:
            if normalize_text(option.label) == normalized:
                return option
        return None

    @staticmethod
    def _label_for_code(options: list[LanguageOption], code: str) -> str | None:
        for option in options:
            if option.code == code:
                return option.label
        return None

    def _build_audio_options(self, *, fallback_defaults: bool = False) -> list[LanguageOption]:
        codes = self._sorted_language_codes(self.detected_audio_codes)
        if not codes and fallback_defaults:
            codes = [code for _label, code in DEFAULT_AUDIO_CHOICES if code != "__custom__"]
        options = [
            LanguageOption(code=code, label=self.friendly_language_name(code), detected=code in self.detected_audio_codes)
            for code in codes
        ]
        options.append(LanguageOption(code="__custom__", label="Other...", detected=False))
        return options or [LanguageOption(code="__custom__", label="Other...", detected=False)]

    def _build_subtitle_options(self, *, fallback_defaults: bool = False) -> list[LanguageOption]:
        codes = self._sorted_language_codes(self.detected_subtitle_codes)
        if not codes and fallback_defaults:
            codes = [code for _label, code in DEFAULT_AUDIO_CHOICES if code not in {"__custom__"}]
        options = [
            LanguageOption(
                code=code,
                label=f"{self.friendly_language_name(code)} regular subtitles",
                detected=code in self.detected_subtitle_codes,
            )
            for code in codes
        ]
        options.append(LanguageOption(code="__custom__", label="Other...", detected=False))
        return options or [LanguageOption(code="__custom__", label="Other...", detected=False)]

    def _apply_empty_selector_state(self) -> None:
        placeholder = [CHOOSER_PLACEHOLDER]
        self.audio_options = []
        self.subtitle_options = []
        self.audio_combo.configure(values=placeholder, state="disabled")
        self.subtitle_combo.configure(values=placeholder, state="disabled")
        self.audio_var.set(CHOOSER_PLACEHOLDER)
        self.subtitle_var.set(CHOOSER_PLACEHOLDER)

    def _preferred_audio_code(self) -> str:
        available = [option.code for option in self.audio_options if option.code != "__custom__"]
        if not available:
            return PRIORITY_AUDIO_CODE
        if PRIORITY_AUDIO_CODE in available:
            return PRIORITY_AUDIO_CODE
        return available[0]

    def _preferred_subtitle_code(self) -> str:
        available = [option.code for option in self.subtitle_options if option.code != "__custom__"]
        if not available:
            return PRIORITY_SUBTITLE_CODE
        if PRIORITY_SUBTITLE_CODE in available:
            return PRIORITY_SUBTITLE_CODE
        return available[0]

    def _update_language_warning(self) -> None:
        warnings: list[str] = []
        if not self.folder_var.get().strip():
            self.warning_var.set("")
            return
        selected_audio_code = self._code_for_audio_label(self.audio_var.get())
        selected_subtitle_code = self._code_for_subtitle_label(self.subtitle_var.get())
        if self.detected_audio_codes and PRIORITY_AUDIO_CODE not in self.detected_audio_codes:
            warnings.append("French audio was not detected in this folder.")
        if self.detected_subtitle_codes and PRIORITY_SUBTITLE_CODE not in self.detected_subtitle_codes:
            warnings.append("French subtitles were not detected in this folder.")
        if self.detected_audio_codes and selected_audio_code not in self.detected_audio_codes:
            warnings.append(f"No {self.friendly_language_name(selected_audio_code).lower()} audio track was detected in this folder.")
        if self.detected_subtitle_codes and selected_subtitle_code not in self.detected_subtitle_codes:
            warnings.append(f"No {self.friendly_language_name(selected_subtitle_code).lower()} subtitles were detected in this folder.")
        self.warning_var.set(" ".join(warnings))

    def _update_banner(self) -> None:
        if not hasattr(self, "banner_var") or not hasattr(self, "banner_label"):
            return
        summary = summarize_reports(self.reports)
        if not self.folder_var.get().strip():
            self.banner_var.set("Choose a video folder to get started.")
            self.banner_label.configure(bg="#e8eef9", fg="#164277")
            return
        if self._is_busy():
            self.banner_var.set(self.progress_var.get())
            self.banner_label.configure(bg="#e8eef9", fg="#164277")
            return
        if summary.total_files == 0:
            self.banner_var.set("Choose your audio and subtitle preferences, then analyze the folder.")
            self.banner_label.configure(bg="#e8eef9", fg="#164277")
            return
        if summary.errors:
            self.banner_var.set(f"{summary.errors} file(s) could not be analyzed. See Options > Logs for details.")
            self.banner_label.configure(bg="#fff3e0", fg="#9a4d00")
            return
        if summary.needs_review:
            self.banner_var.set(f"{summary.needs_review} file(s) need your confirmation. Click \"Confirm subtitles\".")
            self.banner_label.configure(bg="#fff8e1", fg="#8a6d00")
            return
        self.banner_var.set(
            f"Everything is ready.\nPistePilot can apply {self.audio_var.get()} audio and {self.subtitle_var.get()} to {summary.auto_applicable} file(s)."
        )
        self.banner_label.configure(bg="#e8f5e9", fg="#1b5e20")

    def _refresh_profile_list(self) -> None:
        self.profiles, self.last_profile_name = load_profiles()
        names = [profile.name for profile in self.profiles]
        if self.profile.name not in names and names:
            self.profile = get_profile_by_name(names[0])
        if hasattr(self, "profile_combo"):
            self.profile_combo.configure(values=names)
        self.profile_var.set(self.profile.name)

    def _current_profile(self) -> ProfileSettings:
        audio_code = self._code_for_audio_label(self.audio_var.get()) if self.audio_var.get() != CHOOSER_PLACEHOLDER else self.profile.audio_language
        subtitle_code = (
            self._code_for_subtitle_label(self.subtitle_var.get()) if self.subtitle_var.get() != CHOOSER_PLACEHOLDER else self.profile.subtitle_language
        )
        return ProfileSettings(
            name=self.profile_var.get() or self.profile.name,
            audio_language=audio_code,
            subtitle_language=subtitle_code,
            subtitle_policy=self.profile.subtitle_policy,
            auto_apply_unique_candidate=self.profile.auto_apply_unique_candidate,
            auto_group_series=self.profile.auto_group_series,
            prefer_srt_over_pgs=self.profile.prefer_srt_over_pgs,
            exclude_forced=self.profile.exclude_forced,
            exclude_sdh=self.profile.exclude_sdh,
            exclude_commentary=self.profile.exclude_commentary,
            exclude_dubtitle=self.profile.exclude_dubtitle,
        )

    def _is_busy(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def _ready_reports(self) -> list[MediaFileReport]:
        return [report for report in self.reports if report.is_ready()]

    def _refresh_tool_status(self) -> None:
        self.tool_status_var.set(self._base_tool_status())

    def _base_tool_status(self) -> str:
        return (
            "Tools: "
            f"ffmpeg={'OK' if self.toolset.is_available('ffmpeg') else 'Missing'} | "
            f"ffprobe={'OK' if self.toolset.is_available('ffprobe') else 'Missing'} | "
            f"mkvmerge={'OK' if self.toolset.is_available('mkvmerge') else 'Missing'} | "
            f"mkvpropedit={'OK' if self.toolset.is_available('mkvpropedit') else 'Missing'}"
        )

    def _can_apply_with_current_tools(self) -> tuple[bool, str | None]:
        ready_reports = self._ready_reports()
        if not ready_reports:
            return False, "No ready report is available."
        if any(report.container == "mkv" for report in ready_reports) and not self.toolset.is_available("mkvpropedit"):
            return False, "MKVToolNix is missing: MKV updates are disabled."
        if any(report.container != "mkv" for report in ready_reports) and not self.toolset.is_available("ffmpeg"):
            return False, "FFmpeg is missing: updates for some formats are disabled."
        return True, None

    def _refresh_action_states(self) -> None:
        summary = summarize_reports(self.reports)
        can_apply, reason = self._can_apply_with_current_tools()
        has_folder = bool(self.folder_var.get().strip())
        can_apply_now = can_apply and summary.needs_review == 0 and not self._is_busy()
        if hasattr(self, "analyze_button"):
            self.analyze_button.configure(state="disabled" if self._is_busy() or not has_folder else "normal")
        if hasattr(self, "audio_combo") and hasattr(self, "subtitle_combo"):
            selector_state = "readonly" if has_folder and not self._is_busy() and self.audio_options and self.subtitle_options else "disabled"
            self.audio_combo.configure(state=selector_state)
            self.subtitle_combo.configure(state=selector_state)
        self.apply_button.configure(state="normal" if can_apply_now else "disabled")
        self.resolve_button.configure(state="normal" if summary.needs_review > 0 and not self._is_busy() else "disabled")
        if summary.auto_applicable > 0 and summary.needs_review == 0:
            self.apply_button.configure(text=f"Apply to {summary.auto_applicable} files")
        else:
            self.apply_button.configure(text="Apply")
        self.resolve_button.configure(
            text=f"Confirm subtitles ({summary.needs_review})" if summary.needs_review > 0 else "Confirm subtitles"
        )
        status = self._base_tool_status()
        if reason:
            self.tool_status_var.set(f"{status} | {reason}")
        elif summary.needs_review > 0:
            self.tool_status_var.set(f"{status} | Subtitle confirmation is still required.")
        else:
            self.tool_status_var.set(status)
        if hasattr(self, "view_languages_button"):
            has_detected = bool(self.detected_audio_codes or self.detected_subtitle_codes)
            self.view_languages_button.configure(state="normal" if has_detected else "disabled")
        self._update_banner()

    def _append_log(self, message: str) -> None:
        self.log_lines.append(message)
        if len(self.log_lines) > self.max_log_lines:
            self.log_lines = self.log_lines[-self.max_log_lines :]
        if not hasattr(self, "log_text"):
            return
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "\n".join(self.log_lines) + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _start_worker(self, target) -> None:
        if self._is_busy():
            return
        self.cancel_event = Event()
        self.cancel_button.configure(state="normal")
        self.progress_value.set(0)
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()
        self._refresh_action_states()

    def _update_summary(self) -> None:
        summary = summarize_reports(self.reports)
        self.summary_vars["total"].set(str(summary.total_files))
        self.summary_vars["ready"].set(str(summary.auto_applicable))
        self.summary_vars["review"].set(str(summary.needs_review))
        self.summary_vars["skipped"].set(str(summary.skipped))
        self.summary_vars["errors"].set(str(summary.errors))
        self._update_language_warning()
        self._refresh_action_states()

    def _refresh_tree(self) -> None:
        if not hasattr(self, "tree"):
            return
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, report in enumerate(self.reports):
            message = self._result_label(report)
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    self._status_label(report),
                    report.path.name,
                    self._selected_track_label(report, "audio"),
                    self._selected_track_label(report, "subtitle"),
                    message,
                ),
            )

    def _selected_track_label(self, report: MediaFileReport, kind: str) -> str:
        selected_index = report.plan.selected_audio if kind == "audio" else report.plan.selected_subtitle
        tracks = report.audio_tracks if kind == "audio" else report.subtitle_tracks
        if selected_index is None:
            return "No subtitles" if kind == "subtitle" else "-"
        for track in tracks:
            if track.type_index == selected_index:
                if self.verbose_var.get():
                    title = track.title or "empty title"
                    return f"{track.short_label()} | {title}"
                if kind == "audio":
                    return self.friendly_language_name(track.language_ietf or track.language or "")
                if is_subtitle_forced_like(track):
                    return f"{self.friendly_language_name(track.language_ietf or track.language or '')} forced subtitles only"
                if is_subtitle_sdh_like(track):
                    return f"{self.friendly_language_name(track.language_ietf or track.language or '')} SDH / hearing impaired"
                if is_subtitle_dubtitle_like(track):
                    return f"{self.friendly_language_name(track.language_ietf or track.language or '')} Dubtitle"
                return f"{self.friendly_language_name(track.language_ietf or track.language or '')} regular subtitles"
        prefix = "a" if kind == "audio" else "s"
        return f"{prefix}{selected_index}" if self.verbose_var.get() else ("No subtitles" if kind == "subtitle" else "-")

    def _status_label(self, report: MediaFileReport) -> str:
        mapping = {
            "apply": "Ready",
            "review": "To confirm",
            "skip": "Skipped",
            "error": "Error",
        }
        return mapping.get(report.plan.action, report.plan.action)

    def _result_label(self, report: MediaFileReport) -> str:
        if self.verbose_var.get():
            return report.plan.error or ", ".join(report.plan.notes) or "-"
        if report.plan.action == "apply":
            return "Ready"
        if report.plan.action == "review":
            return "To confirm"
        if report.plan.action == "skip":
            return "Skipped"
        if report.plan.action == "error":
            return "Error"
        return report.plan.action

    def _update_detected_languages(self) -> None:
        self.detected_audio_codes = {
            self.canonical_language_code(track.language_ietf or track.language or "")
            for report in self.reports
            for track in report.audio_tracks
            if self.canonical_language_code(track.language_ietf or track.language or "")
        }
        self.detected_subtitle_codes = {
            self.canonical_language_code(track.language_ietf or track.language or "")
            for report in self.reports
            for track in report.subtitle_tracks
            if self.canonical_language_code(track.language_ietf or track.language or "")
        }
        self.available_tracks_var.set(self._available_tracks_label())
        self._refresh_language_choices()
        self._update_language_warning()

    def _build_apply_confirmation_text(self) -> str:
        ready_reports = validate_reports_before_apply(self.reports)
        audio_label = self.audio_var.get()
        subtitle_label = self.subtitle_var.get()
        lines = [
            "Summary before applying changes",
            "",
            f"Files to update: {len(ready_reports)}",
            f"Audio: {audio_label}",
            f"Subtitles: {subtitle_label}",
            "Avoided tracks: Dubtitle, Forced, SDH",
            "",
            "No tracks will be removed.",
            "No media will be re-encoded.",
            "A metadata backup will be created.",
            "",
            "Files that will be updated:",
        ]
        for report in ready_reports:
            lines.append(
                f"- {report.path.name} | {self._selected_track_label(report, 'audio')} | {self._selected_track_label(report, 'subtitle')}"
            )
        lines.append("")
        lines.append("Continue?")
        return "\n".join(lines)

    def _prepare_for_new_folder(self, folder: Path) -> None:
        self.folder_var.set(str(folder))
        self.log_lines = []
        self._reset_analysis_state(clear_folder=False)
        self._save_gui_state()
        self._append_log(f"Folder selected: {folder}")
        self.available_tracks_var.set("Scanning available tracks...")
        self._refresh_action_states()

    def _reset_analysis_state(self, *, clear_folder: bool) -> None:
        self.reports = []
        self.detected_audio_codes = set()
        self.detected_subtitle_codes = set()
        self.audio_options = []
        self.subtitle_options = []
        self.prescan_token += 1
        if clear_folder:
            self.folder_var.set("")
        self.warning_var.set("")
        self.available_tracks_var.set(AVAILABLE_TRACKS_IDLE)
        self.progress_var.set("Ready")
        self.progress_value.set(0)
        for variable in self.summary_vars.values():
            variable.set("0")
        self._apply_empty_selector_state()
        self._refresh_tree()
        self._refresh_action_states()

    def reset_app_state(self) -> None:
        self.log_lines = []
        self._append_log("Application state reset.")
        self._reset_analysis_state(clear_folder=True)

    def show_about(self) -> None:
        messagebox.showinfo(
            "About PistePilot",
            "PistePilot\n\nBatch tool to set preferred default audio and subtitle tracks.\n\nGUI-first workflow for Windows with a preserved CLI for advanced use.",
            parent=self.root,
        )

    def show_all_languages(self) -> None:
        if not (self.detected_audio_codes or self.detected_subtitle_codes):
            messagebox.showinfo("Available languages", "No detected language list is available yet.", parent=self.root)
            return
        audio = "\n".join(f"- {self.friendly_language_name(code)}" for code in self._sorted_language_codes(self.detected_audio_codes)) or "- None"
        subtitles = "\n".join(
            f"- {self.friendly_language_name(code)}" for code in self._sorted_language_codes(self.detected_subtitle_codes)
        ) or "- None"
        messagebox.showinfo(
            "Available languages",
            f"Audio\n{audio}\n\nSubtitles\n{subtitles}",
            parent=self.root,
        )

    def on_audio_selected(self) -> None:
        selected = self.audio_var.get()
        if selected == "Other...":
            value = simpledialog.askstring("Custom audio language", "Language code", parent=self.root)
            if value:
                code = self.canonical_language_code(value) or normalize_text(value)
                self.audio_var.set(self.friendly_language_name(code))
            else:
                self.audio_var.set(self._label_for_code(self.audio_options, self._preferred_audio_code()) or CHOOSER_PLACEHOLDER)
        self._update_language_warning()

    def on_subtitle_selected(self) -> None:
        selected = self.subtitle_var.get()
        if selected == "Other...":
            value = simpledialog.askstring("Custom subtitle language", "Language code", parent=self.root)
            if value:
                code = self.canonical_language_code(value) or normalize_text(value)
                self.subtitle_var.set(f"{self.friendly_language_name(code)} regular subtitles")
            else:
                self.subtitle_var.set(self._label_for_code(self.subtitle_options, self._preferred_subtitle_code()) or CHOOSER_PLACEHOLDER)
        self._update_language_warning()

    def choose_folder(self) -> None:
        selected = select_directory_interactive()
        if selected is not None:
            self._prepare_for_new_folder(selected)
            self.start_prescan()

    def start_prescan(self) -> None:
        folder_value = self.folder_var.get().strip()
        if not folder_value or self._is_busy():
            return

        self.prescan_token += 1
        token = self.prescan_token
        self.available_tracks_var.set("Scanning available tracks...")
        self.warning_var.set("")

        def worker() -> None:
            try:
                files = scan_video_files(Path(folder_value), recursive=self.recursive_var.get())[:5]
                if not files:
                    self.task_queue.put(("prescan_done", (token, set(), set())))
                    return
                if not (self.toolset.is_available("mkvmerge") or self.toolset.is_available("ffprobe")):
                    self.task_queue.put(("prescan_error", "Analysis tools are unavailable for the quick language scan."))
                    return
                analyzer = MediaAnalyzer(self.toolset, self.logger)
                audio_codes: set[str] = set()
                subtitle_codes: set[str] = set()
                for file_path in files:
                    report = analyzer.analyze_file(file_path)
                    audio_codes.update(
                        self.canonical_language_code(track.language_ietf or track.language or "")
                        for track in report.audio_tracks
                        if self.canonical_language_code(track.language_ietf or track.language or "")
                    )
                    subtitle_codes.update(
                        self.canonical_language_code(track.language_ietf or track.language or "")
                        for track in report.subtitle_tracks
                        if self.canonical_language_code(track.language_ietf or track.language or "")
                    )
                self.task_queue.put(("prescan_done", (token, audio_codes, subtitle_codes)))
            except Exception as exc:
                self.logger.exception("GUI error during the quick language scan")
                self.task_queue.put(("prescan_error", str(exc)))

        self.prescan_thread = threading.Thread(target=worker, daemon=True)
        self.prescan_thread.start()

    def on_profile_change(self) -> None:
        self.profile = get_profile_by_name(self.profile_var.get())
        save_last_used_profile(self.profile.name)
        if self.folder_var.get().strip():
            self._refresh_language_choices()
            self._update_language_warning()
        self._append_log(f"Preset loaded: {self.profile.name}")

    def save_profile(self) -> None:
        name = simpledialog.askstring("Preset", "Preset name", initialvalue=self.profile_var.get() or self.profile.name, parent=self.root)
        if not name:
            return
        profile = ProfileSettings(
            name=name,
            audio_language=self._code_for_audio_label(self.audio_var.get()),
            subtitle_language=self._code_for_subtitle_label(self.subtitle_var.get()),
            subtitle_policy=self.profile.subtitle_policy,
            auto_apply_unique_candidate=self.profile.auto_apply_unique_candidate,
            auto_group_series=self.profile.auto_group_series,
            prefer_srt_over_pgs=self.profile.prefer_srt_over_pgs,
            exclude_forced=self.profile.exclude_forced,
            exclude_sdh=self.profile.exclude_sdh,
            exclude_commentary=self.profile.exclude_commentary,
            exclude_dubtitle=self.profile.exclude_dubtitle,
        )
        upsert_profile(profile)
        save_last_used_profile(profile.name)
        self.profile = profile
        self._refresh_profile_list()
        self.profile_var.set(profile.name)
        self._append_log(f"Preset saved: {profile.name}")

    def start_analysis(self) -> None:
        folder_value = self.folder_var.get().strip()
        if not folder_value:
            messagebox.showwarning("PistePilot", "Choose a folder first.", parent=self.root)
            return
        profile = self._current_profile()
        self.profile = profile
        save_last_used_profile(profile.name)
        self._save_gui_state()

        def worker() -> None:
            try:
                reports = analyze_directory(
                    Path(folder_value),
                    profile=profile,
                    recursive=self.recursive_var.get(),
                    toolset=self.toolset,
                    logger=self.logger,
                    progress_callback=lambda phase, current, total, message: self.task_queue.put(("progress", (phase, current, total, message))),
                    cancel_event=self.cancel_event,
                )
                self.task_queue.put(("analysis_done", reports))
            except Exception as exc:
                self.logger.exception("GUI error during analysis")
                self.task_queue.put(("error", str(exc)))

        self._append_log("Analysis started.")
        self.progress_var.set("Analysis in progress...")
        self._start_worker(worker)

    def start_apply(self) -> None:
        valid_reports = validate_reports_before_apply(self.reports)
        if not valid_reports:
            messagebox.showwarning("PistePilot", "No valid analysis report is available.", parent=self.root)
            return
        confirmation = ApplyConfirmationDialog(self.root, self, valid_reports)
        if not confirmation.show():
            return

        def worker() -> None:
            try:
                summary, backup_path = apply_changes(
                    self.reports,
                    toolset=self.toolset,
                    logger=self.logger,
                    log_dir=self.log_dir,
                    progress_callback=lambda phase, current, total, message: self.task_queue.put(("progress", (phase, current, total, message))),
                    cancel_event=self.cancel_event,
                )
                self.task_queue.put(("apply_done", (summary, backup_path)))
            except Exception as exc:
                self.logger.exception("GUI error during apply")
                self.task_queue.put(("error", str(exc)))

        self._append_log("Apply started.")
        self.progress_var.set("Applying changes...")
        self._start_worker(worker)

    def export_reports(self) -> None:
        if not self.reports:
            messagebox.showinfo("PistePilot", "There is no report to export.", parent=self.root)
            return
        json_path, csv_path = export_analysis_reports(self.reports, toolset=self.toolset, logger=self.logger, log_dir=self.log_dir)
        self._append_log(f"Reports exported: {json_path.name}, {csv_path.name}")
        messagebox.showinfo("Export complete", f"JSON report: {json_path}\nCSV report: {csv_path}", parent=self.root)

    def restore_backup(self) -> None:
        backup_path = filedialog.askopenfilename(
            title="Choose a metadata backup",
            initialdir=str(self.log_dir),
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not backup_path:
            return
        if not messagebox.askyesno(
            "Restore",
            "Restore should be tested on copies first.\nDo you want to continue?",
            parent=self.root,
        ):
            return

        def worker() -> None:
            try:
                restored, errors = restore_from_backup(
                    Path(backup_path),
                    toolset=self.toolset,
                    logger=self.logger,
                    log_dir=self.log_dir,
                    progress_callback=lambda phase, current, total, message: self.task_queue.put(("progress", (phase, current, total, message))),
                    cancel_event=self.cancel_event,
                )
                self.task_queue.put(("restore_done", (restored, errors, backup_path)))
            except Exception as exc:
                self.logger.exception("GUI error during restore")
                self.task_queue.put(("error", str(exc)))

        self._append_log(f"Restore started from {backup_path}")
        self.progress_var.set("Restoring...")
        self._start_worker(worker)

    def open_logs_folder(self) -> None:
        open_path_with_system(self.log_dir)

    def open_bin_folder(self) -> None:
        open_path_with_system(get_local_bin_dir(create=True))

    def show_tools(self) -> None:
        self.toolset = detect_tools()
        self._refresh_tool_status()
        self._refresh_action_states()
        text = (
            "FFmpeg\n"
            f"  ffmpeg   {'OK' if self.toolset.is_available('ffmpeg') else 'Missing'}\n"
            f"  ffprobe  {'OK' if self.toolset.is_available('ffprobe') else 'Missing'}\n\n"
            "MKVToolNix\n"
            f"  mkvmerge     {'OK' if self.toolset.is_available('mkvmerge') else 'Missing'}\n"
            f"  mkvpropedit  {'OK' if self.toolset.is_available('mkvpropedit') else 'Missing'}\n\n"
            "Impact\n"
            "- Basic analysis can still work with ffprobe.\n"
            "- Fast and safe MKV updates require MKVToolNix.\n"
            "- Without MKVToolNix, MKV updates stay disabled.\n"
        )
        messagebox.showinfo("Tool check", text, parent=self.root)

    def open_resolver(self) -> None:
        groups = resolve_groups(self.reports, self._current_profile().subtitle_language)
        if not groups:
            messagebox.showinfo("PistePilot", "There is no subtitle group to confirm.", parent=self.root)
            return
        AmbiguityResolverWindow(self, groups)

    def show_selected_report_details(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        index = int(selection[0])
        report = self.reports[index]
        if not self.verbose_var.get():
            lines = [
                f"File: {report.path.name}",
                f"Status: {self._status_label(report)}",
                f"Audio: {self._selected_track_label(report, 'audio')}",
                f"Subtitles: {self._selected_track_label(report, 'subtitle')}",
                f"Result: {self._result_label(report)}",
            ]
            messagebox.showinfo("File details", "\n".join(lines), parent=self.root)
            return

        lines = [f"File: {report.path}", "", "Audio"]
        for track in report.audio_tracks:
            lines.append(track.display_name())
        lines.append("")
        lines.append("Subtitles")
        for track in report.subtitle_tracks:
            lines.append(track.display_name())
        messagebox.showinfo("Technical details", "\n".join(lines), parent=self.root)

    def cancel_current_task(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
            self._append_log("Cancellation requested. The current file may finish before stopping.")
            self.progress_var.set("Cancellation requested...")

    def _poll_queue(self) -> None:
        try:
            while True:
                event, payload = self.task_queue.get_nowait()
                if event == "progress":
                    phase, current, total, message = payload  # type: ignore[misc]
                    percent = 0 if total == 0 else (current / total) * 100
                    self.progress_value.set(percent)
                    if phase == "analysis":
                        self.progress_var.set(f"Analysis in progress: {current} / {total}")
                    elif phase == "apply":
                        self.progress_var.set(f"Applying changes: {current} / {total}")
                    elif phase == "restore":
                        self.progress_var.set(f"Restore in progress: {current} / {total}")
                    else:
                        self.progress_var.set(message)
                    if self.verbose_var.get():
                        self._append_log(message)
                elif event == "prescan_done":
                    token, audio_codes, subtitle_codes = payload  # type: ignore[misc]
                    if token != self.prescan_token:
                        continue
                    self.detected_audio_codes = {self.canonical_language_code(code) for code in audio_codes if self.canonical_language_code(code)}
                    self.detected_subtitle_codes = {
                        self.canonical_language_code(code) for code in subtitle_codes if self.canonical_language_code(code)
                    }
                    if self.detected_audio_codes or self.detected_subtitle_codes:
                        self.available_tracks_var.set(self._available_tracks_label())
                        self._refresh_language_choices()
                    else:
                        self.audio_options = self._build_audio_options(fallback_defaults=True)
                        self.subtitle_options = self._build_subtitle_options(fallback_defaults=True)
                        self.audio_combo.configure(values=[option.label for option in self.audio_options], state="readonly")
                        self.subtitle_combo.configure(values=[option.label for option in self.subtitle_options], state="readonly")
                        self.audio_var.set(self._label_for_code(self.audio_options, PRIORITY_AUDIO_CODE) or self.audio_options[0].label)
                        self.subtitle_var.set(self._label_for_code(self.subtitle_options, PRIORITY_SUBTITLE_CODE) or self.subtitle_options[0].label)
                        self.available_tracks_var.set("No clear track language list was detected yet. Common language choices are shown.")
                    self._update_language_warning()
                    self._refresh_action_states()
                elif event == "prescan_error":
                    if not self.reports:
                        self.audio_options = self._build_audio_options(fallback_defaults=True)
                        self.subtitle_options = self._build_subtitle_options(fallback_defaults=True)
                        self.audio_combo.configure(values=[option.label for option in self.audio_options], state="readonly")
                        self.subtitle_combo.configure(values=[option.label for option in self.subtitle_options], state="readonly")
                        self.audio_var.set(self._label_for_code(self.audio_options, PRIORITY_AUDIO_CODE) or self.audio_options[0].label)
                        self.subtitle_var.set(self._label_for_code(self.subtitle_options, PRIORITY_SUBTITLE_CODE) or self.subtitle_options[0].label)
                        self.available_tracks_var.set("Track detection is unavailable. Default language choices are shown instead.")
                        self.warning_var.set("Quick track detection was not available for this folder.")
                        self._refresh_action_states()
                elif event == "analysis_done":
                    self.reports = payload  # type: ignore[assignment]
                    self.progress_value.set(100)
                    self.progress_var.set("Analysis complete")
                    self._append_log(f"Analysis complete: {len(self.reports)} files analyzed.")
                    self._refresh_tree()
                    self._update_summary()
                    self._update_detected_languages()
                    groups = resolve_groups(self.reports, self._current_profile().subtitle_language)
                    if groups:
                        self._append_log(f"{len(groups)} subtitle group(s) need confirmation.")
                    else:
                        ready = len([report for report in self.reports if report.is_ready()])
                        self._append_log("Regular subtitles were selected automatically.")
                        self._append_log(f"{ready} file(s) are ready.")
                elif event == "apply_done":
                    summary, backup_path = payload  # type: ignore[misc]
                    self.progress_value.set(100)
                    self.progress_var.set("Apply complete")
                    self._append_log(f"Done: {summary.applied} file(s) updated.")
                    self._refresh_tree()
                    self._update_summary()
                    messagebox.showinfo(
                        "Done",
                        f"Updated successfully: {summary.applied}\n"
                        f"Skipped: {summary.skipped}\n"
                        f"Errors: {summary.errors}\n\n"
                        f"Logs: {self.log_file}\n"
                        f"Metadata backup: {backup_path}",
                        parent=self.root,
                    )
                elif event == "restore_done":
                    restored, errors, backup_path = payload  # type: ignore[misc]
                    self.progress_value.set(100)
                    self.progress_var.set("Restore complete")
                    self._append_log(f"Restore complete: {restored} restored, {errors} errors")
                    messagebox.showinfo(
                        "Restore complete",
                        f"Restored: {restored}\nErrors: {errors}\nSource backup: {backup_path}",
                        parent=self.root,
                    )
                elif event == "error":
                    self.progress_value.set(0)
                    self.progress_var.set("Error")
                    self._append_log(f"Error: {payload}")
                    messagebox.showerror(
                        "Error",
                        f"An unexpected error occurred.\n\n{payload}\n\nLog: {self.log_file}",
                        parent=self.root,
                    )
        except queue.Empty:
            pass
        finally:
            if not self._is_busy():
                self.cancel_button.configure(state="disabled")
                self._refresh_action_states()
            self.root.after(150, self._poll_queue)

    def run(self) -> None:
        self.root.mainloop()


class ApplyConfirmationDialog:
    def __init__(self, parent, app: PistePilotGUI, reports: list[MediaFileReport]) -> None:
        self.app = app
        self.reports = reports
        self.result = False
        self.window = tk.Toplevel(parent)
        self.window.title("Summary before applying changes")
        self.window.transient(parent)
        self.window.grab_set()
        self.window.geometry("640x320")

        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)

        lines = [
            "Summary before applying changes",
            "",
            f"Files to update: {len(reports)}",
            f"Audio: {app.audio_var.get()}",
            f"Subtitles: {app.subtitle_var.get()}",
            "",
            "No tracks will be removed.",
            "No media will be re-encoded.",
            "A metadata backup will be created before any file is changed.",
        ]
        ttk.Label(frame, text="\n".join(lines), justify="left").pack(anchor="w")

        buttons = ttk.Frame(frame, padding=(0, 16, 0, 0))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="View file list", command=self._show_files).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(buttons, text="Apply", command=self._confirm, style="Primary.TButton").pack(side="right", padx=(0, 8))

    def _show_files(self) -> None:
        FileListDialog(self.window, self.app, self.reports)

    def _confirm(self) -> None:
        self.result = True
        self.window.destroy()

    def _cancel(self) -> None:
        self.result = False
        self.window.destroy()

    def show(self) -> bool:
        self.window.wait_window()
        return self.result


class FileListDialog:
    def __init__(self, parent, app: PistePilotGUI, reports: list[MediaFileReport]) -> None:
        self.window = tk.Toplevel(parent)
        self.window.title("Files that will be updated")
        self.window.transient(parent)
        self.window.geometry("760x420")

        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        columns = ("file", "audio", "subtitle")
        tree = ttk.Treeview(frame, columns=columns, show="headings")
        for column, label, width in [
            ("file", "File", 360),
            ("audio", "Audio", 140),
            ("subtitle", "Subtitles", 180),
        ]:
            tree.heading(column, text=label)
            tree.column(column, width=width, anchor="w")
        tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)

        for index, report in enumerate(reports):
            tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    report.path.name,
                    app._selected_track_label(report, "audio"),
                    app._selected_track_label(report, "subtitle"),
                ),
            )

        ttk.Button(frame, text="Close", command=self.window.destroy).grid(row=1, column=0, sticky="e", pady=(12, 0))


class AmbiguityResolverWindow:
    def __init__(self, app: PistePilotGUI, groups: list[AmbiguityGroup]) -> None:
        self.app = app
        self.groups = groups
        self.window = tk.Toplevel(app.root)
        self.window.title("Subtitles to confirm")
        self.window.geometry("980x620")
        self.window.transient(app.root)

        left = ttk.Frame(self.window, padding=8)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Groups").pack(anchor="w")
        self.group_list = tk.Listbox(left, width=32, exportselection=False)
        self.group_list.pack(fill="y", expand=True, pady=(6, 0))
        self.group_list.bind("<<ListboxSelect>>", lambda _event: self._show_group())

        right = ttk.Frame(self.window, padding=8)
        right.pack(side="left", fill="both", expand=True)
        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)

        self.header = ttk.Label(right, text="Select a group", anchor="w", justify="left")
        self.header.grid(row=0, column=0, sticky="ew")

        self.summary = ttk.Label(right, anchor="nw", justify="left", wraplength=620)
        self.summary.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.details = tk.Text(right, state="disabled", height=16)
        self.details.grid(row=2, column=0, sticky="nsew", pady=8)

        buttons = ttk.Frame(right)
        buttons.grid(row=3, column=0, sticky="ew")
        for col in range(4):
            buttons.columnconfigure(col, weight=1)
        self.primary_button = ttk.Button(buttons, text="Use this recommendation", command=self.apply_recommendation)
        self.primary_button.grid(row=0, column=0, sticky="ew", padx=4)
        ttk.Button(buttons, text="Choose something else", command=self.choose_reference).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(buttons, text="Technical details", command=self.show_technical_details).grid(row=0, column=2, sticky="ew", padx=4)
        ttk.Button(buttons, text="Ignore group", command=self.ignore_group).grid(row=0, column=3, sticky="ew", padx=4)

        self._reload_groups()

    def _current_group(self) -> AmbiguityGroup | None:
        selection = self.group_list.curselection()
        if not selection:
            return None
        return self.groups[selection[0]]

    def _reload_groups(self) -> None:
        subtitle_language = self.app._current_profile().subtitle_language
        self.groups = resolve_groups(self.app.reports, subtitle_language)
        self.group_list.delete(0, "end")
        for group in self.groups:
            label = group.display_name or "Group"
            self.group_list.insert("end", f"{label} - {len(group.reports)} files")
        if not self.groups:
            self.window.destroy()
            return
        self.group_list.selection_set(0)
        self._show_group()

    def _reference_report(self, group: AmbiguityGroup) -> MediaFileReport:
        for report in group.reports:
            if report.needs_manual_review():
                return report
        return group.reports[0]

    def _candidate_tracks(self, report: MediaFileReport) -> list:
        language_tracks, plausible_tracks = subtitle_candidates_for_language(
            report.subtitle_tracks,
            self.app._current_profile().subtitle_language,
        )
        return plausible_tracks or language_tracks or report.subtitle_tracks

    def _recommended_track(self, report: MediaFileReport):
        candidates = self._candidate_tracks(report)
        if not candidates:
            return None
        for track in candidates:
            if not is_subtitle_dubtitle_like(track):
                return track
        return candidates[0]

    def _track_family_label(self, track) -> str:
        if is_subtitle_dubtitle_like(track):
            return "Dubtitle subtitles"
        if is_subtitle_forced_like(track):
            return "Forced subtitles"
        if is_subtitle_sdh_like(track):
            return "SDH / hearing impaired subtitles"
        if is_subtitle_commentary_like(track):
            return "Commentary subtitles"
        return self.app.subtitle_var.get()

    def _track_example_label(self, track) -> str:
        if self.app.verbose_var.get():
            return f"{track.short_label()} | {track.language or track.language_ietf or 'und'} | {track.codec or '-'} | {track.title or 'empty title'}"
        return f"{self.app.friendly_language_name(track.language or track.language_ietf or '')} | {track.codec or '-'} | {track.title or 'empty title'}"

    def _avoided_labels(self, group: AmbiguityGroup) -> list[str]:
        labels: set[str] = set()
        for report in group.reports:
            for track in report.subtitle_tracks:
                if is_subtitle_dubtitle_like(track):
                    labels.add("Dubtitle")
                if is_subtitle_forced_like(track):
                    labels.add("Forced")
                if is_subtitle_sdh_like(track):
                    labels.add("SDH / hearing impaired")
        return sorted(labels)

    def _show_group(self) -> None:
        group = self._current_group()
        if group is None:
            return

        reference_report = self._reference_report(group)
        recommended_track = self._recommended_track(reference_report)
        avoided = self._avoided_labels(group)
        avoided_text = "\n".join(f"- {label}" for label in avoided) if avoided else "- None"

        self.header.configure(
            text=(
                f"{group.display_name or 'Group'}\n"
                f"Folder: {group.folder}\n"
                f"Files: {len(group.reports)}"
            )
        )

        if recommended_track is None:
            recommendation_text = (
                "No reliable automatic recommendation is available for this group.\n"
                "Choose another track to apply the same logic to similar files."
            )
            button_text = f"Apply this recommendation to {len(group.reports)} files"
            self.primary_button.configure(state="disabled", text=button_text)
        else:
            recommendation_text = (
                f"Recommended track:\n"
                f"{self._track_family_label(recommended_track)}\n"
                f"Name: {recommended_track.title or 'empty'}\n"
                f"Format: {recommended_track.codec or '-'}\n\n"
                f"Avoided:\n{avoided_text}\n\n"
                f"This recommendation will be applied to {len(group.reports)} files."
            )
            if is_subtitle_dubtitle_like(recommended_track):
                button_text = f"Use Dubtitle subtitles for {len(group.reports)} files"
            else:
                button_text = f"Use {self.app.subtitle_var.get()} for {len(group.reports)} files"
            self.primary_button.configure(state="normal", text=button_text)

        self.summary.configure(text=recommendation_text)

        lines = [
            f"{len(group.reports)} file(s) affected.",
            "",
            "Recommendation:",
            self._track_family_label(recommended_track) if recommended_track else "Manual choice required",
            "",
            "Reference file:",
            reference_report.path.name,
        ]
        if recommended_track is not None:
            lines.append("")
            lines.append("Example:")
            lines.append(self._track_example_label(recommended_track))

        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("end", "\n".join(lines))
        self.details.configure(state="disabled")

    def apply_recommendation(self) -> None:
        group = self._current_group()
        if group is None:
            return
        reference_report = self._reference_report(group)
        track = self._recommended_track(reference_report)
        if track is None:
            messagebox.showwarning("PistePilot", "No automatic recommendation is available.", parent=self.window)
            return
        self._apply_track_to_group(group, reference_report, track)

    def choose_reference(self) -> None:
        group = self._current_group()
        if group is None:
            return
        reference_report = self._reference_report(group)
        candidates = self._candidate_tracks(reference_report)
        if not candidates:
            messagebox.showwarning("PistePilot", "No usable track was found.", parent=self.window)
            return
        dialog = TrackChoiceDialog(self.window, self, reference_report, candidates)
        chosen_track = dialog.show()
        if chosen_track is None:
            return
        self._apply_track_to_group(group, reference_report, chosen_track)

    def _apply_track_to_group(self, group: AmbiguityGroup, reference_report: MediaFileReport, chosen_track) -> None:
        if not messagebox.askyesno(
            "Confirmation",
            (
                f"Selected track:\n"
                f"{self._track_family_label(chosen_track)}\n"
                f"Format: {chosen_track.codec or '-'}\n"
                f"Name: {chosen_track.title or 'empty'}\n\n"
                f"Files affected: {len(group.reports)}\n\n"
                "Continue?"
            ),
            parent=self.window,
        ):
            return

        resolved = apply_group_relative_choice(
            group,
            reference_report,
            chosen_track,
            self.app._current_profile().subtitle_language,
        )
        unresolved = len([report for report in group.reports if report.needs_manual_review()])
        self.app._append_log(f"Group resolved: {resolved} file(s)")
        self.app._refresh_tree()
        self.app._update_summary()
        if resolved == 0:
            messagebox.showwarning(
                "PistePilot",
                "The choice could not be applied automatically to this group. Open the technical details to inspect the tracks.",
                parent=self.window,
            )
            return
        messagebox.showinfo(
            "PistePilot",
            f"{resolved} file(s) updated for this group.\nStill to confirm: {unresolved}",
            parent=self.window,
        )
        self._reload_groups()

    def ignore_group(self) -> None:
        group = self._current_group()
        if group is None:
            return
        current_index = self.group_list.curselection()[0]
        next_index = current_index + 1
        if next_index < len(self.groups):
            self.group_list.selection_clear(0, "end")
            self.group_list.selection_set(next_index)
            self._show_group()
        else:
            self.window.destroy()

    def show_technical_details(self) -> None:
        group = self._current_group()
        if group is None:
            return
        lines = []
        for report in group.reports:
            lines.append(str(report.path))
            for track in report.subtitle_tracks:
                lines.append(f"  {track.display_name()}")
            lines.append("")
        messagebox.showinfo("Technical details", "\n".join(lines), parent=self.window)


class TrackChoiceDialog:
    def __init__(self, parent, resolver: AmbiguityResolverWindow, reference_report: MediaFileReport, candidates: list) -> None:
        self.resolver = resolver
        self.reference_report = reference_report
        self.candidates = candidates
        self.selected_track = None
        self.window = tk.Toplevel(parent)
        self.window.title("Choose another track")
        self.window.transient(parent)
        self.window.grab_set()
        self.window.geometry("760x420")

        ttk.Label(
            self.window,
            text=(
                "Choose the subtitle type to use on the reference file.\n"
                f"File: {reference_report.path.name}"
            ),
            justify="left",
        ).pack(anchor="w", padx=12, pady=(12, 8))

        self.choice_var = tk.StringVar()
        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)
        for index, track in enumerate(candidates):
            value = str(index)
            label = resolver._track_family_label(track)
            detail = resolver._track_example_label(track)
            recommended = " - recommended" if index == 0 and not is_subtitle_dubtitle_like(track) else ""
            ttk.Radiobutton(
                frame,
                text=f"{label}{recommended}\n{detail}",
                variable=self.choice_var,
                value=value,
            ).pack(anchor="w", fill="x", pady=6)
            if index == 0:
                self.choice_var.set(value)

        buttons = ttk.Frame(self.window, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Apply this choice", command=self._apply).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="left", padx=(8, 0))

    def _apply(self) -> None:
        selected = self.choice_var.get()
        if selected == "":
            return
        self.selected_track = self.candidates[int(selected)]
        self.window.destroy()

    def _cancel(self) -> None:
        self.selected_track = None
        self.window.destroy()

    def show(self):
        self.window.wait_window()
        return self.selected_track


def main() -> int:
    try:
        app = PistePilotGUI()
        app.run()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logger, _log_dir, log_file = setup_logging(verbose=False)
        logger.exception("Unexpected GUI error")
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("PistePilot", f"An unexpected error occurred: {exc}\n\nLog: {log_file}", parent=root)
        root.destroy()
        return 1
    finally:
        pause_before_exit_if_interactive(interactive_mode=False, gui_mode=True)


if __name__ == "__main__":
    raise SystemExit(main())
