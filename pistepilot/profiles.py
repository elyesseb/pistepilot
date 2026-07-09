from __future__ import annotations

import json
from pathlib import Path

from pistepilot.ffmpeg_tools import get_runtime_root
from pistepilot.models import ProfileSettings


DEFAULT_PROFILE = ProfileSettings(
    name="Default French",
    audio_language="fr",
    subtitle_language="fr",
    subtitle_policy="full_non_forced_non_sdh",
    auto_apply_unique_candidate=True,
    auto_group_series=True,
    prefer_srt_over_pgs=True,
    exclude_forced=True,
    exclude_sdh=True,
    exclude_commentary=True,
    exclude_dubtitle=True,
)


def get_config_dir() -> Path:
    config_dir = get_runtime_root() / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_profiles_path() -> Path:
    return get_config_dir() / "profiles.json"


def _default_payload() -> dict:
    return {
        "profiles": [DEFAULT_PROFILE.to_dict()],
        "last_used": DEFAULT_PROFILE.name,
    }


def ensure_profiles_file() -> Path:
    profiles_path = get_profiles_path()
    if not profiles_path.exists():
        profiles_path.write_text(json.dumps(_default_payload(), indent=2, ensure_ascii=False), encoding="utf-8")
    return profiles_path


def load_profiles_data() -> dict:
    profiles_path = ensure_profiles_file()
    try:
        payload = json.loads(profiles_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = _default_payload()
        profiles_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    if "profiles" not in payload or not payload["profiles"]:
        payload = _default_payload()
        profiles_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def load_profiles() -> tuple[list[ProfileSettings], str]:
    payload = load_profiles_data()
    profiles = [ProfileSettings(**profile_data) for profile_data in payload.get("profiles", [])]
    last_used = payload.get("last_used", profiles[0].name if profiles else DEFAULT_PROFILE.name)
    return profiles, last_used


def save_profiles(profiles: list[ProfileSettings], *, last_used: str) -> Path:
    profiles_path = get_profiles_path()
    payload = {
        "profiles": [profile.to_dict() for profile in profiles],
        "last_used": last_used,
    }
    profiles_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return profiles_path


def get_profile_by_name(name: str | None) -> ProfileSettings:
    profiles, last_used = load_profiles()
    selected_name = name or last_used
    for profile in profiles:
        if profile.name == selected_name:
            return profile
    return profiles[0] if profiles else DEFAULT_PROFILE


def save_last_used_profile(name: str) -> Path:
    profiles, _ = load_profiles()
    return save_profiles(profiles, last_used=name)


def upsert_profile(profile: ProfileSettings) -> Path:
    profiles, last_used = load_profiles()
    updated: list[ProfileSettings] = []
    replaced = False
    for existing in profiles:
        if existing.name == profile.name:
            updated.append(profile)
            replaced = True
        else:
            updated.append(existing)
    if not replaced:
        updated.append(profile)
    return save_profiles(updated, last_used=profile.name if last_used == profile.name or not replaced else last_used)
