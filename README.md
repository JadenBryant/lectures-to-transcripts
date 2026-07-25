# Lecture Transcriber

A small cross-platform desktop app for turning Panopto lecture URLs or local lecture videos into transcripts.

The app lets you choose a `yt-dlp` cookies file, enter a Groq API key, paste one or more Panopto URLs, or choose a folder of already-downloaded lecture videos. It writes one transcript `.txt` file per lecture.

## What It Does

1. Downloads each Panopto video with `yt-dlp`, or reads local media from a folder.
2. Optionally saves downloaded Panopto media in `videos/`, but does not copy already-downloaded local media.
3. Converts the media into temporary mono MP3 chunks with `ffmpeg`.
4. Sends each chunk to Groq speech-to-text.
5. Combines the chunk transcripts into a single text file.

## Requirements

- Python 3.10 or newer
- `ffmpeg` available on your `PATH`
- `yt-dlp` installed as a Python package or available as a command on your `PATH`
- A Groq API key
- A Panopto-compatible cookies file when the video is not public

Groq's speech-to-text docs list `whisper-large-v3-turbo` and `whisper-large-v3` as supported transcription models. The app defaults to `whisper-large-v3-turbo`; use `whisper-large-v3` when accuracy matters more than cost/speed.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m lecture_transcriber
```

After setup, you can launch the app without activating the virtual environment or typing a Python command.

On Windows, double-click:

```text
Launch Lecture Transcriber.cmd
```

On macOS, double-click:

```text
Launch Lecture Transcriber.command
```

The launchers use `.venv` automatically. If `.venv` or the required Python packages are missing, they ask once whether to set up the app. The macOS launcher also checks the standard Homebrew locations when looking for tools such as `ffmpeg`.

If macOS reports that the launcher is not executable after downloading it, fix its permission once from Terminal:

```bash
chmod +x "Launch Lecture Transcriber.command"
```

On macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m lecture_transcriber
```

## Installing ffmpeg

Windows, with winget:

```powershell
winget install Gyan.FFmpeg
```

macOS, with Homebrew:

```bash
brew install ffmpeg
```

After installing, restart your terminal or the packaged app so it can see the updated `PATH`.

## Cookies

Export cookies in Netscape text format, then choose that file in the app. Browser extensions commonly named "Get cookies.txt" or similar can export this format.

The app passes the file directly to `yt-dlp` as `--cookies`.

Cookies are only used in Panopto URL mode.

In Panopto URL mode, the app first tries to use the `yt-dlp` Python package. If that package is not installed for the Python interpreter running the app, it falls back to the `yt-dlp` command on your `PATH`.

## Local Video Folders

Use **Video folder** mode when you already have lecture media on disk. The app scans the selected folder, non-recursively, for:

```text
.3gp, .aac, .avi, .flac, .flv, .m4a, .m4v, .mkv, .mov, .mp3, .mp4, .mpeg, .mpg, .ogg, .opus, .wav, .webm, .wmv
```

Each matching file is transcribed in filename order.

Files must contain an audio stream. Video-only DASH fragments, such as files with names ending in format codes like `f137.mp4`, are skipped because there is no audio to transcribe.

## Output

Choose one output folder in the app. Output behavior depends on the source mode.

By default, the output folder is:

```text
~/Documents/Lecture Transcripts
```

The app creates a `transcripts/` folder inside the selected output folder.

Transcripts are written to:

```text
~/Documents/Lecture Transcripts/transcripts
```

Each transcript includes a short header with the lecture title, source URL, and model.

In **Panopto URLs** mode, downloaded lecture media is kept when **Save downloaded videos** is enabled:

```text
~/Documents/Lecture Transcripts/videos
```

In **Video folder** mode, the app does not copy the source videos.

Audio chunks are temporary scratch files and are removed automatically after each lecture finishes.

## Resume Behavior

The app keeps a small hidden resume index in the output folder:

```text
.lecture-transcriber-state.json
```

If a job stops because the internet connection drops, the user cancels, or the app is closed, you can run the same job again with the same source and transcription settings. Lectures that already completed are skipped, and incomplete lectures are retried.

Resume is tracked per lecture, not per chunk. If a lecture stops halfway through transcription, that lecture starts over on the next run, but previously completed lectures are not duplicated.

## Building a Desktop App

Install PyInstaller:

```bash
python -m pip install pyinstaller
```

Windows:

```powershell
pyinstaller --onefile --windowed --name LectureTranscriber lecture_transcriber\__main__.py
```

macOS:

```bash
pyinstaller --onefile --windowed --name LectureTranscriber lecture_transcriber/__main__.py
```

The output will be in `dist/`. `ffmpeg` still needs to be installed separately unless you decide to bundle a platform-specific binary.

## Notes

- The Groq direct upload limit is 25 MB on the free tier, so the app uses 15-minute 48 kbps chunks by default.
- You can increase or decrease chunk minutes in the GUI.
- The app can keep Panopto downloads, but does not copy local source videos and does not keep chunk files.
- Completed lectures are skipped on rerun when the same output folder and transcription settings are used.
- The app remembers the last used cookies file and Groq API key in its user config file. If the saved cookies path is missing, it is forgotten and the field starts blank. If Groq rejects the saved API key, the key is forgotten so you can enter a new one.
- The output folder defaults to `~/Documents/Lecture Transcripts`, and the output folder dropdown remembers the last 10 folders used for transcripts.
- The saved API key is stored as plain text in the current user's config directory.

## References

- Groq speech-to-text docs: https://console.groq.com/docs/speech-to-text
- yt-dlp: https://github.com/yt-dlp/yt-dlp
- ffmpeg: https://ffmpeg.org/
