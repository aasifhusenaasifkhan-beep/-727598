import os
import time
import json
import asyncio
import threading
import tempfile
import shutil
import math
from collections import deque
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified, MessageIdInvalid
from http.server import HTTPServer, BaseHTTPRequestHandler

# ================= CONFIGURATION =================

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEST_CHANNEL = ""   
PORT = 10000        

OWNER_ID = 5351848105       
ALLOWED_USERS = [5344078567]             
ALLOWED_GROUPS = [-1003899919015] 

app = Client("EncoderBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Global Variables
users_data = {}
task_queue = deque()
in_queue = set()
main_loop = None
edit = "Maintanence by: @Sub_and_hardsub"

current_encoding = {} 
BANNED_USERS = set()      # Banned users ki list
user_strikes = {}         # Strike count track karne ke liye

# ================= UTILS =================

def is_authorized(message: Message) -> bool:
    if not message.from_user: return False
    u_id = message.from_user.id    
    if u_id in BANNED_USERS: return False
    if u_id == OWNER_ID or u_id in ALLOWED_USERS or message.chat.id in ALLOWED_GROUPS:
        return True
    return False

async def check_size_and_strike(message: Message, file_size: int, u_id: int) -> bool:
    """Check if file is > 1GB. Manage strikes and bans."""
    if file_size > 1073741824:  # 1GB
        user_strikes[u_id] = user_strikes.get(u_id, 0) + 1
        strikes = user_strikes[u_id]
        if strikes >= 3:
            BANNED_USERS.add(u_id)
            await message.reply("🚫 **BANNED!**\nYou uploaded >1GB files 3 times. You can no longer use this bot.")
        else:
            await message.reply(f"⚠️ **WARNING ({strikes}/3)**\nFile is larger than 1GB! Bot only supports up to 1GB.\nNext warnings will result in a permanent ban.")
        return False
    return True

async def get_duration(file):
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", file]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        data = json.loads(stdout.decode())
        return float(data.get("format", {}).get("duration", 0))
    except: return 0

def format_progress_bar(percent, width=10):
    filled = int(percent * width / 100)
    return "█" * filled + "░" * (width - filled)

async def safe_edit(message: Message, text: str):
    try: await message.edit(text)
    except: pass

async def download_with_verification(client, file_id, status_msg, workspace, file_name="file"):
    path = os.path.join(workspace, file_name)
    for attempt in range(5):
        try:
            if os.path.exists(path): os.remove(path)
            downloaded = await client.download_media(file_id, file_name=path)
            if downloaded and os.path.exists(downloaded) and os.path.getsize(downloaded) > 0:
                return downloaded
        except Exception as e:
            if attempt < 4:
                await asyncio.sleep(5)
                continue
            raise Exception(f"Download failed: {str(e)}")
    raise Exception("Download failed after 5 attempts")

def calculate_target_bitrate(original_size_bytes, duration_secs, max_factor=1.5, hard_limit_mb=300):
    """
    Returns video bitrate in kbps such that final file size <= min(original_size * max_factor, hard_limit_mb MB)
    """
    max_allowed_bytes = min(original_size_bytes * max_factor, hard_limit_mb * 1024 * 1024)
    # Audio size is unknown but we assume it's same as original (copied). We'll leave ~10% margin.
    # Total size = video + audio. We'll set video bitrate to achieve 90% of max_allowed.
    target_total_bytes = max_allowed_bytes * 0.9
    video_bitrate_bps = (target_total_bytes * 8) / duration_secs
    # Convert to kbps, ensure minimum 200 kbps
    video_bitrate_kbps = max(200, int(video_bitrate_bps / 1000))
    return video_bitrate_kbps

# ================= CORE FFmpeg FUNCTIONS (with size control) =================
async def resize_only(video_path, output_path, target_height, total_duration, status_msg, user_id, original_size_bytes):
    scale_filter = f"scale=-2:{target_height}"
    
    # Calculate bitrate to keep size under control
    bitrate_kbps = calculate_target_bitrate(original_size_bytes, total_duration, max_factor=1.5, hard_limit_mb=300)
    
    base_cmd = [
        "ffmpeg", "-y", "-i", video_path, "-vf", scale_filter,
        "-c:v", "libx264", "-preset", "superfast", "-b:v", f"{bitrate_kbps}k",
        "-maxrate", f"{bitrate_kbps * 1.5}k", "-bufsize", f"{bitrate_kbps * 2}k",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-progress", "pipe:1", output_path
    ]
    process = await asyncio.create_subprocess_exec(*base_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    current_encoding[user_id] = process
    
    last_update = 0
    progress_data = {}
    
    async def read_stdout():
        nonlocal last_update
        while True:
            line = await process.stdout.readline()
            if not line: break
            line_str = line.decode(errors="ignore").strip()
            if "=" in line_str: key, val = line_str.split("=", 1); progress_data[key] = val
            if key == "out_time_ms":
                try:
                    percent = ((int(progress_data.get("out_time_ms", 0)) / 1_000_000.0) / total_duration) * 100
                    if time.time() - last_update > 5:
                        await safe_edit(status_msg, f"🔄 Resizing...\n`{format_progress_bar(percent)}` {percent:.1f}%")
                        last_update = time.time()
                except: pass
    
    await asyncio.gather(read_stdout(), process.stderr.read())
    returncode = await process.wait()
    current_encoding.pop(user_id, None)
    if returncode != 0: raise Exception("Resize failed")
    return True

async def encode_with_progress(video_path, subtitle_path, output_path, total_duration, status_msg, user_id, original_size_bytes, wm_path=None, wm_pos=None):
    abs_sub = os.path.abspath(subtitle_path).replace('\\', '/')
    sub_filter = f"subtitles='{abs_sub}':charenc=UTF-8"
    
    # Calculate bitrate to keep final size under control
    bitrate_kbps = calculate_target_bitrate(original_size_bytes, total_duration, max_factor=1.5, hard_limit_mb=300)
    
    if wm_path:
        overlay_pos = "20:20" if wm_pos == "TL" else "W-w-20:20"
        filter_complex = f"[0:v]{sub_filter}[sub];[1:v]scale=200:-1[wm];[sub][wm]overlay={overlay_pos}"
        cmd = ["ffmpeg", "-y", "-i", video_path, "-i", wm_path, "-filter_complex", filter_complex]
    else:
        cmd = ["ffmpeg", "-y", "-i", video_path, "-vf", sub_filter]
    
    cmd.extend([
        "-c:v", "libx264", "-preset", "superfast",
        "-b:v", f"{bitrate_kbps}k", "-maxrate", f"{bitrate_kbps * 1.5}k", "-bufsize", f"{bitrate_kbps * 2}k",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-progress", "pipe:1", output_path
    ])
    
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    current_encoding[user_id] = process
    
    last_update = 0
    progress_data = {}
    
    async def read_stdout():
        nonlocal last_update
        while True:
            line = await process.stdout.readline()
            if not line: break
            line_str = line.decode(errors="ignore").strip()
            if "=" in line_str: key, val = line_str.split("=", 1); progress_data[key] = val
            if key == "out_time_ms":
                try:
                    percent = ((int(progress_data.get("out_time_ms", 0)) / 1_000_000.0) / total_duration) * 100
                    if time.time() - last_update > 5:
                        await safe_edit(status_msg, f"🔥 Encoding...\n`{format_progress_bar(percent)}` {percent:.1f}%")
                        last_update = time.time()
                except: pass
                
    await asyncio.gather(read_stdout(), process.stderr.read())
    returncode = await process.wait()
    current_encoding.pop(user_id, None)
    if returncode != 0: raise Exception("FFmpeg failed")
    return True

# ================= HANDLERS =================
@app.on_message(filters.command("start") & filters.private)
async def start(client, message: Message):
    if not is_authorized(message): return
    await message.reply(f"<b>🔥 Hardsub bot is Online!</b>\n\n/hsub - Add subtitle\n/remm or /cancel - Stop task & clear data\n/1080pdd, /720pdd, /480pdd - Resize\n\n{edit}")

@app.on_message(filters.command(["cancel", "remm"]))
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
            await asyncio.wait_for(proc.wait(), timeout=3)
        except:
            proc.kill()
        current_encoding.pop(user_id, None)
        await message.reply("🛑 Task Force Stopped! All temp data deleted.")
        return
    
    if removed:
        in_queue.discard(user_id)
        await message.reply("✅ Task removed from queue and data cleared.")
    else:
        await message.reply("❌ No active task found.")

@app.on_message(filters.command(["1080pdd", "720pdd", "480pdd"]) & filters.private)
async def resize_command(client, message: Message):
    if not is_authorized(message): return
    target = int(message.command[0].replace("pdd", ""))
    
    media = message.reply_to_message.video or message.reply_to_message.document if message.reply_to_message else None
    if not media: return await message.reply("❌ Please reply to a video file.")
    
    if not await check_size_and_strike(message, media.file_size, message.from_user.id): return
    
    status = await message.reply(f"⏳ Resizing to {target}p ...")
    workspace = os.path.join(tempfile.gettempdir(), f"resize_{message.from_user.id}_{int(time.time())}")
    os.makedirs(workspace, exist_ok=True)
    
    try:
        v_path = await download_with_verification(app, media.file_id, status, workspace, "input.mp4")
        out_path = os.path.join(workspace, f"resized_{target}p.mp4")
        dur = await get_duration(v_path)
        # Pass original file size for bitrate calculation
        await resize_only(v_path, out_path, target, dur, status, message.from_user.id, media.file_size)
        
        await safe_edit(status, "📤 Uploading as Document...")
        await app.send_document(chat_id=message.chat.id, document=out_path, caption=f"✅ Resized to {target}p")
        await status.delete()
    except Exception as e:
        if str(e) != "Resize failed": await safe_edit(status, f"❌ Error: {str(e)}")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

@app.on_message(filters.command("hsub"))
async def hsub_cmd(client, message: Message):
    if not is_authorized(message): return
    media = message.reply_to_message.video or message.reply_to_message.document if message.reply_to_message else None
    if not media: return await message.reply("❌ Reply to a video file.")
    
    if not await check_size_and_strike(message, media.file_size, message.from_user.id): return
    
    users_data[message.from_user.id] = {"video": {"file_id": media.file_id, "file_name": media.file_name or "video.mp4", "file_size": media.file_size}, "chat_id": message.chat.id, "state": "WAIT_SUB"}
    await message.reply("📄 Send Subtitle file (.srt / .ass)")

@app.on_message(filters.document | filters.video | filters.photo | filters.text)
async def handle_inputs(client, message: Message):
    if not is_authorized(message): return
    uid = message.from_user.id
    if uid not in users_data: return
    state = users_data[uid].get("state")

    if state == "WAIT_SUB" and message.document and message.document.file_name.endswith((".srt", ".ass")):
        users_data[uid]["subtitle"] = {"file_id": message.document.file_id, "file_name": message.document.file_name}
        users_data[uid]["state"] = "WAIT_WM_CHOICE"
        await message.reply("Add Watermark?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Yes", callback_data="wm_yes"), InlineKeyboardButton("No", callback_data="wm_skip")]]))
        
    elif state == "WAIT_WM_PIC" and message.photo:
        users_data[uid]["watermark"] = {"file_id": message.photo.file_id}
        users_data[uid]["state"] = "WAIT_WM_POS"
        await message.reply("Position:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Top-Left", callback_data="pos_TL"), InlineKeyboardButton("Top-Right", callback_data="pos_TR")]]))

    elif state == "WAIT_RENAME_TEXT" and message.text:
        users_data[uid]["video"]["file_name"] = message.text.strip() + ".mp4" if not message.text.endswith(".mp4") else message.text.strip()
        await add_to_queue(uid, message)

