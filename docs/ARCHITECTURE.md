# Architecture

An automated faceless-content pipeline. Every 50 minutes it writes a horror story
with a locally-hosted LLM, narrates it, renders a vertical subtitled Short, uploads
it to YouTube, and emails a confirmation — unattended.

Everything runs on one Windows PC (RTX 3050). n8n handles orchestration; Python
handles media. The two communicate entirely through the filesystem.

## Components

| Piece | What it does |
|---|---|
| **LM Studio** (`:1234`) | Serves `qwen2.5-14b-instruct` behind an OpenAI-compatible API. Both n8n LLM nodes point at it. |
| **n8n** (`:5678`) | Orchestration. Three workflows: `ContentGenerate2.0`, `PublishingContent2.0`, `PromptHorrorGeneration`. |
| **cloudflared** | Named tunnel exposing n8n on a subdomain, so platform APIs can reach the OAuth callbacks. |
| **`pipeline/storyWatcher.py`** | Watches `Metadata/`. A new JSON there means "a story is ready" and triggers the render. |
| **`pipeline/generateContent.py`** | TTS → music mix → render, in one pass. ~19 minutes per video. |

## The flow

```
Schedule Trigger (every 50 min)
        │
        ├─ generate_lock.lock present? ──yes──> delete lock, Stop and Error (skip run)
        │                                        (self-heals a stale lock for next time)
        no
        │
   create lock
        │
   counter.txt: n = n + 1  ────────────────> filename base is "HorrorStory{n}"
        │
   PromptHorrorGeneration (sub-workflow, multi-prompt story generation)
        │
   write HorrorStories/HorrorStory{n}.txt
        │
   Generate Metadata (qwen → strict JSON: title, hook, captions per platform)
        │
   write Metadata/HorrorStory{n}_metadata.json  ★ HANDOFF
        │                                              │
        │                                    storyWatcher.py sees the file
        │                                              │
        │                                    generateContent.py
        │                                      1. edge_tts narration
        │                                      2. ffmpeg music mix
        │                                      3. whisper timings + MoviePy render
        │                                      4. write .part.mp4, then rename → .mp4  ★ DONE SIGNAL
        │
   Wait 25 min, then poll for HorrorStory{n}.part.mp4
        │
   PublishingContent2.0
        ├─ read counter + metadata JSON
        ├─ DELETE generate_lock.lock          ← releases the mutex for the next cycle
        ├─ read HorrorVideos/HorrorStory{n}.mp4
        ├─ POST  googleapis resumable upload session
        ├─ PUT   the video bytes
        ├─ YouTube "Update a video" → real title, description, tags, public
        └─ Gmail confirmation with the /shorts/{id} link
```

## Synchronisation contracts

There is no message queue. The two workflows and the Python watcher coordinate
through four pieces of shared filesystem state:

| Signal | Written by | Read by | Purpose |
|---|---|---|---|
| `HorrorStories/counter.txt` | ContentGenerate (increments) | both workflows, both scripts | The story number. Every filename derives from it. |
| `Metadata/HorrorStory{n}_metadata.json` | ContentGenerate | `storyWatcher.py`, PublishingContent | Trigger for the render, and the caption source for upload. |
| `HorrorVideos/HorrorStory{n}.part.mp4` → `.mp4` | `generateContent.py` (atomic rename) | ContentGenerate's poll | "Render finished." The rename is atomic so a partial file is never observed. |
| `HorrorStories/generate_lock.lock` | ContentGenerate creates | ContentGenerate checks, **PublishingContent deletes** | Mutex. Stops the 50-minute cron from starting a second run while a ~19-minute render is in flight. |

The lock being released by the *publish* workflow rather than the generate workflow
is deliberate: it means the gate stays shut for the entire generate → render →
publish cycle, not just the generate step.

## Known rough edges

Carried over and not yet fixed:

1. **The `.part.mp4` poll in `ContentGenerate2.0` compares mismatched values.**
   `FINALREADPART` returns `fileName` as a full path, and the `If` node's left side
   prepends the directory again — so equality likely never fires and every run takes
   the "render finished" branch immediately. If a render overruns the 25-minute
   `Wait`, publish reads an `.mp4` that does not exist yet.

2. **`Code in JavaScript3` reads `item.json.metadata?.title`** after `Merge3` has
   already flattened those fields, so the initial upload title is probably literally
   `undefined: undefined #shorts`. Masked because the later `Update a video` node
   overwrites the title.

3. **`ytBody.snippet.tags` is an array concatenated into a description string**, so
   tags render inline as `#shorts,#ytshorts,...`.

4. **Publish re-reads `counter.txt`** instead of receiving the story name from its
   caller — its `executeWorkflowTrigger` is `passthrough` with empty inputs. Works
   only because the lock guarantees the counter has not moved.

5. **Render cost is misattributed.** `BENCHMARKS.md` labels ~18 minutes as
   "NVENC", but NVENC encode of a ~3-minute 1080×1920 clip is well under a minute.
   The time is MoviePy compositing ~5,400 frames in Python and feeding them to the
   encoder one at a time. Moving subtitle burn-in into ffmpeg (generate an `.ass`
   file from the Whisper word timings, then one `ffmpeg -vf ass=...` command) should
   cut this dramatically — and would remove the GPU requirement entirely.

## Related docs

- [SETUP.md](SETUP.md) — installing and configuring everything from scratch
- [BENCHMARKS.md](BENCHMARKS.md) — render timing history
