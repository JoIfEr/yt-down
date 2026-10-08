import os
import shutil
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory

app = Flask(__name__, static_folder="static")

ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                 "music.youtube.com", "youtu.be"}
JOBS = {}          # job_id -> dict(status, progress, stage, error, path, dir, created)
JOB_TTL = 30 * 60  # seconds before finished files are deleted


def is_youtube(url):
    try:
        return urlparse(url).hostname in ALLOWED_HOSTS
    except Exception:
        return False


def cleanup_old_jobs():
    now = time.time()
    for jid in [j for j, v in JOBS.items() if now - v["created"] > JOB_TTL]:
        shutil.rmtree(JOBS[jid].get("dir", ""), ignore_errors=True)
        JOBS.pop(jid, None)


def format_selector(height):
    h = int(height)
    return (f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/"
            f"bv*[height<={h}]+ba/b[height<={h}]")


def run_job(job_id, url, quality):
    job = JOBS[job_id]
    tmp = tempfile.mkdtemp(prefix="clipfetch_")
    job["dir"] = tmp

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                job["progress"] = round(d["downloaded_bytes"] / total * 100, 1)
            job["stage"] = "Downloading"
        elif d["status"] == "finished":
            job["progress"] = 100
            job["stage"] = "Processing"

    opts = {
        "outtmpl": os.path.join(tmp, "%(title).150B.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    if quality == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                   "preferredcodec": "mp3",
                                   "preferredquality": "192"}]
    else:
        opts["format"] = format_selector(quality)
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [f for f in os.listdir(tmp) if not f.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("No file was produced.")
        job["path"] = os.path.join(tmp, files[0])
        job["status"] = "done"
    except Exception as e:  # surface yt-dlp errors to the UI
        job["status"] = "error"
        job["error"] = str(e).replace("ERROR: ", "")[:300]


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/info")
def info():
    url = (request.json or {}).get("url", "").strip()
    if not is_youtube(url):
        return jsonify(error="Paste a youtube.com or youtu.be link."), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True,
                               "skip_download": True}) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify(error=str(e).replace("ERROR: ", "")[:300]), 400
    heights = sorted({f["height"] for f in data.get("formats", [])
                      if f.get("height") and f.get("vcodec") not in (None, "none")},
                     reverse=True)
    return jsonify(title=data.get("title"), thumbnail=data.get("thumbnail"),
                   uploader=data.get("uploader"), duration=data.get("duration"),
                   heights=heights)


@app.post("/api/start")
def start():
    body = request.json or {}
    url, quality = body.get("url", "").strip(), str(body.get("quality", ""))
    if not is_youtube(url):
        return jsonify(error="Paste a youtube.com or youtu.be link."), 400
    if quality != "audio" and not quality.isdigit():
        return jsonify(error="Pick a quality."), 400
    cleanup_old_jobs()
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "running", "progress": 0, "stage": "Starting",
                    "created": time.time()}
    threading.Thread(target=run_job, args=(job_id, url, quality), daemon=True).start()
    return jsonify(job=job_id)


@app.get("/api/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Unknown or expired job."), 404
    return jsonify(status=job["status"], progress=job["progress"],
                   stage=job["stage"], error=job.get("error"))


@app.get("/api/file/<job_id>")
def file(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        return jsonify(error="File not ready."), 404
    return send_file(job["path"], as_attachment=True,
                     download_name=os.path.basename(job["path"]))


if __name__ == "__main__":
    # Bind to 127.0.0.1 by default. Set HOST=0.0.0.0 to reach it from your phone on the same Wi-Fi.
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", 5000)), threaded=True)
