import os
from db import SessionLocal, Meeting
from dotenv import load_dotenv
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
from telegram import Update,InputFile,InlineKeyboardButton, InlineKeyboardMarkup
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
    new_status = update.my_chat_member.new_chat_member.status
    if new_status != "member":
        return

    text = (
        "👋 *Hello and welcome! I'm @coordinator_meetbot, your AI-powered meeting assistant.*\n\n"
        "📌 *What I do for your group chat:*\n"
        " • Auto-listen to your planning (when you ask)\n"
        " • Summarize 📅 Date, 🕒 Time, 📍 Place, 👥 Attendees & 🎯 Activity\n"
        " • Fetch nearest 🚇 MRT & 🚌 bus stops\n"
        " • Generate an Outlook 📆 calendar link & .ics file\n"
        " • Send ⏰ reminders (default 12 h before)\n\n"
        "▶️ *Getting started:*\n"
        "1️⃣ `/startlistening` — I’ll capture your chat\n"
        "2️⃣ Chat freely about date/time/place/etc.\n"
        "3️⃣ `/stoplistening` — I’ll post a neat summary\n\n"
        "🔧 *Quick commands:* `/listmeetings`, `/editmeeting <id>`, `/deletemeeting <id>`, `/cancelreminder <id>`\n\n"
        "🔒 I only record when you ask. Let’s make planning smooth and stress-free! 🗓️✨"
    )

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=escape_markdown_v2(text),
        parse_mode="MarkdownV2"
    )



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

    # Safely grab the incoming text (or empty string if None)
    user_text = update.message.text or ""

    # --- Custom reminder input handler ---
    if "awaiting_custom_reminder_for" in context.user_data:
        meeting_id = context.user_data.pop("awaiting_custom_reminder_for")
        duration = parse_custom_duration(user_text)
        if duration is None:
            await update.message.reply_text("❌ Invalid format. Try something like `90m` or `1h30m`.")
            return

        class DummyQuery:
            def __init__(self, message, chat_id):
                self.message = message
                self.chat_id = chat_id
                self.from_user = message.from_user

            async def answer(self, *args, **kwargs):
                pass

            async def edit_message_text(self, text, parse_mode=None, reply_markup=None):
                await context.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup
                )

        dummy_query = DummyQuery(update.message, chat_id)
        await schedule_reminder(dummy_query, context, meeting_id, duration)
        return

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

        # STEP 1: choose which field
        if step == 'choose_field':
            choice = user_text.lower()
            if choice not in ['date', 'time', 'place', 'pax', 'activity']:
                await update.message.reply_text("❌ Invalid field. Please choose a valid option below.")
                await perform_edit_start(user_id, chat_id, session['meeting_id'], context)
                return

            session['field'] = choice
            session['step'] = 'enter_value'
            await update.message.reply_text(
                f"✏️ Please enter the new value for *{choice}*:", parse_mode="Markdown"
            )
            return

        # STEP 2: user has entered the new value
        elif step == 'enter_value':
            field = session['field']
            lines = meeting.summary.split('\n')

            # If we're editing the place, drop any existing map/transit lines first
            if field == 'place':
                lines = [
                    l for l in lines
                    if not any(l.startswith(prefix) for prefix in (
                        "🌐 Map:", "🚇 Nearest MRT:", "🚌 Nearest Bus Stop:"
                    ))
                ]

            updated_lines = []
            for line in lines:
                # split label and content
                if ':' in line:
                    label, rest = line.split(':', 1)
                    # normalize label by removing emoji/punctuation
                    key = re.sub(r'[^\w ]', '', label).strip().lower()
                else:
                    updated_lines.append(line)
                    continue

                # non-place fields: match on normalized key
                if field != 'place' and key == field:
                    updated_lines.append(f"{label}: {user_text}")

                # place field: rebuild full block
                elif field == 'place' and key == 'place':
                    updated_lines.append(f"📍 Place: {user_text}")
                    map_url = f"https://www.google.com/maps/search/?api=1&query={quote(user_text)}"
                    updated_lines.append(f"🌐 Map: {map_url}")
                    mrt = await get_nearest_mrt(user_text)
                    updated_lines.append(f"🚇 Nearest MRT: {mrt}")
                    bus = await find_nearest_bus_stop(user_text)
                    updated_lines.append(f"🚌 Nearest Bus Stop: {bus}")

                # everything else stays the same
                else:
                    updated_lines.append(line)

            # validate date/time if needed
            if field == 'date':
                parsed_date = dateparser.parse(user_text)
                if not parsed_date or parsed_date.date() < date.today():
                    await context.bot.send_message(chat_id=chat_id,
                        text="❌ Invalid date. Please enter a future date like `next Friday`.")
                    db.close()
                    return
                meeting.meet_date = parsed_date.date()

            elif field == 'time':
                parsed_time = dateparser.parse(user_text)
                if not parsed_time:
                    await context.bot.send_message(chat_id=chat_id,
                        text="❌ Invalid time. Please enter like `7pm` or `19:30`.")
                    db.close()
                    return

            # commit changes
            meeting.summary = '\n'.join(updated_lines)
            db.commit()
            meeting_id = meeting.id
            summary_text = meeting.summary
            db.close()
            del editing_sessions[user_id]

            # rebuild the final summary with Outlook link
            domain = os.getenv('DOMAIN_BASE_URL')
            sync_link = f"{domain}/login?telegram_id={update.effective_user.id}&meeting_id={meeting_id}"
            final_message = (
                f"📋 Final Summary:\n\n{summary_text}\n\n"
                f"🔗 [🗓️ Click here to add to Outlook Calendar]({sync_link})"
            )

            # re-edit the original message (or send a new one)
            msg_id = context.chat_data.get(f"meeting_msg_{meeting_id}")
            buttons = [
                [InlineKeyboardButton("✏️ Edit Meeting", callback_data=f'edit:{meeting_id}')],
                [InlineKeyboardButton("🗑️ Delete Meeting", callback_data=f'delete_prompt:{meeting_id}')],
                [InlineKeyboardButton("⏰ Set Reminder", callback_data=f'setreminder:{meeting_id}')]
            ]

            if msg_id:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=final_message,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=final_message,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
            return

    # --- Normal listening mode ---
    if chat_id in listening_sessions:
        user = update.message.from_user.full_name
        listening_sessions[chat_id].setdefault(user, []).append(user_text)

