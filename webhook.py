import requests

N8N_WEBHOOK_URL = "http://localhost:5678/webhook-test/video_ready"  # adjust if needed

requests.post(
    N8N_WEBHOOK_URL,
    json={
        "file_path": "C:\\Users\\Thomas M\\Desktop\\InstagramContent\\HorrorVideos\\HorrorStory2_2026-01-05.mp4",
        "file_name": "HorrorStory2_2026-01-05.mp4",
        "caption": "POV: you found the tape… #horror #storytime",
        "platforms": ["instagram", "tiktok"]
    },
    timeout=10
)
