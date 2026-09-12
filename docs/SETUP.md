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
N8N_BLOCK_ENV_ACCESS_IN_NODE=false      # the IG/TikTok nodes read $env.* below
N8N_EDITOR_BASE_URL=https://<your-subdomain>
WEBHOOK_URL=https://<your-subdomain>

# Launcher
CLOUDFLARED_TUNNEL=<tunnel-name>        # optional; ~/.cloudflared/config.yml is used if unset
LMS_MODEL=qwen2.5-14b-instruct          # optional, this is the default; see §5 for the model we actually use

# Render -> publish handoff. generateContent.py POSTs here when a render
# finishes, which is what triggers PublishingContent2.0. If unset, videos are
# still rendered but nothing is published.
N8N_RENDER_WEBHOOK=http://127.0.0.1:5678/webhook/render-complete

# Instagram (optional - leave unset for a YouTube-only install).
# The video server only starts when VIDEO_URL_SECRET is set.
IG_USER_ID=<instagram user id>            # from GET graph.instagram.com/me?fields=id
VIDEO_URL_SECRET=<long random string>     # path segment that gates the public video URL
VIDEO_PUBLIC_BASE=https://videos.<your-domain>   # tunnel hostname for videoServer.py

# TikTok (optional). See §9. Posts stay private until the Direct Post audit passes.
TIKTOK_CLIENT_KEY=<client key from the TikTok developer app>
TIKTOK_CLIENT_SECRET=<client secret>
TIKTOK_TOKEN_FILE=C:\n8n-data\tiktok_token.json   # written by the workflow, keep it outside the repo
TIKTOK_PRIVACY_LEVEL=SELF_ONLY            # PUBLIC_TO_EVERYONE once audited
```

`N8N_BLOCK_ENV_ACCESS_IN_NODE` must be `false` (n8n's default is to block
`$env`); otherwise `IG Params` and every `TT …` node fail with
"access to env vars denied". n8n reads it at start-up, so restart the pipeline
after changing it.

`start-pipeline.ps1` parses `.env` and exports it before launching n8n, which
replaces the old `npx dotenv-cli -- n8n start` invocation.

### 5. LM Studio

Load the story model and start the server on `http://127.0.0.1:1234/v1`.
The launcher does this for you via `lms server start` / `lms load`; set
`LMS_MODEL` in `.env` to the model identifier LM Studio shows.

Current model: **MN-12B-Mag-Mell** (Mistral Nemo fine-tune; IQ4_XS fits the
8 GB card). `qwen2.5-14b-instruct` still works but writes flatter prose.

For Mistral-Nemo-based models (Mag-Mell, Rocinante) open the model's settings in
LM Studio and set the **prompt template to Mistral Instruct** (not ChatML), and
add `</s>` and `[TOOL_CALLS]` as **stop strings**. Under a ChatML template the
model leaks its end-of-sequence token as text and keeps going. Both parsers in
the workflows cut at those tokens anyway, so a wrong template only wastes tokens
rather than breaking the run.

In n8n, create an OpenAI credential pointing at that base URL (if the direct
address fails from a container, try `http://host.docker.internal:1234/v1`). The
API key field must be non-empty but the value is ignored — `local` works.

### 6. Import the workflows

Import all three JSON files from `workflows/` into n8n, then attach credentials:

| Workflow | Credentials needed |
|---|---|
| `ContentGenerate2.0` | OpenAI (pointed at LM Studio) |
| `PublishingContent3.0` | YouTube OAuth2, Gmail OAuth2, Instagram Graph (Query Auth, see §8); TikTok needs no n8n credential (see §9) |
| `InstagramTokenRefresh` | Instagram Graph (Query Auth), Gmail OAuth2 |
| `TikTokTest` | Gmail OAuth2 (TikTok via `.env`, see §9) |
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

**c. n8n credential.** Credentials → *Query Auth*. The dialog's **Name** field
is the query-parameter name Meta expects, so it must be exactly `access_token`
(not a label like "Instagram Graph" — that sends `?Instagram Graph=…` and every
call fails with "Invalid OAuth 2.0 Access Token"); **Value** = the token. Attach it to the four
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

