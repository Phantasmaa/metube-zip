#!/usr/bin/env python3
"""
MeTube ZIP Wrapper

Wrapper ligero sobre yt-dlp que:
- Detecta si la URL es playlist/canal o video individual
- Si es playlist: descarga todos los items y los comprime en UN .zip
- Si es video individual: deja que MeTube lo maneje (passthrough)
- Sube cookies para destrabar YouTube datacenter IP

Endpoints:
- POST /zip    {url, format, quality}  -> devuelve ZIP directo
- GET  /health -> health check
- POST /upload  multipart cookies.txt   -> guarda cookies y reinicia metube
- GET  /upload-ui                       -> form HTML simple
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
COOKIES_PATH = Path("/root/metube/cookies.txt")

# Match playlists/channels/sets (not single videos with &list=)
PLAYLIST_PATTERNS = [
    r"[?&]list=[A-Za-z0-9_-]{10,}",          # ?list=PL... long enough
    r"^https?://(www\.)?youtube\.com/playlist",
    r"^https?://(www\.)?youtube\.com/c/",
    r"^https?://(www\.)?youtube\.com/@",
    r"^https?://(www\.)?youtube\.com/channel/",
    r"^https?://(www\.)?youtube\.com/user/",
    r"^https?://soundcloud\.com/[^/]+/sets/",
    r"^https?://vimeo\.com/(album|showcase)/\d+",
    r"^https?://(www\.)?vimeo\.com/user\d+/videos",
]

# Single-video with optional list param that we treat as playlist
# (e.g. youtube.com/watch?v=X&list=Y&index=1)
WATCH_WITH_LIST = re.compile(r"^https?://(www\.)?youtube\.com/watch.*[?&]list=[A-Za-z0-9_-]{10,}")


def is_playlist_url(url: str) -> bool:
    if WATCH_WITH_LIST.match(url):
        return True
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
        "--ignore-errors",  # skip items that fail (e.g. YouTube bot check)
        "--continue",       # resume partial
        "--no-playlist-reverse",
    ]

    # Use cookies file if present (solves YouTube bot check for datacenter IPs)
    if COOKIES_PATH.exists() and COOKIES_PATH.stat().st_size > 0:
        cmd.extend(["--cookies", str(COOKIES_PATH)])

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


def restart_services():
    """Restart metube + zip-wrapper so they pick up new cookies."""
    subprocess.run(["systemctl", "restart", "metube.service"], capture_output=True)
    # Don't restart ourselves — systemd will pick up the file next call


# ---------- Endpoints ----------

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "yt_dlp": os.path.exists(YT_DLP),
        "zip_dir": str(ZIP_DIR),
        "cookies_loaded": COOKIES_PATH.exists() and COOKIES_PATH.stat().st_size > 0,
        "cookies_size": COOKIES_PATH.stat().st_size if COOKIES_PATH.exists() else 0,
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

    workdir = Path(tempfile.mkdtemp(prefix="zipwrap_", dir=DOWNLOAD_DIR))
    try:
        rc, out, err = run_ytdlp_playlist(url, quality, fmt, workdir)
        # rc != 0 is OK if some items failed (--ignore-errors)

        if not any(workdir.iterdir()):
            return jsonify({
                "error": "no files downloaded",
                "yt_dlp_exit": rc,
                "stderr_tail": err[-1500:] if err else "",
            }), 500

        # Find a name for the ZIP
        items = list(workdir.iterdir())
        if len(items) == 1 and items[0].is_dir():
            zip_name = items[0].name
        else:
            zip_name = re.sub(r"[^\w\-]+", "_", url)[:80] or "download"

        zip_path = ZIP_DIR / f"{zip_name}.zip"
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


@app.route("/upload", methods=["POST"])
def upload_cookies():
    """Receive cookies.txt upload and save it."""
    if "cookies" not in request.files:
        return jsonify({"error": "field 'cookies' missing (multipart form)"}), 400
    f = request.files["cookies"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400

    # Validate Netscape cookies format (first line is a comment)
    raw = f.read()
    text = raw.decode("utf-8", errors="ignore")
    if not text.startswith("#") and not text.startswith("# Netscape"):
        # Netscape format starts with "# Netscape HTTP Cookie File" or "# HTTP Cookie File"
        if "HTTP Cookie File" not in text[:200]:
            return jsonify({
                "error": "doesn't look like Netscape cookies format",
                "hint": "Use Chrome extension 'Get cookies.txt LOCALLY' or Firefox 'cookies.txt'",
                "first_line": text.split("\n", 1)[0][:100],
            }), 400

    # Backup existing cookies
    if COOKIES_PATH.exists():
        shutil.copy(COOKIES_PATH, COOKIES_PATH.with_suffix(".txt.bak"))

    COOKIES_PATH.write_bytes(raw)
    os.chmod(COOKIES_PATH, 0o600)

    restart_services()

    return jsonify({
        "status": "ok",
        "bytes": len(raw),
        "cookies_path": str(COOKIES_PATH),
        "hint": "metube restarted; next download will use cookies",
    })


@app.route("/upload-ui", methods=["GET"])
def upload_ui():
    """Simple HTML upload form."""
    return """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Subir cookies.txt</title>
<style>
  body{font-family:system-ui,-apple-system,sans-serif;max-width:480px;margin:40px auto;padding:20px;background:#0e0e10;color:#eaeaea}
  h1{font-size:18px}
  .box{border:2px dashed #444;padding:24px;border-radius:8px;text-align:center;background:#18181b}
  button{background:#7c3aed;color:#fff;border:0;padding:10px 20px;border-radius:6px;cursor:pointer;font-size:14px;margin-top:12px}
  button:hover{background:#6d28d9}
  input[type=file]{margin-top:12px}
  .help{font-size:13px;color:#9ca3af;margin-top:24px;line-height:1.5}
  .help code{background:#27272a;padding:2px 6px;border-radius:3px;color:#a78bfa}
  #status{margin-top:16px;padding:12px;border-radius:6px;display:none;font-size:13px}
  .ok{background:#064e3b;color:#6ee7b7}
  .err{background:#7f1d1d;color:#fca5a5}
</style>
</head>
<body>
<h1>Subir cookies.txt (Netscape format)</h1>
<div class="box">
  <form id="f">
    <input type="file" name="cookies" id="file" accept=".txt" required>
    <br>
    <button type="submit">Subir y reiniciar MeTube</button>
  </form>
  <div id="status"></div>
</div>
<div class="help">
  <b>Cómo exportar cookies de YouTube logueado:</b>
  <ol>
    <li>Chrome/Firefox: instalá la extensión <b>"Get cookies.txt LOCALLY"</b></li>
    <li>Andá a youtube.com (logueado)</li>
    <li>Click en la extensión → "Export"</li>
    <li>Subí el archivo acá</li>
  </ol>
  Después de subir, probá: <code>http://173.249.3.113:8103/zip</code> con un link de playlist.
</div>
<script>
document.getElementById('f').onsubmit = async e => {
  e.preventDefault();
  const fd = new FormData();
  fd.append('cookies', document.getElementById('file').files[0]);
  const r = await fetch('/upload', {method:'POST', body:fd});
  const j = await r.json();
  const s = document.getElementById('status');
  s.style.display='block';
  if (r.ok) { s.className='ok'; s.textContent='✓ Cookies guardadas ('+j.bytes+' bytes)'; }
  else { s.className='err'; s.textContent='✗ '+j.error+' — '+(j.hint||''); }
};
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8097)
