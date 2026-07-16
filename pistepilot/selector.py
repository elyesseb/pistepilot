from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass

from pistepilot.i18n import LANGUAGE_ALIASES, language_display_name, normalize_language_code
from pistepilot.models import (
    MediaFileReport,
    PlannedChange,
    SelectionDecision,
    SessionResolutionRule,
    SessionRuleScope,
    StreamInfo,
    TrackReference,
)


AUDIO_EXCLUDED_KEYWORDS = {
    "commentary",
    "commentaire",
    "description",
    "audio description",
    "descriptive audio",
}

SUBTITLE_EXCLUDED_KEYWORDS = {
    "forced",
    "force",
    "forcé",
    "forcee",
    "signs",
    "songs",
    "sdh",
    "hi",
    "hearing impaired",
    "malentendant",
    "malentendants",
    "sourds",
    "cc",
    "commentary",
    "commentaire",
}

SUBTITLE_DISFAVORED_KEYWORDS = {
    "dubtitle",
    "dubtitles",
}

SUBTITLE_POSITIVE_KEYWORDS = {"full", "complete", "complet", "dialogue", "dialogues", "normal"}


@dataclass(frozen=True, slots=True)
class LanguageProfile:
    code: str
    label: str
    aliases: set[str]
    audio_hints: set[str]
    subtitle_hints: set[str]


LANGUAGE_PROFILES: dict[str, LanguageProfile] = {
    "fr": LanguageProfile(
        code="fr",
        label="French",
        aliases={"fr", "fra", "fre", "fr-fr", "french", "francais", "français"},
        audio_hints={"vff", "vfq", "vf", "french", "francais", "français"},
        subtitle_hints={"francais", "français", "french", "fr"},
    ),
    "en": LanguageProfile(
        code="en",
        label="English",
        aliases={"en", "eng", "english", "en-us", "en-gb"},
        audio_hints={"english", "en"},
        subtitle_hints={"english", "en"},
    ),
    "ja": LanguageProfile(
        code="ja",
        label="Japanese",
        aliases={"ja", "jpn", "japanese", "jp"},
        audio_hints={"japanese", "jpn"},
        subtitle_hints={"japanese", "jpn"},
    ),
    "ko": LanguageProfile(
        code="ko",
        label="Korean",
        aliases={"ko", "kor", "korean"},
        audio_hints={"korean", "kor"},
        subtitle_hints={"korean", "kor"},
    ),
    "es": LanguageProfile(
        code="es",
        label="Spanish",
        aliases={"es", "spa", "spanish", "es-es", "es-la"},
        audio_hints={"spanish", "espanol", "español", "castilian"},
        subtitle_hints={"spanish", "espanol", "español"},
    ),
    "de": LanguageProfile(
        code="de",
        label="German",
        aliases={"de", "deu", "ger", "german", "de-de"},
        audio_hints={"german", "deutsch"},
        subtitle_hints={"german", "deutsch"},
    ),
    "it": LanguageProfile(
        code="it",
        label="Italian",
        aliases={"it", "ita", "italian", "it-it"},
        audio_hints={"italian", "italiano"},
        subtitle_hints={"italian", "italiano"},
    ),
}


def _aliases_for_canonical(code: str) -> set[str]:
    canonical = normalize_language_code(code)
    if not canonical:
        return set()
    aliases = {normalize_text(alias) for alias, value in LANGUAGE_ALIASES.items() if normalize_language_code(value) == canonical}
    aliases.add(canonical)
    return {alias for alias in aliases if alias}


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    stripped = "".join(character for character in normalized if not unicodedata.combining(character))
    return " ".join(stripped.lower().replace("_", " ").split())


