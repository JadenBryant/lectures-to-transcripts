from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[float, int, str], None]
STATE_FILENAME = ".lecture-transcriber-state.json"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
DOWNLOAD_PROGRESS_RE = re.compile(
    r"(?:\[download\]\s*)?"
    r"(?P<percent>\d+(?:\.\d+)?)%\s+"
    r"(?:of\s+~?[\w.]+\s+)?"
    r"(?:at\s+)?(?P<speed>[\w./-]+/s)?\s*"
    r"(?:ETA\s+(?P<eta>[\w:.-]+))?",
    re.IGNORECASE,
)
MEDIA_EXTENSIONS = {
    ".3gp",
    ".aac",
    ".avi",
    ".flac",
    ".flv",
    ".m4a",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
    ".wmv",
}


class PipelineError(RuntimeError):
    """Raised when the transcription pipeline cannot continue."""


@dataclass(frozen=True)
class TranscriptJob:
    groq_api_key: str
    output_dir: Path
    urls: list[str] = field(default_factory=list)
    input_dir: Path | None = None
    cookies_file: Path | None = None
    model: str = "whisper-large-v3-turbo"
    language: str = "en"
    prompt: str = ""
    chunk_seconds: int = 900
    keep_video_files: bool = True
    use_transcripts_subdir: bool = True


@dataclass(frozen=True)
class TranscriptResult:
    url: str
    title: str
    transcript_path: Path
    video_path: Path | None = None


def run_transcript_job(
    job: TranscriptJob,
    log: LogCallback,
    progress: ProgressCallback,
    cancel_event: threading.Event,
) -> list[TranscriptResult]:
    _validate_job(job)
    _require_command("ffmpeg")

    try:
        from groq import Groq
    except ImportError as exc:
        raise PipelineError(
            "Missing Groq dependency. Run: python -m pip install -r requirements.txt"
        ) from exc

    ydl_cls = None
    if job.urls:
        try:
            from yt_dlp import YoutubeDL
        except ImportError as exc:
            if shutil.which("yt-dlp") is None:
                raise PipelineError(
                    "yt-dlp is required for Panopto URL mode. Install the Python package "
                    "with 'python -m pip install -r requirements.txt' or make the yt-dlp "
                    "command available on PATH."
                ) from exc
        else:
            ydl_cls = YoutubeDL

    job.output_dir.mkdir(parents=True, exist_ok=True)
    _transcript_output_dir(job).mkdir(parents=True, exist_ok=True)
    if job.keep_video_files:
        (job.output_dir / "videos").mkdir(parents=True, exist_ok=True)
    resume_state = _load_resume_state(job.output_dir)
    client = Groq(api_key=job.groq_api_key)
    results: list[TranscriptResult] = []
    sources = _job_sources(job)
    total_sources = len(sources)

    for source_index, source in enumerate(sources, start=1):
        _raise_if_cancelled(cancel_event)
        source_base = float(source_index - 1)

        def source_progress(fraction: float, message: str) -> None:
            bounded_fraction = max(0.0, min(1.0, fraction))
            progress(source_base + bounded_fraction, total_sources, message)

        source_progress(0.0, f"Starting {source_index}/{total_sources}")
        log(f"[{source_index}/{total_sources}] Preparing {source}")
        source_key = _source_resume_key(source, job)
        resumed_result = _resume_result_if_complete(resume_state, source_key, job.output_dir, log)
        if resumed_result is not None:
            results.append(resumed_result)
            source_progress(1.0, f"Skipped completed {source_index}/{total_sources}")
            continue

        with _working_directory() as work_dir:
            if isinstance(source, Path):
                media_path, title = _local_media(source, log)
                source_progress(0.10, f"Loaded local media {source_index}/{total_sources}")
            else:
                media_path, title = _download_media(
                    ydl_cls,
                    source,
                    job.cookies_file,
                    work_dir,
                    log,
                    lambda percent: source_progress(
                        0.50 * (percent / 100),
                        f"Downloading {source_index}/{total_sources}: {percent:.1f}%",
                    ),
                    cancel_event,
                )
                source_progress(0.50, f"Downloaded {source_index}/{total_sources}")
            if not _has_audio_stream(media_path):
                log(f"Skipping media with no audio stream: {media_path.name}")
                source_progress(1.0, f"Skipped no-audio media {source_index}/{total_sources}")
                continue
            video_path = _save_video_copy(media_path, title, job.output_dir, log) if job.keep_video_files else None
            source_progress(0.55, f"Saved media {source_index}/{total_sources}")
            chunks_dir = work_dir / "chunks"
            chunk_paths = _chunk_audio(media_path, chunks_dir, job.chunk_seconds, log, cancel_event)
            source_progress(0.60, f"Created chunks {source_index}/{total_sources}")
            transcript_path = _transcribe_chunks(
                client,
                str(source),
                title,
                chunk_paths,
                job,
                log,
                lambda fraction, message: source_progress(0.60 + (0.40 * fraction), message),
                cancel_event,
            )
            results.append(
                TranscriptResult(
                    url=str(source),
                    title=title,
                    transcript_path=transcript_path,
                    video_path=video_path,
                )
            )
            _mark_source_complete(
                resume_state,
                source_key,
                job.output_dir,
                TranscriptResult(
                    url=str(source),
                    title=title,
                    transcript_path=transcript_path,
                    video_path=video_path,
                ),
            )

        source_progress(1.0, f"Finished {source_index}/{total_sources}")

    return results


