from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
import os, requests, base64, json
from urllib.parse import urlencode
from dotenv import load_dotenv
from datetime import datetime, timedelta
from db import SessionLocal, OutlookToken, Meeting

load_dotenv()

CLIENT_ID = os.getenv("MS_CLIENT_ID")
CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")
REDIRECT_URI = os.getenv("MS_REDIRECT_URI")
TENANT_ID = os.getenv("MS_TENANT_ID") or "common"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/Calendars.ReadWrite", "offline_access", "User.Read"]

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="any-random-secret")


@app.get("/")
async def home():
    return HTMLResponse("<a href='/login'>🔗 Connect Outlook Calendar</a>")


@app.get("/login")
async def login(request: Request):
    telegram_user_id = request.query_params.get("telegram_id")
    meeting_id = request.query_params.get("meeting_id")

    if not telegram_user_id or not meeting_id:
        return HTMLResponse("⚠️ Missing telegram_id or meeting_id")

    print("🔎 Received telegram_id:", telegram_user_id)
    print("🔎 Received meeting_id:", meeting_id)

    state_payload = json.dumps({
        "telegram_id": telegram_user_id,
        "meeting_id": meeting_id
    })
    state_encoded = base64.urlsafe_b64encode(state_payload.encode()).decode().rstrip("=")

    print("🔐 Encoded state:", state_encoded)

    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "response_mode": "query",
        "scope": " ".join(SCOPES),
        "state": state_encoded
    }

    url = f"{AUTHORITY}/oauth2/v2.0/authorize?{urlencode(params)}"
    print("🔗 Redirecting to:", url)
    return RedirectResponse(url)


@app.get("/callback")
async def callback(request: Request, code: str = None, state: str = None):
    if not code:
        return HTMLResponse("❌ Authorization failed")

    try:
        print("📥 Raw state received:", state)
        padded_state = state + '=' * (-len(state) % 4)
        print("📥 Padded state:", padded_state)

        state_json = base64.urlsafe_b64decode(padded_state.encode()).decode()
        print("📥 Decoded state JSON:", state_json)

        state_data = json.loads(state_json)
        telegram_user_id = state_data["telegram_id"]
        meeting_id = int(state_data["meeting_id"])
        print("✅ Parsed telegram_id:", telegram_user_id)
        print("✅ Parsed meeting_id:", meeting_id)
    except Exception as e:
        return HTMLResponse(f"⚠️ Invalid state format: {e}")

    token_data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
        "scope": " ".join(SCOPES)
    }

    try:
        token_response = requests.post(f"{AUTHORITY}/oauth2/v2.0/token", data=token_data)
        token_json = token_response.json()
    except Exception as e:
        return HTMLResponse(f"❌ Token exchange failed: {e}")

    access_token = token_json.get("access_token")
    refresh_token = token_json.get("refresh_token")
    expires_in = int(token_json.get("expires_in", 3600))
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    if not access_token:
        print("❌ Full token response:", token_json)
        return HTMLResponse(f"❌ Token error: {token_json}")

    db = SessionLocal()
    meeting = db.query(Meeting).filter_by(id=meeting_id).first()
    if not meeting:
        db.close()
        print("❌ Meeting not found for ID:", meeting_id)
        return HTMLResponse("❌ Meeting not found")

    existing = db.query(OutlookToken).filter_by(telegram_user_id=telegram_user_id).first()
    if existing:
        existing.access_token = access_token
        existing.refresh_token = refresh_token
        existing.expires_at = expires_at
    else:
        db.add(OutlookToken(
            telegram_user_id=telegram_user_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at
        ))
    db.commit()

    try:
        start_dt = datetime.combine(
            meeting.meet_date,
            datetime.strptime(meeting.time or "10:00", "%H:%M").time()
        )
    except ValueError:
        db.close()
        return HTMLResponse("⚠️ Invalid time format in DB")

    end_dt = start_dt + timedelta(hours=1)

    calendar_data = {
        "subject": meeting.activity or "Meeting",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Singapore"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Singapore"},
        "location": {"displayName": meeting.place or "Unknown Location"},
        "body": {"contentType": "text", "content": meeting.summary or "Planned via MeetingBot"}
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    try:
        event_response = requests.post(
            "https://graph.microsoft.com/v1.0/me/events",
            headers=headers,
            json=calendar_data
        )
    except Exception as e:
        db.close()
        return HTMLResponse(f"❌ Calendar API error: {e}")

    if event_response.status_code != 201:
        print("❌ Event creation failed:", event_response.text)

    db.close()

    if event_response.status_code == 201:
        return HTMLResponse("✅ Event created and added to your Outlook Calendar.")
    else:
        return HTMLResponse(f"⚠️ Token saved but event creation failed: {event_response.text}")
