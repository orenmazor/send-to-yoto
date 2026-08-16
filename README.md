# send-to-yoto

Paste a YouTube playlist URL into a web form. The service downloads each video's
audio in order, uploads it to your Yoto account, and creates a new MYO playlist.
Audio is written to a temp directory and deleted as soon as each file is uploaded.

## Run it

1. Create a **Confidential Client** at https://dashboard.yoto.dev/ and set its
   Allowed Callback URLs to `http://localhost:8080/callback`. Public clients are
   rejected at the token exchange with `access_denied` (see Design notes).
2. Set `YOTO_CLIENT_ID` and `YOTO_CLIENT_SECRET` in the environment — either
   `cp .env.example .env` and fill them in, or export them from your shell
   (autoenv/direnv). Compose reads both.
3. `docker compose up --build`
4. Open http://localhost:8080, click **Connect Yoto account**, log in to Yoto,
   then paste a playlist URL.

The new playlist appears in your Yoto library. Linking it to a physical MYO card
still has to happen on the Player — Yoto has no API for the NFC bind.

## Published image

Every push builds and pushes to GHCR:

- `ghcr.io/orenmazor/send-to-yoto:YYYY.MM.DD-<short sha>` — every commit
- `ghcr.io/orenmazor/send-to-yoto:latest` — `main` only

Built for `linux/amd64` and `linux/arm64`. The arm64 leg runs under QEMU; if you
only deploy on amd64, drop that platform and the `setup-qemu-action` step from
`.github/workflows/docker.yml`.

## Configuration

All configuration is environment variables; see `.env.example`. Nothing is read
from a config file. The only state on disk is `YOTO_TOKEN_FILE`, which caches the
OAuth refresh token so a restart doesn't force a re-auth. Everything persistent
lives under `/config`, which is the only volume. Leave `YOTO_TOKEN_FILE` unset
and the app is fully stateless, at the cost of reconnecting after every restart.

If you host this anywhere other than localhost, set `PUBLIC_BASE_URL` to its
external URL and add `<that URL>/callback` to the dashboard's Allowed Callback
URLs. The two must match exactly or the token exchange fails.

## Design notes

- **Authorization code + PKCE with a confidential client.** Getting here took
  three tries against the live tenant. Device code is rejected outright
  (`unauthorized_client` — the dashboard has no grant-type toggle to enable it).
  A public client passes `/authorize` but fails the token exchange with
  `access_denied`, regardless of scopes. A confidential client with a secret
  works. PKCE is kept anyway since it costs nothing.
- **Distroless runtime image.** `gcr.io/distroless/python3-debian12:nonroot`,
  which is Python 3.11 — hence the 3.11 builder stage. No shell, so directories
  are staged in a builder and `COPY --chown`'d in, and gunicorn reads `PORT` from
  `gunicorn.conf.py` rather than shell expansion in CMD.
- **deno, not node, and no ffmpeg.** deno is yt-dlp's highest-priority JS runtime
  and ships as one self-contained binary, which is the only reason vendoring a JS
  runtime into distroless is tolerable. ffmpeg is gone entirely: YouTube's
  audio-only formats are single streams, so `bestaudio` with no postprocessors
  never needs to merge anything. yt-dlp warns about the missing ffmpeg; verified
  it still pulls opus at ~124 kbps, which Yoto would re-encode anyway.
- **Three pinned versions that rot.** `yt-dlp` breaks whenever YouTube changes;
  `yt-dlp-ejs` is the JS bundle it feeds to deno (without it you get
  "The page needs to be reloaded"); and deno itself must be **>= 2.3.0** or
  yt-dlp logs `deno-x.y.z (unsupported)` and silently falls back to no runtime.
  If extraction starts failing, bump all three before debugging anything else.
- **One job at a time, in memory.** No queue, no database. A second submit while
  a job runs returns 409. If that becomes annoying, add a queue then — not now.
- **Single gunicorn worker.** Job state is a process-local dict, so scaling out
  would need real shared state. Don't raise `-w` without changing that.
- **No MP3 transcode.** yt-dlp grabs `bestaudio` and the bytes go straight to
  Yoto, which re-encodes everything to Opus server-side anyway. Converting to MP3
  first would just be a slower, lossier round trip.
- **`format: "opus"` on every track.** Yoto's API accepts `"mp3"` with a 200 and
  the Player then fails to decode the file, playing ~0.5s per track. Same class of
  silent failure applies to `duration` (seconds, not ms), track-level
  `overlayLabel`, `channels`, and the `metadata.media` totals. All are set.
- **100-track cap.** `POST /content` rejects more; `MAX_TRACKS` truncates the
  playlist rather than failing at the end after a long download.
- **Node in the image.** yt-dlp needs a JS runtime for current YouTube extraction;
  without it you get 429s and missing formats.

## Prior art

- [meng-tsai/youtube-to-yoto](https://github.com/meng-tsai/youtube-to-yoto) — a
  Claude skill doing the same pipeline by hand. Its `references/yoto-api.md` and
  `references/pitfalls.md` are the best available documentation of the Yoto API.
- [tmcinerney/yoto-mcp](https://github.com/tmcinerney/yoto-mcp) — MCP server on
  the official `@yotoplay/yoto-sdk`, if you'd rather drive Yoto from an assistant.
