"""send-to-yoto: paste a YouTube playlist URL, get a Yoto MYO playlist.

Single-process Flask app. One job at a time, held in memory. Audio is
downloaded to a temp dir, uploaded to Yoto, and deleted.
"""

import base64
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import requests
import yt_dlp
from flask import Flask, jsonify, redirect, render_template, request

AUTH_BASE = os.environ.get("YOTO_AUTH_BASE", "https://login.yotoplay.com")
API_BASE = os.environ.get("YOTO_API_BASE", "https://api.yotoplay.com")
CLIENT_ID = os.environ.get("YOTO_CLIENT_ID")
# Only set this for a Confidential Client. Public clients authenticate with PKCE
# alone and must not send a secret.
CLIENT_SECRET = os.environ.get("YOTO_CLIENT_SECRET")
SCOPES = os.environ.get("YOTO_SCOPES", "offline_access user:content:manage")
TOKEN_FILE = os.environ.get("YOTO_TOKEN_FILE")  # optional, for surviving restarts
DEFAULT_ICON = os.environ.get(
    "YOTO_DEFAULT_ICON", "yoto:#aUm9i3ex3qqAMYBv-i-O-pYMKuMJGICtR3Vhf289u2Q"
)
MAX_TRACKS = int(os.environ.get("MAX_TRACKS", "100"))  # Yoto hard cap
# Where this app is reachable. Must match an Allowed Callback URL in the Yoto
# dashboard, with /callback appended.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")
REDIRECT_URI = f"{PUBLIC_BASE_URL}/callback"

app = Flask(__name__)


# --------------------------------------------------------------------------
# Auth: OAuth authorization code flow with PKCE. Yoto's dashboard doesn't offer
# the device code grant, and a public client has no secret, so PKCE is what's
# left. Tokens live in memory; optionally mirrored to TOKEN_FILE so a container
# restart doesn't force a re-auth.
# --------------------------------------------------------------------------

_tokens = {}
_tokens_lock = threading.Lock()


def _load_tokens():
    if TOKEN_FILE and Path(TOKEN_FILE).exists():
        return json.loads(Path(TOKEN_FILE).read_text())
    return {}


def _save_tokens(tok):
    global _tokens
    _tokens = tok
    if TOKEN_FILE:
        p = Path(TOKEN_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(tok))
        p.chmod(0o600)


