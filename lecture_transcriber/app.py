from __future__ import annotations

import os
import queue
import subprocess
import sys
import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from lecture_transcriber.pipeline import PipelineError, TranscriptJob, run_transcript_job


APP_TITLE = "Lecture Transcriber"
DEFAULT_MODEL = "whisper-large-v3-turbo"
WINDOW_BG = "#f5f5f7"
FIELD_BG = "#ffffff"
FIELD_DISABLED_BG = "#ececf1"
TEXT_FG = "#1d1d1f"
MUTED_FG = "#6e6e73"
FIELD_BORDER = "#8e8e93"
CONTROL_BG = "#e9e9ed"
CONTROL_ACTIVE_BG = "#d9d9df"
CONTROL_PRESSED_BG = "#c9c9d0"
ACCENT = "#0a84ff"
ACCENT_ACTIVE = "#0071e3"
SETTINGS_FILENAME = "settings.json"


def _settings_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "LectureTranscriber"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LectureTranscriber"

    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "lecture-transcriber"
    return Path.home() / ".config" / "lecture-transcriber"


SETTINGS_PATH = _settings_dir() / SETTINGS_FILENAME


def _load_settings() -> dict[str, str]:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(value, str)}


def _write_settings(settings: dict[str, str]) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = SETTINGS_PATH.with_suffix(f"{SETTINGS_PATH.suffix}.tmp")
        temp_path.write_text(json.dumps(settings, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(SETTINGS_PATH)
    except OSError:
        # Remembering these fields is helpful, but it should never block a job.
        pass


def _looks_like_invalid_api_key_error(error: object) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code in {401, 403}:
        return True
    name = type(error).__name__.lower()
    message = str(error).lower()
    return (
        "authentication" in name
        or "unauthorized" in name
        or "permissiondenied" in name
        or "invalid api key" in message
        or "incorrect api key" in message
        or "unauthorized" in message
        or "forbidden" in message
    )


def _looks_like_invalid_cookies_error(error: object) -> bool:
    name = type(error).__name__.lower()
    message = str(error).lower()
    came_from_downloader = (
        "download" in name
        or "yt-dlp" in message
        or "cookie" in message
        or "cookies" in message
    )
    return came_from_downloader and any(
        marker in message
        for marker in (
            "cookie",
            "cookies",
            "login",
            "sign in",
            "signin",
            "unauthorized",
            "forbidden",
            "private",
        )
    )


class LectureTranscriberApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.minsize(920, 680)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.closing_after_cancel = False
        self.settings = _load_settings()
        had_saved_cookies = bool(self.settings.get("cookies_file", "").strip())
        saved_cookies = self._valid_saved_cookies()
        saved_api_key = os.environ.get("GROQ_API_KEY") or self.settings.get("groq_api_key", "")

        self.source_mode_var = tk.StringVar(value="urls")
        self.input_dir_var = tk.StringVar()
        self.cookies_var = tk.StringVar(value=saved_cookies)
        self.api_key_var = tk.StringVar(value=saved_api_key)
        self.output_var = tk.StringVar(value=str(Path.home() / "Documents" / "Lecture Transcripts"))
        self.model_var = tk.StringVar(value=DEFAULT_MODEL)
        self.language_var = tk.StringVar(value="en")
        self.chunk_minutes_var = tk.IntVar(value=15)
        self.status_var = tk.StringVar(value="Ready")

        self._build_styles()
        self._build_ui()
        self._update_source_mode()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_events()

        if had_saved_cookies and not saved_cookies:
            self._log("Saved cookies file was not found. Choose a new cookies file if the video requires one.")

    def _valid_saved_cookies(self) -> str:
        saved_cookies = self.settings.get("cookies_file", "").strip()
        if not saved_cookies:
            return ""

        path = Path(saved_cookies).expanduser()
        if path.is_file():
            return saved_cookies

        self.settings.pop("cookies_file", None)
        _write_settings(self.settings)
        return ""

    def _remember_settings(self, job: TranscriptJob) -> None:
        settings = dict(self.settings)
        if job.groq_api_key.strip():
            settings["groq_api_key"] = job.groq_api_key.strip()
        else:
            settings.pop("groq_api_key", None)

        if job.cookies_file is not None:
            settings["cookies_file"] = str(job.cookies_file)
        elif job.urls:
            settings.pop("cookies_file", None)

        self.settings = settings
        _write_settings(settings)

    def _forget_saved_api_key(self) -> None:
        self.settings.pop("groq_api_key", None)
        _write_settings(self.settings)

    def _forget_saved_cookies(self) -> None:
        self.settings.pop("cookies_file", None)
        _write_settings(self.settings)

    def _build_styles(self) -> None:
        style = ttk.Style(self)

        # Native ttk themes can follow the OS appearance while ignoring some
        # application colors. On macOS this creates dark controls on the light
        # backgrounds below. Clam is available with Tk on every supported
        # platform and allows the complete palette to remain internally
        # consistent, regardless of the system appearance.
        for theme in ("clam", "alt", "default"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        self.configure(background=WINDOW_BG)
        self.option_add("*selectBackground", ACCENT)
        self.option_add("*selectForeground", "#ffffff")

        style.configure("TFrame", padding=0, background=WINDOW_BG)
        style.configure("TLabel", background=WINDOW_BG, foreground=TEXT_FG)
        style.configure("Header.TLabel", font=("TkDefaultFont", 18, "bold"))
        style.configure("Section.TLabel", font=("TkDefaultFont", 10, "bold"))
        style.configure(
            "TButton",
            background=CONTROL_BG,
            foreground=TEXT_FG,
            bordercolor=FIELD_BORDER,
            lightcolor=CONTROL_BG,
            darkcolor=CONTROL_BG,
            focuscolor=ACCENT,
            padding=(8, 4),
        )
        style.map(
            "TButton",
            background=[("pressed", CONTROL_PRESSED_BG), ("active", CONTROL_ACTIVE_BG)],
            foreground=[("disabled", MUTED_FG)],
        )
        style.configure(
            "Primary.TButton",
            font=("TkDefaultFont", 10, "bold"),
            background=ACCENT,
            foreground="#ffffff",
            bordercolor=ACCENT_ACTIVE,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )
        style.map(
            "Primary.TButton",
            background=[("pressed", ACCENT_ACTIVE), ("active", ACCENT_ACTIVE)],
            foreground=[("disabled", "#d1d1d6")],
        )
        style.configure(
            "TRadiobutton",
            background=WINDOW_BG,
            foreground=TEXT_FG,
            indicatorbackground=FIELD_BG,
            indicatorforeground=ACCENT,
        )
        style.map(
            "TRadiobutton",
            background=[("active", WINDOW_BG)],
            foreground=[("disabled", MUTED_FG)],
            indicatorbackground=[("selected", ACCENT), ("disabled", FIELD_DISABLED_BG)],
        )
        style.configure(
            "TEntry",
            fieldbackground=FIELD_BG,
            background=FIELD_BG,
            foreground=TEXT_FG,
            bordercolor=FIELD_BORDER,
            lightcolor=FIELD_BORDER,
            darkcolor=FIELD_BORDER,
        )
        style.map(
            "TEntry",
            fieldbackground=[("disabled", FIELD_DISABLED_BG), ("readonly", FIELD_DISABLED_BG)],
            foreground=[("disabled", MUTED_FG), ("readonly", TEXT_FG)],
        )
        style.configure(
            "TCombobox",
            fieldbackground=FIELD_BG,
            background=FIELD_BG,
            foreground=TEXT_FG,
            bordercolor=FIELD_BORDER,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("disabled", FIELD_DISABLED_BG), ("readonly", FIELD_BG)],
            foreground=[("disabled", MUTED_FG)],
        )
        style.configure(
            "TScrollbar",
            background=CONTROL_BG,
            troughcolor=FIELD_DISABLED_BG,
            bordercolor=WINDOW_BG,
            arrowcolor=TEXT_FG,
        )
        style.map("TScrollbar", background=[("active", CONTROL_ACTIVE_BG)])
        style.configure(
            "TProgressbar",
            background=ACCENT,
            troughcolor=FIELD_DISABLED_BG,
            bordercolor=FIELD_BORDER,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )
        style.configure("TPanedwindow", background=WINDOW_BG)
        style.configure(
            "TSpinbox",
            fieldbackground=FIELD_BG,
            background=FIELD_BG,
            foreground=TEXT_FG,
            bordercolor=FIELD_BORDER,
        )
        style.map(
            "TSpinbox",
            fieldbackground=[("disabled", FIELD_DISABLED_BG), ("readonly", FIELD_DISABLED_BG)],
            foreground=[("disabled", MUTED_FG)],
        )

    def _configure_text_field(self, widget: tk.Text) -> None:
        widget.configure(
            background=FIELD_BG,
            foreground=TEXT_FG,
            insertbackground=TEXT_FG,
            selectbackground=ACCENT,
            selectforeground="#ffffff",
            highlightbackground=FIELD_BORDER,
            highlightcolor=ACCENT,
            highlightthickness=1,
            relief="solid",
            borderwidth=1,
        )

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=(18, 16, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_TITLE, style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_var).grid(row=0, column=1, sticky="e")

        content = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        content.grid(row=1, column=0, sticky="nsew", padx=18, pady=8)

        left = ttk.Frame(content, padding=(0, 0, 12, 0))
        right = ttk.Frame(content, padding=(12, 0, 0, 0))
        content.add(left, weight=3)
        content.add(right, weight=2)

        left.columnconfigure(0, weight=1)
        left.rowconfigure(3, weight=1)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(7, weight=1)

        ttk.Label(left, text="Source", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        source_frame = ttk.Frame(left)
        source_frame.grid(row=1, column=0, sticky="ew", pady=(6, 12))
        source_frame.columnconfigure(1, weight=1)
        self.urls_radio = ttk.Radiobutton(
            source_frame,
            text="Panopto URLs",
            value="urls",
            variable=self.source_mode_var,
            command=self._update_source_mode,
        )
        self.folder_radio = ttk.Radiobutton(
            source_frame,
            text="Video folder",
            value="folder",
            variable=self.source_mode_var,
            command=self._update_source_mode,
        )
        self.input_dir_entry = ttk.Entry(source_frame, textvariable=self.input_dir_var)
        self.input_dir_button = ttk.Button(source_frame, text="Browse", command=self._choose_input_folder)
        self.urls_radio.grid(row=0, column=0, sticky="w")
        self.folder_radio.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.input_dir_entry.grid(row=1, column=1, sticky="ew", padx=(8, 6), pady=(6, 0))
        self.input_dir_button.grid(row=1, column=2, sticky="e", pady=(6, 0))

        self.urls_label = ttk.Label(left, text="Panopto URLs", style="Section.TLabel")
        self.urls_label.grid(row=2, column=0, sticky="w")
        urls_frame = ttk.Frame(left)
        urls_frame.grid(row=3, column=0, sticky="nsew", pady=(6, 12))
        urls_frame.columnconfigure(0, weight=1)
        urls_frame.rowconfigure(0, weight=1)
        self.urls_text = tk.Text(urls_frame, height=12, wrap="word", undo=True)
        self._configure_text_field(self.urls_text)
        urls_scroll = ttk.Scrollbar(urls_frame, command=self.urls_text.yview)
        self.urls_text.configure(yscrollcommand=urls_scroll.set)
        self.urls_text.grid(row=0, column=0, sticky="nsew")
        urls_scroll.grid(row=0, column=1, sticky="ns")

        ttk.Label(left, text="Progress", style="Section.TLabel").grid(row=4, column=0, sticky="w")
        self.progress = ttk.Progressbar(left, mode="determinate", maximum=100)
        self.progress.grid(row=5, column=0, sticky="ew", pady=(6, 12))

        log_header = ttk.Frame(left)
        log_header.grid(row=6, column=0, sticky="ew")
        log_header.columnconfigure(0, weight=1)
        ttk.Label(log_header, text="Log", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(log_header, text="Clear", command=self._clear_log).grid(row=0, column=1, sticky="e")

        log_frame = ttk.Frame(left)
        log_frame.grid(row=7, column=0, sticky="nsew", pady=(6, 0))
        left.rowconfigure(7, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        self._configure_text_field(self.log_text)
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

        self._build_settings(right)

        footer = ttk.Frame(self, padding=(18, 8, 18, 18))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.start_button = ttk.Button(
            footer,
            text="Start Transcription",
            style="Primary.TButton",
            command=self._start,
        )
        self.cancel_button = ttk.Button(footer, text="Cancel", command=self._cancel, state="disabled")
        self.open_output_button = ttk.Button(footer, text="Open Output Folder", command=self._open_output_folder)
        self.open_output_button.grid(row=0, column=0, sticky="w")
        self.cancel_button.grid(row=0, column=1, padx=(0, 8))
        self.start_button.grid(row=0, column=2)

    def _build_settings(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Settings", style="Section.TLabel").grid(row=0, column=0, sticky="w")

        fields = ttk.Frame(parent)
        fields.grid(row=1, column=0, sticky="ew", pady=(6, 12))
        fields.columnconfigure(1, weight=1)

        ttk.Label(fields, text="Cookies").grid(row=0, column=0, sticky="w", pady=4)
        self.cookies_entry = ttk.Entry(fields, textvariable=self.cookies_var)
        self.cookies_button = ttk.Button(fields, text="Browse", command=self._choose_cookies)
        self.cookies_entry.grid(row=0, column=1, sticky="ew", padx=(8, 6))
        self.cookies_button.grid(row=0, column=2, sticky="e")

        ttk.Label(fields, text="Groq API key").grid(row=1, column=0, sticky="w", pady=4)
        self.api_entry = ttk.Entry(fields, textvariable=self.api_key_var, show="*")
        self.api_entry.grid(row=1, column=1, sticky="ew", padx=(8, 6))
        self.reveal_button = ttk.Button(fields, text="Show", command=self._toggle_api_key)
        self.reveal_button.grid(row=1, column=2, sticky="e")

        ttk.Label(fields, text="Output folder").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(fields, textvariable=self.output_var).grid(row=2, column=1, sticky="ew", padx=(8, 6))
        ttk.Button(fields, text="Browse", command=self._choose_output).grid(row=2, column=2, sticky="e")

        ttk.Label(fields, text="Model").grid(row=3, column=0, sticky="w", pady=4)
        model_box = ttk.Combobox(
            fields,
            textvariable=self.model_var,
            values=("whisper-large-v3-turbo", "whisper-large-v3"),
        )
        model_box.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(8, 0))

        ttk.Label(fields, text="Language").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(fields, textvariable=self.language_var).grid(
            row=4, column=1, columnspan=2, sticky="ew", padx=(8, 0)
        )

        ttk.Label(fields, text="Chunk minutes").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Spinbox(fields, from_=1, to=60, textvariable=self.chunk_minutes_var, width=8).grid(
            row=5, column=1, sticky="w", padx=(8, 0)
        )

        ttk.Label(parent, text="Prompt", style="Section.TLabel").grid(row=2, column=0, sticky="w")
        prompt_frame = ttk.Frame(parent)
        prompt_frame.grid(row=3, column=0, sticky="nsew", pady=(6, 12))
        prompt_frame.columnconfigure(0, weight=1)
        prompt_frame.rowconfigure(0, weight=1)
        self.prompt_text = tk.Text(prompt_frame, height=6, wrap="word", undo=True)
        self._configure_text_field(self.prompt_text)
        prompt_scroll = ttk.Scrollbar(prompt_frame, command=self.prompt_text.yview)
        self.prompt_text.configure(yscrollcommand=prompt_scroll.set)
        self.prompt_text.grid(row=0, column=0, sticky="nsew")
        prompt_scroll.grid(row=0, column=1, sticky="ns")

    def _choose_cookies(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose cookies file",
            filetypes=(("Text files", "*.txt"), ("All files", "*.*")),
        )
        if path:
            self.cookies_var.set(path)

    def _choose_input_folder(self) -> None:
        path = filedialog.askdirectory(title="Choose lecture video folder")
        if path:
            self.input_dir_var.set(path)
            self.source_mode_var.set("folder")
            self._update_source_mode()

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="Choose output folder")
        if path:
            self.output_var.set(path)

    def _toggle_api_key(self) -> None:
        if self.api_entry.cget("show") == "*":
            self.api_entry.configure(show="")
            self.reveal_button.configure(text="Hide")
        else:
            self.api_entry.configure(show="*")
            self.reveal_button.configure(text="Show")

    def _update_source_mode(self) -> None:
        using_urls = self.source_mode_var.get() == "urls"
        url_state = "normal" if using_urls else "disabled"
        folder_state = "disabled" if using_urls else "normal"
        cookies_state = "normal" if using_urls else "disabled"

        self.urls_text.configure(
            state=url_state,
            background=FIELD_BG if using_urls else FIELD_DISABLED_BG,
            foreground=TEXT_FG if using_urls else MUTED_FG,
            insertbackground=TEXT_FG if using_urls else MUTED_FG,
        )
        self.input_dir_entry.configure(state=folder_state)
        self.input_dir_button.configure(state=folder_state)
        self.cookies_entry.configure(state=cookies_state)
        self.cookies_button.configure(state=cookies_state)

    def _start(self) -> None:
        try:
            job = self._job_from_form()
        except PipelineError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self._remember_settings(job)

        self.cancel_event.clear()
        self._set_running(True)
        self.progress.configure(value=0)
        self.status_var.set("Running")
        self._log("Starting transcription job.")

        self.worker = threading.Thread(target=self._run_worker, args=(job,), daemon=True)
        self.worker.start()

    def _job_from_form(self) -> TranscriptJob:
        urls = [
            line.strip()
            for line in self.urls_text.get("1.0", "end").splitlines()
            if line.strip()
        ]
        cookies_text = self.cookies_var.get().strip()
        input_dir_text = self.input_dir_var.get().strip()
        output_text = self.output_var.get().strip()
        prompt = self.prompt_text.get("1.0", "end").strip()
        using_urls = self.source_mode_var.get() == "urls"

        if not output_text:
            raise PipelineError("Choose an output folder.")
        if using_urls and not urls:
            raise PipelineError("Add at least one Panopto URL.")
        if not using_urls and not input_dir_text:
            raise PipelineError("Choose a folder of lecture videos.")
        if using_urls and cookies_text and not Path(cookies_text).expanduser().is_file():
            self._forget_saved_cookies()
            self.cookies_var.set("")
            raise PipelineError(f"Cookies file does not exist: {Path(cookies_text).expanduser()}")
        try:
            chunk_minutes = int(self.chunk_minutes_var.get())
        except (tk.TclError, ValueError) as exc:
            raise PipelineError("Chunk minutes must be a whole number.") from exc

        return TranscriptJob(
            urls=urls if using_urls else [],
            input_dir=None if using_urls else Path(input_dir_text).expanduser(),
            groq_api_key=self.api_key_var.get().strip(),
            output_dir=Path(output_text).expanduser(),
            cookies_file=Path(cookies_text).expanduser() if using_urls and cookies_text else None,
            model=self.model_var.get().strip() or DEFAULT_MODEL,
            language=self.language_var.get().strip(),
            prompt=prompt,
            chunk_seconds=chunk_minutes * 60,
            keep_video_files=using_urls,
            use_transcripts_subdir=True,
        )

    def _run_worker(self, job: TranscriptJob) -> None:
        try:
            results = run_transcript_job(
                job,
                log=lambda message: self.events.put(("log", message)),
                progress=lambda current, total, message: self.events.put(
                    ("progress", (current, total, message))
                ),
                cancel_event=self.cancel_event,
            )
        except PipelineError as exc:
            if str(exc) == "Cancelled.":
                self.events.put(("cancelled", exc))
            else:
                self.events.put(("error", exc))
        except Exception as exc:
            self.events.put(("error", exc))
        else:
            self.events.put(("done", results))

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.status_var.set("Cancelling")
        self._log("Cancellation requested.")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            should_close = messagebox.askyesno(
                APP_TITLE,
                "A transcription job is still running. Cancel it and close the app?",
            )
            if not should_close:
                return
            self.closing_after_cancel = True
            self._cancel()
            self._set_running(True)
            return
        self.destroy()

    def _poll_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                self._log(str(payload))
            elif kind == "progress":
                current, total, message = payload
                percent = 0 if total == 0 else int((current / total) * 100)
                self.progress.configure(value=percent)
                self.status_var.set(str(message))
            elif kind == "done":
                self._set_running(False)
                self.progress.configure(value=100)
                results = payload
                count = len(results)
                self.status_var.set(f"Done: {count} transcript(s)")
                self._log(f"Finished. Wrote {count} transcript(s).")
                if self.closing_after_cancel:
                    self.destroy()
                    return
                messagebox.showinfo(APP_TITLE, f"Wrote {count} transcript(s).")
            elif kind == "cancelled":
                self._set_running(False)
                self.status_var.set("Cancelled")
                self._log("Cancelled.")
                if self.closing_after_cancel:
                    self.destroy()
                    return
            elif kind == "error":
                self._set_running(False)
                self.status_var.set("Error")
                message = self._handle_failure(payload)
                self._log(message)
                if self.closing_after_cancel:
                    self.destroy()
                    return
                messagebox.showerror(APP_TITLE, message)

        self.after(150, self._poll_events)

    def _handle_failure(self, error: object) -> str:
        message = str(error)
        if _looks_like_invalid_api_key_error(error):
            self._forget_saved_api_key()
            if os.environ.get("GROQ_API_KEY") is None:
                self.api_key_var.set("")
            return f"{message}\n\nThe Groq API key was not accepted. Any saved API key has been forgotten."

        if _looks_like_invalid_cookies_error(error):
            self._forget_saved_cookies()
            return f"{message}\n\nThe saved cookies file may be expired or invalid and has been forgotten."

        return message

    def _set_running(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _open_output_folder(self) -> None:
        path = Path(self.output_var.get().strip() or ".").expanduser()
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])


def main() -> None:
    app = LectureTranscriberApp()
    app.mainloop()
