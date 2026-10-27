import os
import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait

from database import FileDB
from helper_func import encode_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("file-share-bot")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_URI = os.environ["MONGODB_URI"]
DB_NAME = os.getenv("DATABASE_NAME", "file_share_bot")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

app = Client(
    "file_share_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=32,
    sleep_threshold=60,
)
db = FileDB(DB_URI, DB_NAME)

@app.on_message(filters.command("start"))
async def start_handler(_, message: Message):
    if len(message.command) > 1:
        token = message.command[1]
        doc = await db.get_file(token)
        if not doc:
            await message.reply_text("❌ Invalid or expired file link.")
            return
        try:
            await message.reply_cached_media(
                doc["file_id"],
                protect_content=doc.get("protect", False),
            )
        except FloodWait as e:
            await asyncio.sleep(e.value)
            await message.reply_cached_media(
                doc["file_id"],
                protect_content=doc.get("protect", False),
            )
        return

    await message.reply_text(
        "👋 Welcome to File Share Bot!\n\n"
        "Send me a Telegram file and I will create a shareable link.\n\n"
        "⚡ Fast • 🔗 Shareable • 24/7"
    )

@app.on_message(filters.command("help"))
async def help_handler(_, message: Message):
    await message.reply_text(
        "Send me a document, video, audio, photo or animation.\n"
        "I will return a permanent share link while its record remains in the database."
    )

@app.on_message(filters.document | filters.video | filters.audio | filters.photo | filters.animation)
async def file_handler(_, message: Message):
    media = message.document or message.video or message.audio or message.photo or message.animation
    token = encode_token(media.file_id)
    await db.save_file(
        token,
        media.file_id,
        getattr(media, "file_unique_id", ""),
        bool(getattr(message, "protect_content", False)),
    )

    me = await app.get_me()
    link = f"https://t.me/{me.username}?start={token}"
    size = getattr(media, "file_size", 0) or 0
    suffix = f"\n📦 Size: `{size / (1024 * 1024):.2f} MB`" if size else ""

    await message.reply_text(
        f"✅ **File saved!**\n\n🔗 **Share link:**\n{link}{suffix}",
        disable_web_page_preview=True,
    )

@app.on_message(filters.command("stats") & filters.user(OWNER_ID))
async def stats_handler(_, message: Message):
    await message.reply_text(f"📊 Stored files: `{await db.count_files()}`")

async def run():
    await db.connect()
    await app.start()
    me = await app.get_me()
    log.info("Started as @%s", me.username)
    await asyncio.Event().wait()