@app.on_callback_query()
async def callbacks(client, query: CallbackQuery):
    uid = query.from_user.id
    if uid not in users_data: return await query.answer("Not Yours!", show_alert=True)
    d = query.data
    if d == "wm_yes":
        users_data[uid]["state"] = "WAIT_WM_PIC"; await query.message.edit("🖼️ Send Photo.")
    elif d == "wm_skip":
        users_data[uid]["watermark"] = None; users_data[uid]["state"] = "WAIT_RENAME_CHOICE"; await ask_rename(query.message)
    elif d.startswith("pos_"):
        users_data[uid]["wm_pos"] = "TL" if d == "pos_TL" else "TR"; users_data[uid]["state"] = "WAIT_RENAME_CHOICE"; await ask_rename(query.message)
    elif d == "rn_yes":
        users_data[uid]["state"] = "WAIT_RENAME_TEXT"; await query.message.edit("📝 Send new name.")
    elif d == "rn_skip":
        users_data[uid]["video"]["file_name"] = os.path.splitext(users_data[uid]["video"]["file_name"])[0] + ".mp4"; await add_to_queue(uid, query.message)

async def ask_rename(msg):
    await msg.edit("Rename file?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Yes", callback_data="rn_yes"), InlineKeyboardButton("Skip", callback_data="rn_skip")]]))

