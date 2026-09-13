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
| ComfyUI (optional) | Generates the AI backgrounds; see §10. Without it videos render over the Minecraft footage. |

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

- `assets/MCParkour.mp4` — fallback background gameplay footage (~2.3 GB); used
  when ComfyUI is not configured or a generation fails
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
LMS_MODEL=mn-12b-mag-mell-r1            # optional, this is the default; see §5

# AI backgrounds (optional - leave COMFYUI_DIR unset for the Minecraft footage). See §10.
COMFYUI_DIR=C:\ComfyUI_windows_portable  # the launcher starts ComfyUI from here
COMFYUI_URL=http://127.0.0.1:8188        # optional, this is the default
BACKGROUND_MODE=ai                       # ai (default) | minecraft
BACKGROUND_STYLE=image                   # image (default, Flux still + camera move) | video (Wan clip)
#COMFY_UNET=...                          # optional: a different Wan 2.2 5B checkpoint file name
#COMFY_WIDTH=704 / COMFY_HEIGHT=1280 / COMFY_LENGTH=121   # optional: model-native geometry, ~5x slower on a 3050

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

### 10. AI backgrounds (ComfyUI + Flux / Wan 2.2)

`generateContent.py` splits each narration into 10–15 s beats, asks the story
model for a shot description per beat, and generates one shot per beat in
ComfyUI. Two styles, chosen by `BACKGROUND_STYLE` in `.env` (or `--style`):

| Style | Model | Per beat | Look |
|---|---|---|---|
| **`image`** (default) | Flux.1-schnell still, animated with a slow ffmpeg push-in / pull-out / pan | ~1 min | sharp 1080x1920, cinematic still |
| `video` | Wan 2.2 TI2V-5B clip, ping-ponged to the beat length | ~5 min | real motion, soft (480p upscaled) |

The shots are stitched and the subtitles are rendered over them instead of
the Minecraft footage. Everything is local; the step costs nothing but GPU
time.

**a. Install ComfyUI.** Use the *portable* Windows build — it bundles its own
Python (this repo's interpreter is 3.14, which ComfyUI does not support) and
needs no install: https://github.com/comfyanonymous/ComfyUI/releases → download
`ComfyUI_windows_portable_nvidia.7z`, extract to e.g. `C:\ComfyUI_windows_portable`.

**b. Download the model files** into the portable build's `ComfyUI\models\`
tree. All repos are under https://huggingface.co/Comfy-Org/.

For the `image` style (one file, 17.2 GB — unet, T5, CLIP and VAE in one):

| File | From repo | Goes in |
|---|---|---|
| `flux1-schnell-fp8.safetensors` | `flux1-schnell` | `models\checkpoints\` |

For the `video` style (~18 GB, the files sit in each repo's `split_files/`):

| File | From repo | Goes in |
|---|---|---|
| `wan2.2_ti2v_5B_fp16.safetensors` | `Wan_2.2_ComfyUI_Repackaged` → `split_files/diffusion_models/` | `models\diffusion_models\` |
| `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | `Wan_2.1_ComfyUI_repackaged` → `split_files/text_encoders/` | `models\text_encoders\` |
| `wan2.2_vae.safetensors` | `Wan_2.2_ComfyUI_Repackaged` → `split_files/vae/` | `models\vae\` |

These are the exact names in `assets/comfy/flux_schnell_t2i_api.json` and
`assets/comfy/wan22_5b_t2v_api.json`. Flux.1-schnell is Apache-2.0 (fine for
a monetised channel; Flux *dev* is not). A different Wan 5B checkpoint can be
used without editing the workflow by setting `COMFY_UNET=<file name>`.

**c. Set `COMFYUI_DIR` in `.env`** and restart the pipeline. The launcher starts
ComfyUI on `:8188`; `generateContent.py` finds it through `COMFYUI_URL`.

**d. Benchmark one shot before trusting the schedule.** With the pipeline
stopped (so LM Studio is not holding the VRAM) and ComfyUI running:

```powershell
python .\pipeline\clips.py "a dark forest trail at night, fog"            # image style
python .\pipeline\clips.py --video "a dark forest trail at night, fog"    # video style
```

It runs the exact workflow the pipeline uses and prints the seconds per shot.
Measured on the RTX 3050 (8 GB) for the video style, 20 steps, fp16 weights:

| Geometry | Step time | Per clip | Per 4-beat story |
|---|---|---|---|
| 704x1280 × 121 frames (model native) | ~58 s | ~25 min | ~100 min — too slow |
| **480x832 × 81 frames (default)** | ~11 s | **~4.6 min** | **~18 min** |

The card is compute-bound at this size: casting the weights to fp8 changed
nothing (same step time) and, on the 5B model, produced flat near-black clips,
so the workflow keeps `weight_dtype: default` (fp16). Override the geometry
with `COMFY_WIDTH` / `COMFY_HEIGHT` / `COMFY_LENGTH` in `.env` if a faster GPU
turns up; the render upscales to 1080x1920 either way.

Two things about the ComfyUI web UI (http://127.0.0.1:8188): after
downloading models, reload the tab or the "Missing Models" banner shows a
stale list; and the ✕ next to *Run* cancels whatever the server is executing —
the pipeline's clip included — so use the UI for experiments only while no
story is rendering. A cancelled clip is retried once with a new seed; if that
fails too, the story falls back to the Minecraft footage.

**e. Cadence.** Generation is the new long pole (≈ 4 × clip time per story).
Set the `Schedule Trigger` interval in `ContentGeneration3.0` so a run finishes
before the next starts — ~96 minutes for 15 stories a day gives ~5× headroom
at the default geometry.

VRAM: the RTX 3050 has 8 GB and the story model holds 7 of them, so
`generateContent.py` unloads it (`lms unload --all`) after the shot prompts are
written, generates, then reloads it (`lms load`). The `generate_lock.lock`
mutex already stops n8n from prompting the model while a render is in flight.
If anything in the generation step fails — ComfyUI down, a timeout, an ffmpeg
error — the log shows `[!] AI background failed (...)` and the video is
rendered over the Minecraft footage and published as normal. Force that path
for one render with `--background minecraft`.

Generated clips live in `HorrorVideos/clips/<n>/` (gitignored) and are reused
if the same story is rendered again, so a re-render only regenerates clips that
are missing.

### 11. Public hosting

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

**Every video has the Minecraft background.** Look for `[!] AI background failed`
in `logs/watcher-*.out.log`: the line names the cause (ComfyUI unreachable, a
missing model file reported by ComfyUI, a clip timeout). `COMFYUI_DIR` unset
or `BACKGROUND_MODE=minecraft` also select the fallback, silently.

**LM Studio has no model loaded after a render.** The reload after generation
failed (`lms load` not on PATH, or the model name in `LMS_MODEL` is wrong).
n8n's next request will time out; run `lms load <model>` by hand and fix `.env`.
