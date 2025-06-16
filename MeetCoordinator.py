import os
from db import SessionLocal, Meeting
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes, ChatMemberHandler
from openai import OpenAI
from datetime import datetime, date
import re
import dateparser
import googlemaps
from urllib.parse import quote

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

def escape_markdown_v2(text: str) -> str:
    escape_chars = r"\_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)

# --- COMMANDS 

async def welcome_on_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_status = update.my_chat_member.new_chat_member.status
        if new_status == "member":  # or "administrator" if added as admin
            chat = update.effective_chat
            await context.bot.send_message(
            chat.id,
            escape_markdown_v2(
                "👋 *Welcome to your group’s personal meeting assistant\\!* I'm \\@coordinator\\_meetbot — your AI scheduler\\. 🧠🤖\n\n"
                "📌 *Here’s how I can help:*\n"
                "I listen to your group chat and generate smart summaries for your meetups\\. This includes:\n"
                "• 📅 *Date*\n"
                "• 🕒 *Time*\n"
                "• 📍 *Place* with nearest MRT info\n"
                "• 👥 *Attendees*\n"
                "• 🎯 *Activity*\n\n"
                "▶️ *To get started:*\n"
                "1\\. Type `/startlistening` — I’ll start collecting messages\\.\n"
                "2\\. Chat naturally about your meeting plans\\.\n"
                "3\\. Type `/stoplistening` — I’ll process everything and summarize\\.\n\n"
                "🧠 *Other useful commands:*\n"
                "`/listmeetings` – View all previous summaries\n"
                "`/deletemeeting <id>` – Delete a saved summary\n\n"
                "🔒 I *only listen* when you explicitly tell me to\\.\n"
                "Let’s make planning smooth and stress\\-free\\. 🗓️✨"
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
    await process_availability(update, chat_id)
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

        if step == 'choose_field':
            if text.lower() not in ['date', 'time', 'place', 'pax', 'activity']:
                await update.message.reply_text("❌ Invalid field. Please choose from `date`, `time`, `place`, `pax`, or `activity`.")
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

            meeting.summary = '\n'.join(updated_lines)
            db.commit()
            del editing_sessions[user_id]

            await update.message.reply_text("✅ Meeting updated successfully!")
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

# --- GOOGLE MAPS ---
async def get_nearest_mrt(place):
    try:
        geo = gmaps.geocode(place)
        if not geo:
            return "❌ Could not find location."
        latlng = geo[0]["geometry"]["location"]
        lat, lng = latlng["lat"], latlng["lng"]
        results = gmaps.places_nearby(location=(lat, lng), radius=2000, type='transit_station')

        stations = [r for r in results.get("results", []) if "MRT" in r["name"]]
        if not stations:
            results = gmaps.places(query=f"MRT station near {place}")
            stations = [r for r in results.get("results", []) if "MRT" in r["name"]]
        if not stations:
            return "❌ No MRT station nearby."

        station = stations[0]
        mrt_name = station["name"]
        dest = station["geometry"]["location"]
        distance_data = gmaps.distance_matrix(
            [f"{lat},{lng}"],
            [f"{dest['lat']},{dest['lng']}"],
            mode="walking"
        )
        element = distance_data["rows"][0]["elements"][0]
        if element["status"] == "OK":
            dist = element["distance"]["text"]
            dur = element["duration"]["text"]
            return f"{mrt_name} ({dist}, {dur} walk)"
        return f"{mrt_name} (⚠️ distance unavailable)"
    except Exception as e:
        return f"⚠️ MRT error: {str(e)}"

# --- PROCESSING WITH GPT ---

async def process_availability(update: Update, chat_id: int):
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
        "- 'next Friday' means the upcoming Friday after today\n"
        "- Be precise with date calculations\n\n"
        "Please summarize the group chat into a Meeting Summary using this exact format:\n\n"
        "📅 Date: <date>\n"
        "🕒 Time: <time>\n"
        "📍 Place: <place>\n"
        "🚇 Nearest MRT: <nearest_mrt_info>\n"
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
        if meet_date:
            summary += f"\n\n✅ **Extracted Date: {meet_date.strftime('%A, %B %d, %Y')}**"

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

        await update.message.reply_text(final_message, parse_mode="Markdown", disable_web_page_preview=True)


    except Exception as e:
        error_msg = getattr(e, 'response', str(e))
        await update.message.reply_text(f"❌ Error processing with GPT:\n{error_msg}")



async def list_meetings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db = SessionLocal()
    meetings = db.query(Meeting).filter_by(chat_id=chat_id).all()

    if not meetings:
        await update.message.reply_text("📭 No saved meetings found.")
        return

    reply = "🗂️ *Saved Meetings:*\n\n"

    for m in meetings:
        date_str = m.meet_date.strftime('%A, %B %d, %Y') if m.meet_date else "Unknown"
        created_str = m.created_at.strftime('%Y-%m-%d %H:%M')

        # Convert escaped \n into actual line breaks
        clean_summary = m.summary.replace("\\n", "\n")

        # Extract fields from cleaned summary
        details = {
            "📅 Date": "Not found",
            "🕒 Time": "Not found",
            "📍 Place": "Not found",
            "🚇 Nearest MRT": "Not found",
            "👥 Pax": "Not found",
            "🎯 Activity": "Not found"
        }

        for line in clean_summary.splitlines():
            for key in details:
                if line.strip().startswith(key):
                    details[key] = line[len(key)+1:].strip()

        reply += (
            f"🆔 *ID:* `{m.id}`\n"
            f"📌 *Created:* {created_str}\n"
            f"📅 *Date:* {details['📅 Date']}\n"
            f"🕒 *Time:* {details['🕒 Time']}\n"
            f"📍 *Place:* {details['📍 Place']}\n"
            f"🚇 *MRT:* {details['🚇 Nearest MRT']}\n"
            f"👥 *Pax:* {details['👥 Pax']}\n"
            f"🎯 *Activity:* {details['🎯 Activity']}\n"
            "───────────────\n"
        )

    await update.message.reply_text(reply, parse_mode="Markdown")




async def start_edit_meeting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("❓ Please provide the meeting ID.\nExample: /editmeeting 3")
        return

    try:
        meeting_id = int(args[0])
        db = SessionLocal()
        meeting = db.query(Meeting).filter_by(id=meeting_id).first()
        if not meeting:
            await update.message.reply_text("❌ Meeting not found.")
            return

        user_id = update.effective_user.id
        editing_sessions[user_id] = {
            'step': 'choose_field',
            'meeting_id': meeting_id
        }

        await update.message.reply_text(
            f"📋 You're editing Meeting ID: {meeting_id}\n\n"
            "Which field do you want to update?\n"
            "`date`, `time`, `place`, `pax`, or `activity`",
            parse_mode="Markdown"
        )

    except ValueError:
        await update.message.reply_text("⚠️ Invalid meeting ID.")



async def delete_meeting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("❌ Usage: /deletemeeting <meeting_id>")
        return

    try:
        meeting_id = int(args[0])
        db = SessionLocal()
        meeting = db.query(Meeting).filter_by(id=meeting_id).first()

        if meeting:
            db.delete(meeting)
            db.commit()
            await update.message.reply_text("🗑️ Meeting deleted.")
        else:
            await update.message.reply_text("❌ Meeting not found.")
    except ValueError:
        await update.message.reply_text("⚠️ Invalid ID. Please provide a number.")

# --- APP SETUP ---

app = ApplicationBuilder().token(BOT_TOKEN).build()

# Commands for control
app.add_handler(ChatMemberHandler(welcome_on_add, chat_member_types=["member"]))
app.add_handler(CommandHandler("startlistening", start_listening))
app.add_handler(CommandHandler("stoplistening", stop_listening))
app.add_handler(CommandHandler("listmeetings", list_meetings))
app.add_handler(CommandHandler("deletemeeting", delete_meeting))
app.add_handler(CommandHandler("editmeeting", start_edit_meeting))

# Passive message tracking
app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_group_message))

print("✅ Bot is running and ready for group chat...")
app.run_polling()
