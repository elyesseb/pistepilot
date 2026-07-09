from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


ConfidenceLevel = Literal["high", "medium", "low", "none"]
DecisionStatus = Literal["selected", "ambiguous", "not_found", "skipped"]
ActionStatus = Literal["apply", "review", "skip", "error"]
StreamKind = Literal["audio", "subtitle", "video", "other"]
SessionRuleScope = Literal["same_signature", "same_folder", "same_candidate_names"]


@dataclass(slots=True)
class ToolInfo:
    name: str
    path: str | None
    found: bool
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "found": self.found,
            "source": self.source,
        }


@dataclass(slots=True)
class Toolset:
    tools: dict[str, ToolInfo]

    def get(self, name: str) -> ToolInfo:
        return self.tools[name]

    def path_for(self, name: str) -> str | None:
        return self.tools[name].path

    def is_available(self, name: str) -> bool:
        return self.tools[name].found

    def missing(self, names: list[str] | None = None) -> list[str]:
        target_names = names or list(self.tools.keys())
        return [name for name in target_names if not self.is_available(name)]

    def to_dict(self) -> dict[str, Any]:
        return {name: info.to_dict() for name, info in self.tools.items()}


@dataclass(slots=True)
class StreamInfo:
    kind: StreamKind
    id: int | None
    type_index: int | None
    codec: str | None
    language: str | None
    language_ietf: str | None
    title: str | None
    channels: int | None
    default: bool
    forced: bool
    tags: dict[str, Any] = field(default_factory=dict)
    dispositions: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def short_label(self) -> str:
        prefix = "a" if self.kind == "audio" else "s" if self.kind == "subtitle" else "t"
        index = self.type_index if self.type_index is not None else "?"
        return f"{prefix}{index}"

    def display_name(self) -> str:
        lang = self.language_ietf or self.language or "und"
        title = self.title or "-"
        codec = self.codec or "-"
        channels = f"{self.channels}ch" if self.channels else "-"
        flags = []
        if self.default:
            flags.append("default")
        if self.forced:
            flags.append("forced")
        flag_label = ", ".join(flags) if flags else "-"
        return f"{self.short_label()} | {lang} | {title} | {codec} | {channels} | {flag_label}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "type_index": self.type_index,
            "codec": self.codec,
            "language": self.language,
            "language_ietf": self.language_ietf,
            "title": self.title,
            "channels": self.channels,
            "default": self.default,
            "forced": self.forced,
            "tags": self.tags,
            "dispositions": self.dispositions,
        }


@dataclass(slots=True)
class SelectionDecision:
    status: DecisionStatus
    selected_type_index: int | None
    candidate_type_indices: list[int]
    confidence: ConfidenceLevel
    reason: str
    details: list[str] = field(default_factory=list)
    manual_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "selected_type_index": self.selected_type_index,
            "candidate_type_indices": self.candidate_type_indices,
            "confidence": self.confidence,
            "reason": self.reason,
            "details": self.details,
            "manual_required": self.manual_required,
        }


@dataclass(slots=True)
class PlannedChange:
    container: str
    action: ActionStatus
    tool: str | None
    selected_audio: int | None
    selected_subtitle: int | None
    confidence: ConfidenceLevel
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "action": self.action,
            "tool": self.tool,
            "selected_audio": self.selected_audio,
            "selected_subtitle": self.selected_subtitle,
            "confidence": self.confidence,
            "notes": self.notes,
            "error": self.error,
        }


@dataclass(slots=True)
class MediaFileReport:
    path: Path
    container: str
    analysis_tool: str
    video_codec: str | None
    audio_tracks: list[StreamInfo]
    subtitle_tracks: list[StreamInfo]
    audio_decision: SelectionDecision
    subtitle_decision: SelectionDecision
    plan: PlannedChange
    errors: list[str] = field(default_factory=list)
    track_signature: str = ""
    candidate_signature: str = ""
    resolution_source: str | None = None

    def needs_manual_review(self) -> bool:
        return self.plan.action == "review"

    def is_ready(self) -> bool:
        return self.plan.action == "apply"

    def is_skipped(self) -> bool:
        return self.plan.action == "skip"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "container": self.container,
            "analysis_tool": self.analysis_tool,
            "video_codec": self.video_codec,
            "audio_tracks": [track.to_dict() for track in self.audio_tracks],
            "subtitle_tracks": [track.to_dict() for track in self.subtitle_tracks],
            "audio_decision": self.audio_decision.to_dict(),
            "subtitle_decision": self.subtitle_decision.to_dict(),
            "plan": self.plan.to_dict(),
            "errors": self.errors,
            "track_signature": self.track_signature,
            "candidate_signature": self.candidate_signature,
            "resolution_source": self.resolution_source,
        }