# --- DATE EXTRACTION ---
def extract_meeting_date(original_messages, gpt_summary, current_date=None):
    """
    1) Look for “📅 Date:” in the GPT summary and parse it.
    2) If that fails, scan the raw chat for explicit date tokens.
    Clamps same-month/day parses back to the current year.
    """
    if current_date is None:
        current_date = date.today()

    # 1) Try GPT summary first
    for line in gpt_summary.splitlines():
        if line.strip().startswith("📅 Date:"):
            date_text = line.split("📅 Date:")[1].strip()
            parsed = dateparser.parse(
                date_text,
                settings={
                    'RELATIVE_BASE': datetime.combine(current_date, datetime.min.time()),
                    'PREFER_DATES_FROM': 'future'
                }
            )
            if parsed and parsed.date() >= current_date:
                return parsed.date()
            # if GPT gave a Date line but it didn't parse to a valid future date, stop here
            break

    # 2) Fallback: scan raw messages
    all_messages = []
    for msgs in original_messages.values():
        all_messages.extend(msgs)

    date_patterns = [
        r'\b(tomorrow|tmr)\b',
        r'\b(today|tdy)\b',
        r'\b(next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b',
        r'\b(this\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b',
        r'\b(\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)\b',
        r'\b(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b',
        r'\b(\d{1,2}-\d{1,2}(?:-\d{2,4})?)\b'
    ]

    def clamp_year(dt: datetime) -> datetime:
        if dt.month == current_date.month and dt.day == current_date.day:
            return dt.replace(year=current_date.year)
        return dt

    for message in all_messages:
        msg_low = message.lower()
        for pat in date_patterns:
            for match in re.findall(pat, msg_low, re.IGNORECASE):
                parsed = dateparser.parse(
                    match,
                    settings={
                        'RELATIVE_BASE': datetime.combine(current_date, datetime.min.time()),
                        'PREFER_DATES_FROM': 'future'
                    }
                )
                if parsed:
                    parsed = clamp_year(parsed)
                    if parsed.date() >= current_date:
                        return parsed.date()

    # nothing valid found
    return None


