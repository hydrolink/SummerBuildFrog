import os
from db import SessionLocal, Meeting
from dotenv import load_dotenv
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes, ChatMemberHandler, CallbackQueryHandler
from openai import OpenAI
from datetime import datetime, date, timedelta, timezone
import re
import dateparser
import googlemaps
from urllib.parse import quote
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from ics import Calendar, Event as IcsEvent
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
import aiohttp
import tempfile
import subprocess
import uuid
from io import BytesIO

editing_sessions = {}



# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
gmaps = googlemaps.Client(key=GOOGLE_MAPS_API_KEY)

# States per group
listening_sessions = {}  # {chat_id: {user: [messages]}}

scheduler = AsyncIOScheduler(timezone="Asia/Singapore")

def escape_markdown_v2(text: str) -> str:
    escape_chars = r"\_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)

def extract_time_from_summary(summary: str) -> str:
    """Extract time from meeting summary"""
    for line in summary.split('\n'):
        if line.strip().startswith("🕒 Time:"):
            return line.split("🕒 Time:")[1].strip()
    return None

def parse_meeting_datetime(meet_date: date, time_str: str) -> datetime:
    """Combine meeting date and time into datetime object"""
    if not meet_date or not time_str:
        return None
    
    try:
        # Parse time string into time object
        time_obj = dateparser.parse(time_str)
        if not time_obj:
            return None
        
        # Combine date and time
        meeting_datetime = datetime.combine(
            meet_date, 
            time_obj.time()
        )
        
        # Set timezone to Singapore
        sg_tz = pytz.timezone('Asia/Singapore')
        meeting_datetime = sg_tz.localize(meeting_datetime)
        
        return meeting_datetime
        
    except Exception as e:
        print(f"❌ Error parsing meeting datetime: {e}")
        return None

# --- COMMANDS 

async def welcome_on_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_status = update.my_chat_member.new_chat_member.status
        if new_status == "member":  # or "administrator" if added as admin
            chat = update.effective_chat
            await context.bot.send_message(
            chat.id,
            escape_markdown_v2(
                "👋 *Welcome to your group's personal meeting assistant\\!* I'm \\@coordinator\\_meetbot — your AI scheduler\\. 🧠🤖\n\n"
                "📌 *Here's how I can help:*\n"
                "I listen to your group chat and generate smart summaries for your meetups\\. This includes:\n"
                "• 📅 *Date*\n"
                "• 🕒 *Time*\n"
                "• 📍 *Place* with nearest MRT info\n"
                "• 👥 *Attendees*\n"
                "• 🎯 *Activity*\n\n"
                "• ⏰ *Auto reminders* 12 hours before your meeting\\!\n\n"
                "▶️ *To get started:*\n"
                "1\\. Type `/startlistening` — I'll start collecting messages\\.\n"
                "2\\. Chat naturally about your meeting plans\\.\n"
                "3\\. Type `/stoplistening` — I'll process everything and summarize\\.\n\n"
                "🧠 *Other useful commands:*\n"
                "`/listmeetings` – View all previous summaries\n"
                "`/deletemeeting <id>` – Delete a saved summary\n\n"
                "`/cancelreminder <id>` – Cancel a scheduled reminder\n\n"
                "🔒 I *only listen* when you explicitly tell me to\\.\n"
                "Let's make planning smooth and stress\\-free\\. 🗓️✨"
            ),
            parse_mode="MarkdownV2"
        )


    except Exception as e:
        print("Error in welcome message:", e)


