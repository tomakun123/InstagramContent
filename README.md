# InstagramContent

Automated horror-short pipeline. On a schedule: writes a story with a local LLM,
narrates it, renders a vertical subtitled video, uploads it to YouTube, Instagram
Reels and TikTok, and emails a confirmation for each. (TikTok posts arrive as
"Only me" and are set to "Everyone" by hand — see `docs/SETUP.md` §9c.) Each platform has a daily
limit (YouTube's default API quota allows roughly six uploads); when one starts
refusing, the publish workflow emails once and pauses that platform for 24 h while
the others keep posting. Only when all three are paused does it stop the whole
pipeline (`stop-pipeline.ps1`) — restart it with `start-pipeline.ps1`.

## Start it

```powershell
.\scripts\start-pipeline.ps1
```

Brings up LM Studio, n8n, the cloudflared tunnel, and the story watcher — in
dependency order, with health checks. Safe to run twice.

```powershell
.\scripts\stop-pipeline.ps1        # shut down
.\scripts\start-pipeline.ps1 -Install   # also start at logon
```

## Layout

```
pipeline/    generateContent.py (TTS -> music -> render), storyWatcher.py, videoServer.py, paths.py
scripts/     start/stop launchers
workflows/   n8n workflow exports
docs/        architecture, setup, benchmarks
assets/      background video, music, subtitle font
web/         privacy/ToS pages required for platform API review

HorrorStories/ HorrorAudio/ HorrorVideos/ Metadata/   runtime output (gitignored)
logs/                                                 service logs (gitignored)
```

## Docs

- [docs/SETUP.md](docs/SETUP.md) — install from scratch, `.env`, credentials
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the parts synchronise, known rough edges
- [docs/BENCHMARKS.md](docs/BENCHMARKS.md) — render timings and the next optimisation
