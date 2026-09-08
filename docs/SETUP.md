# Setup

Everything runs locally on Windows. See [ARCHITECTURE.md](ARCHITECTURE.md) for how
the pieces fit together.

## Daily use

```powershell
.\scripts\start-pipeline.ps1     # brings up LM Studio, n8n, cloudflared, watcher
.\scripts\stop-pipeline.ps1      # shuts it all down
```

Register it to start at logon so the pipeline survives a reboot:

```powershell
.\scripts\start-pipeline.ps1 -Install
```

Useful flags: `-SkipTunnel` (local work, no cloudflared), `-TimeoutSeconds N`
(default 90s per health check). The launcher is idempotent — running it twice will
not spawn duplicate services, which matters because two n8n instances would
double-fire the 50-minute cron.

Logs land in `logs/<service>-<date>.out.log` and `.err.log`.

## First-time install

### 1. Prerequisites

| Tool | Install |
|---|---|
| Python 3.11+ | https://python.org |
| ffmpeg | https://www.ffmpeg.org/download.html#build-windows — must be on `PATH`. Needs `libass` (subtitle burn-in) and `h264_nvenc`. Verify with `ffmpeg -filters \| findstr ass` and `ffmpeg -encoders \| findstr nvenc`. |
| Node + n8n | `npm i -g n8n` (do **not** install it into this repo) |
| LM Studio | https://lmstudio.ai — install the `lms` CLI so the launcher can drive it |
| cloudflared | https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/ |

### 2. Python environment

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

The launcher prefers `.venv\Scripts\python.exe` and warns if it falls back to
system Python.

### 3. Assets

`assets/` holds the static inputs. The two large ones are gitignored and must be
present locally:

- `assets/MCParkour.mp4` — background gameplay footage (~2.3 GB)
- `assets/musicOutput.mp3` — background music bed
- `assets/use.ttf` — subtitle font (tracked in git)

### 4. `.env`

```ini
# n8n
N8N_RESTRICT_FILE_ACCESS_TO=C:\Users\<you>\Desktop\InstagramContent
N8N_EDITOR_BASE_URL=https://<your-subdomain>
WEBHOOK_URL=https://<your-subdomain>

# Launcher
CLOUDFLARED_TUNNEL=<tunnel-name>        # optional; ~/.cloudflared/config.yml is used if unset
LMS_MODEL=qwen2.5-14b-instruct          # optional, this is the default

# Render -> publish handoff. generateContent.py POSTs here when a render
# finishes, which is what triggers PublishingContent2.0. If unset, videos are
# still rendered but nothing is published.
N8N_RENDER_WEBHOOK=http://127.0.0.1:5678/webhook/render-complete
```

`start-pipeline.ps1` parses `.env` and exports it before launching n8n, which
replaces the old `npx dotenv-cli -- n8n start` invocation.

### 5. LM Studio

Load `qwen2.5-14b-instruct` and start the server on `http://127.0.0.1:1234/v1`.
The launcher does this for you via `lms server start` / `lms load`.

In n8n, create an OpenAI credential pointing at that base URL (if the direct
address fails from a container, try `http://host.docker.internal:1234/v1`). The
API key field must be non-empty but the value is ignored — `local` works.

### 6. Import the workflows

Import all three JSON files from `workflows/` into n8n, then attach credentials:

| Workflow | Credentials needed |
|---|---|
| `ContentGenerate2.0` | OpenAI (pointed at LM Studio) |
| `PublishingContent2.0` | YouTube OAuth2, Gmail OAuth2 |
| `PromptHorrorGeneration` | OpenAI (pointed at LM Studio) |

Credentials are **not** in these exports — they live in n8n's own database and must
be recreated by hand.

> `workflows/PromptHorrorGeneration.json` has not been exported yet. Export it from
> the n8n UI and commit it — right now that sub-workflow exists only inside n8n's
> database.

### 7. Register the publish webhook

`PublishingContent2.0` is triggered by a Webhook node on `/webhook/render-complete`
rather than being called by `ContentGenerate2.0`. After importing it, activate the
workflow so the production webhook URL is registered, then confirm
`N8N_RENDER_WEBHOOK` in `.env` matches the URL n8n shows on that node.

Use the local address (`http://127.0.0.1:5678/...`) rather than the tunnel
hostname — the render runs on the same machine as n8n, so there is no reason to
route the callback out through Cloudflare and back.

### 8. Public hosting

`web/Instagram/` and `web/TikTok/` hold the privacy policy, terms of service, and
TikTok domain-verification files required for platform API review. Deploy them to
your main domain (they are served from a `/Public/...` route) — TikTok's review
checks these URLs are reachable.

## Troubleshooting

**A health check times out.** The launcher stops and names the failing service
rather than starting downstream ones. Check that service's `.err.log`.

**Runs are being skipped.** A stale `HorrorStories/generate_lock.lock` from an
interrupted render. `stop-pipeline.ps1` clears it; the workflow also self-heals by
deleting the lock on the run that finds it.

**Story number is wrong.** `HorrorStories/counter.txt` is the source of truth for
the next story number. It is now written only after the story file exists, so a
failed run no longer consumes a number. If it drifts, set it to the highest
`HorrorStory*.txt` in `HorrorStories/`.

**A video rendered but nothing was published.** The render POSTs to
`N8N_RENDER_WEBHOOK` on success. Check that the variable is set, that
`PublishingContent2.0` is active, and look for `[!] Could not notify n8n` in
`logs/watcher-*.out.log`. To publish it by hand:
`python .\pipeline\generateContent.py --story N --notify`.

**Nothing renders when metadata appears.** `storyWatcher.py` is not running, or was
started before `Metadata/` existed. Check `logs/watcher-*.err.log`.