async def start_listening(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id in listening_sessions:
        await update.message.reply_text("⚠️ Already listening for this group. Use /stoplistening when done.")
        return

    listening_sessions[chat_id] = {}
    await update.message.reply_text("👂 Listening for availability suggestions... Use /stoplistening when you're done.")


async def stop_listening(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id not in listening_sessions:
        await update.message.reply_text("⚠️ I'm not currently listening. Use /startlistening to begin.")
        return

    await update.message.reply_text("✅ Stopped listening. Processing availability now...")
    await process_availability(update, context, chat_id)
    del listening_sessions[chat_id]  # Clear after processing


# --- MESSAGE HANDLING ---

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text

    # --- Editing flow ---
    if user_id in editing_sessions:
        session = editing_sessions[user_id]
        step = session['step']
        db = SessionLocal()
        meeting = db.query(Meeting).filter_by(id=session['meeting_id']).first()

        if not meeting:
            del editing_sessions[user_id]
            await update.message.reply_text("❌ Meeting not found (may have been deleted). Edit cancelled.")
            return


        if step == 'choose_field':
            if text.lower() not in ['date', 'time', 'place', 'pax', 'activity']:
                await update.message.reply_text("❌ Invalid field. Please choose a valid option below.")
                await perform_edit_start(user_id, chat_id, session['meeting_id'], context)
                return
            session['field'] = text.lower()
            session['step'] = 'enter_value'
            await update.message.reply_text(f"✏️ Please enter the new value for *{text}*:", parse_mode="Markdown")
            return

        elif step == 'enter_value':
            field = session['field']
            lines = meeting.summary.split('\n')
            updated_lines = []

            for line in lines:
                if field in line.lower():
                    prefix = line.split(':')[0]
                    updated_lines.append(f"{prefix}: {text}")
                else:
                    updated_lines.append(line)

            # Validation for 'date' or 'time' field
            if field == 'date':
                parsed_date = dateparser.parse(text)
                if not parsed_date or parsed_date.date() < date.today():
                    await update.message.reply_text("❌ Invalid date. Please enter a future date like `next Friday` or `July 10`.")
                    return

                meeting.meet_date = parsed_date.date()

            elif field == 'time':
                parsed_time = dateparser.parse(text)
                if not parsed_time:
                    await update.message.reply_text("❌ Invalid time. Please enter something like `7pm`, `19:30`, or `8:15am`.")
                    return

            # Update summary
            meeting.summary = '\n'.join(updated_lines)
            db.commit()    
            del editing_sessions[user_id]
            buttons = [
                [InlineKeyboardButton("✏️ Edit More", callback_data=f"edit:{meeting.id}")],
                [InlineKeyboardButton("👁️ View Summary", callback_data=f"view:{meeting.id}")]
            ]

            await update.message.reply_text(
                "✅ Meeting updated successfully!",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

            return

    # --- Listening mode ---
    if chat_id in listening_sessions:
        user = update.message.from_user.full_name
        message = update.message.text
        if user not in listening_sessions[chat_id]:
            listening_sessions[chat_id][user] = []
        listening_sessions[chat_id][user].append(message)



# --- DATE EXTRACTION ---
def extract_meeting_date(original_messages, gpt_summary, current_date=None):
    """
    Extract meeting date from original messages and GPT summary with better context awareness
    """
    if current_date is None:
        current_date = date.today()
    
    # First, try to extract from original messages (more reliable)
    all_messages = []
    for user_messages in original_messages.values():
        all_messages.extend(user_messages)
    
    # Common date patterns people use in chat
    date_patterns = [
        r'\b(tomorrow|tmr)\b',
        r'\b(today|tdy)\b', 
        r'\b(next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b',
        r'\b(this\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b',
        r'\b(\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)\b',
        r'\b(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b',
        r'\b(\d{1,2}-\d{1,2}(?:-\d{2,4})?)\b'
    ]
    
    # Try to find date mentions in original messages
    for message in all_messages:
        message_lower = message.lower()
        for pattern in date_patterns:
            matches = re.findall(pattern, message_lower, re.IGNORECASE)
            for match in matches:
                # Parse with current date as reference
                parsed_date = dateparser.parse(
                    match, 
                    settings={
                        'RELATIVE_BASE': datetime.combine(current_date, datetime.min.time()),
                        'PREFER_DATES_FROM': 'future'
                    }
                )
                if parsed_date and parsed_date.date() >= current_date:
                    return parsed_date.date()
    
        # NEW — find line that starts with 📅 Date:
    for line in gpt_summary.splitlines():
        if line.strip().startswith("📅 Date:"):
            date_text = line.split("📅 Date:")[1].strip()
            parsed_date = dateparser.parse(
                date_text,
                settings={
                    'RELATIVE_BASE': datetime.combine(current_date, datetime.min.time()),
                    'PREFER_DATES_FROM': 'future'
                }
            )
            if parsed_date and parsed_date.date() >= current_date:
                return parsed_date.date()
            break  # stop after the first valid 📅 Date
    return None

# Making Buttons 
from telegram import InputFile

async def send_final_summary_with_buttons(context, chat_id, summary_text, meeting_id: int):
    # Parse for .ics fields
    db = SessionLocal()
    meeting = db.query(Meeting).filter_by(id=meeting_id).first()
    time_str = extract_time_from_summary(meeting.summary)
    meeting_datetime = parse_meeting_datetime(meeting.meet_date, time_str)

    # Create .ics file if possible
    ics_buf = None
    if meeting_datetime:
        ics_buf = create_ics_file(
            meeting_title="Group Meeting",
            description=meeting.summary,
            start_time=meeting_datetime
        )


    buttons = [
        [InlineKeyboardButton("✏️ Edit Meeting", callback_data=f'edit:{meeting_id}')],
        [InlineKeyboardButton("🗑️ Delete Meeting", callback_data=f'delete:{meeting_id}')],
        [InlineKeyboardButton("⏰ Set Reminder", callback_data=f'setreminder:{meeting_id}')]
    ]
    await context.bot.send_message(
        chat_id=chat_id,
        text=summary_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

    if ics_buf:
        # ics_buf is a BytesIO with .name == "meeting.ics"
        await context.bot.send_document(
            chat_id=chat_id,
            document=InputFile(ics_buf, filename=ics_buf.name),
            caption="📅 Tap to add this meeting to your calendar!"
        )



async def meeting_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("delete:"):
        meeting_id = int(data.split(":")[1])
        success = await perform_meeting_deletion(update.effective_chat.id, meeting_id, context)
        if success:
            await query.edit_message_text("✅ Meeting deleted.")
        else:
            await query.edit_message_text("❌ Meeting not found.")

    elif data.startswith("edit:"):
        meeting_id = int(data.split(":")[1])
        await perform_edit_start(update.effective_user.id, update.effective_chat.id, meeting_id, context)

    elif data.startswith("editfield:"):
        parts = data.split(":")
        meeting_id = int(parts[1])
        field = parts[2]
        user_id = update.effective_user.id
        editing_sessions[user_id] = {
            'step': 'enter_value',
            'meeting_id': meeting_id,
            'field': field
        }

        field_name = field.capitalize()
        await query.edit_message_text(
            f"✏️ Please enter the new value for *{field_name}*:",
            parse_mode="Markdown"
        )


    elif data.startswith("view:"):
        meeting_id = int(data.split(":")[1])
        db = SessionLocal()
        meeting = db.query(Meeting).filter_by(id=meeting_id).first()
        if meeting:
            await query.edit_message_text(meeting.summary, parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ Meeting not found.")

    elif data.startswith("setreminder:"):
        meeting_id = int(data.split(":",1)[1])
        await offer_reminder_presets(query, context, meeting_id)

    elif data.startswith("remind:"):
        # pattern remind:<meeting_id>:<minutes>
        _, mid, mins = data.split(":")
        await schedule_reminder(query, context, int(mid), int(mins))

    elif data.startswith("remindcustom:"):
        meeting_id = int(data.split(":",1)[1])
        # ask user to type e.g. "90m" or "2h30m"
        await query.edit_message_text(
            "✏️ Please enter a custom reminder interval (e.g. `90m` or `2h30m`):",
            parse_mode="Markdown"
        )
        # store state so the next text message from this user is parsed as a custom interval
        context.user_data["awaiting_custom_reminder_for"] = meeting_id

# --- helper to show preset buttons ---
async def offer_reminder_presets(query, context, meeting_id):
    presets = [("1 h before", 60), ("3 h", 180), ("6 h", 360), ("12 h", 720), ("24 h", 1440)]
    kb = [
        [InlineKeyboardButton(label, callback_data=f"remind:{meeting_id}:{mins}")]
        for label, mins in presets
    ] + [[InlineKeyboardButton("🔧 Custom…", callback_data=f"remindcustom:{meeting_id}")]]
    await query.edit_message_text(
        "⏰ When would you like to be reminded?",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# --- helper to schedule the reminder job ---
async def schedule_reminder(query, context, meeting_id: int, minutes_before: int):
    db = SessionLocal()
    meeting = db.query(Meeting).filter_by(id=meeting_id).first()
    if not meeting or not meeting.meet_date:
        return await query.answer("❌ Missing meeting date.", show_alert=True)

    # parse meeting datetime…
    time_str = extract_time_from_summary(meeting.summary)
    meeting_dt = parse_meeting_datetime(meeting.meet_date, time_str)
    if not meeting_dt:
        return await query.answer("❌ Can't parse meeting time.", show_alert=True)

    remind_dt = meeting_dt - timedelta(minutes=minutes_before)
    if remind_dt < datetime.now(pytz.timezone("Asia/Singapore")):
        return await query.answer("❌ That time is already past.", show_alert=True)

    # schedule the job as before…
    job_id = f"reminder_{meeting_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        send_reminder,
        DateTrigger(run_date=remind_dt),
        args=[context.bot, query.message.chat_id, meeting_id, minutes_before],
        id=job_id,
        replace_existing=True
    )

    # now _update_ the existing summary message instead of replacing it
    orig = query.message.text
    # build a human‐friendly label
    hrs, mins = divmod(minutes_before, 60)
    label = f"{hrs}h" + (f"{mins}m" if mins else "")
    new_line = f"\n⏰ Reminder set: {label} before meeting"
    markup = query.message.reply_markup

    await query.edit_message_text(
        text=orig + new_line,
        parse_mode="Markdown",
        reply_markup=markup
    )


# --- the actual reminder action ---
async def send_reminder(bot, chat_id: int, meeting_id: int, mins_before: int):
    db = SessionLocal()
    meeting = db.query(Meeting).filter_by(id=meeting_id).first()
    if not meeting:
        return
    text = f"⏰ Reminder: your meeting is in {mins_before} minutes!\n\n{meeting.summary}"
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")

# --- GOOGLE MAPS ---
async def get_nearest_mrt(place):
    try:
        geo = gmaps.geocode(place)
        if not geo:
            return "❌ Could not find location."

        latlng = geo[0]["geometry"]["location"]
        lat, lng = latlng["lat"], latlng["lng"]

        # Search for transit stations within 2km
        results = gmaps.places_nearby(location=(lat, lng), radius=2000, type="subway_station")
        stations = results.get("results", [])

        # If no subway_station found, try transit_station but filter for MRT in name
        if not stations:
            results = gmaps.places_nearby(location=(lat, lng), radius=2000, type="transit_station")
            candidates = results.get("results", [])
            # Filter to names containing "MRT" or known MRT keywords
            stations = [s for s in candidates if "mrt" in s["name"].lower()]

        if not stations:
            return "❌ No MRT station nearby."

        # Find closest station by walking distance
        min_dist = None
        closest_station = None

        for station in stations:
            dest = station["geometry"]["location"]
            distance_data = gmaps.distance_matrix(
                [f"{lat},{lng}"],
                [f"{dest['lat']},{dest['lng']}"],
                mode="walking"
            )
            element = distance_data["rows"][0]["elements"][0]
            if element["status"] == "OK":
                dist_val = element["distance"]["value"]
                if (min_dist is None) or (dist_val < min_dist):
                    min_dist = dist_val
                    closest_station = (station, element)

        if closest_station:
            station, element = closest_station
            dist = element["distance"]["text"]
            dur = element["duration"]["text"]
            mrt_name = station["name"]
            return f"{mrt_name} ({dist}, {dur} walk)"

        return f"{stations[0]['name']} (⚠️ distance unavailable)"

    except Exception as e:
        return f"⚠️ MRT error: {str(e)}"



async def find_nearest_bus_stop(location_name):
    try:
        geocode_result = gmaps.geocode(location_name)
        if not geocode_result:
            return "❌ No bus stop nearby."

        location = geocode_result[0]["geometry"]["location"]
        latlng = (location["lat"], location["lng"])

        # Increase radius to 1000 meters for better coverage
        nearby_places = gmaps.places_nearby(
            location=latlng,
            radius=1000,
            keyword="bus stop",
            type="transit_station"
        )

        results = nearby_places.get("results", [])
        if not results:
            return "❌ No bus stop nearby."

        # Find closest by walking distance
        min_dist = None
        closest_stop = None

        for stop in results:
            dest = stop["geometry"]["location"]
            distance_data = gmaps.distance_matrix(
                [f"{latlng[0]},{latlng[1]}"],
                [f"{dest['lat']},{dest['lng']}"],
                mode="walking"
            )
            element = distance_data["rows"][0]["elements"][0]
            if element["status"] == "OK":
                dist_val = element["distance"]["value"]
                if (min_dist is None) or (dist_val < min_dist):
                    min_dist = dist_val
                    closest_stop = (stop, element)

        if closest_stop:
            stop, element = closest_stop
            dist = element["distance"]["text"]
            dur = element["duration"]["text"]
            return f"🚌 {stop['name']} ({dist}, {dur} walk)"

        return f"🚌 {results[0]['name']} (⚠️ distance unavailable)"

    except Exception as e:
        print(f"Bus stop error: {e}")
        return "❌ Error occurred during bus stop search."
    
async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice
    user = update.message.from_user.full_name
    chat_id = update.effective_chat.id
    file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as ogg_file:
        await file.download_to_drive(ogg_file.name)
        ogg_path = ogg_file.name

    # Convert to mp3 using ffmpeg
    mp3_path = ogg_path.replace(".ogg", ".mp3")
    try:
        subprocess.run(["ffmpeg", "-y", "-i", ogg_path, "-ar", "16000", "-ac", "1", mp3_path], check=True)
    except Exception as e:
        await update.message.reply_text("❌ Audio conversion failed.")
        print("FFmpeg error:", e)
        return

    # Transcribe with OpenAI Whisper
    transcription = await transcribe_with_whisper(mp3_path)

    if transcription:
        await update.message.reply_text(f"📝 *Transcription from {user}:*\n\n{transcription}", parse_mode="Markdown")

        # Append to listening session
        if chat_id not in listening_sessions:
            listening_sessions[chat_id] = {}
        if user not in listening_sessions[chat_id]:
            listening_sessions[chat_id][user] = []
        listening_sessions[chat_id][user].append(f"[voice] {transcription}")
    else:
        await update.message.reply_text("❌ Failed to transcribe voice message.")

async def transcribe_with_whisper(audio_file_path):
    try:
        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        url = "https://api.openai.com/v1/audio/transcriptions"
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        }

        async with aiohttp.ClientSession() as session:
            with open(audio_file_path, 'rb') as f:
                form = aiohttp.FormData()
                form.add_field("file", f, filename="audio.mp3", content_type="audio/mpeg")
                form.add_field("model", "whisper-1")
                form.add_field("language", "en")  # Force English transcription

                async with session.post(url, headers=headers, data=form) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        return result.get("text")
                    else:
                        print("Whisper API failed:", await resp.text())
                        return None
    except Exception as e:
        print("Whisper transcription error:", e)
        return None



# --- PROCESSING WITH GPT ---

async def process_availability(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    group_data = listening_sessions.get(chat_id, {})
    if not group_data:
        await update.message.reply_text("❌ No messages were collected.")
        return

    today = date.today()
    today_str = today.strftime('%A, %B %d, %Y')

    prompt = (
        f"Today is {today_str}. "
        "Summarize the following group chat into meeting suggestions. "
        "Extract clearly: date (be very careful with relative dates like 'next Friday'), time, place, pax (people), and activity.\n\n"
        "When interpreting dates:\n"
        "- 'tomorrow' means the day after today\n"
        "- 'next Friday' means the next week Friday after this Friday\n"
        "- Be precise with date calculations\n\n"
        "Please summarize the group chat into a Meeting Summary using this exact format:\n\n"
        "📅 Date: <date>\n"
        "🕒 Time: <time>\n"
        "📍 Place: <place>\n"
        "🚇 Nearest MRT: <nearest_mrt_info>\n"
        "🚌 Nearest Bus Stop: <bus_stop_info>\n"
        "👥 Pax: <number_of_people>\n"
        "🎯 Activity: <activity>\n\n"
        "Do not use HTML or Markdown formatting."
    )

    for user, messages in group_data.items():
        prompt += f"{user}:\n"
        for msg in messages:
            prompt += f"- {msg}\n"
        prompt += "\n"

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        summary = response.choices[0].message.content

        # Extract the date from messages and GPT output
        meet_date = extract_meeting_date(group_data, summary, today)

        # Try to extract place line and fetch nearest MRT
        place = None
        for line in summary.split('\n'):
            if "place" in line.lower():
                place = line.split(":")[-1].strip()
                break

        if place:
            mrt_info = await get_nearest_mrt(place)
            google_maps_url = f"https://www.google.com/maps/search/?api=1&query={place.replace(' ', '+')}"

            try:
                geocode_result = gmaps.geocode(place)
                if geocode_result:
                    location = geocode_result[0]["geometry"]["location"]
                    lat, lng = location["lat"], location["lng"]

                    # Send a Telegram location message
                    await update.message.reply_location(latitude=lat, longitude=lng)
            except Exception as e:
                print(f"⚠️ Failed to send location: {e}")


            new_lines = []
            for line in summary.split('\n'):
                if "place" in line.lower():
                    new_line = f"{line.strip()} (Nearest MRT = {mrt_info})"
                    new_lines.append(new_line)
                    new_lines.append(f"🌐 Map: {google_maps_url}")
                else:
                    new_lines.append(line)
            summary = "\n".join(new_lines)

        # Save to DB
        db = SessionLocal()
        meeting = Meeting(chat_id=chat_id, summary=summary, meet_date=meet_date)
        db.add(meeting)
        db.commit()
        sync_link = f"{os.getenv('DOMAIN_BASE_URL')}/login?telegram_id={update.effective_user.id}&meeting_id={meeting.id}"
        final_message = (
            f"📋 Final Summary:\n\n{summary}\n\n"
            f"🔗 [🗓️ Click here to add to Outlook Calendar]({sync_link})"
        )

        await send_final_summary_with_buttons(context, chat_id, final_message, meeting.id)


    except Exception as e:
        error_msg = getattr(e, 'response', str(e))
        await update.message.reply_text(f"❌ Error processing with GPT:\n{error_msg}")

def escape_ics_text(text: str) -> str:
    """
    Escapes characters according to RFC 5545 so that calendar apps can parse it correctly.
    """
    return (
        text.replace('\\', '\\\\')  # Escape backslash
            .replace('\n', '\\n')   # Escape newlines
            .replace(',', '\\,')    # Escape commas
            .replace(';', '\\;')    # Escape semicolons
    )

def create_ics_file(meeting_title: str,
                    description: str,
                    start_time: datetime,
                    duration_minutes: int = 60) -> BytesIO:
    """
    Builds an .ics file in memory and returns it as a BytesIO buffer
    named 'meeting.ics', ready to send as an attachment.
    """
    # 1) Ensure tz-aware
    if start_time.tzinfo is None:
        start_time = pytz.timezone("Asia/Singapore").localize(start_time)

    # 2) Build Calendar + Event
    cal = Calendar()
    ev = IcsEvent()
    ev.name        = meeting_title.strip()
    ev.begin       = start_time
    ev.duration    = timedelta(minutes=duration_minutes)
    ev.description = description
    ev.uid         = f"{uuid.uuid4()}@meetcoord.local"
    # use a timezone-aware UTC now
    ev.created     = datetime.now(timezone.utc)

    cal.events.add(ev)

    # 3) Serialize to bytes and wrap in BytesIO
    ics_bytes = cal.serialize().encode("utf-8")
    buf = BytesIO(ics_bytes)
    buf.name = "meeting.ics"  # Telegram will use this as filename
    buf.seek(0)
    return buf



async def list_meetings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db = SessionLocal()
    meetings = db.query(Meeting).filter_by(chat_id=chat_id).all()

    if not meetings:
        await update.message.reply_text("📭 No saved meetings found.")
        return

    for m in meetings:
        date_str = m.meet_date.strftime('%b %d') if m.meet_date else "?"
        time_str = "?"  # Extract from summary
        place_str = "?"

        for line in m.summary.splitlines():
            if line.strip().startswith("🕒 Time:"):
                time_str = line.split("🕒 Time:")[1].strip()
            elif line.strip().startswith("📍 Place:"):
                place_str = line.split("📍 Place:")[1].strip()

        text = f"🆔 *ID {m.id}* | 📅 *{date_str}* | 🕒 *{time_str}* | 📍 *{place_str}*"
        buttons = [
            [
                InlineKeyboardButton("👁️ View", callback_data=f"view:{m.id}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{m.id}"),
                InlineKeyboardButton("🗑️ Delete", callback_data=f"delete:{m.id}")
            ]
        ]

        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown"
        )

async def perform_edit_start(user_id, chat_id, meeting_id, context):
    db = SessionLocal()
    meeting = db.query(Meeting).filter_by(id=meeting_id, chat_id=chat_id).first()
    if not meeting:
        return False

    editing_sessions[user_id] = {
        'step': 'choose_field',
        'meeting_id': meeting_id
    }

    buttons = [
        [
            InlineKeyboardButton("📅 Date", callback_data=f"editfield:{meeting_id}:date"),
            InlineKeyboardButton("🕒 Time", callback_data=f"editfield:{meeting_id}:time")
        ],
        [
            InlineKeyboardButton("📍 Place", callback_data=f"editfield:{meeting_id}:place"),
            InlineKeyboardButton("👥 Pax", callback_data=f"editfield:{meeting_id}:pax")
        ],
        [
            InlineKeyboardButton("🎯 Activity", callback_data=f"editfield:{meeting_id}:activity")
        ]
    ]

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📋 You're editing *Meeting ID: {meeting_id}*\n\n"
            "Which field do you want to update?",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )

    return True


async def start_edit_meeting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("❓ Please provide the meeting ID.\nExample: /editmeeting 3")
        return

    try:
        meeting_id = int(args[0])
        await perform_edit_start(update.effective_user.id, update.effective_chat.id, meeting_id, context)
    except ValueError:
        await update.message.reply_text("⚠️ Invalid meeting ID.")

async def perform_meeting_deletion(chat_id, meeting_id, context):
    db = SessionLocal()
    meeting = db.query(Meeting).filter_by(id=meeting_id, chat_id=chat_id).first()
    if meeting:
        db.delete(meeting)
        db.commit()
        return True
    return False


async def delete_meeting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("❌ Usage: /deletemeeting <meeting_id>")
        return

    try:
        meeting_id = int(args[0])
        success = await perform_meeting_deletion(update.effective_chat.id, meeting_id, context)
        if success:
            await update.message.reply_text("🗑️ Meeting deleted.")
        else:
            await update.message.reply_text("❌ Meeting not found.")
    except ValueError:
        await update.message.reply_text("⚠️ Invalid ID. Please provide a number.")


async def clear_meetings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db = SessionLocal()
    deleted_count = db.query(Meeting).filter(Meeting.chat_id == chat_id).delete()
    db.commit()
    db.close()

    if deleted_count > 0:
        await context.bot.send_message(chat_id=chat_id, text=f"🧹 Cleared {deleted_count} meeting(s) from *this chat*.", parse_mode="Markdown")
    else:
        await context.bot.send_message(chat_id=chat_id, text="ℹ️ No meetings found to delete in this chat.")

# --- APP SETUP ---
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands for control
    app.add_handler(ChatMemberHandler(welcome_on_add, chat_member_types=["member"]))
    app.add_handler(CommandHandler("startlistening", start_listening))
    app.add_handler(CommandHandler("stoplistening", stop_listening))
    app.add_handler(CommandHandler("listmeetings", list_meetings))
    app.add_handler(CommandHandler("deletemeeting", delete_meeting))
    app.add_handler(CommandHandler("editmeeting", start_edit_meeting))
    app.add_handler(CommandHandler("clearmeetings", clear_meetings)) 
    app.add_handler(CallbackQueryHandler(meeting_button_handler))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))

    # Passive message tracking
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_group_message))

    scheduler.start()
    await app.initialize()

    print("✅ Bot is running and ready for group chat...")

    # Start polling
    await app.start()
    await app.updater.start_polling()

    # Keep the application running
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("🛑 Shutting down...")
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
