import os
import time
import json
import asyncio
import threading
import tempfile
import shutil
from collections import deque
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
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
cancelled_tasks = set()
edit = "Maintanence by: @Sub_and_hardsub"
current_encoding = {} 
BANNED_USERS = set()
user_strikes = {}

# ================= UTILS =================
def is_authorized(message: Message) -> bool:
    if not message.from_user: return False
    u_id = message.from_user.id    
    if u_id in BANNED_USERS: return False
    if u_id == OWNER_ID or u_id in ALLOWED_USERS or message.chat.id in ALLOWED_GROUPS:
        return True
    return False

async def check_size_and_strike(message: Message, file_size: int, u_id: int) -> bool:
    if file_size > 1073741824:
        user_strikes[u_id] = user_strikes.get(u_id, 0) + 1
        strikes = user_strikes[u_id]
        if strikes >= 3:
            BANNED_USERS.add(u_id)
            await message.reply("🚫 **BANNED!**\nYou uploaded >1GB files 3 times.")
        else:
            await message.reply(f"⚠️ **WARNING ({strikes}/3)**\nFile >1GB not supported.")
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

async def download_with_verification(client, file_id, status_msg, workspace, user_id, file_name="file"):
    path = os.path.join(workspace, file_name)
    
    async def prog(current, total):
        if user_id in cancelled_tasks:
            raise Exception("Task Cancelled by User")

    for attempt in range(5):
        try:
            if user_id in cancelled_tasks: raise Exception("Task Cancelled by User")
            if os.path.exists(path): os.remove(path)
            downloaded = await client.download_media(file_id, file_name=path, progress=prog)
            if downloaded and os.path.exists(downloaded) and os.path.getsize(downloaded) > 0:
                return downloaded
        except Exception as e:
            if "Cancelled" in str(e): raise e
            if attempt < 4:
                await asyncio.sleep(5)
                continue
            raise Exception(f"Download failed: {str(e)}")
    raise Exception("Download failed")

# ================= FAST ENCODE FUNCTIONS =================
async def resize_only(video_path, output_path, target_height, total_duration, status_msg, user_id):
    scale_filter = f"scale=-2:{target_height}"
    
    # MAXIMUM SPEED (ultrafast) + BETTER COMPRESSION (CRF 34 & Audio 96k)
    cmd = [
        "ffmpeg", "-y", "-i", video_path, "-vf", scale_filter,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "34",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
        "-progress", "pipe:1", output_path
    ]
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    current_encoding[user_id] = process
    
    last_update = 0
    async def read_stdout():
        nonlocal last_update
        while True:
            if user_id in cancelled_tasks:
                process.kill()
                break
            line = await process.stdout.readline()
            if not line: break
            line_str = line.decode(errors="ignore").strip()
            if "out_time_ms" in line_str:
                try:
                    parts = line_str.split("=")
                    if len(parts) == 2:
                        ms = int(parts[1])
                        percent = (ms / 1_000_000.0 / total_duration) * 100
                        if time.time() - last_update > 5:
                            await safe_edit(status_msg, f"🔄 Resizing & Compressing...\n`{format_progress_bar(percent)}` {percent:.1f}%")
                            last_update = time.time()
                except: pass
    await asyncio.gather(read_stdout(), process.stderr.read())
    returncode = await process.wait()
    current_encoding.pop(user_id, None)
    if user_id in cancelled_tasks: raise Exception("Task Cancelled by User")
    if returncode != 0: raise Exception("Resize failed")
    return True