def language_profile_for(code: str) -> LanguageProfile:
    normalized = normalize_text(code)
    canonical = normalize_language_code(code) or normalized
    if canonical in LANGUAGE_PROFILES:
        profile = LANGUAGE_PROFILES[canonical]
        merged_aliases = profile.aliases | _aliases_for_canonical(canonical)
        merged_audio_hints = profile.audio_hints | merged_aliases
        merged_subtitle_hints = profile.subtitle_hints | merged_aliases
        return LanguageProfile(
            code=profile.code,
            label=profile.label,
            aliases=merged_aliases,
            audio_hints=merged_audio_hints,
            subtitle_hints=merged_subtitle_hints,
        )

    label = normalize_text(language_display_name(canonical)) if canonical else normalized
    aliases = _aliases_for_canonical(canonical) | {value for value in {normalized, canonical, label} if value}
    if "-" in normalized:
        aliases.add(normalized.split("-", 1)[0])
    if "-" in canonical:
        aliases.add(canonical.split("-", 1)[0])

    return LanguageProfile(
        code=canonical,
        label=language_display_name(canonical) if canonical else f"Custom code ({normalized})",
        aliases=aliases,
        audio_hints=aliases,
        subtitle_hints=aliases,
    )


def _explicit_language_match(track: StreamInfo, profile: LanguageProfile) -> bool:
    raw_values = {
        normalize_text(track.language),
        normalize_text(track.language_ietf),
    }
    raw_values = {value for value in raw_values if value}
    canonical_values = {normalize_language_code(value) for value in raw_values if normalize_language_code(value)}
    if profile.code and profile.code in canonical_values:
        return True
    return any(
        value == alias or value.startswith(f"{alias}-") or alias.startswith(f"{value}-")
        for alias in profile.aliases
        for value in raw_values | canonical_values
    )


def _title_language_match(track: StreamInfo, profile: LanguageProfile) -> bool:
    title = normalize_text(track.title)
    return any(alias in title for alias in profile.aliases | profile.audio_hints | profile.subtitle_hints)


def _contains_any(text: str, keywords: set[str]) -> bool:
    normalized_keywords = {normalize_text(keyword) for keyword in keywords}
    return any(keyword and keyword in text for keyword in normalized_keywords)


def _audio_is_excluded(track: StreamInfo) -> bool:
    title = normalize_text(track.title)
    if _contains_any(title, AUDIO_EXCLUDED_KEYWORDS):
        return True
    return " ad " in f" {title} "


def _subtitle_is_excluded(track: StreamInfo) -> bool:
    return is_subtitle_forced_like(track) or is_subtitle_sdh_like(track) or is_subtitle_commentary_like(track)


def is_subtitle_dubtitle_like(track: StreamInfo) -> bool:
    title = normalize_text(track.title)
    return _contains_any(title, SUBTITLE_DISFAVORED_KEYWORDS)


def is_subtitle_forced_like(track: StreamInfo) -> bool:
    title = normalize_text(track.title)
    return bool(track.forced) or any(keyword in title for keyword in {"forced", "force", "forcee", "forcé"})


def is_subtitle_sdh_like(track: StreamInfo) -> bool:
    title = normalize_text(track.title)
    if bool(track.dispositions.get("hearing_impaired")):
        return True
    return _contains_any(title, {"sdh", "hi", "hearing impaired", "malentendant", "malentendants", "sourds", "cc"})


def is_subtitle_commentary_like(track: StreamInfo) -> bool:
    title = normalize_text(track.title)
    return _contains_any(title, {"commentary", "commentaire", "signs", "songs"})


def subtitle_matches_target_language(track: StreamInfo, profile: LanguageProfile) -> bool:
    return _explicit_language_match(track, profile) or _title_language_match(track, profile)


def subtitle_candidates_for_language(
    tracks: list[StreamInfo],
    target_code: str,
) -> tuple[list[StreamInfo], list[StreamInfo]]:
    profile = language_profile_for(target_code)
    language_tracks = [track for track in tracks if subtitle_matches_target_language(track, profile)]
    plausible_tracks = [
        track
        for track in language_tracks
        if not _subtitle_is_excluded(track) and not is_subtitle_dubtitle_like(track)
    ]
    return language_tracks, plausible_tracks


