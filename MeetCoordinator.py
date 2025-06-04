import os
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

# --- COMMANDS ---

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
        await update.message.reply_text("📋 *Final Summary *:\n\n" + summary, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error processing with GPT: {str(e)}")


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
