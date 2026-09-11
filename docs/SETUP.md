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
double-fire the generation cron.

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

# Instagram (optional - leave unset for a YouTube-only install).
# The video server only starts when VIDEO_URL_SECRET is set.
IG_USER_ID=<instagram user id>            # from GET graph.instagram.com/me?fields=id
VIDEO_URL_SECRET=<long random string>     # path segment that gates the public video URL
VIDEO_PUBLIC_BASE=https://videos.<your-domain>   # tunnel hostname for videoServer.py
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
| `PublishingContent3.0` | YouTube OAuth2, Gmail OAuth2, Instagram Graph (Query Auth, see §8) |
| `InstagramTokenRefresh` | Instagram Graph (Query Auth), Gmail OAuth2 |
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

### 8. Instagram Reels

Instagram does not accept uploaded bytes; Meta downloads the finished mp4 from a
public URL. `pipeline/videoServer.py` serves `HorrorVideos/` on `:8090` and the
tunnel exposes it. Four one-time steps:

**a. Meta app.** At developers.facebook.com create an app and add the *Instagram*
product using **"API setup with Instagram business login"** (no Facebook Page
needed). The Instagram account must be a Professional account. In
*1. Generate access tokens* add the account, accept the tester invite in the
Instagram app (Settings → Website permissions → Tester invites), then
*Generate token*. It is already long-lived (60 days). Webhooks, business login and
App Review on that page are not needed — a dev-mode app can publish to its own
tester accounts.

**b. Verify** (paste in a browser, token redacted from anything you commit):

```
https://graph.instagram.com/me?fields=id,username&access_token=<token>
https://graph.instagram.com/<id>/content_publishing_limit?access_token=<token>
```

The first gives `IG_USER_ID`; the second must return `quota_usage` (25 posts /
24 h), which proves `instagram_business_content_publish` was granted.

**c. n8n credential.** Credentials → *Query Auth*, name `Instagram Graph`,
parameter name `access_token`, value = the token. Attach it to the four
`IG …` HTTP nodes in `PublishingContent3.0` and to `Refresh Token` in
`InstagramTokenRefresh` after importing them.

**d. Tunnel hostname.** Add an ingress to `~/.cloudflared/config.yml`, above the
404 catch-all, and route DNS for it once:

```yaml
ingress:
  - hostname: n8n.<your-domain>
    service: http://localhost:5678
  - hostname: videos.<your-domain>
    service: http://localhost:8090
  - service: http_status:404
```

```powershell
cloudflared tunnel route dns <tunnel-name> videos.<your-domain>
```

Then fill in the three `IG`/`VIDEO_*` keys in `.env` and restart the pipeline.
Check with `curl -I https://videos.<your-domain>/v/<secret>/HorrorStory1.mp4` —
expect `200` and `Accept-Ranges: bytes`; without the secret, `404`.

Import `workflows/InstagramTokenRefresh.json` and activate it: it refreshes the
token monthly and emails the new value, which must be pasted into the credential
by hand (n8n cannot rewrite its own credentials from a workflow).

### 9. Public hosting

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
