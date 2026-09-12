# Architecture

An automated faceless-content pipeline. On a schedule (30 minutes on the live
instance; the export says 50) it writes a horror story
with a locally-hosted LLM, narrates it, renders a vertical subtitled Short, uploads
it to YouTube, Instagram Reels and TikTok, and emails a confirmation — unattended.

Everything runs on one Windows PC (RTX 3050). n8n handles orchestration; Python
handles media. They coordinate through shared files plus one webhook.

## Components

| Piece | What it does |
|---|---|
| **LM Studio** (`:1234`) | Serves the story model (`LMS_MODEL`, currently MN-12B-Mag-Mell) behind an OpenAI-compatible API. Both n8n LLM nodes point at it. |
| **n8n** (`:5678`) | Orchestration. Workflows: `ContentGeneration3.0`, `PublishingContent3.0`, `PromptHorrorGeneration`, plus the monthly `InstagramTokenRefresh`. |
| **cloudflared** | Named tunnel exposing n8n on a subdomain (OAuth callbacks) and the video server on a second one (Instagram fetches the mp4 from it). |
| **`pipeline/videoServer.py`** (`:8090`) | Serves `HorrorVideos/*.mp4` at `/v/<secret>/<name>.mp4` with Range support. Only runs when `VIDEO_URL_SECRET` is set. |
| **`TIKTOK_TOKEN_FILE`** (`C:\n8n-data\tiktok_token.json`) | The TikTok access/refresh token pair. Read and rewritten by the TikTok lane on every run. |
| **`pipeline/storyWatcher.py`** | Watches `Metadata/`. A new JSON there means "a story is ready" and triggers the render. |
| **`pipeline/generateContent.py`** | TTS → music mix → ffmpeg render, in one pass. ~17 seconds per video. |

## The flow

