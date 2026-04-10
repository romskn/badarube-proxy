#!/usr/bin/env python3
"""
BadaRube Proxy Server
Назначение:
  - Принимает HTTP запросы от Bada (TLS 1.0)
  - Проксирует их к Google APIs через HTTPS/TLS 1.2
  - Извлекает прямые URL видеопотоков через yt-dlp
  
Требования:
  pip install flask requests yt-dlp

Запуск:
  python proxy.py
  
Деплой (Railway/Render):
  Добавь Procfile: web: python proxy.py
"""

import os
import json
import logging
import requests
from flask import Flask, request, Response, jsonify

try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False
    print("WARNING: yt-dlp not installed. Stream URLs won't work.")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("BadaRubeProxy")

PORT = int(os.environ.get("PORT", 8080))
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
OAUTH_TOKEN_URL  = "https://oauth2.googleapis.com/token"


# ── CORS + headers ────────────────────────────────────────────────────────
def make_response(data, status=200):
    resp = jsonify(data) if isinstance(data, (dict, list)) else Response(
        data, status=status, mimetype="application/json")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def proxy_to_google(path, params, headers_extra=None):
    """Forward request to Google APIs with TLS 1.2"""
    url = f"{YOUTUBE_API_BASE}/{path}"
    headers = {
        "User-Agent": "BadaRube-Proxy/1.0",
        "Accept": "application/json",
    }
    if headers_extra:
        headers.update(headers_extra)
    
    # Forward Authorization header if present
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        log.info(f"GET {path} -> {resp.status_code}")
        return Response(resp.text, status=resp.status_code,
                       mimetype="application/json")
    except requests.RequestException as e:
        log.error(f"Proxy error: {e}")
        return make_response({"error": str(e)}, 503)


# ── YouTube Data API v3 routes ────────────────────────────────────────────
@app.route("/youtube/v3/<path:endpoint>", methods=["GET"])
def youtube_api(endpoint):
    """Pass-through to YouTube Data API"""
    params = dict(request.args)
    return proxy_to_google(endpoint, params)


# ── OAuth token endpoint ───────────────────────────────────────────────────
@app.route("/oauth/token", methods=["POST"])
def oauth_token():
    """Proxy OAuth token exchange/refresh to Google"""
    try:
        data = request.get_data(as_text=True)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "BadaRube-Proxy/1.0",
        }
        resp = requests.post(OAUTH_TOKEN_URL, data=data,
                            headers=headers, timeout=15)
        log.info(f"OAuth token -> {resp.status_code}")
        return Response(resp.text, status=resp.status_code,
                       mimetype="application/json")
    except requests.RequestException as e:
        return make_response({"error": str(e)}, 503)


# ── Video stream URL extraction ────────────────────────────────────────────
@app.route("/stream")
def get_stream_url():
    """
    Extract direct video stream URL using yt-dlp.
    Query params:
      id   - YouTube video ID
      itag - format (17=144p, 36=240p, 18=360p)
    Returns JSON: {"url": "https://..."}
    """
    video_id = request.args.get("id", "")
    itag     = request.args.get("itag", "18")

    if not video_id:
        return make_response({"error": "Missing video id"}, 400)

    if not YT_DLP_AVAILABLE:
        return make_response(
            {"error": "yt-dlp not installed on server"}, 503)

    # Map itag to yt-dlp format selector
    itag_map = {
        "17": "17",   # 3GP 144p
        "36": "36",   # 3GP 240p
        "18": "18",   # MP4 360p
    }
    fmt = itag_map.get(str(itag), "18")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": fmt,
        "socket_timeout": 15,
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}",
                download=False)
            
            stream_url = info.get("url", "")
            if not stream_url:
                # Try from formats list
                for f in info.get("formats", []):
                    if str(f.get("format_id")) == fmt:
                        stream_url = f.get("url", "")
                        break

            if not stream_url:
                return make_response(
                    {"error": "No stream URL found for itag " + fmt}, 404)

            log.info(f"Stream URL for {video_id} itag={fmt}: OK")
            return make_response({
                "url":      stream_url,
                "video_id": video_id,
                "itag":     fmt,
                "ext":      info.get("ext", "mp4"),
                "tbr":      info.get("tbr", 0),
            })

    except yt_dlp.utils.DownloadError as e:
        log.error(f"yt-dlp error for {video_id}: {e}")
        return make_response({"error": str(e)}, 404)
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        return make_response({"error": "Internal server error"}, 500)


# ── Thumbnail proxy (optional — avoids HTTPS from Bada) ──────────────────
@app.route("/thumb")
def proxy_thumbnail():
    """
    Proxy YouTube thumbnail images.
    Query param: url - full thumbnail URL
    """
    thumb_url = request.args.get("url", "")
    if not thumb_url or "ytimg.com" not in thumb_url:
        return make_response({"error": "Invalid thumbnail URL"}, 400)
    try:
        resp = requests.get(thumb_url, timeout=10,
                           headers={"User-Agent": "BadaRube-Proxy/1.0"})
        return Response(resp.content, status=resp.status_code,
                       mimetype=resp.headers.get("Content-Type", "image/jpeg"))
    except Exception as e:
        return make_response({"error": str(e)}, 503)


# ── Health check ──────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return make_response({
        "status":     "ok",
        "yt_dlp":     YT_DLP_AVAILABLE,
        "version":    "1.0",
        "app":        "BadaRube Proxy",
    })


@app.route("/")
def index():
    return make_response({
        "name":    "BadaRube Proxy Server",
        "version": "1.0",
        "routes": {
            "/youtube/v3/<endpoint>": "YouTube Data API v3 pass-through",
            "/oauth/token":           "OAuth 2.0 token endpoint",
            "/stream?id=ID&itag=18":  "Video stream URL via yt-dlp",
            "/thumb?url=URL":         "Thumbnail proxy",
            "/health":                "Health check",
        }
    })


if __name__ == "__main__":
    log.info(f"BadaRube Proxy starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