def _score_audio(track: StreamInfo, profile: LanguageProfile) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    title = normalize_text(track.title)

    if _explicit_language_match(track, profile):
        score += 80
        reasons.append("explicit language")
    elif _title_language_match(track, profile):
        score += 50
        reasons.append("language detected in title")
    else:
        reasons.append("preferred language not detected")

    if _audio_is_excluded(track):
        score -= 1000
        reasons.append("track excluded (commentary/description)")

    if _contains_any(title, profile.audio_hints):
        score += 15
        reasons.append("preferred title hint")

    if track.channels:
        score += min(track.channels, 8)
        reasons.append(f"{track.channels} channels")

    if track.default:
        score += 2
        reasons.append("already default")

    return score, reasons


def _score_subtitle(track: StreamInfo, profile: LanguageProfile) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    title = normalize_text(track.title)

    if _explicit_language_match(track, profile):
        score += 80
        reasons.append("explicit language")
    elif _title_language_match(track, profile):
        score += 40
        reasons.append("language detected in title")
    else:
        reasons.append("preferred language not detected")

    if _subtitle_is_excluded(track):
        score -= 1000
        reasons.append("track excluded (forced/SDH/HI/commentary)")

    if is_subtitle_dubtitle_like(track):
        score -= 60
        reasons.append("dubtitle avoided by default")

    if _contains_any(title, SUBTITLE_POSITIVE_KEYWORDS):
        score += 20
        reasons.append("preferred title hint")
    elif title:
        score += 5
        reasons.append("neutral title")

    if track.default:
        score += 1
        reasons.append("already default")

    return score, reasons


def select_audio_track(tracks: list[StreamInfo], target_code: str) -> SelectionDecision:
    profile = language_profile_for(target_code)
    ranked: list[tuple[int, StreamInfo, list[str]]] = []

    for track in tracks:
        score, reasons = _score_audio(track, profile)
        ranked.append((score, track, reasons))

    candidates = [item for item in ranked if item[0] > 0]
    candidates.sort(key=lambda item: (item[0], item[1].channels or 0), reverse=True)

    if not candidates:
        return SelectionDecision(
            status="not_found",
            selected_type_index=None,
            candidate_type_indices=[],
            confidence="none",
            reason=f"No usable audio track found in {profile.label}.",
        )

    if len(candidates) == 1:
        score, track, reasons = candidates[0]
        return SelectionDecision(
            status="selected",
            selected_type_index=track.type_index,
            candidate_type_indices=[track.type_index or -1],
            confidence="high" if score >= 80 else "medium",
            reason="Single clear audio choice.",
            details=["; ".join(reasons)],
        )

    top_score, top_track, top_reasons = candidates[0]
    second_score, second_track, second_reasons = candidates[1]
    delta = top_score - second_score

    if delta <= 2 and (top_track.channels or 0) == (second_track.channels or 0):
        return SelectionDecision(
            status="ambiguous",
            selected_type_index=None,
            candidate_type_indices=[track.type_index or -1 for _, track, _ in candidates[:5]],
            confidence="low",
            reason="Several equivalent audio tracks require confirmation.",
            details=[
                f"{top_track.short_label()}: {'; '.join(top_reasons)}",
                f"{second_track.short_label()}: {'; '.join(second_reasons)}",
            ],
            manual_required=True,
        )

    return SelectionDecision(
        status="selected",
        selected_type_index=top_track.type_index,
        candidate_type_indices=[track.type_index or -1 for _, track, _ in candidates[:5]],
        confidence="high" if delta >= 10 else "medium",
        reason="Best audio track selected automatically.",
        details=[f"{top_track.short_label()}: {'; '.join(top_reasons)}"],
    )