async def add_to_queue(uid, msg):
    d = users_data.pop(uid)
    task_queue.append({"user_id": uid, "video": d.get("video"), "subtitle": d.get("subtitle"), "watermark": d.get("watermark"), "wm_pos": d.get("wm_pos"), "chat_id": d.get("chat_id")})
    in_queue.add(uid)
    await msg.reply(f"✅ Added to Queue. Position: {len(task_queue)}")

# ================= WORKER =================
async def worker():
    while True:
        if not task_queue:
            await asyncio.sleep(5); continue
        
        task = task_queue.popleft()
        uid, v_info, s_info, wm_info, wm_pos, chat = task["user_id"], task["video"], task["subtitle"], task.get("watermark"), task.get("wm_pos"), task["chat_id"]
        status = await app.send_message(chat, "⏳ Starting Process...")
        
        workspace = os.path.join(tempfile.gettempdir(), f"task_{uid}_{int(time.time())}")
        os.makedirs(workspace, exist_ok=True)
        
        try:
            await safe_edit(status, "📥 Downloading video...")
            v_path = await download_with_verification(app, v_info["file_id"], status, workspace, "input.mp4")
            original_size = v_info.get("file_size", os.path.getsize(v_path))
            
            await safe_edit(status, "📥 Downloading subtitle...")
            s_ext = os.path.splitext(s_info["file_name"])[1]
            s_path = await download_with_verification(app, s_info["file_id"], status, workspace, f"sub{s_ext}")
            
            wm_path = None
            if wm_info: 
                wm_path = await download_with_verification(app, wm_info["file_id"], status, workspace, "wm.jpg")

            out_path = os.path.join(workspace, v_info["file_name"])
            await safe_edit(status, "🔥 Encoding...")
            dur = await get_duration(v_path)
            
            await encode_with_progress(v_path, s_path, out_path, dur, status, uid, original_size, wm_path, wm_pos)

            await safe_edit(status, "📤 Uploading Document...")
            await app.send_document(chat_id=DEST_CHANNEL or chat, document=out_path, caption=v_info['file_name'])
            await status.delete()
        except Exception as e:
            if str(e) != "FFmpeg failed": await app.send_message(chat, f"❌ Error: {str(e)}")
        finally:
            in_queue.discard(uid)
            shutil.rmtree(workspace, ignore_errors=True)

# ================= HEALTH SERVER =================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Running")

async def main():
    if edit != "Maintanence by: @Sub_and_hardsub": return
    await app.start()
    print("Bot is started!")
    asyncio.create_task(worker())
    await idle()

if __name__ == "__main__":
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever(), daemon=True).start()
    asyncio.get_event_loop().run_until_complete(main())