async def encode_with_progress(video_path, subtitle_path, output_path, total_duration, status_msg, user_id, wm_path=None, wm_pos=None):
    abs_sub = os.path.abspath(subtitle_path).replace('\\', '/')
    sub_filter = f"subtitles='{abs_sub}':charenc=UTF-8"
    
    if wm_path:
        overlay_pos = "20:20" if wm_pos == "TL" else "W-w-20:20"
        filter_complex = f"[0:v]{sub_filter}[sub];[1:v]scale=200:-1[wm];[sub][wm]overlay={overlay_pos}"
        cmd = ["ffmpeg", "-y", "-i", video_path, "-i", wm_path, "-filter_complex", filter_complex]
    else:
        cmd = ["ffmpeg", "-y", "-i", video_path, "-vf", sub_filter]
    
    # MAXIMUM SPEED (ultrafast) + BETTER COMPRESSION (CRF 34 & Audio 96k)
    cmd.extend([
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "34", "-tune", "fastdecode",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
        "-progress", "pipe:1", output_path
    ])
    
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    current_encoding[user_id] = process
    
    last_update = 0
    async def read_stdout():
        nonlocal last_update
        while True:
            if user_id in cancelled_tasks:
                process.kill()
                break
            line = await process.stdout.readline()
            if not line: break
            line_str = line.decode(errors="ignore").strip()
            if "out_time_ms" in line_str:
                try:
                    parts = line_str.split("=")
                    if len(parts) == 2:
                        ms = int(parts[1])
                        percent = (ms / 1_000_000.0 / total_duration) * 100
                        if time.time() - last_update > 5:
                            await safe_edit(status_msg, f"🔥 Encoding & Compressing...\n`{format_progress_bar(percent)}` {percent:.1f}%")
                            last_update = time.time()
                except: pass
                
    await asyncio.gather(read_stdout(), process.stderr.read())
    returncode = await process.wait()
    current_encoding.pop(user_id, None)
    if user_id in cancelled_tasks: raise Exception("Task Cancelled by User")
    if returncode != 0: raise Exception("FFmpeg failed")
    return True

# ================= HANDLERS =================
@app.on_message(filters.command("start"))
async def start(client, message: Message):
    if not is_authorized(message): return
    await message.reply(f"<b>🔥 Hardsub bot (UltraFast + Compressed)</b>\n\n/hsub - Add subtitle\n/cancel - Stop task\n/1080pdd, /720pdd, /480pdd - Resize\n\n{edit}")

@app.on_message(filters.command(["cancel", "remm"]))
async def cancel_task(client, message: Message):
    if not is_authorized(message): return
    user_id = message.from_user.id
    removed = False
    
    if user_id in users_data:
        del users_data[user_id]
        removed = True

    for i, task in enumerate(task_queue):
        if task["user_id"] == user_id:
            del task_queue[i]
            removed = True
            break
            
    cancelled_tasks.add(user_id)
    if user_id in current_encoding:
        proc = current_encoding[user_id]
        try:
            proc.kill()
        except: pass
        current_encoding.pop(user_id, None)
        removed = True

    if removed or user_id in in_queue:
        in_queue.discard(user_id)
        await message.reply("🛑 Task successfully cancelled.")
    else:
        await message.reply("❌ No active task to cancel.")

@app.on_message(filters.command(["1080pdd", "720pdd", "480pdd"]))
async def resize_command(client, message: Message):
    if not is_authorized(message): return
    target = int(message.command[0].replace("pdd", ""))
    media = message.reply_to_message.video or message.reply_to_message.document if message.reply_to_message else None
    if not media: return await message.reply("❌ Reply to a video.")
    if not await check_size_and_strike(message, media.file_size, message.from_user.id): return
    
    status = await message.reply(f"⏳ Resizing to {target}p ...")
    user_id = message.from_user.id
    cancelled_tasks.discard(user_id)
    workspace = os.path.join(tempfile.gettempdir(), f"resize_{user_id}_{int(time.time())}")
    os.makedirs(workspace, exist_ok=True)
    
    try:
        in_queue.add(user_id)
        v_path = await download_with_verification(app, media.file_id, status, workspace, user_id, "input.mp4")
        out_path = os.path.join(workspace, f"resized_{target}p.mp4")
        await resize_only(v_path, out_path, target, await get_duration(v_path), status, user_id)
        await safe_edit(status, "📤 Uploading...")
        await app.send_document(chat_id=message.chat.id, document=out_path, caption=f"✅ Resized & Compressed to {target}p")
        await status.delete()
    except Exception as e:
        if "Cancelled" in str(e):
            await safe_edit(status, "🛑 Task Cancelled.")
        elif "Resize failed" not in str(e): 
            await safe_edit(status, f"❌ Error: {str(e)}")
    finally:
        in_queue.discard(user_id)
        cancelled_tasks.discard(user_id)
        shutil.rmtree(workspace, ignore_errors=True)

