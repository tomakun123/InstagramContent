# Architecture

An automated faceless-content pipeline. Every 50 minutes it writes a horror story
with a locally-hosted LLM, narrates it, renders a vertical subtitled Short, uploads
it to YouTube, and emails a confirmation — unattended.

Everything runs on one Windows PC (RTX 3050). n8n handles orchestration; Python
handles media. They coordinate through shared files plus one webhook.

## Components

| Piece | What it does |
|---|---|
| **LM Studio** (`:1234`) | Serves `qwen2.5-14b-instruct` behind an OpenAI-compatible API. Both n8n LLM nodes point at it. |
| **n8n** (`:5678`) | Orchestration. Three workflows: `ContentGenerate2.0`, `PublishingContent2.0`, `PromptHorrorGeneration`. |
| **cloudflared** | Named tunnel exposing n8n on a subdomain, so platform APIs can reach the OAuth callbacks. |
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
   PromptHorrorGeneration (sub-workflow, multi-prompt story generation)
        │
   write HorrorStories/HorrorStory{n}.txt
        │
   Commit Counter -> write counter.txt      ← only after the story exists, so a
        │                                     failed run does not burn a number
   Generate Metadata (qwen -> strict JSON: title, hook, captions per platform)
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
   PublishingContent2.0  (Webhook trigger, /webhook/render-complete)
        ├─ story name comes from the POST body, not from counter.txt
        ├─ read Metadata/HorrorStory{n}_metadata.json
        ├─ DELETE generate_lock.lock          ← releases the mutex for the next cycle
        ├─ read HorrorVideos/HorrorStory{n}.mp4
        ├─ POST  googleapis resumable upload session
        ├─ PUT   the video bytes
        ├─ YouTube "Update a video" -> real title, description, tags, public
        └─ Gmail confirmation with the /shorts/{id} link
```

## Synchronisation contracts

| Signal | Written by | Read by | Purpose |
|---|---|---|---|
| `HorrorStories/counter.txt` | ContentGenerate, **after** the story file is written | ContentGenerate, `generateContent.py` | The story number. Every filename derives from it. |
| `Metadata/HorrorStory{n}_metadata.json` | ContentGenerate | `storyWatcher.py`, PublishingContent | Trigger for the render, and the caption source for upload. |
| `HorrorVideos/HorrorStory{n}.part.mp4` → `.mp4` | `generateContent.py` (atomic rename) | nothing polls it | The rename is atomic, so a partial file is never observed. |
| `POST /webhook/render-complete` | `generateContent.py`, after a successful render | PublishingContent2.0 | "Render finished, publish story n." Replaces the old 25-minute blind wait. |
| `HorrorStories/generate_lock.lock` | ContentGenerate creates | ContentGenerate checks, **PublishingContent deletes** | Mutex. Stops the cron starting a second run while one is still in flight. |

The lock being released by the *publish* workflow rather than the generate workflow
is deliberate: the gate stays shut for the entire generate → render → publish
cycle, not just the generate step.

### Why a webhook rather than a timer

ContentGenerate used to `Wait` 25 minutes and then check whether
`HorrorStory{n}.part.mp4` still existed. That check never actually worked — the
`If` node compared `'…/HorrorVideos/' + $json.fileName` against a full path, and
`fileName` was already absolute (or undefined when the read failed), so the
comparison never matched and publishing fired on the timer regardless of whether
the render had finished. It only appeared to work because 19 minutes is less than
25. A longer story would have published nothing.

Now the render says when it is done, and says *which* story it finished.

## Failure behaviour

- **LLM down** → the run fails before `Commit Counter`, so the story number is not
  consumed. (Numbers 37–42 were burned this way under the old ordering, while
  LM Studio was down.)
- **Render fails** → `generateContent.py` exits non-zero, no webhook fires, nothing
  is published. The lock stays until the next cron tick clears it.
- **Webhook POST fails** → the video is already finalised on disk; the run logs a
  warning and does not fail. Publish it by hand by re-POSTing, or re-run with
  `--story N --notify`.

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
