# SloEmergencyHub

Live dashboard for Slovenian emergency services (PGD, GZ, CZ, NMP, AED, PPO).

## Data sources
- FireApp (`fireapp.eu`) public APIs – GPS positions, active interventions, NMP crew presence.
- No authentication required for the endpoints used.

## Deploy on Railway
1. Push this folder to a private GitHub repo.
2. In Railway: `New Project → Deploy from GitHub repo`.
3. Railway auto-detects the `Dockerfile`.
4. Set environment variable `PORT=8080` (default).
5. Done — public URL is on your Railway dashboard.

## Local run
```bash
pip install -r requirements.txt
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8080
# open http://localhost:8080
```

## API endpoints
- `GET /api/orgs` – all units with GPS
- `GET /api/nmp` – live NMP ambulance crew
- `GET /api/unit/{id}/running` – active intervention for unit
- `GET /api/unit/{id}/check` – quick intervention check
- `GET /api/scan?start=0&end=200` – scan for active interventions
- `POST /api/gps/{id}?lat=...&lon=...` – report GPS
- `WebSocket /ws` – real-time push events

## Anti-ban measures
- Rotating User-Agent strings
- 5-minute cache for organisation map
- ~1-minute cache for NMP crew
- Exponential backoff on upstream errors
- Gentle scan pacing (0.7-1.0 s between checks)