@app.on_message(filters.command("hsub"))
async def hsub_cmd(client, message: Message):
    if not is_authorized(message): return
    media = message.reply_to_message.video or message.reply_to_message.document if message.reply_to_message else None
    if not media: return await message.reply("❌ Reply to a video.")
    if not await check_size_and_strike(message, media.file_size, message.from_user.id): return
    users_data[message.from_user.id] = {"video": {"file_id": media.file_id, "file_name": media.file_name or "video.mp4"}, "chat_id": message.chat.id, "state": "WAIT_SUB"}
    await message.reply("📄 Send Subtitle (.srt/.ass)", reply_to_message_id=message.id)

@app.on_message(filters.document | filters.video | filters.photo | filters.text)
async def handle_inputs(client, message: Message):
    if not is_authorized(message): return
    uid = message.from_user.id
    if uid not in users_data: return
    state = users_data[uid].get("state")
    
    if state == "WAIT_SUB" and message.document and message.document.file_name.endswith((".srt", ".ass")):
        users_data[uid]["subtitle"] = {"file_id": message.document.file_id, "file_name": message.document.file_name}
        users_data[uid]["state"] = "WAIT_WM_CHOICE"
        await message.reply("Add Watermark?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Yes", callback_data="wm_yes"), InlineKeyboardButton("No", callback_data="wm_skip")]]), reply_to_message_id=message.id)
    
    elif state == "WAIT_WM_PIC" and message.photo:
        users_data[uid]["watermark"] = {"file_id": message.photo.file_id}
        users_data[uid]["state"] = "WAIT_WM_POS"
        await message.reply("Position:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Top-Left", callback_data="pos_TL"), InlineKeyboardButton("Top-Right", callback_data="pos_TR")]]), reply_to_message_id=message.id)
    
    elif state == "WAIT_RENAME_TEXT" and message.text:
        users_data[uid]["video"]["file_name"] = message.text.strip() + ".mp4" if not message.text.endswith(".mp4") else message.text.strip()
        await add_to_queue(uid, message)

@app.on_callback_query()
async def callbacks(client, query: CallbackQuery):
    uid = query.from_user.id
    if uid not in users_data: return await query.answer("Ye aapka task nahi hai!", show_alert=True)
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
    cancelled_tasks.discard(uid)
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
        
        if uid in cancelled_tasks:
            in_queue.discard(uid)
            continue

        status = await app.send_message(chat, "⏳ Starting...")
        workspace = os.path.join(tempfile.gettempdir(), f"task_{uid}_{int(time.time())}")
        os.makedirs(workspace, exist_ok=True)
        
        try:
            await safe_edit(status, "📥 Downloading video...")
            v_path = await download_with_verification(app, v_info["file_id"], status, workspace, uid, "input.mp4")
            
            await safe_edit(status, "📥 Downloading subtitle...")
            s_ext = os.path.splitext(s_info["file_name"])[1]
            s_path = await download_with_verification(app, s_info["file_id"], status, workspace, uid, f"sub{s_ext}")
            
            wm_path = None
            if wm_info: 
                wm_path = await download_with_verification(app, wm_info["file_id"], status, workspace, uid, "wm.jpg")
                
            out_path = os.path.join(workspace, v_info["file_name"])
            
            await safe_edit(status, "🔥 Encoding & Compressing...")
            await encode_with_progress(v_path, s_path, out_path, await get_duration(v_path), status, uid, wm_path, wm_pos)
            
            await safe_edit(status, "📤 Uploading...")
            await app.send_document(chat_id=DEST_CHANNEL or chat, document=out_path, caption=v_info['file_name'])
            await status.delete()
            
        except Exception as e:
            if "Cancelled" in str(e):
                await safe_edit(status, "🛑 Task Cancelled.")
            elif "FFmpeg failed" not in str(e): 
                await safe_edit(status, f"❌ Error: {str(e)}")
        finally:
            in_queue.discard(uid)
            cancelled_tasks.discard(uid)
            shutil.rmtree(workspace, ignore_errors=True)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Running")

async def main():
    if edit != "Maintanence by: @Sub_and_hardsub": return
    await app.start()
    print("Bot started with ULTRAFAST + CRF34 (Max Speed + High Compression)")
    asyncio.create_task(worker())
    await idle()

if __name__ == "__main__":
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever(), daemon=True).start()
    asyncio.get_event_loop().run_until_complete(main())