@dataclass(slots=True)
class BatchSummary:
    total_files: int = 0
    auto_applicable: int = 0
    needs_review: int = 0
    skipped: int = 0
    errors: int = 0
    applied: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_files": self.total_files,
            "auto_applicable": self.auto_applicable,
            "needs_review": self.needs_review,
            "skipped": self.skipped,
            "errors": self.errors,
            "applied": self.applied,
        }


@dataclass(slots=True)
class ProfileSettings:
    name: str
    audio_language: str
    subtitle_language: str
    subtitle_policy: str
    auto_apply_unique_candidate: bool
    auto_group_series: bool
    prefer_srt_over_pgs: bool
    exclude_forced: bool
    exclude_sdh: bool
    exclude_commentary: bool
    exclude_dubtitle: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "audio_language": self.audio_language,
            "subtitle_language": self.subtitle_language,
            "subtitle_policy": self.subtitle_policy,
            "auto_apply_unique_candidate": self.auto_apply_unique_candidate,
            "auto_group_series": self.auto_group_series,
            "prefer_srt_over_pgs": self.prefer_srt_over_pgs,
            "exclude_forced": self.exclude_forced,
            "exclude_sdh": self.exclude_sdh,
            "exclude_commentary": self.exclude_commentary,
            "exclude_dubtitle": self.exclude_dubtitle,
        }


@dataclass(slots=True, frozen=True)
class LanguageOption:
    code: str
    label: str
    detected: bool = False


@dataclass(slots=True, frozen=True)
class TrackReference:
    kind: StreamKind
    language: str
    title_normalized: str
    codec: str
    forced: bool
    channels: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "language": self.language,
            "title_normalized": self.title_normalized,
            "codec": self.codec,
            "forced": self.forced,
            "channels": self.channels,
        }


@dataclass(slots=True, frozen=True)
class RelativeTrackRule:
    target_language: str
    codec: str
    title_normalized: str
    forced: bool
    sdh_like: bool
    commentary_like: bool
    language_rank: int
    candidate_rank: int
    kind: StreamKind = "subtitle"

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_language": self.target_language,
            "codec": self.codec,
            "title_normalized": self.title_normalized,
            "forced": self.forced,
            "sdh_like": self.sdh_like,
            "commentary_like": self.commentary_like,
            "language_rank": self.language_rank,
            "candidate_rank": self.candidate_rank,
            "kind": self.kind,
        }


@dataclass(slots=True)
class SessionResolutionRule:
    scope: SessionRuleScope
    signature: str
    folder: str | None
    candidate_signature: str
    audio_reference: TrackReference | None
    subtitle_reference: TrackReference | None
    source_file: str
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "signature": self.signature,
            "folder": self.folder,
            "candidate_signature": self.candidate_signature,
            "audio_reference": self.audio_reference.to_dict() if self.audio_reference else None,
            "subtitle_reference": self.subtitle_reference.to_dict() if self.subtitle_reference else None,
            "source_file": self.source_file,
            "description": self.description,
        }


@dataclass(slots=True)
class AmbiguityGroup:
    key: str
    folder: Path
    reports: list[MediaFileReport]
    reason: str
    recommendation: str
    confidence: ConfidenceLevel
    relative_rule: RelativeTrackRule | None = None
    display_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "folder": str(self.folder),
            "reports": [report.to_dict() for report in self.reports],
            "reason": self.reason,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "relative_rule": self.relative_rule.to_dict() if self.relative_rule else None,
            "display_name": self.display_name,
        }
