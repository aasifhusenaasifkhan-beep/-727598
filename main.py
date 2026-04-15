import os
import time
import json
import asyncio
import threading
import tempfile
import re
import shutil
from collections import deque
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified, MessageIdInvalid
from http.server import HTTPServer, BaseHTTPRequestHandler

# ================= CONFIGURATION =================

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEST_CHANNEL = ""   # Yaha channel ka username dena ya khali chhod dena.
PORT = 10000        # ye change mat karna 

OWNER_ID = 5351848105       
ALLOWED_USERS = [5344078567]             
ALLOWED_GROUPS = [-1003899919015] 

app = Client("EncoderBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Global Variables
users_data = {}
task_queue = deque()
in_queue = set()
processing_lock = asyncio.Lock()
main_loop = None
edit = "Maintanence by: @Sub_and_hardsub"

current_encoding = {} 

# ================= UTILS =================

def is_authorized(message: Message) -> bool:
    if not message.from_user: return False
    u_id = message.from_user.id    
    if u_id == OWNER_ID or u_id in ALLOWED_USERS or message.chat.id in ALLOWED_GROUPS:
        return True
    return False

def is_owner(message: Message) -> bool:
    return message.from_user and message.from_user.id == OWNER_ID

async def get_duration(file):
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", file]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        data = json.loads(stdout.decode())
        return float(data.get("format", {}).get("duration", 0))
    except:
        return 0

def format_progress_bar(percent, width=10):
    filled = int(percent * width / 100)
    bar = "█" * filled + "░" * (width - filled)
    return bar

async def safe_edit(message: Message, text: str):
    try:
        await message.edit(text)
    except:
        pass

async def download_with_verification(client, file_id, status_msg, phase="Downloading"):
    temp_dir = tempfile.gettempdir()
    base_name = f"temp_{int(time.time())}_{file_id}"
    
    for attempt in range(5):
        temp_file = os.path.join(temp_dir, f"{base_name}_{attempt}")
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
                
            path = await client.download_media(file_id, file_name=temp_file)
            if path and os.path.exists(path) and os.path.getsize(path) > 0:
                if phase == "Downloading video":
                    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
                    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    await proc.communicate()
                    if proc.returncode != 0: raise Exception("File corrupt")
                return path
        except Exception as e:
            if attempt < 4:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            raise Exception(f"Download failed: {str(e)}")
    raise Exception("Download failed after 5 attempts")

# ================= FINAL ENCODE FUNCTION (ALL FIXES + FAST) =================
async def encode_with_progress(video_path, subtitle_path, output_path, total_duration, status_msg, user_id, wm_path=None, wm_pos=None):
    # ---------- FIX 1: Preserve original subtitle extension ----------
    ext = os.path.splitext(subtitle_path)[1]  # .srt or .ass
    safe_sub = os.path.join(tempfile.gettempdir(), f"subtitle{ext}")
    try:
        shutil.copy(subtitle_path, safe_sub)
    except:
        safe_sub = subtitle_path   # fallback agar copy fail ho
    
    abs_sub = os.path.abspath(safe_sub).replace('\\', '/')
    
    # Base command with mapping and faststart
    base_cmd = ["ffmpeg", "-y", "-i", video_path, "-map", "0"]
    
    # Subtitle filter with UTF-8
    sub_filter = f"subtitles='{abs_sub}':charenc=UTF-8"
    
    if wm_path:
        base_cmd.extend(["-i", wm_path])
        pos_x, pos_y = ("10", "10") if wm_pos == "TL" else ("W-w-10", "10")
        filter_str = f"[0:v]{sub_filter}[sub];[1:v]scale=-1:60[wm];[sub][wm]overlay={pos_x}:{pos_y}"
        base_cmd.extend(["-filter_complex", filter_str])
    else:
        base_cmd.extend(["-vf", sub_filter])
    
    # Encoding settings (ultrafast for speed, threads auto, faststart for streaming)
    base_cmd.extend([
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
        "-threads", "auto",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-progress", "pipe:1", output_path
    ])
    
    # ---------- FIX 2: Try with subtitles ----------
    try:
        process = await asyncio.create_subprocess_exec(
            *base_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
    except Exception:
        # Fallback: command failed to start
        fallback_cmd = ["ffmpeg", "-y", "-i", video_path, "-map", "0",
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
                        "-threads", "auto",
                        "-c:a", "copy",
                        "-movflags", "+faststart",
                        output_path]
        process = await asyncio.create_subprocess_exec(
            *fallback_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await safe_edit(status_msg, "⚠️ Subtitle corrupt or unsupported. Encoding without subtitles.")
    
    current_encoding[user_id] = process
    
    last_update = 0
    progress_data = {}

    async def read_stdout():
        nonlocal last_update
        while True:
            line = await process.stdout.readline()
            if not line: break
            line_str = line.decode(errors="ignore").strip()
            if "=" in line_str:
                key, val = line_str.split("=", 1)
                progress_data[key] = val
            if key == "out_time_ms":
                try:
                    ms = int(progress_data.get("out_time_ms", 0))
                    current_seconds = ms / 1_000_000.0
                    percent = (current_seconds / total_duration) * 100 if total_duration > 0 else 0
                    now = time.time()
                    if now - last_update > 5 or percent >= 100:
                        bar = format_progress_bar(percent)
                        await safe_edit(status_msg, f"🔥 Encoding...\n`{bar}` {percent:.1f}%")
                        last_update = now
                except: pass

    async def read_stderr():
        while True:
            line = await process.stderr.readline()
            if not line: break

    await asyncio.gather(read_stdout(), read_stderr())
    returncode = await process.wait()
    current_encoding.pop(user_id, None)
    
    # ---------- FIX 2 (Extended): Returncode check fallback ----------
    if returncode != 0:
        await safe_edit(status_msg, "⚠️ Subtitle error, retrying without subtitles...")
        
        fallback_cmd = ["ffmpeg", "-y", "-i", video_path, "-map", "0",
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
                        "-threads", "auto",
                        "-c:a", "copy",
                        "-movflags", "+faststart",
                        output_path]
        
        fallback_proc = await asyncio.create_subprocess_exec(
            *fallback_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await fallback_proc.wait()
        if fallback_proc.returncode != 0:
            raise Exception("FFmpeg failed even without subtitles")
    
    if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
        raise Exception("Output file missing or too small")
    return True

# ================= HANDLERS (unchanged) =================

@app.on_message(filters.command("start") & filters.private)
async def start(client, message: Message):
    if not is_authorized(message): return
    await message.reply(f"<b>🔥 Hardsub bot is Online again!</b>\n\nUse /hsub to add subtitle into video\nUse /cancel to stop task\n\n{edit}")

@app.on_message(filters.command("cancel"))
async def cancel_task(client, message: Message):
    if not is_authorized(message): return
    user_id = message.from_user.id
    
    removed = False
    for i, task in enumerate(task_queue):
        if task["user_id"] == user_id:
            del task_queue[i]
            removed = True
            break
    
    if user_id in current_encoding:
        proc = current_encoding[user_id]
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except:
            proc.kill()
        current_encoding.pop(user_id, None)
        await message.reply("🛑 Your encoding task has been cancelled.")
        return
    
    if removed:
        in_queue.discard(user_id)
        await message.reply("✅ Your task has been removed from queue.")
    else:
        await message.reply("❌ No active task found.")

@app.on_message(filters.command("hsub"))
async def hsub_cmd(client, message: Message):
    if not is_authorized(message): return
    replied = message.reply_to_message
    if not replied or not (replied.video or replied.document):
        return await message.reply("❌ Reply to a video file with /hsub")
    
    media = replied.video or replied.document
    users_data[message.from_user.id] = {
        "video": {"file_id": media.file_id, "file_name": media.file_name or "video.mp4"},
        "chat_id": message.chat.id,
        "state": "WAIT_SUB"
    }
    await message.reply("📄 Now send the Subtitle file (.srt / .ass)")

@app.on_message(filters.document | filters.video | filters.photo | filters.text)
async def handle_all_inputs(client, message: Message):
    if not is_authorized(message): return
    user_id = message.from_user.id
    if user_id not in users_data: return

    state = users_data[user_id].get("state")

    if state == "WAIT_SUB" and message.document:
        if message.document.file_name.lower().endswith((".srt", ".ass")):
            users_data[user_id]["subtitle"] = {"file_id": message.document.file_id, "file_name": message.document.file_name}
            users_data[user_id]["state"] = "WAIT_WM_CHOICE"
            
            btn = InlineKeyboardMarkup([[
                InlineKeyboardButton("Yes, Add Photo", callback_data="wm_yes"),
                InlineKeyboardButton("Skip Watermark", callback_data="wm_skip")
            ]])
            await message.reply("Do you want to add a Watermark Image (Logo)?", reply_markup=btn)
        return

    if state == "WAIT_WM_PIC" and message.photo:
        users_data[user_id]["watermark"] = {"file_id": message.photo.file_id}
        users_data[user_id]["state"] = "WAIT_WM_POS"
        btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("Top-Left", callback_data="pos_TL"),
            InlineKeyboardButton("Top-Right", callback_data="pos_TR")
        ]])
        await message.reply("Select Watermark Position:", reply_markup=btn)
        return

    if state == "WAIT_RENAME_TEXT" and message.text:
        new_name = message.text.strip()
        if not new_name.endswith(".mp4"): new_name += ".mp4"
        users_data[user_id]["video"]["file_name"] = new_name
        await add_to_queue(user_id, message)
        return

@app.on_callback_query()
async def callback_queries(client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id not in users_data:
        return await query.answer("Not Yours!", show_alert=True)
    
    data = query.data

    if data == "wm_yes":
        users_data[user_id]["state"] = "WAIT_WM_PIC"
        await query.message.edit("🖼️ Send the Watermark Image (Photo format).")
    elif data == "wm_skip":
        users_data[user_id]["watermark"] = None
        users_data[user_id]["state"] = "WAIT_RENAME_CHOICE"
        await ask_rename(query.message)
    elif data.startswith("pos_"):
        users_data[user_id]["wm_pos"] = "TL" if data == "pos_TL" else "TR"
        users_data[user_id]["state"] = "WAIT_RENAME_CHOICE"
        await ask_rename(query.message)
    elif data == "rn_yes":
        users_data[user_id]["state"] = "WAIT_RENAME_TEXT"
        await query.message.edit("📝 Send new name for the video (without extension)\n\nEx: [S01 - Ep 02] Oshi no Ko - HD")
    elif data == "rn_skip":
        base = os.path.splitext(users_data[user_id]["video"]["file_name"])[0]
        users_data[user_id]["video"]["file_name"] = base + ".mp4"
        await query.message.edit("🚀 Processing with original name...")
        await add_to_queue(user_id, query.message)

async def ask_rename(message):
    btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("Rename", callback_data="rn_yes"),
        InlineKeyboardButton("Skip", callback_data="rn_skip")
    ]])
    await message.edit("Do you want to rename the output file?", reply_markup=btn)

