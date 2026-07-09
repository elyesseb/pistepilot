from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from pistepilot.models import AmbiguityGroup, MediaFileReport, RelativeTrackRule, StreamInfo
from pistepilot.selector import (
    is_subtitle_commentary_like,
    is_subtitle_forced_like,
    is_subtitle_sdh_like,
    normalize_text,
    subtitle_candidates_for_language,
)


EPISODE_PATTERN = re.compile(r"(s\d{1,2}e\d{1,2}|\d{1,3})", re.IGNORECASE)
SEASON_PATTERN = re.compile(r"s\d{1,2}", re.IGNORECASE)
TECHNICAL_TOKENS = {
    "2160p",
    "1080p",
    "720p",
    "480p",
    "web",
    "webdl",
    "webrip",
    "bluray",
    "bdrip",
    "hdrip",
    "amzn",
    "nf",
    "netflix",
    "x264",
    "x265",
    "h264",
    "h265",
    "hevc",
    "avc",
    "10bit",
    "aac",
    "ddp",
    "eac3",
    "dts",
}


def _release_hint(path: Path) -> str:
    stem = normalize_text(path.stem)
    tokens = [token for token in re.split(r"[\s\.\-_]+", stem) if token]
    if not tokens:
        return stem

    episode_index = next((index for index, token in enumerate(tokens) if EPISODE_PATTERN.fullmatch(token)), None)
    title_tokens = tokens if episode_index is None else tokens[:episode_index]
    title_tokens = [token for token in title_tokens if not SEASON_PATTERN.fullmatch(token) and not token.isdigit()]

    technical_tokens = [token for token in tokens if token in TECHNICAL_TOKENS]
    stable_tokens = (title_tokens[:4] or tokens[:2]) + technical_tokens[:3]
    return " ".join(stable_tokens)


def _audio_language_signature(report: MediaFileReport) -> tuple[str, ...]:
    return tuple(sorted({normalize_text(track.language_ietf or track.language) for track in report.audio_tracks if track.language or track.language_ietf}))


def _subtitle_candidate_signature(report: MediaFileReport, target_language: str) -> tuple[tuple[str, str, bool, bool, bool], ...]:
    _, plausible_tracks = subtitle_candidates_for_language(report.subtitle_tracks, target_language)
    if plausible_tracks:
        candidates = plausible_tracks
    else:
        language_tracks, _ = subtitle_candidates_for_language(report.subtitle_tracks, target_language)
        candidates = language_tracks

    payload = []
    for track in candidates:
        payload.append(
            (
                normalize_text(track.language_ietf or track.language),
                normalize_text(track.codec),
                bool(track.forced),
                is_subtitle_sdh_like(track),
                is_subtitle_commentary_like(track),
            )
        )
    return tuple(payload)


def _target_subtitle_shape(report: MediaFileReport, target_language: str) -> str:
    language_tracks, plausible_tracks = subtitle_candidates_for_language(report.subtitle_tracks, target_language)
    if not language_tracks:
        return "none"

    codecs = sorted({normalize_text(track.codec) or "-" for track in language_tracks})
    blank_titles = sum(1 for track in language_tracks if not normalize_text(track.title))
    forced_count = sum(1 for track in language_tracks if is_subtitle_forced_like(track))
    sdh_count = sum(1 for track in language_tracks if is_subtitle_sdh_like(track))
    commentary_count = sum(1 for track in language_tracks if is_subtitle_commentary_like(track))
    plausible_count = len(plausible_tracks)
    return "|".join(
        [
            f"lang={len(language_tracks)}",
            f"plausible={plausible_count}",
            f"blank={blank_titles}",
            f"forced={forced_count}",
            f"sdh={sdh_count}",
            f"commentary={commentary_count}",
            f"codecs={','.join(codecs)}",
        ]
    )


def build_release_group_key(report: MediaFileReport, target_language: str) -> str:
    folder = str(report.path.parent)
    release_hint = _release_hint(report.path)
    extension = report.path.suffix.lower()
    video_codec = normalize_text(report.video_codec)
    audio_signature = "|".join(_audio_language_signature(report))
    subtitle_shape = _target_subtitle_shape(report, target_language)
    candidate_signature = "|".join(
        f"{lang}:{codec}:{int(forced)}:{int(sdh)}:{int(commentary)}"
        for lang, codec, forced, sdh, commentary in _subtitle_candidate_signature(report, target_language)
    )
    return "||".join(
        [
            folder,
            release_hint,
            extension,
            video_codec,
            audio_signature,
            subtitle_shape,
            candidate_signature,
        ]
    )


