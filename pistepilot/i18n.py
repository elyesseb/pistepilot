from __future__ import annotations

import os


DEFAULT_LANGUAGE = "en"

TEXT: dict[str, dict[str, str]] = {
    "en": {
        "choose_folder": "Choose folder",
        "include_subfolders": "Include subfolders",
        "audio": "Audio",
        "subtitles": "Subtitles",
        "options": "Options",
        "analyze_folder": "Analyze folder",
        "confirm_subtitles": "Confirm subtitles",
        "apply": "Apply",
        "recent_activity": "Recent activity",
        "press_enter_to_close": "Press Enter to close PistePilot...",
        "manual_folder_prompt": "Folder to analyze",
        "directory_dialog_title": "Choose the folder containing the videos to analyze",
    },
    "fr": {
        "choose_folder": "Choisir un dossier",
        "include_subfolders": "Inclure les sous-dossiers",
        "audio": "Audio",
        "subtitles": "Sous-titres",
        "options": "Options",
        "analyze_folder": "Analyser le dossier",
        "confirm_subtitles": "Confirmer les sous-titres",
        "apply": "Appliquer",
        "recent_activity": "Activite recente",
        "press_enter_to_close": "Appuyez sur Entree pour fermer PistePilot...",
        "manual_folder_prompt": "Dossier a analyser",
        "directory_dialog_title": "Choisir le dossier contenant les videos a analyser",
    },
}

CANONICAL_LANGUAGE_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "ar": "Arabic",
        "cs": "Czech",
        "da": "Danish",
        "de": "German",
        "el": "Greek",
        "en": "English",
        "es": "Spanish",
        "fi": "Finnish",
        "fil": "Filipino",
        "fr": "French",
        "hi": "Hindi",
        "hu": "Hungarian",
        "id": "Indonesian",
        "it": "Italian",
        "ja": "Japanese",
        "ko": "Korean",
        "nl": "Dutch",
        "pl": "Polish",
        "pt": "Portuguese",
        "zh": "Chinese",
    },
    "fr": {
        "ar": "Arabe",
        "cs": "Tcheque",
        "da": "Danois",
        "de": "Allemand",
        "el": "Grec",
        "en": "Anglais",
        "es": "Espagnol",
        "fi": "Finnois",
        "fil": "Filipino",
        "fr": "Francais",
        "hi": "Hindi",
        "hu": "Hongrois",
        "id": "Indonesien",
        "it": "Italien",
        "ja": "Japonais",
        "ko": "Coreen",
        "nl": "Neerlandais",
        "pl": "Polonais",
        "pt": "Portugais",
        "zh": "Chinois",
    },
}

LANGUAGE_ALIASES: dict[str, str] = {
    "ar": "ar",
    "ara": "ar",
    "arabic": "ar",
    "cs": "cs",
    "ces": "cs",
    "cze": "cs",
    "czech": "cs",
    "da": "da",
    "dan": "da",
    "danish": "da",
    "de": "de",
    "de-de": "de",
    "deu": "de",
    "ger": "de",
    "german": "de",
    "deutsch": "de",
    "el": "el",
    "ell": "el",
    "gre": "el",
    "greek": "el",
    "en": "en",
    "eng": "en",
    "en-gb": "en",
    "en-us": "en",
    "english": "en",
    "es": "es",
    "es-419": "es",
    "es-es": "es",
    "es-la": "es",
    "spa": "es",
    "spanish": "es",
    "espanol": "es",
    "español": "es",
    "fi": "fi",
    "fin": "fi",
    "finnish": "fi",
    "fil": "fil",
    "tl": "fil",
    "tgl": "fil",
    "tagalog": "fil",
    "filipino": "fil",
    "fr": "fr",
    "fra": "fr",
    "fre": "fr",
    "fr-fr": "fr",
    "french": "fr",
    "francais": "fr",
    "français": "fr",
    "hi": "hi",
    "hin": "hi",
    "hindi": "hi",
    "hu": "hu",
    "hun": "hu",
    "hungarian": "hu",
    "id": "id",
    "ind": "id",
    "indonesian": "id",
    "it": "it",
    "ita": "it",
    "it-it": "it",
    "italian": "it",
    "ja": "ja",
    "jp": "ja",
    "jpn": "ja",
    "japanese": "ja",
    "ko": "ko",
    "kor": "ko",
    "korean": "ko",
    "nl": "nl",
    "dut": "nl",
    "nld": "nl",
    "dutch": "nl",
    "pl": "pl",
    "pol": "pl",
    "polish": "pl",
    "pt": "pt",
    "pt-br": "pt",
    "por": "pt",
    "portuguese": "pt",
    "zh": "zh",
    "zh-cn": "zh",
    "zh-tw": "zh",
    "chi": "zh",
    "zho": "zh",
    "chinese": "zh",
}

LANGUAGE_PRIORITY = ("fr", "en", "ja", "ko", "es")


def get_language() -> str:
    value = os.environ.get("PISTEPILOT_LANG", DEFAULT_LANGUAGE).strip().lower()
    return value if value in TEXT else DEFAULT_LANGUAGE


def t(key: str, *, lang: str | None = None) -> str:
    language = lang or get_language()
    return TEXT.get(language, TEXT[DEFAULT_LANGUAGE]).get(key, key)


def normalize_language_code(code: str | None) -> str:
    if not code:
        return ""
    normalized = code.strip().replace("_", "-").lower()
    if not normalized:
        return ""
    if normalized in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[normalized]
    if "-" in normalized:
        base = normalized.split("-", 1)[0]
        if base in LANGUAGE_ALIASES:
            return LANGUAGE_ALIASES[base]
        return base
    return normalized


def language_display_name(code: str | None, *, lang: str | None = None) -> str:
    language = lang or get_language()
    canonical = normalize_language_code(code)
    if not canonical:
        return "Unknown" if language == "en" else "Inconnu"
    labels = CANONICAL_LANGUAGE_LABELS.get(language, CANONICAL_LANGUAGE_LABELS[DEFAULT_LANGUAGE])
    return labels.get(canonical, canonical.upper())


def display_language_name(code: str, *, lang: str | None = None) -> str:
    return language_display_name(code, lang=lang)


def sort_language_codes(codes: set[str] | list[str] | tuple[str, ...]) -> list[str]:
    canonical = {normalize_language_code(code) for code in codes if normalize_language_code(code)}
    return sorted(
        canonical,
        key=lambda code: (
            LANGUAGE_PRIORITY.index(code) if code in LANGUAGE_PRIORITY else len(LANGUAGE_PRIORITY),
            language_display_name(code),
        ),
    )