async def send_final_summary_with_buttons(context, chat_id, summary_text, meeting_id: int):
    # Fetch meeting & parse .ics
    db      = SessionLocal()
    meeting = db.query(Meeting).filter_by(id=meeting_id).first()
    time_str = extract_time_from_summary(meeting.summary)
    meeting_dt = parse_meeting_datetime(meeting.meet_date, time_str)

    ics_buf = None
    if meeting_dt:
        ics_buf = create_ics_file(
            meeting_title="Group Meeting",
            description=meeting.summary,
            start_time=meeting_dt
        )

    # Build the first two buttons
    buttons = [
        [InlineKeyboardButton("✏️ Edit Meeting",   callback_data=f'edit:{meeting_id}')],
        [InlineKeyboardButton("🗑️ Delete Meeting", callback_data=f'delete_prompt:{meeting_id}')]
    ]

    # Toggle reminder button
    job_id  = f"reminder_{meeting_id}"
    if scheduler.get_job(job_id):
        buttons.append([InlineKeyboardButton("❌ Cancel Reminder", callback_data=f"cancel_reminder:{meeting_id}")])
    else:
        buttons.append([InlineKeyboardButton("⏰ Set Reminder",    callback_data=f"setreminder:{meeting_id}")])

    # Send the summary + buttons
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=summary_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    # Store for later edits
    context.chat_data[f"meeting_msg_{meeting_id}"] = msg.message_id

    # Finally, send the .ics if we built one
    if ics_buf:
        await context.bot.send_document(
            chat_id=chat_id,
            document=InputFile(ics_buf, filename=ics_buf.name),
            caption="📅 Tap to add this meeting to your calendar!"
        )
    db.close()