def select_subtitle_track(
    tracks: list[StreamInfo],
    target_code: str,
    *,
    prefer_srt_over_pgs: bool = False,
    auto_apply_unique_candidate: bool = True,
) -> SelectionDecision:
    profile = language_profile_for(target_code)
    language_tracks, plausible_tracks = subtitle_candidates_for_language(tracks, target_code)
    dubtitle_tracks = [track for track in language_tracks if is_subtitle_dubtitle_like(track)]
    non_excluded_language_tracks = [track for track in language_tracks if not _subtitle_is_excluded(track)]

    if auto_apply_unique_candidate and len(plausible_tracks) == 1:
        track = plausible_tracks[0]
        reasons = ["only plausible track in the preferred language"]
        if normalize_text(track.title):
            reasons.append("sufficient metadata")
        else:
            reasons.append("empty title but no plausible alternative")
        return SelectionDecision(
            status="selected",
            selected_type_index=track.type_index,
            candidate_type_indices=[track.type_index or -1],
            confidence="high",
            reason="Only one plausible subtitle track remains after exclusions.",
            details=["; ".join(reasons)],
        )

    if (
        not plausible_tracks
        and dubtitle_tracks
        and non_excluded_language_tracks
        and all(is_subtitle_dubtitle_like(track) for track in non_excluded_language_tracks)
    ):
        return SelectionDecision(
            status="ambiguous",
            selected_type_index=None,
            candidate_type_indices=[track.type_index or -1 for track in language_tracks[:5]],
            confidence="low",
            reason="Seuls des sous-titres Dubtitle ont ete detectes. Confirmation necessaire.",
            details=[
                f"{track.short_label()}: dubtitle detecte"
                for track in dubtitle_tracks[:5]
            ],
            manual_required=True,
        )

    ranked: list[tuple[int, StreamInfo, list[str]]] = []

    for track in tracks:
        score, reasons = _score_subtitle(track, profile)
        codec = normalize_text(track.codec)
        if prefer_srt_over_pgs:
            if "subrip" in codec or codec == "srt":
                score += 5
                reasons.append("preference SRT")
            if "pgs" in codec or "hdmv" in codec:
                score -= 2
                reasons.append("PGS is less preferred")
        ranked.append((score, track, reasons))

    candidates = [item for item in ranked if item[0] > 0]
    candidates.sort(key=lambda item: item[0], reverse=True)

    if not candidates:
        return SelectionDecision(
            status="not_found",
            selected_type_index=None,
            candidate_type_indices=[],
            confidence="none",
            reason=f"No usable subtitle track found for preferred language {profile.label}.",
        )

    clean_candidates = [item for item in candidates if item[1] in plausible_tracks]
    if len(clean_candidates) == 1:
        score, track, reasons = clean_candidates[0]
        return SelectionDecision(
            status="selected",
            selected_type_index=track.type_index,
            candidate_type_indices=[track.type_index or -1],
            confidence="high" if score >= 80 else "medium",
            reason="Single regular subtitle track selected.",
            details=["; ".join(reasons)],
        )

    target_candidates = [item for item in candidates if item[1] in language_tracks]
    if target_candidates:
        candidates = target_candidates

    top_score, top_track, top_reasons = candidates[0]
    if len(candidates) > 1:
        second_score, second_track, second_reasons = candidates[1]
        if top_score - second_score <= 5:
            return SelectionDecision(
                status="ambiguous",
                selected_type_index=None,
                candidate_type_indices=[track.type_index or -1 for _, track, _ in candidates[:5]],
                confidence="low",
                reason="Several subtitle tracks look valid.",
                details=[
                    f"{top_track.short_label()}: {'; '.join(top_reasons)}",
                    f"{second_track.short_label()}: {'; '.join(second_reasons)}",
                ],
                manual_required=True,
            )

    return SelectionDecision(
        status="selected",
        selected_type_index=top_track.type_index,
        candidate_type_indices=[track.type_index or -1 for _, track, _ in candidates[:5]],
        confidence="medium" if len(candidates) > 1 else "high",
        reason="Best subtitle track selected automatically.",
        details=[f"{top_track.short_label()}: {'; '.join(top_reasons)}"],
    )


def _track_signature_payload(track: StreamInfo) -> dict[str, object]:
    return {
        "language": normalize_text(track.language_ietf or track.language),
        "title": normalize_text(track.title),
        "codec": normalize_text(track.codec),
        "channels": track.channels,
        "default": track.default,
        "forced": track.forced,
    }