async def add_to_queue(user_id, message):
    data = users_data.pop(user_id)
    task_queue.append({
        "user_id": user_id,
        "video": data.get("video"),
        "subtitle": data.get("subtitle"),
        "watermark": data.get("watermark"),
        "wm_pos": data.get("wm_pos"),
        "chat_id": data.get("chat_id")
    })
    in_queue.add(user_id)
    await message.reply(f"✅ Added to Queue. Position: {len(task_queue)}")

# ================= CORE ENCODER =================

async def worker():
    while True:
        if not task_queue:
            await asyncio.sleep(5)
            continue
        
        task = task_queue.popleft()
        uid = task["user_id"]
        v_info = task["video"]
        s_info = task["subtitle"]
        wm_info = task.get("watermark")
        wm_pos = task.get("wm_pos")
        original_chat = task["chat_id"]
        
        status = await app.send_message(original_chat, "⏳ Starting Process...")
        channel_log = None
        v_path = s_path = wm_path = out_path = None
        
        try:
            if DEST_CHANNEL:
                channel_log = await app.send_message(DEST_CHANNEL, f"<b>🔄 Starting:</b> {v_info['file_name']}")

            await safe_edit(status, "📥 Downloading video...")
            v_path = await download_with_verification(app, v_info["file_id"], status, "Downloading video")

            if os.path.getsize(v_path) > 2000 * 1024 * 1024:
                await safe_edit(status, "❌ Video is larger than 2GB limit.")
                continue

            await safe_edit(status, "📥 Downloading subtitle...")
            s_path = await download_with_verification(app, s_info["file_id"], status, "Downloading subtitle")

            if wm_info:
                await safe_edit(status, "📥 Downloading watermark...")
                wm_path = await download_with_verification(app, wm_info["file_id"], status, "Downloading watermark")

            dur = await get_duration(v_path)
            
            # Safe output path in temp directory
            out_path = os.path.join(tempfile.gettempdir(), v_info["file_name"])
            
            await safe_edit(status, "🔥 Encoding...")
            
            success = await encode_with_progress(v_path, s_path, out_path, dur, status, uid, wm_path, wm_pos)

            if success:
                await safe_edit(status, "📤 Uploading as Document...")
                upload_target = DEST_CHANNEL if DEST_CHANNEL else original_chat
                
                # FIX: caption only filename, not full path
                await app.send_document(
                    chat_id=upload_target,
                    document=out_path,
                    caption=os.path.basename(out_path)
                )
                
                await safe_edit(status, f"✅ Successfully Completed!\n\nFile Sent.")
                if channel_log: await channel_log.delete()
            else:
                await safe_edit(status, "❌ Encoding Failed.")
                
        except Exception as e:
            await app.send_message(original_chat, f"❌ Error: {str(e)}")
        finally:
            in_queue.discard(uid)
            for f in [v_path, s_path, wm_path, out_path]:
                if f and os.path.exists(f):
                    try: os.remove(f)
                    except: pass

# ================= RENDER KEEP ALIVE =================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Running")

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()

# ================= MAIN =================

async def main():
    if edit != "Maintanence by: @Sub_and_hardsub":
        print("credit hataya isiliye nahi chala. Sahi karo wo pehele.")
        return
    global main_loop
    main_loop = asyncio.get_event_loop()
    await app.start()
    print("Bot is started!")
    asyncio.create_task(worker())
    await idle()

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
