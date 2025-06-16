from fastapi import FastAPI
from auth_server import app as auth_app  # Import your Outlook calendar app

app = FastAPI()

# Mount your Outlook auth routes (optional if already inside FastAPI)
app.mount("/", auth_app)

@app.get("/")
async def root():
    return {"status": "FastAPI server for Outlook + Telegram is running"}
