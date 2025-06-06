import os
from db import SessionLocal, Meeting
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes, ChatMemberHandler
from openai import OpenAI

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

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
                "👋 Hello everyone! I'm **@coordinator_meetbot**, your group's friendly meeting scheduler bot.\n\n"
                "📌 *What I do:*\n"
                "I help you summarize group discussions into clear meetup plans — including time, place, people, and activity.\n\n"
                "▶️ *How to use me:*\n"
                "1. Type `/startlistening` to begin tracking everyone's messages.\n"
                "2. Chat as usual about when and where to meet.\n"
                "3. Type `/stoplistening` to stop and get a smart summary powered by AI.\n\n"
                "🧠 *Bonus commands:*\n"
                "`/listmeetings` – See all past meeting summaries\n"
                "`/deletemeeting <id>` – Remove an old summary\n\n"
                "🔒 I only listen when you tell me to. Let's make planning simple! 🎯",
                parse_mode="Markdown"
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


# --- PROCESSING WITH GPT ---

async def process_availability(update: Update, chat_id: int):
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

        # Save to DB
        db = SessionLocal()
        meeting = Meeting(chat_id=chat_id, summary=summary)
        db.add(meeting)
        db.commit()

        await update.message.reply_text("📋 *Final Summary *:\n\n" + summary, parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ Error processing with GPT: {str(e)}")

async def list_meetings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db = SessionLocal()
    meetings = db.query(Meeting).filter_by(chat_id=chat_id).all()

    if not meetings:
        await update.message.reply_text("📭 No saved meetings found.")
        return

    reply = "🗂️ *Saved Meetings:*\n\n"
    for m in meetings:
        reply += f"🆔 {m.id}\n🕒 {m.created_at.strftime('%Y-%m-%d %H:%M')}\n📋 {m.summary[:100]}...\n\n"

    await update.message.reply_text(reply, parse_mode="Markdown")

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


# Passive message tracking
app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_group_message))

print("✅ Bot is running and ready for group chat...")
app.run_polling()
