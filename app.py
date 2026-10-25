import os
import time
import uuid
import json
import threading
import requests
from flask import Flask, request, Response, send_from_directory, jsonify, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Update, WebAppInfo

# ---------- Config ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEST_CHANNEL = int(os.getenv("DEST_CHANNEL", "0"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
ADMIN_KEY = os.getenv("ADMIN_KEY", "change_me_now")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env variable missing")

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__, static_folder="static")

# Database File
DB_FILE = "db.json"

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {}

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f)

video_db = load_db()
media_buffer = {}
buffer_lock = threading.Lock()

# ---------- Bot Handler (Private Chat) ----------
@bot.message_handler(content_types=["photo", "video"])
def handle_bot_upload(message):
    # Media group (album) buffer logic
    if not message.media_group_id:
        bot.reply_to(message, "⚠️ Please send the Photo and Video **together** as an album.")
        return

    mgid = message.media_group_id
    with buffer_lock:
        media_buffer.setdefault(mgid, []).append(message)

    threading.Timer(3.0, process_upload, args=[mgid, message.chat.id]).start()

def process_upload(mgid, chat_id):
    with buffer_lock:
        msgs = media_buffer.pop(mgid, [])
    if not msgs:
        return

    photo_msg = next((m for m in msgs if m.content_type == "photo"), None)
    video_msg = next((m for m in msgs if m.content_type == "video"), None)

    if not photo_msg or not video_msg:
        bot.send_message(chat_id, "❌ Error: Please send exactly 1 Photo and 1 Video together.")
        return

    video_file_id = video_msg.video.file_id
    photo_file_id = photo_msg.photo[-1].file_id

    # Generate Unique String
    unique_id = uuid.uuid4().hex[:10].upper()
    
    # Save to DB
    video_db[unique_id] = video_file_id
    save_db(video_db)

    # Send to Destination Channel
    markup = InlineKeyboardMarkup()
    web_app_info = WebAppInfo(url=f"{PUBLIC_URL}/miniapp?id={unique_id}")
    markup.add(InlineKeyboardButton("▶️ PLAY", web_app=web_app_info))

    try:
        bot.send_photo(DEST_CHANNEL, photo_file_id, reply_markup=markup)
        bot.send_message(chat_id, f"✅ Uploaded successfully!\nYour Video ID is: `{unique_id}`", parse_mode="Markdown")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Failed to send to channel. Error: {e}")


# ---------- Flask Routes ----------
@app.route("/health")
def health():
    return "OK", 200

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        update = Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
        return "", 200
    return "", 403

@app.route("/miniapp")
def miniapp_page():
    return send_from_directory("static", "miniapp.html")

@app.route("/stream/<vid_id>")
def stream_video(vid_id):
    file_id = video_db.get(vid_id)
    if not file_id:
        abort(404, "Video not found in database")

    try:
        file_info = bot.get_file(file_id)
    except Exception as e:
        abort(500, "Telegram file fetch failed")

    tg_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
    file_size = file_info.file_size
    range_header = request.headers.get("Range")

    if not range_header:
        headers = {"Content-Type": "video/mp4", "Accept-Ranges": "bytes", "Content-Length": str(file_size)}
        def gen_full():
            with requests.get(tg_url, stream=True, timeout=30) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk: yield chunk
        return Response(gen_full(), status=200, headers=headers)

    try:
        unit, rng = range_header.split("=", 1)
        start_s, end_s = rng.split("-", 1)
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else file_size - 1
    except Exception:
        abort(400, "bad range header")

    if start > end or end >= file_size:
        end = min(end, file_size - 1)

    chunk_size = end - start + 1
    headers = {
        "Content-Type": "video/mp4",
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(chunk_size),
    }

    def gen_range():
        with requests.get(tg_url, headers={"Range": f"bytes={start}-{end}"}, stream=True, timeout=30) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk: yield chunk

    return Response(gen_range(), status=206, headers=headers)

@app.route("/admin/set-webhook")
def admin_set_webhook():
    if request.args.get("key") != ADMIN_KEY:
        abort(403)
    bot.remove_webhook()
    time.sleep(1)
    ok = bot.set_webhook(url=f"{PUBLIC_URL}/{BOT_TOKEN}")
    return jsonify({"ok": ok})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