async def meeting_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    try:
        # --- STEP 1: prompt for delete ---
        if data.startswith("delete_prompt:"):
            meeting_id = int(data.split(":", 1)[1])
            # guard: only prompt if meeting still exists
            db = SessionLocal()
            exists = db.query(Meeting.id).filter_by(id=meeting_id).first()
            db.close()
            if not exists:
                return await query.edit_message_text("❌ Meeting not found.")
            kb = [
                InlineKeyboardButton("✅ Yes, delete", callback_data=f"confirm_delete:{meeting_id}"),
                InlineKeyboardButton("❌ Cancel",       callback_data=f"cancel_delete:{meeting_id}")
            ]
            return await query.edit_message_text(
                text="⚠️ Are you sure you want to delete this meeting?",
                reply_markup=InlineKeyboardMarkup([kb])
            )

        # --- STEP 2: confirm deletion ---
        elif data.startswith("confirm_delete:"):
            meeting_id = int(data.split(":", 1)[1])
            success = await perform_meeting_deletion(update.effective_chat.id, meeting_id, context)
            if success:
                return await query.edit_message_text("✅ Meeting deleted.")
            else:
                return await query.edit_message_text("❌ Meeting not found.")

        # --- STEP 3: cancel deletion ---
        elif data.startswith("cancel_delete:"):
            meeting_id = int(data.split(":", 1)[1])
            db = SessionLocal()
            meeting = db.query(Meeting).filter_by(id=meeting_id).first()
            db.close()
            if not meeting:
                return await query.edit_message_text("❌ Meeting not found.")
            buttons = [
                [InlineKeyboardButton("✏️ Edit Meeting",   callback_data=f'edit:{meeting_id}')],
                [InlineKeyboardButton("🗑️ Delete Meeting", callback_data=f'delete_prompt:{meeting_id}')],
                [InlineKeyboardButton("⏰ Set Reminder",    callback_data=f'setreminder:{meeting_id}')]
            ]
            return await query.edit_message_text(
                text=meeting.summary,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

        # --- Edit meeting flow ---
        elif data.startswith("edit:"):
            meeting_id = int(data.split(":", 1)[1])
            return await perform_edit_start(
                update.effective_user.id, 
                update.effective_chat.id, 
                meeting_id, 
                context
            )

        elif data.startswith("editfield:"):
            parts = data.split(":")
            meeting_id, field = int(parts[1]), parts[2]
            # re-use same session safety
            editing_sessions[update.effective_user.id] = {
                'step': 'enter_value',
                'meeting_id': meeting_id,
                'field': field
            }
            field_name = field.capitalize()
            return await query.edit_message_text(
                f"✏️ Please enter the new value for *{field_name}*:",
                parse_mode="Markdown"
            )

        # --- View summary ---
        elif data.startswith("view:"):
            meeting_id = int(data.split(":", 1)[1])
            db = SessionLocal()
            meeting = db.query(Meeting).filter_by(id=meeting_id).first()
            db.close()
            if not meeting:
                await query.answer("❌ Meeting not found.", show_alert=True)
                return

            # Build full summary with Outlook link
            summary = meeting.summary
            sync_link = (
                f"{os.getenv('DOMAIN_BASE_URL')}"
                f"/login?telegram_id={query.from_user.id}&meeting_id={meeting_id}"
            )
            full_text = (
                f"{summary}\n\n"
                f"🔗 [🗓️ Add to Outlook Calendar]({sync_link})"
            )

            # Rebuild the action buttons
            buttons = [
                [InlineKeyboardButton("✏️ Edit Meeting",   callback_data=f"edit:{meeting_id}")],
                [InlineKeyboardButton("🗑️ Delete Meeting", callback_data=f"delete_prompt:{meeting_id}")],
                [InlineKeyboardButton("⏰ Set Reminder",    callback_data=f"setreminder:{meeting_id}")]
            ]
            markup = InlineKeyboardMarkup(buttons)

            await query.edit_message_text(
                text=full_text,
                parse_mode="Markdown",
                reply_markup=markup
            )

        # --- Reminder controls ---
        elif data.startswith("setreminder:"):
            meeting_id = int(data.split(":", 1)[1])
            return await offer_reminder_presets(query, context, meeting_id)

        elif data.startswith("cancel_reminder:"):
            meeting_id = int(data.split(":", 1)[1])
            job_id = f"reminder_{meeting_id}"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
                # refresh buttons
                db = SessionLocal()
                meeting = db.query(Meeting).filter_by(id=meeting_id).first()
                db.close()
                buttons = [
                    [InlineKeyboardButton("✏️ Edit Meeting",   callback_data=f'edit:{meeting_id}')],
                    [InlineKeyboardButton("🗑️ Delete Meeting", callback_data=f'delete_prompt:{meeting_id}')],
                    [InlineKeyboardButton("⏰ Set Reminder",    callback_data=f"setreminder:{meeting_id}")]
                ]
                return await query.edit_message_text(
                    text=f"📋 Final Summary:\n\n{meeting.summary}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
            else:
                return await query.answer("❌ No active reminder to cancel.", show_alert=True)

        elif data.startswith("remind:"):
            _, mid, mins = data.split(":")
            return await schedule_reminder(query, context, int(mid), int(mins))

        elif data.startswith("remindcustom:"):
            meeting_id = int(data.split(":", 1)[1])
            context.user_data["awaiting_custom_reminder_for"] = meeting_id
            return await query.edit_message_text(
                "✏️ Please enter a custom reminder interval (e.g. `90m` or `2h30m`):",
                parse_mode="Markdown"
            )

        else:
            # catch any unknown or stale callback_data
            return await query.answer("⚠️ This action is no longer available.", show_alert=True)

    except Exception as e:
        print(f"Error in meeting_button_handler: {e}")
        # inform user without exposing internals
        await query.answer("❌ An error occurred. Please try again.", show_alert=True)


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
        await query.answer("❌ Missing meeting date.", show_alert=True)
        db.close()
        return

    time_str = extract_time_from_summary(meeting.summary)
    meeting_dt = parse_meeting_datetime(meeting.meet_date, time_str)
    if not meeting_dt:
        await query.answer("❌ Can't parse meeting time.", show_alert=True)
        db.close()
        return

    remind_dt = meeting_dt - timedelta(minutes=minutes_before)
    if remind_dt < datetime.now(pytz.timezone("Asia/Singapore")):
        await query.answer("❌ That time is already past.", show_alert=True)
        db.close()
        return

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

    # build sync link
    domain = os.getenv('DOMAIN_BASE_URL')
    user    = getattr(query, 'from_user', query.message.from_user)
    sync_link = f"{domain}/login?telegram_id={user.id}&meeting_id={meeting_id}"

    # build label
    label = f"{minutes_before//60}h" + (f"{minutes_before%60}m" if minutes_before%60 else "")

    # include both the Outlook link AND the reminder line
    final_text = (
        f"📋 Final Summary:\n\n"
        f"{meeting.summary}\n\n"
        f"🔗 [🗓️ Click here to add to Outlook Calendar]({sync_link})\n\n"
        f"⏰ Reminder set: {label} before meeting"
    )

    buttons = [
        [InlineKeyboardButton("✏️ Edit Meeting",   callback_data=f'edit:{meeting_id}')],
        [InlineKeyboardButton("🗑️ Delete Meeting", callback_data=f'delete_prompt:{meeting_id}')],
        [InlineKeyboardButton("❌ Cancel Reminder", callback_data=f"cancel_reminder:{meeting_id}")]
    ]

    await query.edit_message_text(
        text=final_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

    db.close()

# --- the actual reminder action ---
async def send_reminder(bot, chat_id: int, meeting_id: int, mins_before: int):
    db = SessionLocal()
    meeting = db.query(Meeting).filter_by(id=meeting_id).first()
    if not meeting:
        return
    text = f"⏰ Reminder: your meeting is in {mins_before} minutes!\n\n{meeting.summary}"
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")

def parse_custom_duration(text: str) -> int:
    """
    Parse a duration string like '90m', '1h', '1h30m' into total minutes.
    Returns None if invalid.
    """
    pattern = r"(?:(\d+)h)?(?:(\d+)m)?"
    match = re.fullmatch(pattern, text.strip().lower())
    if not match:
        return None

    hours = int(match.group(1)) if match.group(1) else 0
    minutes = int(match.group(2)) if match.group(2) else 0
    total_minutes = hours * 60 + minutes
    return total_minutes if total_minutes > 0 else None


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
        time_str  = extract_time_from_summary(summary)
        meeting_dt = parse_meeting_datetime(meet_date, time_str)
        now = datetime.now(pytz.timezone("Asia/Singapore"))
        if meeting_dt and meeting_dt < now:
            await update.message.reply_text(
                "❌ The proposed meeting time "
                f"({meet_date} {time_str}) has already passed—"
                "please agree a future date/time and try again."
            )
            return

        # Try to extract place line and fetch nearest MRT
        place = None
        for line in summary.split('\n'):
            if "place" in line.lower():
                place = line.split(":")[-1].strip()
                break

        if place:
            mrt_info = await get_nearest_mrt(place)
            google_maps_url = f"https://www.google.com/maps/search/?api=1&query={place.replace(' ', '+')}"
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
    db.close()

    if not meetings:
        await context.bot.send_message(
            chat_id=chat_id,
            text="📭 No saved meetings found."
        )
        return

    for m in meetings:
        # Build a brief header for each meeting
        date_str = m.meet_date.strftime('%b %d') if m.meet_date else "?"
        time_str = "?"
        place_str = "?"

        for line in m.summary.splitlines():
            if line.startswith("🕒 Time:"):
                time_str = line.split("🕒 Time:")[1].strip()
            elif line.startswith("📍 Place:"):
                place_str = line.split("📍 Place:")[1].strip()

        header = f"🆔 *ID {m.id}* | 📅 *{date_str}* | 🕒 *{time_str}* | 📍 *{place_str}*"

        # Four buttons: View, Edit, Delete, Set Reminder
        buttons = [[
            InlineKeyboardButton("👁️ View",         callback_data=f"view:{m.id}"),
            InlineKeyboardButton("✏️ Edit",         callback_data=f"edit:{m.id}"),
            InlineKeyboardButton("🗑️ Delete",       callback_data=f"delete_prompt:{m.id}"),
            InlineKeyboardButton("⏰ Reminder",      callback_data=f"setreminder:{m.id}")
        ]]
        markup = InlineKeyboardMarkup(buttons)

        await context.bot.send_message(
            chat_id=chat_id,
            text=header,
            parse_mode="Markdown",
            reply_markup=markup
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