```
Schedule Trigger (every 50 min)  /  manual "Execute workflow"
        │
        ├─ generate_lock.lock present? ──yes──> delete lock, Stop and Error (skip run)
        │                                        (self-heals a stale lock for next time)
        no
        │
   create lock
        │
   read counter.txt -> n = n + 1   (held in memory, NOT yet written)
        │
   PromptHorrorGeneration (sub-workflow: rolls one of 18 horror subgenres,
        │                     realistic or supernatural, then one LLM call
        │                     with a matching worked example; ~170 words)
        │
   write HorrorStories/HorrorStory{n}.txt
        │
   Commit Counter -> write counter.txt      ← only after the story exists, so a
        │                                     failed run does not burn a number
   Generate Metadata (LLM -> strict JSON: title, hook, captions per platform;
        │                     parser tolerates fences and leaked EOS/[TOOL_CALLS] trailers)
        │
   write Metadata/HorrorStory{n}_metadata.json  ★ HANDOFF  (workflow ends here)
                                                   │
                                         storyWatcher.py sees the file
                                                   │
                                         generateContent.py
                                           1. edge_tts narration + word timings
                                           2. ffmpeg music mix
                                           3. ffmpeg: crop 9:16, scale, burn in
                                              subtitles (libass), NVENC encode
                                           4. write .part.mp4, rename -> .mp4
                                           5. POST N8N_RENDER_WEBHOOK  ★ DONE SIGNAL
                                                   │
   PublishingContent3.0  (Webhook trigger, /webhook/render-complete)
        ├─ Story Paths        ← story name and both file paths come from the POST
        │                        body; counter.txt is never re-read
        ├─ Read Metadata File / Metadata to Text / Parse Metadata
        ├─ Platform Status    ← reads HorrorStories/blocked_<platform>.flag (fresh < 24 h)
        │                        so each lane below can skip a paused platform
        ├─ Release Lock       ← deletes generate_lock.lock, before the upload, so
        │                        the mutex is freed even if publishing fails
        ├─ Read Video File
        ├─ YouTube Blocked?   ← skips the lane while blocked_youtube.flag is fresh
        ├─ Start Resumable Upload  (POST googleapis session)   ──error─┐
        ├─ Upload Video Bytes      (PUT the bytes)             ──error─┤
        ├─ Update a video     → real title, description, tags, public ──error─┤
        ├─ Email Success      → Gmail with the /shorts/{id} link           │
        ├─ Classify Failure   ← errorText + stopPipeline (quota/limit regex) ┘
        ├─ Email Failure
        ├─ Is Upload Limit?  ──true──▶ Block YouTube (writes the flag) ──▶ All Blocked?
        │                                 All Blocked? ──true──▶ Email Pipeline Stopped
        │                                                       ──▶ Stop Pipeline
        │
        ├─ Instagram lane (forks from Platform Status, runs beside YouTube)
        │    ├─ Instagram Blocked?  skip while blocked_instagram.flag is fresh
        │    ├─ IG Params           videoUrl = VIDEO_PUBLIC_BASE/v/<secret>/<story>.mp4
        │    ├─ IG Create Container POST graph.instagram.com/{ig}/media  media_type=REELS
        │    ├─ IG Wait 20 s ⇄ IG Container Status ⇄ IG Ready?   (≤15 polls)
        │    ├─ IG Publish          POST /{ig}/media_publish
        │    ├─ IG Permalink → IG Email Success
        │    ├─ IG Classify Failure → IG Email Failure
        │    └─ IG Is Limit? ──true──▶ Block Instagram ──▶ All Blocked?
        │
        └─ TikTok lane (forks from Read Video File — it needs the bytes)
             ├─ TT Video Size       exact byte count from the binary
             ├─ TikTok Blocked?     skip while blocked_tiktok.flag is fresh
             ├─ TT Read Token File / TT Parse Token     TIKTOK_TOKEN_FILE
             ├─ TT Refresh Token    POST open.tiktokapis.com/v2/oauth/token/  (24 h tokens)
             ├─ TT Token OK? → TT Token To File → TT Save Token   (rotated pair written back)
             ├─ TT Creator Info     POST /v2/post/publish/creator_info/query/
             ├─ TT Params           privacy = TIKTOK_PRIVACY_LEVEL if offered, else SELF_ONLY
             ├─ TT Init Post        POST /v2/post/publish/video/init/  FILE_UPLOAD, 1 chunk
             ├─ TT Attach Video → TT Upload Bytes   PUT upload_url, Content-Range
             ├─ TT Wait 20 s ⇄ TT Status ⇄ TT Done?   (≤15 polls)
             ├─ TT Email Success
             ├─ TT Classify Failure → TT Email Failure
             └─ TT Is Limit? ──true──▶ Block TikTok ──▶ All Blocked?

All three YouTube nodes use `onError: continueErrorOutput`. `videos.insert` costs
1,600 quota units against a 10,000/day default — roughly six uploads a day — so
"exceeded the number of videos they may upload" / quotaExceeded is the expected
failure, not an exceptional one. Because the error branch completes normally, n8n
records such a run as **success**; the failure email is the real signal.

Limits are handled **per platform**. A limit error (YouTube quota regex; Instagram
codes 4/9/17/32/613; TikTok `spam_risk_too_many_posts`, `rate_limit_exceeded`, …)
writes `HorrorStories/blocked_<platform>.flag`. `Platform Status` reads the three
flags at the start of every run and each lane's `… Blocked?` gate skips the lane
silently while its flag is younger than 24 h, so one platform running out of
quota never stops the others. Only when all three flags are fresh does
`All Blocked?` send one "Pipeline STOPPED" email and launch `Stop Pipeline`,
which runs `scripts/stop-pipeline.ps1` detached (5 s delay so the email and the
execution record land first; the script kills the n8n process running the node,
which is why it must not be invoked synchronously). `start-pipeline.ps1` clears
the flags; deleting one by hand resumes that platform early. Any other failure
just emails and leaves the pipeline running.

`Merge Upload Session` has `includeUnpaired: false` on purpose: with it on, the
error item from a failed session start was paired with the metadata item and
`Upload Video Bytes` ran with no URL, producing a second failure email per story.
```

## Synchronisation contracts