### 9. TikTok

Direct Post through the Content Posting API, with the bytes pushed from n8n
(`FILE_UPLOAD`), so no domain verification is needed. Two TikTok rules shape the
setup: access tokens last 24 h (refresh tokens a year), and an app that has not
passed the **Direct Post audit** may only post `SELF_ONLY` — the video appears
on the account as "Only me". The lane handles both: it refreshes the token on
every run and falls back to `SELF_ONLY` whenever TikTok does not offer the level
in `TIKTOK_PRIVACY_LEVEL`.

**a. Developer app** (developers.tiktok.com). Add the products *Login Kit* and
*Content Posting API*. Under Login Kit tick the scopes `user.info.basic` and
`video.publish` — they only appear once Content Posting API is added, and an
authorize URL that asks for an unticked scope is rejected. Redirect URI (Web):
`https://n8n.<your-domain>/rest/oauth2-credential/callback` — n8n's own OAuth
callback. It will show an n8n error page after approval (no credential matches),
which is fine: the `?code=` in the address bar is all step b needs. Privacy policy / terms URLs: the pages in
`web/TikTok/`. While the app is unaudited, add your own account as a target user.

**b. Authorize once**, by hand. Open in a browser (client key from the app's
*Credentials* panel):

```
https://www.tiktok.com/v2/auth/authorize/?client_key=<KEY>&scope=user.info.basic,video.publish&response_type=code&redirect_uri=https://n8n.<your-domain>/rest/oauth2-credential/callback&state=x
```

Approve, then copy the `code` from the address bar of the page it lands on and
exchange it:

```powershell
$r = Invoke-RestMethod -Method Post -Uri https://open.tiktokapis.com/v2/oauth/token/ -ContentType 'application/x-www-form-urlencoded' -Body @{
  client_key = '<KEY>'; client_secret = '<SECRET>'; code = '<CODE>'
  grant_type = 'authorization_code'; redirect_uri = 'https://n8n.<your-domain>/rest/oauth2-credential/callback'
}
New-Item -ItemType Directory -Force C:\n8n-data | Out-Null
$r | ConvertTo-Json | Set-Content -Encoding ascii C:\n8n-data\tiktok_token.json
```

The file must contain `access_token` and `refresh_token`. The workflow rewrites
it on every run (TikTok rotates refresh tokens), which is why it lives outside
the repo. It writes the pair back as a one-element JSON array (`[ { … } ]`);
`TT Refresh Token` accepts both that and the plain object you saved by hand. Fill in the four `TIKTOK_*` keys in `.env` and restart the pipeline.

**c. Making posts public.** Every post lands as "Only me" (`SELF_ONLY`) and is
made public by hand: set the TikTok account to public once, then in TikTok
Studio → Posts change each video's privacy dropdown from "Only me" to
"Everyone". The caption and hashtags the pipeline supplied are kept.

This is deliberate. Lifting the restriction needs the Direct Post audit, and
TikTok's [Content Sharing Guidelines](https://developers.tiktok.com/docs/en/content-sharing-guidelines)
list "a utility tool to help upload contents to the account(s) you or your team
manages" as not acceptable — which is exactly what this pipeline is — so the
audit is not pursued and `TIKTOK_PRIVACY_LEVEL` stays `SELF_ONLY`. The
unaudited caps (5 posting users and roughly 15 posts per creator per 24 h) are
well above what the schedule produces. `workflows/TikTokTest.json` remains the
manual re-post tool: a form that takes a story number and a privacy level and
runs the same `TT …` nodes.

If the refresh token ever expires or is revoked, the lane emails "TikTok publish
FAILED … Stage: TT Refresh Token"; repeat step b.

### 10. Public hosting

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

**One platform is being skipped.** It hit its posting limit in the last 24 h and
has a `HorrorStories/blocked_<platform>.flag`. It resumes on its own after 24 h,
when the pipeline is restarted, or as soon as you delete the flag. When all three
flags exist the pipeline stops itself (email "Pipeline STOPPED").

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