def _validate_job(job: TranscriptJob) -> None:
    has_urls = bool(job.urls)
    has_input_dir = job.input_dir is not None
    if has_urls == has_input_dir:
        raise PipelineError("Choose either Panopto URLs or a folder of lecture videos.")
    if not job.groq_api_key.strip():
        raise PipelineError("Add your Groq API key.")
    if job.cookies_file and not job.cookies_file.exists():
        raise PipelineError(f"Cookies file does not exist: {job.cookies_file}")
    if job.input_dir and not job.input_dir.is_dir():
        raise PipelineError(f"Lecture video folder does not exist: {job.input_dir}")
    if job.chunk_seconds < 60:
        raise PipelineError("Chunk length must be at least 60 seconds.")


def _job_sources(job: TranscriptJob) -> list[str | Path]:
    if job.urls:
        return list(job.urls)
    if job.input_dir is None:
        return []

    media_files = [
        path
        for path in sorted(job.input_dir.iterdir(), key=lambda item: item.name.lower())
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
    ]
    if not media_files:
        supported = ", ".join(sorted(MEDIA_EXTENSIONS))
        raise PipelineError(
            f"No supported media files were found in {job.input_dir}.\nSupported extensions: {supported}"
        )
    return media_files


def _source_resume_key(source: str | Path, job: TranscriptJob) -> str:
    source_payload: dict[str, object]
    if isinstance(source, Path):
        try:
            stat = source.stat()
            source_payload = {
                "type": "file",
                "path": str(source.resolve()),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        except OSError:
            source_payload = {"type": "file", "path": str(source)}
    else:
        source_payload = {"type": "url", "url": source.strip()}

    payload = {
        "source": source_payload,
        "model": job.model,
        "language": job.language,
        "prompt": job.prompt,
        "chunk_seconds": job.chunk_seconds,
        "use_transcripts_subdir": job.use_transcripts_subdir,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_resume_state(output_dir: Path) -> dict:
    state_path = output_dir / STATE_FILENAME
    if not state_path.exists():
        return {"version": 1, "items": {}}

    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "items": {}}

    if not isinstance(data, dict):
        return {"version": 1, "items": {}}
    items = data.get("items")
    if not isinstance(items, dict):
        data["items"] = {}
    data["version"] = 1
    return data


def _resume_result_if_complete(
    state: dict,
    source_key: str,
    output_dir: Path,
    log: LogCallback,
) -> TranscriptResult | None:
    item = state.get("items", {}).get(source_key)
    if not isinstance(item, dict) or item.get("status") != "done":
        return None

    transcript_path = _state_path(output_dir, item.get("transcript_path"))
    if transcript_path is None or not transcript_path.exists():
        return None

    video_path = _state_path(output_dir, item.get("video_path"))
    result = TranscriptResult(
        url=str(item.get("source") or ""),
        title=str(item.get("title") or transcript_path.stem),
        transcript_path=transcript_path,
        video_path=video_path if video_path and video_path.exists() else None,
    )
    log(f"Skipping completed lecture: {result.title}")
    return result


def _mark_source_complete(
    state: dict,
    source_key: str,
    output_dir: Path,
    result: TranscriptResult,
) -> None:
    state.setdefault("items", {})[source_key] = {
        "status": "done",
        "source": result.url,
        "title": result.title,
        "transcript_path": _relative_state_path(output_dir, result.transcript_path),
        "video_path": _relative_state_path(output_dir, result.video_path) if result.video_path else None,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_resume_state(output_dir, state)


def _write_resume_state(output_dir: Path, state: dict) -> None:
    state_path = output_dir / STATE_FILENAME
    temp_path = output_dir / f"{STATE_FILENAME}.tmp"
    temp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(state_path)


def _relative_state_path(output_dir: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(output_dir.resolve()))
    except ValueError:
        return str(path)


def _state_path(output_dir: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return output_dir / path


def _require_command(command: str) -> None:
    if shutil.which(command) is None:
        raise PipelineError(
            f"'{command}' was not found on PATH. Install ffmpeg and restart this app."
        )


def _has_audio_stream(media_path: Path) -> bool:
    ffprobe_command = shutil.which("ffprobe")
    if ffprobe_command is None:
        return True

    command = [
        ffprobe_command,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        str(media_path),
    ]
    process = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return "audio" in process.stdout.lower()


def _working_directory():
    return _TemporaryDirectory()


class _TemporaryDirectory:
    def __init__(self) -> None:
        import tempfile

        self._manager = tempfile.TemporaryDirectory(prefix="lecture-transcriber-")

    def __enter__(self) -> Path:
        return Path(self._manager.__enter__())

    def __exit__(self, exc_type, exc, tb) -> bool:
        return self._manager.__exit__(exc_type, exc, tb)


def _download_media(
    ydl_cls,
    url: str,
    cookies_file: Path | None,
    work_dir: Path,
    log: LogCallback,
    progress_update: Callable[[float], None],
    cancel_event: threading.Event,
) -> tuple[Path, str]:
    if ydl_cls is None:
        return _download_media_with_cli(url, cookies_file, work_dir, log, progress_update, cancel_event)

    log("Downloading media with yt-dlp...")
    download_dir = work_dir / "download"
    download_dir.mkdir(parents=True, exist_ok=True)
    last_progress_bucket: int | None = None

    def hook(status: dict) -> None:
        nonlocal last_progress_bucket
        _raise_if_cancelled(cancel_event)
        if status.get("status") == "downloading":
            percent = _clean_log_message(status.get("_percent_str", ""))
            progress_percent = _progress_percent(percent)
            if progress_percent is not None:
                progress_update(progress_percent)
            progress_bucket = _progress_bucket(percent)
            if progress_bucket is not None and progress_bucket == last_progress_bucket:
                return
            last_progress_bucket = progress_bucket
            speed = _clean_log_message(status.get("_speed_str", ""))
            eta = _clean_log_message(status.get("_eta_str", ""))
            details = " ".join(part for part in (percent, speed, f"ETA {eta}" if eta else "") if part)
            if details:
                log(f"yt-dlp: {details}")
        elif status.get("status") == "finished":
            log("Download complete.")

    options = {
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "outtmpl": str(download_dir / "%(title).200B.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    if cookies_file:
        options["cookiefile"] = str(cookies_file)

    with ydl_cls(options) as ydl:
        info = ydl.extract_info(url, download=True)

    title = _safe_title(info.get("title") or "lecture")
    media_path = _downloaded_path(info, download_dir)
    log(f"Saved media: {media_path.name}")
    return media_path, title


def _download_media_with_cli(
    url: str,
    cookies_file: Path | None,
    work_dir: Path,
    log: LogCallback,
    progress_update: Callable[[float], None],
    cancel_event: threading.Event,
) -> tuple[Path, str]:
    yt_dlp_command = shutil.which("yt-dlp")
    if yt_dlp_command is None:
        raise PipelineError("yt-dlp is required to download Panopto URLs.")

    log("Downloading media with yt-dlp command...")
    download_dir = work_dir / "download"
    download_dir.mkdir(parents=True, exist_ok=True)
    output_template = download_dir / "%(title).200B.%(ext)s"
    command = [
        yt_dlp_command,
        "--newline",
        "--no-colors",
        "--no-playlist",
        "--no-warnings",
        "-f",
        "bestvideo*+bestaudio/best",
        "--merge-output-format",
        "mp4",
        "-o",
        str(output_template),
    ]
    if cookies_file:
        command.extend(["--cookies", str(cookies_file)])
    command.append(url)

    _run_logged_process(command, cancel_event, log, "yt-dlp", progress_update)
    media_path = _downloaded_path({}, download_dir)
    title = _safe_title(media_path.stem)
    log(f"Saved media: {media_path.name}")
    return media_path, title


def _local_media(media_path: Path, log: LogCallback) -> tuple[Path, str]:
    title = _safe_title(media_path.stem)
    log(f"Using local media: {media_path}")
    return media_path, title


def _save_video_copy(media_path: Path, title: str, output_dir: Path, log: LogCallback) -> Path:
    videos_dir = output_dir / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)
    try:
        if media_path.resolve().parent == videos_dir.resolve():
            log(f"Lecture media already in output videos folder: {media_path}")
            return media_path
    except OSError:
        pass

    destination = _unique_output_path(videos_dir, f"{title}{media_path.suffix}")
    shutil.copy2(media_path, destination)
    log(f"Kept lecture media: {destination}")
    return destination


def _downloaded_path(info: dict, download_dir: Path) -> Path:
    for item in info.get("requested_downloads") or []:
        filepath = item.get("filepath")
        if filepath and Path(filepath).exists():
            return Path(filepath)

    filepath = info.get("filepath") or info.get("_filename")
    if filepath and Path(filepath).exists():
        return Path(filepath)

    files = [path for path in download_dir.iterdir() if path.is_file()]
    if not files:
        raise PipelineError("yt-dlp finished, but no downloaded media file was found.")
    return max(files, key=lambda path: path.stat().st_mtime)


def _chunk_audio(
    audio_path: Path,
    chunks_dir: Path,
    chunk_seconds: int,
    log: LogCallback,
    cancel_event: threading.Event,
) -> list[Path]:
    log(f"Creating {chunk_seconds // 60:g}-minute MP3 chunks...")
    chunks_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = chunks_dir / "chunk_%03d.mp3"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(audio_path),
        "-vn",
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "48k",
        "-f",
        "segment",
        "-segment_time",
        str(chunk_seconds),
        "-reset_timestamps",
        "1",
        str(output_pattern),
    ]
    _run_process(command, cancel_event)

    chunk_paths = sorted(chunks_dir.glob("chunk_*.mp3"))
    if not chunk_paths:
        raise PipelineError("ffmpeg did not create any audio chunks.")

    log(f"Created {len(chunk_paths)} chunk(s).")
    return chunk_paths


def _run_process(command: list[str], cancel_event: threading.Event) -> None:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output_tail: list[str] = []

    def collect_output() -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            output_tail.append(line.rstrip())
            del output_tail[:-80]

    output_thread = threading.Thread(target=collect_output, daemon=True)
    output_thread.start()

    while process.poll() is None:
        if cancel_event.is_set():
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise PipelineError("Cancelled.")
        cancel_event.wait(0.2)

    output_thread.join(timeout=2)
    if process.returncode != 0:
        details = "\n".join(output_tail).strip()
        if len(details) > 1600:
            details = details[-1600:]
        raise PipelineError(f"ffmpeg failed:\n{details}")


def _run_logged_process(
    command: list[str],
    cancel_event: threading.Event,
    log: LogCallback,
    label: str,
    progress_update: Callable[[float], None] | None = None,
) -> None:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output_tail: list[str] = []
    last_progress_bucket: int | None = None

    def collect_output() -> None:
        nonlocal last_progress_bucket
        if process.stdout is None:
            return
        for line in process.stdout:
            message = _clean_yt_dlp_line(line) if label == "yt-dlp" else _clean_log_message(line)
            if not message:
                continue
            output_tail.append(message)
            del output_tail[:-80]
            if label == "yt-dlp":
                progress_percent = _progress_percent(message)
                if progress_percent is not None and progress_update is not None:
                    progress_update(progress_percent)
                progress_bucket = _progress_bucket(message)
                if progress_bucket is not None and progress_bucket == last_progress_bucket:
                    continue
                last_progress_bucket = progress_bucket
            log(f"{label}: {message}")

    output_thread = threading.Thread(target=collect_output, daemon=True)
    output_thread.start()

    while process.poll() is None:
        if cancel_event.is_set():
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise PipelineError("Cancelled.")
        cancel_event.wait(0.2)

    output_thread.join(timeout=2)
    if process.returncode != 0:
        details = "\n".join(output_tail).strip()
        if len(details) > 1600:
            details = details[-1600:]
        raise PipelineError(f"{label} failed:\n{details}")


def _clean_log_message(message: object) -> str:
    cleaned = ANSI_ESCAPE_RE.sub("", str(message))
    cleaned = cleaned.replace("\r", "").strip()
    return re.sub(r"\s+", " ", cleaned)


def _clean_yt_dlp_line(line: str) -> str:
    message = _clean_log_message(line)
    if not message:
        return ""

    progress_match = DOWNLOAD_PROGRESS_RE.search(message)
    if progress_match:
        percent = progress_match.group("percent")
        speed = progress_match.group("speed")
        eta = progress_match.group("eta")
        parts = [f"{percent}%"]
        if speed and speed != "Unknown":
            parts.append(f"at {speed}")
        if eta and eta != "Unknown":
            parts.append(f"ETA {eta}")
        return " ".join(parts)

    return message


def _progress_bucket(message: str) -> int | None:
    progress_percent = _progress_percent(message)
    if progress_percent is None:
        return None
    return int(progress_percent)


def _progress_percent(message: str) -> float | None:
    match = re.search(r"(?P<percent>\d+(?:\.\d+)?)%", message)
    if not match:
        return None
    return max(0.0, min(100.0, float(match.group("percent"))))


def _transcribe_chunks(
    client,
    url: str,
    title: str,
    chunk_paths: list[Path],
    job: TranscriptJob,
    log: LogCallback,
    progress_update: Callable[[float, str], None],
    cancel_event: threading.Event,
) -> Path:
    transcript_parts: list[str] = []
    total_chunks = len(chunk_paths)

    for chunk_index, chunk_path in enumerate(chunk_paths, start=1):
        _raise_if_cancelled(cancel_event)
        size_mb = chunk_path.stat().st_size / (1024 * 1024)
        message = f"Transcribing chunk {chunk_index}/{total_chunks}"
        progress_update((chunk_index - 1) / total_chunks, message)
        log(f"{message} ({size_mb:.1f} MB)...")

        with chunk_path.open("rb") as audio_file:
            kwargs = {
                "file": audio_file,
                "model": job.model,
                "response_format": "text",
                "temperature": 0.0,
            }
            if job.language.strip():
                kwargs["language"] = job.language.strip()
            if job.prompt.strip():
                kwargs["prompt"] = job.prompt.strip()

            transcription = client.audio.transcriptions.create(**kwargs)

        transcript_parts.append(_extract_text(transcription))
        progress_update(chunk_index / total_chunks, f"Transcribed chunk {chunk_index}/{total_chunks}")

    transcript = "\n\n".join(part.strip() for part in transcript_parts if part.strip())
    if not transcript:
        raise PipelineError("Groq returned an empty transcript.")

    transcripts_dir = _transcript_output_dir(job)
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    output_path = _unique_output_path(transcripts_dir, f"{title}.txt")
    header = f"Title: {title}\nSource: {url}\nModel: {job.model}\n\n"
    output_path.write_text(header + transcript + "\n", encoding="utf-8")
    log(f"Wrote transcript: {output_path}")
    return output_path


def _extract_text(transcription) -> str:
    if isinstance(transcription, str):
        return transcription
    text = getattr(transcription, "text", None)
    if text is not None:
        return str(text)
    if isinstance(transcription, dict) and "text" in transcription:
        return str(transcription["text"])
    return str(transcription)


def _unique_output_path(output_dir: Path, filename: str) -> Path:
    stem = _safe_title(Path(filename).stem)
    suffix = Path(filename).suffix or ".txt"
    candidate = output_dir / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = output_dir / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def _transcript_output_dir(job: TranscriptJob) -> Path:
    if job.use_transcripts_subdir:
        return job.output_dir / "transcripts"
    return job.output_dir


def _safe_title(title: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", " ", title).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:150] or "lecture"


def _raise_if_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise PipelineError("Cancelled.")