| Signal | Written by | Read by | Purpose |
|---|---|---|---|
| `HorrorStories/counter.txt` | ContentGenerate, **after** the story file is written | ContentGenerate, `generateContent.py` | The story number. Every filename derives from it. |
| `Metadata/HorrorStory{n}_metadata.json` | ContentGenerate | `storyWatcher.py`, PublishingContent | Trigger for the render, and the caption source for upload. |
| `HorrorVideos/HorrorStory{n}.part.mp4` → `.mp4` | `generateContent.py` (atomic rename) | nothing polls it | The rename is atomic, so a partial file is never observed. |
| `POST /webhook/render-complete` | `generateContent.py`, after a successful render | PublishingContent3.0 | "Render finished, publish story n." Replaces the old 25-minute blind wait. |
| `HorrorStories/generate_lock.lock` | ContentGenerate creates | ContentGenerate checks, **PublishingContent's Release Lock deletes** | Mutex. Stops the cron starting a second run while one is still in flight. |

The lock being released by the *publish* workflow rather than the generate workflow
is deliberate: the gate stays shut for the entire generate → render → publish
cycle, not just the generate step.

### Why a webhook rather than a timer

ContentGenerate used to `Wait` 25 minutes, then read `HorrorStory{n}.part.mp4`
and branch: if the `.part` file was still there the render was ongoing, so it
waited another 10 minutes; if the read failed the render had finished, so it
published immediately. That logic was correct — `readWriteFile` reports
`json.fileName` as a basename, so the `If` comparison did match.

It was replaced because it is imprecise rather than because it was broken:

- **A 25-minute floor on a 17-second render.** The wait was sized for the old
  MoviePy renderer and dominates the whole pipeline now.
- **Only one grace period.** A render overrunning 35 minutes would still publish
  before the video existed.
- **It infers success from a missing file.** A crashed render that never produced
  a `.part` file looks exactly like a finished one, so publishing proceeds on a
  video that is absent or stale.

The webhook removes all three: the render reports when it is done, that it
succeeded, and which story it was.

## Failure behaviour

- **LLM down** → the run fails before `Commit Counter`, so the story number is not
  consumed. (Numbers 37–42 were burned this way under the old ordering, while
  LM Studio was down.)
- **Render fails** → `generateContent.py` exits non-zero, no webhook fires, nothing
  is published. The lock stays until the next cron tick clears it.
- **Webhook POST fails** → the video is already finalised on disk; the run logs a
  warning and does not fail. Publish it by hand by re-POSTing, or re-run with
  `--story N --notify`.
- **A platform refuses the post (daily limit / quota)** → one failure email, then
  that platform is paused for 24 h (`HorrorStories/blocked_<platform>.flag`) while
  the others keep posting. When all three are paused the whole pipeline is stopped
  so it does not keep burning LLM/render cycles on stories that cannot be posted.
  Videos and metadata stay on disk; re-POST `/webhook/render-complete` after
  restarting. Any other publish error emails and keeps running.
- **Instagram container never reaches FINISHED / #9004 "media could not be
  fetched"** → Meta could not download the mp4: the video server or the tunnel
  hostname is down. Emails and keeps running; YouTube is unaffected.
- **Instagram token expired** → every IG call returns an OAuth error. Tokens last
  60 days; `InstagramTokenRefresh` renews monthly and emails the new value, which
  has to be pasted into the credential. If it lapsed, generate a fresh one in the
  Meta app.
- **TikTok refresh fails** (file missing, refresh token expired or revoked) → one
  failure email naming `TT Refresh Token`; re-authorize by hand (SETUP §9b) and
  rewrite the token file. Nothing else is affected.
- **TikTok post is private** → by design. The app is unaudited (TikTok does not
  audit tools that post to the developer's own account, SETUP §9c), so every post
  is `SELF_ONLY` and is switched to "Everyone" by hand in TikTok Studio. `TT
  Params` downgrades to `SELF_ONLY` whenever `creator_info` does not offer the
  requested level, and the success email says so.
- **Video over 64 MB** → TikTok rejects the single-chunk init; the lane emails and
  keeps running. Renders are 30–40 MB today; multi-chunk upload is not implemented.

## Manual operation

```powershell
# Render one story without publishing it (safe: --story implies --no-notify)
python .\pipeline\generateContent.py --story 36

# Render and publish
python .\pipeline\generateContent.py --story 36 --notify

# Compare the ffmpeg renderer against the old MoviePy one
python .\pipeline\generateContent.py --story 36 --renderer moviepy
```

## Related docs

- [SETUP.md](SETUP.md) — installing and configuring everything from scratch
- [BENCHMARKS.md](BENCHMARKS.md) — render timings, and what the old bottleneck was
