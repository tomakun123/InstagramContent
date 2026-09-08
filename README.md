# InstagramContent

Automated horror-short pipeline. Every 50 minutes: writes a story with a local LLM,
narrates it, renders a vertical subtitled video, uploads it to YouTube, and emails
a confirmation.

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
pipeline/    generateContent.py (TTS -> music -> render), storyWatcher.py, paths.py
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