def build_relative_subtitle_rule(
    media_info: MediaFileReport,
    selected_track: StreamInfo,
    target_language: str,
) -> RelativeTrackRule:
    language_tracks, plausible_tracks = subtitle_candidates_for_language(media_info.subtitle_tracks, target_language)
    language_rank = next(
        (index for index, track in enumerate(language_tracks, start=1) if track.type_index == selected_track.type_index),
        1,
    )
    candidate_rank = next(
        (index for index, track in enumerate(plausible_tracks, start=1) if track.type_index == selected_track.type_index),
        language_rank,
    )
    return RelativeTrackRule(
        target_language=normalize_text(target_language),
        codec=normalize_text(selected_track.codec),
        title_normalized=normalize_text(selected_track.title),
        forced=is_subtitle_forced_like(selected_track),
        sdh_like=is_subtitle_sdh_like(selected_track),
        commentary_like=is_subtitle_commentary_like(selected_track),
        language_rank=language_rank,
        candidate_rank=candidate_rank,
        kind="subtitle",
    )


def apply_relative_subtitle_rule(
    media_info: MediaFileReport,
    rule: RelativeTrackRule,
) -> StreamInfo | None:
    if rule.kind != "subtitle":
        return None

    language_tracks, plausible_tracks = subtitle_candidates_for_language(media_info.subtitle_tracks, rule.target_language)
    candidates = plausible_tracks or language_tracks
    if not candidates:
        return None

    exact_matches = [
        track
        for track in candidates
        if normalize_text(track.codec) == rule.codec
        and normalize_text(track.title) == rule.title_normalized
        and is_subtitle_forced_like(track) == rule.forced
        and is_subtitle_sdh_like(track) == rule.sdh_like
        and is_subtitle_commentary_like(track) == rule.commentary_like
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]

    if 0 < rule.candidate_rank <= len(candidates):
        return candidates[rule.candidate_rank - 1]
    if 0 < rule.language_rank <= len(language_tracks):
        return language_tracks[rule.language_rank - 1]
    return None


def _group_key(report: MediaFileReport, target_language: str) -> str:
    return build_release_group_key(report, target_language)


def _group_display_name(report: MediaFileReport) -> str:
    hint = _release_hint(report.path) or normalize_text(report.path.parent.name) or "Serie"
    base = " ".join(token.capitalize() for token in hint.split()[:4]) or "Serie"
    return f"Saison {base}"


def group_ambiguous_files(results: list[MediaFileReport], target_language: str) -> list[AmbiguityGroup]:
    groups: dict[str, list[MediaFileReport]] = defaultdict(list)
    for report in results:
        if report.needs_manual_review():
            groups[_group_key(report, target_language)].append(report)

    ambiguity_groups: list[AmbiguityGroup] = []
    for key, reports in groups.items():
        reports.sort(key=lambda report: str(report.path))
        representative = reports[0]
        language_tracks, plausible_tracks = subtitle_candidates_for_language(representative.subtitle_tracks, target_language)
        if len(plausible_tracks) == 1:
            recommendation = f"Use the single plausible {target_language} subtitle track for this group."
            confidence = "high"
        elif plausible_tracks:
            recommendation = f"Use the {target_language} subtitle candidate selected on a reference file."
            confidence = "medium"
        else:
            recommendation = f"Review the available {target_language} subtitle tracks before applying changes."
            confidence = "low"
        reason = "Incomplete metadata or multiple plausible subtitle tracks were detected."
        ambiguity_groups.append(
            AmbiguityGroup(
                key=key,
                folder=representative.path.parent,
                reports=reports,
                reason=reason,
                recommendation=recommendation,
                confidence=confidence,  # type: ignore[arg-type]
                display_name=_group_display_name(representative),
            )
        )

    ambiguity_groups.sort(key=lambda group: (str(group.folder), group.key))
    return ambiguity_groups
