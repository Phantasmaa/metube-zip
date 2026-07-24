#!/usr/bin/env python3
"""
MeTube ZIP Wrapper

Wrapper ligero sobre yt-dlp que:
- Detecta si la URL es playlist/canal o video individual
- Si es playlist: descarga todos los items y los comprime en UN .zip
- Si es video individual: deja que MeTube lo maneje (passthrough)

Endpoints:
- POST /zip    {url, format, quality}  -> devuelve ZIP directo
- GET  /health -> health check
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

# Config
DOWNLOAD_DIR = Path("/root/metube/downloads")
ZIP_DIR = Path("/root/metube/zips")
ZIP_DIR.mkdir(parents=True, exist_ok=True)
YT_DLP = "/usr/local/lib/hermes-agent/venv/bin/yt-dlp"
PLAYLIST_PATTERNS = [
    r"[?&]list=",
    r"^https?://(www\.)?youtube\.com/playlist",
    r"^https?://(www\.)?youtube\.com/c/",
    r"^https?://(www\.)?youtube\.com/@",
    r"^https?://(www\.)?youtube\.com/channel/",
    r"^https?://soundcloud\.com/[^/]+/sets/",
]


def is_playlist_url(url: str) -> bool:
    return any(re.search(p, url) for p in PLAYLIST_PATTERNS)


def run_ytdlp_playlist(url: str, quality: str, fmt: str, workdir: Path) -> tuple[int, str, str]:
    """Download playlist with yt-dlp. Returns (exit_code, stdout, stderr)."""
    # Format selection
    if fmt == "audio":
        format_sel = "bestaudio/best"
        postprocess = ["--extract-audio", "--audio-format", "mp3"]
    else:
        quality_map = {
            "best": "bestvideo*+bestaudio/best",
            "worst": "worstvideo*+worstaudio/worst",
            "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
            "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        }
        format_sel = quality_map.get(quality, quality_map["best"])
        postprocess = []

    cmd = [
        YT_DLP,
        "--no-warnings",
        "--no-progress",
        "--ignore-errors",  # skip items that fail (e.g. YouTube bot check)
        "--continue",  # resume partial
        "--no-playlist-reverse",  # keep original order
    ]

    # Use cookies file if present (solves YouTube bot check for datacenter IPs)
    cookies_path = Path("/root/metube/cookies.txt")
    if cookies_path.exists():
        cmd.extend(["--cookies", str(cookies_path)])

    cmd.extend([
        "-f", format_sel,
        "-o", str(workdir / "%(playlist_title)s/%(title)s.%(ext)s"),
        *postprocess,
        url,
    ])

    proc = subprocess.run(
        cmd,
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=3600,  # 1 hour max
    )
    return proc.returncode, proc.stdout, proc.stderr


def make_zip(source_dir: Path, zip_path: Path) -> tuple[int, int]:
    """Zip everything under source_dir into zip_path. Returns (file_count, total_size)."""
    count = 0
    total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, _, files in os.walk(source_dir):
            for f in files:
                fp = Path(root) / f
                if fp.suffix in (".part", ".ytdl", ".tmp"):
                    continue
                rel = fp.relative_to(source_dir)
                zf.write(fp, rel)
                count += 1
                total += fp.stat().st_size
    return count, total


@app.route("/health")
def health():
    cookies_path = Path("/root/metube/cookies.txt")
    return jsonify({
        "status": "ok",
        "yt_dlp": os.path.exists(YT_DLP),
        "zip_dir": str(ZIP_DIR),
        "cookies_loaded": cookies_path.exists() and cookies_path.stat().st_size > 0,
    })


@app.route("/zip", methods=["POST"])
def zip_download():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    quality = data.get("quality", "best")
    fmt = data.get("format", "video")  # video|audio

    if not url:
        return jsonify({"error": "missing 'url'"}), 400

    if not is_playlist_url(url):
        return jsonify({
            "error": "not a playlist URL",
            "hint": "use MeTube directly for single videos: http://173.249.3.113:8102/",
        }), 400

    # Make a unique work dir
    workdir = Path(tempfile.mkdtemp(prefix="zipwrap_", dir=DOWNLOAD_DIR))
    try:
        rc, out, err = run_ytdlp_playlist(url, quality, fmt, workdir)
        # rc != 0 is OK if some items failed (--ignore-errors)

        if not any(workdir.iterdir()):
            return jsonify({
                "error": "no files downloaded",
                "yt_dlp_exit": rc,
                "stderr_tail": err[-1000:] if err else "",
            }), 500

        # Find a name for the ZIP
        # If single subdir, use its name; else use sanitized URL
        items = list(workdir.iterdir())
        if len(items) == 1 and items[0].is_dir():
            zip_name = items[0].name
        else:
            # Extract domain + sanitized path
            zip_name = re.sub(r"[^\w\-]+", "_", url)[:80] or "download"

        zip_path = ZIP_DIR / f"{zip_name}.zip"
        # Avoid collision
        if zip_path.exists():
            i = 1
            while (ZIP_DIR / f"{zip_name}_{i}.zip").exists():
                i += 1
            zip_path = ZIP_DIR / f"{zip_name}_{i}.zip"

        count, total = make_zip(workdir, zip_path)

        return send_file(
            zip_path,
            as_attachment=True,
            download_name=zip_path.name,
            mimetype="application/zip",
        )

    except subprocess.TimeoutExpired:
        return jsonify({"error": "download timed out after 1h"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8097)
