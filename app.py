import os
import time
import uuid
import threading
import requests
from flask import Flask, request, Response, send_from_directory, jsonify, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Update, WebAppInfo

# ---------- Config ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
SOURCE_CHANNEL = int(os.getenv("SOURCE_CHANNEL", "0"))
DEST_CHANNEL = int(os.getenv("DEST_CHANNEL", "0"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
ADMIN_KEY = os.getenv("ADMIN_KEY", "change_me_now")
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB Bot API limit

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env variable missing")

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__, static_folder="static")

# In-memory store: vid_id -> file_id
video_store = {}

# Media group buffer
media_buffer = {}
buffer_lock = threading.Lock()

# ---------- Media Group Handler ----------
@bot.channel_post_handler(content_types=["photo", "video"])
def handle_channel_post(message):
    if message.chat.id != SOURCE_CHANNEL:
        return

    if not message.media_group_id:
        return

    mgid = message.media_group_id
    with buffer_lock:
        media_buffer.setdefault(mgid, []).append(message)

    threading.Timer(3.0, process_album, args=[mgid]).start()


def process_album(mgid):
    with buffer_lock:
        msgs = media_buffer.pop(mgid, [])
    if not msgs:
        return

    photo_msg = next((m for m in msgs if m.content_type == "photo"), None)
    video_msg = next((m for m in msgs if m.content_type == "video"), None)

    if not photo_msg or not video_msg:
        print(f"[skip] album {mgid} me photo ya video missing")
        return

    video_file_id = video_msg.video.file_id
    photo_file_id = photo_msg.photo[-1].file_id

    vid_id = uuid.uuid4().hex[:16]
    video_store[vid_id] = {
        "file_id": video_file_id,
        "size": video_msg.video.file_size,
        "duration": video_msg.video.duration,
    }

    # YAHAN CHANGE HUA HAI (WebAppInfo use kiya hai)
    markup = InlineKeyboardMarkup()
    web_app_info = WebAppInfo(url=f"{PUBLIC_URL}/miniapp?vid={vid_id}")
    markup.add(InlineKeyboardButton(
        "▶️ PLAY",
        web_app=web_app_info
    ))

    try:
        bot.send_photo(
            DEST_CHANNEL,
            photo_file_id,
            reply_markup=markup
        )
        print(f"[ok] posted to dest channel, vid_id={vid_id}")
    except Exception as e:
        print(f"[err] send_photo failed: {e}")


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
    entry = video_store.get(vid_id)
    if not entry:
        abort(404, "video not found")

    file_id = entry["file_id"]

    try:
        file_info = bot.get_file(file_id)
    except Exception as e:
        print(f"[err] get_file: {e}")
        abort(500, "Telegram file fetch failed")

    tg_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
    file_size = file_info.file_size or entry.get("size") or 0
    range_header = request.headers.get("Range")

    if not range_header:
        headers = {
            "Content-Type": "video/mp4",
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Cache-Control": "no-store",
        }
        def gen_full():
            with requests.get(tg_url, stream=True, timeout=30) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        yield chunk
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
        "Cache-Control": "no-store",
    }

    def gen_range():
        with requests.get(tg_url, headers={"Range": f"bytes={start}-{end}"}, stream=True, timeout=30) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk

    return Response(gen_range(), status=206, headers=headers)


@app.route("/admin/set-webhook")
def admin_set_webhook():
    if request.args.get("key") != ADMIN_KEY:
        abort(403)
    bot.remove_webhook()
    time.sleep(1)
    ok = bot.set_webhook(url=f"{PUBLIC_URL}/{BOT_TOKEN}")
    return jsonify({"ok": ok, "url": f"{PUBLIC_URL}/{BOT_TOKEN}"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
