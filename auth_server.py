from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from starlette.middleware.sessions import SessionMiddleware
import os, requests, base64, json, logging, sys
from urllib.parse import urlencode
from dotenv import load_dotenv
from datetime import datetime, timedelta
from db import SessionLocal, OutlookToken, Meeting
from dateutil import parser

# Load environment variables
load_dotenv()

# Logging setup
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)s - %(asctime)s - %(message)s")
logger = logging.getLogger("uvicorn")

# Microsoft Graph settings
CLIENT_ID = os.getenv("MS_CLIENT_ID")
CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")
REDIRECT_URI = os.getenv("MS_REDIRECT_URI")
TENANT_ID = os.getenv("MS_TENANT_ID") or "common"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/Calendars.ReadWrite", "offline_access", "User.Read"]

# FastAPI app
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key="any-random-secret")

@app.get("/")
async def home():
    return HTMLResponse("<a href='/login'>🔗 Connect Outlook Calendar</a>")


def generate_title_from_summary(summary: str) -> str:
    lines = summary.splitlines()
    for line in lines:
        if line.strip().startswith("📍 Place:"):
            return "Meeting at " + line.split("📍 Place:")[1].strip().split(" (")[0]
    return "Meeting"

def extract_time_from_summary(summary: str) -> str:
    lines = summary.splitlines()
    for line in lines:
        if line.strip().startswith("🕒 Time:"):
            time_str = line.split("🕒 Time:")[1].strip()
            try:
                dt = parser.parse(time_str)
                return dt.strftime("%H:%M")
            except:
                return "10:00"
    return "10:00"


@app.get("/login")
async def login(request: Request):
    telegram_user_id = request.query_params.get("telegram_id")
    meeting_id = request.query_params.get("meeting_id")

    if not telegram_user_id or not meeting_id:
        return HTMLResponse("⚠️ Missing telegram_id or meeting_id")

    logger.info(f"🔎 Received telegram_id: {telegram_user_id}")
    logger.info(f"🔎 Received meeting_id: {meeting_id}")

    state_payload = json.dumps({"telegram_id": telegram_user_id, "meeting_id": meeting_id})
    state_encoded = base64.urlsafe_b64encode(state_payload.encode()).decode().rstrip("=")

    logger.info(f"🔐 Encoded state: {state_encoded}")

    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "response_mode": "query",
        "scope": " ".join(SCOPES),
        "state": state_encoded
    }

    url = f"{AUTHORITY}/oauth2/v2.0/authorize?{urlencode(params)}"
    logger.info(f"🔗 Redirecting to: {url}")
    return RedirectResponse(url)

@app.get("/callback")
async def callback(request: Request, code: str = None, state: str = None):
    if not code:
        return HTMLResponse("❌ Authorization failed")

    try:
        logger.info(f"📥 Raw state received: {state}")
        padded_state = state + '=' * (-len(state) % 4)
        logger.info(f"📥 Padded state: {padded_state}")
        state_json = base64.urlsafe_b64decode(padded_state.encode()).decode()
        logger.info(f"📥 Decoded state JSON: {state_json}")
        state_data = json.loads(state_json)
        telegram_user_id = state_data["telegram_id"]
        meeting_id = int(state_data["meeting_id"])
        logger.info(f"✅ Parsed telegram_id: {telegram_user_id}")
        logger.info(f"✅ Parsed meeting_id: {meeting_id}")
    except Exception as e:
        logger.error(f"⚠️ Invalid state format: {e}")
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
        logger.error(f"❌ Token exchange failed: {e}")
        return HTMLResponse(f"❌ Token exchange failed: {e}")

    access_token = token_json.get("access_token")
    refresh_token = token_json.get("refresh_token")
    expires_in = int(token_json.get("expires_in", 3600))
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    if not access_token:
        logger.error(f"❌ Full token response: {token_json}")
        return HTMLResponse(f"❌ Token error: {token_json}")

    db = SessionLocal()
    meeting = db.query(Meeting).filter_by(id=meeting_id).first()
    if not meeting:
        logger.error(f"❌ Meeting not found for ID: {meeting_id}")
        db.close()
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
        logger.error("⚠️ Invalid time format in DB")
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
        logger.error(f"❌ Calendar API error: {e}")
        return HTMLResponse(f"❌ Calendar API error: {e}")

    if event_response.status_code == 201:
        event = event_response.json()
        logger.info("✅ Event successfully created:")
        logger.info(f"🆔 ID: {event.get('id')}")
        logger.info(f"📅 Subject: {event.get('subject')}")
        logger.info(f"📆 Starts: {event.get('start')}")
        logger.info(f"📆 Ends: {event.get('end')}")
        logger.info(f"📍 Location: {event.get('location', {}).get('displayName')}")
        logger.info(f"📝 Body: {event.get('body', {}).get('content')}")

        test_response = requests.get(
            "https://graph.microsoft.com/v1.0/me/calendar/events",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        logger.info("📋 Events fetched from calendar:")
        logger.info(json.dumps(test_response.json(), indent=2))

        db.close()
        return HTMLResponse("✅ Event created and added to your Outlook Calendar.")
    else:
        logger.error(f"❌ Event creation failed: {event_response.text}")
        db.close()
        return HTMLResponse(f"⚠️ Token saved but event creation failed: {event_response.text}")
