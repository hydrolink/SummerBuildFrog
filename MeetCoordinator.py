import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes, ChatMemberHandler
from openai import OpenAI
import googlemaps
import re



# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
gmaps = googlemaps.Client(key=GOOGLE_MAPS_API_KEY)

# States per group
listening_sessions = {}  # {chat_id: {user: [messages]}}

# --- COMMANDS 

async def welcome_on_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_status = update.my_chat_member.new_chat_member.status
        if new_status == "member":  # or "administrator" if added as admin
            chat = update.effective_chat
            await context.bot.send_message(
                chat.id,
                "👋 Hello everyone! I'm @coordinator_meetbot.\n\n"
                "I'm here to help summarize your group's availability for meetups.\n\n"
                "To get started:\n"
                "▶️ Use /startlistening — I’ll start recording messages.\n"
                "⏹️ Use /stoplistening — I’ll summarize everything.\n\n"
                "🔒 I only listen when asked. Let’s get your meetups planned smoothly!"
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
    if chat_id not in listening_sessions:
        return  # Only record if listening is active

    user = update.message.from_user.full_name
    message = update.message.text

    if user not in listening_sessions[chat_id]:
        listening_sessions[chat_id][user] = []

    listening_sessions[chat_id][user].append(message)


def escape_markdown_v2(text: str) -> str:
    escape_chars = r"\_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)


# --- PROCESSING WITH GPT ---

async def process_availability(update: Update, chat_id: int):
    """Use GPT-4o to summarize availability from collected messages"""
    group_data = listening_sessions.get(chat_id, {})
    if not group_data:
        await update.message.reply_text("❌ No messages were collected.")
        return

    prompt = (
        "Summarize the following group chat into meeting suggestions. "
        "Extract clearly: time, place, pax (people), and activity.\n\n"
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

        # Optional: extract meetup place from summary using simple logic
        # You can make this smarter with regex or structured GPT output
        place = None
        for line in summary.split('\n'):
            if "place" in line.lower():
                place = line.split(":")[-1].strip()
                break

        if place:
            mrt_info = await get_nearest_mrt(place)

            new_lines = []
            for line in summary.split('\n'):
                if "place" in line.lower():
                    new_line = f"{line.strip()} (Nearest MRT = {mrt_info})"
                    new_lines.append(new_line)
                else:
                    new_lines.append(line)  # <- KEEP all other lines!
            summary = "\n".join(new_lines)


        escaped_summary = escape_markdown_v2("📋 *Final Summary*:\n\n" + summary)
        await update.message.reply_text(escaped_summary, parse_mode="MarkdownV2")


    except Exception as e:
        await update.message.reply_text(f"❌ Error processing with GPT: {str(e)}")


#--- Processing with Google Maps---
async def get_nearest_mrt(meetup_location: str):
    try:
        # Step 1: Get coordinates of the meetup location
        geocode_result = gmaps.geocode(meetup_location)
        if not geocode_result:
            return "❌ Couldn't find coordinates for the location."

        location = geocode_result[0]['geometry']['location']
        lat, lng = location['lat'], location['lng']
        origin = f"{lat},{lng}"

        # Step 2: Search for transit stations nearby
        places_result = gmaps.places_nearby(
            location=(lat, lng),
            radius=2000,
            type='transit_station'
        )

        # Step 3: Filter for MRT stations by name
        stations = [
            place for place in places_result.get("results", [])
            if "MRT" in place["name"]
        ]

        if not stations:
            # Fallback: do a text search
            text_result = gmaps.places(query="MRT station near " + meetup_location)
            stations = [
                place for place in text_result.get("results", [])
                if "MRT" in place["name"]
            ]

        if not stations:
            return "❌ No MRT station found nearby."

        # Take the closest matching MRT
        result = stations[0]
        mrt_name = result["name"]
        mrt_location = result["geometry"]["location"]
        destination = f"{mrt_location['lat']},{mrt_location['lng']}"

        # Step 4: Use Distance Matrix API for walk info
        distance_result = gmaps.distance_matrix(
            origins=[origin],
            destinations=[destination],
            mode="walking"
        )

        element = distance_result["rows"][0]["elements"][0]
        if element["status"] == "OK":
            distance_meters = element["distance"]["value"]
            if distance_meters > 50000:  # 50 km is far for an MRT, this is clearly a bug
                return f"{mrt_name} (⚠️ distance seems invalid)"
            distance_text = element["distance"]["text"]
            duration_text = element["duration"]["text"]
            return f"{mrt_name} ({distance_text}, {duration_text} walk)"
        else:
            return f"{mrt_name} (⚠️ distance unavailable)"

    
    except Exception as e:
        return f"⚠️ Error finding MRT: {str(e)}"



# --- APP SETUP ---

app = ApplicationBuilder().token(BOT_TOKEN).build()

# Commands for control
app.add_handler(ChatMemberHandler(welcome_on_add, chat_member_types=["member"]))
app.add_handler(CommandHandler("startlistening", start_listening))
app.add_handler(CommandHandler("stoplistening", stop_listening))

# Passive message tracking
app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_group_message))

print("✅ Bot is running and ready for group chat...")
app.run_polling()