def _store_token_response(data):
    tok = dict(_tokens)
    tok["access_token"] = data["access_token"]
    tok["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
    if data.get("refresh_token"):
        tok["refresh_token"] = data["refresh_token"]
    _save_tokens(tok)


def token_request(data):
    """POST to the token endpoint, adding the client secret if we have one."""
    if CLIENT_SECRET:
        data = {**data, "client_secret": CLIENT_SECRET}
    return requests.post(f"{AUTH_BASE}/oauth/token", data=data, timeout=30)


def access_token():
    """Return a valid access token, refreshing if needed. Raises if unauthed."""
    with _tokens_lock:
        if _tokens.get("access_token") and time.time() < _tokens.get("expires_at", 0):
            return _tokens["access_token"]
        if not _tokens.get("refresh_token"):
            raise RuntimeError("not authenticated with Yoto — visit / and click Connect")
        r = token_request({
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": _tokens["refresh_token"],
        })
        r.raise_for_status()
        _store_token_response(r.json())
        return _tokens["access_token"]


def yoto(method, path, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {access_token()}"
    r = requests.request(method, f"{API_BASE}{path}", headers=headers, timeout=120, **kwargs)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------
# Job state. One job at a time; the UI polls /status.
# --------------------------------------------------------------------------

job = {"state": "idle", "message": "", "done": 0, "total": 0, "card_id": None}


def set_job(**kw):
    job.update(kw)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def download_playlist(url, workdir):
    """Download every video's audio, in playlist order. Returns [(title, path)]."""
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(Path(workdir) / "%(playlist_index)05d-%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "playlistend": MAX_TRACKS,
        # No js_runtimes here on purpose: yt-dlp enables deno by default and
        # finds the one vendored in the image. Passing it explicitly needs a
        # dict, not a list, and buys nothing.
        "progress_hooks": [
            lambda d: set_job(message=f"downloading {Path(d.get('filename','')).name}")
            if d["status"] == "downloading"
            else None
        ],
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    entries = [e for e in (info.get("entries") or [info]) if e]
    tracks = []
    for entry in entries:
        path = entry.get("requested_downloads", [{}])[0].get("filepath")
        if path and Path(path).exists():
            tracks.append((entry.get("title") or Path(path).stem, Path(path)))
    return info.get("title") or "YouTube playlist", tracks


def upload_audio(path):
    """Upload one file and wait for Yoto to transcode it. Returns transcodedInfo."""
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    up = yoto(
        "GET",
        "/media/transcode/audio/uploadUrl",
        params={"sha256": sha, "filename": path.name},
    )["upload"]

    if up.get("uploadUrl"):  # null means Yoto already has this file (dedup by sha256)
        r = requests.put(
            up["uploadUrl"],
            data=path.read_bytes(),
            headers={"Content-Type": "audio/mpeg"},
            timeout=600,
        )
        r.raise_for_status()

    deadline = time.time() + 900
    while time.time() < deadline:
        t = yoto("GET", f"/media/upload/{up['uploadId']}/transcoded",
                 params={"loudnorm": "false"})["transcode"]
        if t.get("transcodedSha256"):
            return t["transcodedSha256"], t["transcodedInfo"]
        time.sleep(3)
    raise RuntimeError(f"transcode timed out for {path.name}")


def build_chapter(index, title, sha, info):
    key = f"{index:03d}"
    label = str(index)
    track = {
        "key": key,
        "title": title,
        "overlayLabel": label,  # required at track level, not just chapter
        "trackUrl": f"yoto:#{sha}",
        "duration": int(info["duration"]),  # seconds, never milliseconds
        "fileSize": info["fileSize"],
        "channels": info.get("channels", "stereo"),
        "type": "audio",
        "format": "opus",  # Yoto always transcodes to Opus; saying "mp3" breaks playback
        "display": {"icon16x16": DEFAULT_ICON},
    }
    return {
        "key": key,
        "title": title,
        "overlayLabel": label,
        "tracks": [track],
        "display": {"icon16x16": DEFAULT_ICON},
    }


def run_job(url):
    with tempfile.TemporaryDirectory() as workdir:
        try:
            set_job(state="running", message="fetching playlist", done=0, total=0,
                    card_id=None)
            playlist_title, tracks = download_playlist(url, workdir)
            if not tracks:
                raise RuntimeError("no downloadable audio found in that playlist")
            set_job(total=len(tracks), message=f"downloaded {len(tracks)} tracks")

            chapters = []
            for i, (title, path) in enumerate(tracks, start=1):
                set_job(message=f"uploading {i}/{len(tracks)}: {title}")
                sha, info = upload_audio(path)
                chapters.append(build_chapter(i, title, sha, info))
                path.unlink(missing_ok=True)
                set_job(done=i)

            total_duration = sum(c["tracks"][0]["duration"] for c in chapters)
            total_size = sum(c["tracks"][0]["fileSize"] for c in chapters)
            set_job(message="creating Yoto playlist")
            card = yoto("POST", "/content", json={
                "title": playlist_title,
                "metadata": {
                    "description": f"Imported from {url}",
                    "media": {
                        "duration": total_duration,
                        "fileSize": total_size,
                        "readableFileSize": round(total_size / 1024 / 1024, 1),
                    },
                },
                "content": {
                    "activity": "yoto_Player",
                    "version": "1",
                    "config": {"onlineOnly": False},
                    "chapters": chapters,
                },
            })["card"]

            set_job(state="done", card_id=card["cardId"],
                    message=f'Created "{card["title"]}" with {len(chapters)} tracks. '
                            "Tap a blank MYO card on your Player to link it.")
        except Exception as e:  # surface the failure in the UI rather than a 500
            set_job(state="error", message=f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html", authed=bool(_tokens.get("refresh_token")))


# Single-user app, so the in-flight PKCE verifier is a module global rather than
# a signed session cookie. Overwritten by each new login attempt.
_pending_auth = {}


@app.get("/login")
def login():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    _pending_auth.clear()
    _pending_auth.update(verifier=verifier, state=state)

    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "audience": API_BASE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return redirect(f"{AUTH_BASE}/authorize?{query}")


@app.get("/callback")
def callback():
    if request.args.get("error"):
        return f"Yoto refused the login: {request.args.get('error_description')}", 400
    if not _pending_auth or request.args.get("state") != _pending_auth.get("state"):
        return "State mismatch — start again from /", 400

    r = token_request({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": request.args.get("code", ""),
        "redirect_uri": REDIRECT_URI,
        "code_verifier": _pending_auth["verifier"],
    })
    _pending_auth.clear()
    if r.status_code != 200:
        app.logger.error("token exchange %s: %s", r.status_code, r.text)
        return f"Token exchange failed ({r.status_code}): {r.text}", 502

    with _tokens_lock:
        _store_token_response(r.json())
    return redirect("/")


@app.post("/submit")
def submit():
    if job["state"] == "running":
        return jsonify(error="a job is already running"), 409
    url = (request.form.get("url") or request.json.get("url", "")).strip()
    if not url:
        return jsonify(error="no URL given"), 400
    threading.Thread(target=run_job, args=(url,), daemon=True).start()
    return jsonify(ok=True)


@app.get("/status")
def status():
    return jsonify(job)


@app.get("/healthz")
def healthz():
    return jsonify(ok=True, authed=bool(_tokens.get("refresh_token")))


_tokens = _load_tokens()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
