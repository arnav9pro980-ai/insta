# Chat Browser API (Render)

A FastAPI service that drives headless Chrome with a **saved profile**. Your login cookies are stored on a Render disk (`/data/chrome_profile`), so the service only logs in when the session has expired.

## Deploy on Render
1. Push these files to a GitHub repo.
2. In Render, go to New, then Blueprint, and pick the repo (it uses `render.yaml`).
3. Fill in `USER_EMAIL`, `USER_PASSWORD`, `LOGIN_URL`, `REDIRECT_URL`, `BASE_URL`.
4. Copy the generated `API_KEY` from the Environment tab.

> The persistent disk needs a paid plan (Starter or higher). On the free plan the profile is wiped on every restart, so the service logs in again each time.

## Run locally
```bash
pip install -r requirements.txt && playwright install chromium
cp .env.example .env   # edit it, then export the variables
python app.py
```

## Endpoints
Every endpoint except `/health` needs the header `X-API-Key: <API_KEY>`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check |
| GET | `/status` | Current URL, page title, logged-in state, open thread |
| GET | `/screenshot` | PNG of the current page (for debugging) |
| POST | `/login` | Logs in only if the session has expired |
| GET | `/users` | Lists the chat threads you can open |
| GET | `/messages/{user}?limit=10` | Reads the last N messages |
| POST | `/send` | `{"user": "name", "message": "hi"}` |
| POST | `/reply` | `{"user": "name", "message": "ok", "message_index": -1}` |

Interactive docs: `https://<your-app>.onrender.com/docs`

## Examples
```bash
H="X-API-Key: $API_KEY"; U=https://your-app.onrender.com
curl $U/health
curl -H "$H" $U/status
curl -H "$H" $U/users
curl -H "$H" "$U/messages/John?limit=20"
curl -H "$H" -H "Content-Type: application/json" -d '{"user":"John","message":"Hello!"}' $U/send
curl -H "$H" -H "Content-Type: application/json" -d '{"user":"John","message":"Sure","message_index":-1}' $U/reply
```

## Notes
- Requests are queued one at a time because they share a single browser tab.
- If login is blocked by a 2FA or checkpoint page, open `/screenshot` to see it. You can also log in once locally with `HEADLESS=false`, then upload that `chrome_profile` folder to the disk.
- Automating a personal account may break the site's terms of service, so use it with care.
