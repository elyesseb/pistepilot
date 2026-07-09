from __future__ import annotations

from pathlib import Path
from typing import Any

from pistepilot.ffmpeg_tools import run_json_command
from pistepilot.models import MediaFileReport, PlannedChange, SelectionDecision, StreamInfo, Toolset


EMPTY_DECISION = SelectionDecision(
    status="skipped",
    selected_type_index=None,
    candidate_type_indices=[],
    confidence="none",
    reason="No selection has been made yet.",
)


class MediaAnalyzer:
    def __init__(self, toolset: Toolset, logger) -> None:
        self.toolset = toolset
        self.logger = logger

    def analyze_file(self, file_path: Path) -> MediaFileReport:
        suffix = file_path.suffix.lower()
        if suffix == ".mkv" and self.toolset.is_available("mkvmerge"):
            self.logger.info("Analyzing MKV via mkvmerge: %s", file_path)
            payload = run_json_command([self.toolset.path_for("mkvmerge") or "mkvmerge", "-J", str(file_path)])
            audio_tracks, subtitle_tracks, video_codec = self._parse_mkvmerge_tracks(payload)
            analysis_tool = "mkvmerge"
        elif self.toolset.is_available("ffprobe"):
            self.logger.info("Analyzing via ffprobe: %s", file_path)
            payload = run_json_command(
                [
                    self.toolset.path_for("ffprobe") or "ffprobe",
                    "-v",
                    "quiet",
                    "-print_format",
                    "json",
                    "-show_streams",
                    "-show_format",
                    str(file_path),
                ]
            )
            audio_tracks, subtitle_tracks, video_codec = self._parse_ffprobe_streams(payload)
            analysis_tool = "ffprobe"
        else:
            raise RuntimeError(
                "Unable to analyze files: neither mkvmerge nor ffprobe is available."
            )

        return MediaFileReport(
            path=file_path,
            container=suffix.lstrip("."),
            analysis_tool=analysis_tool,
            video_codec=video_codec,
            audio_tracks=audio_tracks,
            subtitle_tracks=subtitle_tracks,
            audio_decision=EMPTY_DECISION,
            subtitle_decision=EMPTY_DECISION,
            plan=PlannedChange(
                container=suffix.lstrip("."),
                action="skip",
                tool=None,
                selected_audio=None,
                selected_subtitle=None,
                confidence="none",
                notes=["Analysis complete, selection not computed yet."],
            ),
        )

    def _parse_mkvmerge_tracks(self, payload: dict[str, Any]) -> tuple[list[StreamInfo], list[StreamInfo], str | None]:
        counters = {"audio": 0, "subtitles": 0}
        audio_tracks: list[StreamInfo] = []
        subtitle_tracks: list[StreamInfo] = []
        video_codec: str | None = None

        for track in payload.get("tracks", []):
            track_type = track.get("type")
            properties = track.get("properties", {})

            if track_type == "audio":
                counters["audio"] += 1
                audio_tracks.append(
                    StreamInfo(
                        kind="audio",
                        id=track.get("id"),
                        type_index=counters["audio"],
                        codec=track.get("codec"),
                        language=properties.get("language"),
                        language_ietf=properties.get("language_ietf"),
                        title=properties.get("track_name"),
                        channels=properties.get("audio_channels"),
                        default=bool(properties.get("default_track")),
                        forced=bool(properties.get("forced_track")),
                        tags={},
                        dispositions={
                            "default": int(bool(properties.get("default_track"))),
                            "forced": int(bool(properties.get("forced_track"))),
                        },
                        raw=track,
                    )
                )
            elif track_type == "subtitles":
                counters["subtitles"] += 1
                subtitle_tracks.append(
                    StreamInfo(
                        kind="subtitle",
                        id=track.get("id"),
                        type_index=counters["subtitles"],
                        codec=track.get("codec"),
                        language=properties.get("language"),
                        language_ietf=properties.get("language_ietf"),
                        title=properties.get("track_name"),
                        channels=None,
                        default=bool(properties.get("default_track")),
                        forced=bool(properties.get("forced_track")),
                        tags={},
                        dispositions={
                            "default": int(bool(properties.get("default_track"))),
                            "forced": int(bool(properties.get("forced_track"))),
                        },
                        raw=track,
                    )
                )
            elif track_type == "video" and video_codec is None:
                video_codec = track.get("codec")

        return audio_tracks, subtitle_tracks, video_codec

    def _parse_ffprobe_streams(self, payload: dict[str, Any]) -> tuple[list[StreamInfo], list[StreamInfo], str | None]:
        counters = {"audio": 0, "subtitle": 0}
        audio_tracks: list[StreamInfo] = []
        subtitle_tracks: list[StreamInfo] = []
        video_codec: str | None = None

        for stream in payload.get("streams", []):
            codec_type = stream.get("codec_type")
            tags = stream.get("tags", {})
            dispositions = stream.get("disposition", {})

            if codec_type == "audio":
                counters["audio"] += 1
                audio_tracks.append(
                    StreamInfo(
                        kind="audio",
                        id=stream.get("index"),
                        type_index=counters["audio"],
                        codec=stream.get("codec_name"),
                        language=tags.get("language"),
                        language_ietf=tags.get("LANGUAGE"),
                        title=tags.get("title") or tags.get("TITLE"),
                        channels=stream.get("channels"),
                        default=bool(dispositions.get("default")),
                        forced=bool(dispositions.get("forced")),
                        tags=tags,
                        dispositions=dispositions,
                        raw=stream,
                    )
                )
            elif codec_type == "subtitle":
                counters["subtitle"] += 1
                subtitle_tracks.append(
                    StreamInfo(
                        kind="subtitle",
                        id=stream.get("index"),
                        type_index=counters["subtitle"],
                        codec=stream.get("codec_name"),
                        language=tags.get("language"),
                        language_ietf=tags.get("LANGUAGE"),
                        title=tags.get("title") or tags.get("TITLE"),
                        channels=None,
                        default=bool(dispositions.get("default")),
                        forced=bool(dispositions.get("forced")),
                        tags=tags,
                        dispositions=dispositions,
                        raw=stream,
                    )
                )
            elif codec_type == "video" and video_codec is None:
                video_codec = stream.get("codec_name")

        return audio_tracks, subtitle_tracks, video_codec