def build_track_signature(media_info: MediaFileReport) -> str:
    payload = {
        "audio_count": len(media_info.audio_tracks),
        "subtitle_count": len(media_info.subtitle_tracks),
        "audio": [_track_signature_payload(track) for track in media_info.audio_tracks],
        "subtitles": [_track_signature_payload(track) for track in media_info.subtitle_tracks],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _track_by_type_index(tracks: list[StreamInfo], type_index: int | None) -> StreamInfo | None:
    if type_index is None:
        return None
    for track in tracks:
        if track.type_index == type_index:
            return track
    return None


def build_track_reference(track: StreamInfo | None) -> TrackReference | None:
    if track is None:
        return None
    return TrackReference(
        kind=track.kind,
        language=normalize_text(track.language_ietf or track.language),
        title_normalized=normalize_text(track.title),
        codec=normalize_text(track.codec),
        forced=track.forced,
        channels=track.channels,
    )


def _build_candidate_group_signature(tracks: list[StreamInfo], candidate_indices: list[int]) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for type_index in candidate_indices:
        track = _track_by_type_index(tracks, type_index)
        if track is None:
            continue
        payload.append(
            {
                "language": normalize_text(track.language_ietf or track.language),
                "title": normalize_text(track.title),
                "codec": normalize_text(track.codec),
                "forced": track.forced,
            }
        )
    return payload


def build_candidate_signature(report: MediaFileReport) -> str:
    payload = {
        "audio_candidates": _build_candidate_group_signature(
            report.audio_tracks,
            report.audio_decision.candidate_type_indices,
        ),
        "subtitle_candidates": _build_candidate_group_signature(
            report.subtitle_tracks,
            report.subtitle_decision.candidate_type_indices,
        ),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _match_track_reference(tracks: list[StreamInfo], reference: TrackReference | None) -> StreamInfo | None:
    if reference is None:
        return None

    matches = [
        track
        for track in tracks
        if normalize_text(track.language_ietf or track.language) == reference.language
        and normalize_text(track.title) == reference.title_normalized
        and normalize_text(track.codec) == reference.codec
        and bool(track.forced) == bool(reference.forced)
        and (reference.channels is None or track.channels == reference.channels)
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def build_session_rule(
    report: MediaFileReport,
    scope: SessionRuleScope,
    *,
    audio_type_index: int | None,
    subtitle_type_index: int | None,
) -> SessionResolutionRule:
    audio_track = _track_by_type_index(report.audio_tracks, audio_type_index)
    subtitle_track = _track_by_type_index(report.subtitle_tracks, subtitle_type_index)
    descriptions = {
        "same_signature": "Apply to all files with the same track structure",
        "same_folder": "Apply to all files in this folder",
        "same_candidate_names": "Apply to remaining files with the same candidate track names",
    }
    return SessionResolutionRule(
        scope=scope,
        signature=report.track_signature,
        folder=str(report.path.parent),
        candidate_signature=report.candidate_signature,
        audio_reference=build_track_reference(audio_track),
        subtitle_reference=build_track_reference(subtitle_track),
        source_file=str(report.path),
        description=descriptions[scope],
    )


def _rule_matches_report(rule: SessionResolutionRule, report: MediaFileReport) -> bool:
    if rule.scope == "same_signature":
        return report.track_signature == rule.signature
    if rule.scope == "same_folder":
        return str(report.path.parent) == (rule.folder or "")
    if rule.scope == "same_candidate_names":
        return report.candidate_signature == rule.candidate_signature
    return False


def apply_session_rule(
    report: MediaFileReport,
    rule: SessionResolutionRule,
    *,
    logger=None,
) -> bool:
    if not report.needs_manual_review():
        return False
    if not _rule_matches_report(rule, report):
        return False

    audio_track = _match_track_reference(report.audio_tracks, rule.audio_reference)
    subtitle_track = _match_track_reference(report.subtitle_tracks, rule.subtitle_reference)

    if rule.audio_reference and audio_track is None:
        if logger is not None:
            logger.warning("Rule not applicable for %s: audio track not found.", report.path)
        return False
    if rule.subtitle_reference and subtitle_track is None:
        if logger is not None:
            logger.warning("Rule not applicable for %s: subtitle track not found.", report.path)
        return False

    apply_manual_selection(
        report,
        audio_track.type_index if audio_track else None,
        subtitle_track.type_index if subtitle_track else None,
    )
    report.resolution_source = "session_rule"
    report.plan.notes.append("Choice applied automatically from a session rule.")
    if logger is not None:
        logger.info("Choice applied automatically from a session rule: %s", report.path)
    return True


def apply_session_rules(
    report: MediaFileReport,
    rules: list[SessionResolutionRule],
    *,
    logger=None,
) -> bool:
    for rule in rules:
        if apply_session_rule(report, rule, logger=logger):
            return True
    return False


def build_plan(report: MediaFileReport) -> PlannedChange:
    notes: list[str] = []
    container = report.container
    tool = "mkvpropedit" if container == "mkv" else "ffmpeg"

    if report.errors:
        return PlannedChange(
            container=container,
            action="error",
            tool=tool,
            selected_audio=None,
            selected_subtitle=None,
            confidence="none",
            notes=notes,
            error="; ".join(report.errors),
        )

    if report.audio_decision.status == "selected" and report.subtitle_decision.status == "selected":
        notes.append("File is ready to be updated.")
        confidence = "high" if "low" not in {report.audio_decision.confidence, report.subtitle_decision.confidence} else "medium"
        return PlannedChange(
            container=container,
            action="apply",
            tool=tool,
            selected_audio=report.audio_decision.selected_type_index,
            selected_subtitle=report.subtitle_decision.selected_type_index,
            confidence=confidence,
            notes=notes,
        )

    if report.audio_decision.manual_required or report.subtitle_decision.manual_required:
        notes.append("Needs confirmation.")
        return PlannedChange(
            container=container,
            action="review",
            tool=tool,
            selected_audio=report.audio_decision.selected_type_index,
            selected_subtitle=report.subtitle_decision.selected_type_index,
            confidence="low",
            notes=notes,
        )

    notes.append("No reliable choice can be applied.")
    return PlannedChange(
        container=container,
        action="skip",
        tool=tool,
        selected_audio=report.audio_decision.selected_type_index,
        selected_subtitle=report.subtitle_decision.selected_type_index,
        confidence="none",
        notes=notes,
    )


def evaluate_report(
    report: MediaFileReport,
    audio_code: str,
    subtitle_code: str,
    *,
    prefer_srt_over_pgs: bool = False,
    auto_apply_unique_candidate: bool = True,
) -> MediaFileReport:
    report.audio_decision = select_audio_track(report.audio_tracks, audio_code)
    report.subtitle_decision = select_subtitle_track(
        report.subtitle_tracks,
        subtitle_code,
        prefer_srt_over_pgs=prefer_srt_over_pgs,
        auto_apply_unique_candidate=auto_apply_unique_candidate,
    )
    report.track_signature = build_track_signature(report)
    report.candidate_signature = build_candidate_signature(report)
    report.plan = build_plan(report)
    return report


def apply_manual_selection(
    report: MediaFileReport,
    audio_type_index: int | None,
    subtitle_type_index: int | None,
) -> MediaFileReport:
    if audio_type_index is not None:
        report.audio_decision = SelectionDecision(
            status="selected",
            selected_type_index=audio_type_index,
            candidate_type_indices=[audio_type_index],
            confidence="medium",
            reason="Choix audio confirme manuellement.",
            details=[],
            manual_required=False,
        )
    if subtitle_type_index is not None:
        report.subtitle_decision = SelectionDecision(
            status="selected",
            selected_type_index=subtitle_type_index,
            candidate_type_indices=[subtitle_type_index],
            confidence="medium",
            reason="Choix sous-titres confirme manuellement.",
            details=[],
            manual_required=False,
        )

    report.track_signature = build_track_signature(report)
    report.candidate_signature = build_candidate_signature(report)
    report.plan = build_plan(report)
    return report
