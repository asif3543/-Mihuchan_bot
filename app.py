import os
import time
import base64
import threading
import requests
import json
import psutil
import pyrogram.utils
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from http.server import BaseHTTPRequestHandler, HTTPServer

# Safe Pyrogram Bypass Hack
pyrogram.utils.get_peer_type = lambda p: "channel" if str(p).startswith("-100") else "chat" if str(p).startswith("-") else "user"

API_ID_ENV = os.getenv("API_ID", "").strip()
API_ID = int(API_ID_ENV) if API_ID_ENV.isdigit() else 0

API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
REPO_NAME = os.getenv("REPO_NAME", "aasifhusenaasifkhan-beep/tamatar-laal-").strip()

OWNER_ID = 5344078567
ALLOWED_USER = 5351848105
GROUP_ID = -1003899919015
SAFE_CHANNEL_ID = -1003962165512

app = Client("MangaFrontend", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=16)

# Global tracker for workflow dispatch styling selection stage
pending_tasks = {}

def is_authorized(m: Message):
    if not m.from_user:
        return False
    u_id = m.from_user.id
    if m.chat.type == pyrogram.enums.ChatType.PRIVATE:
        return u_id in [OWNER_ID, ALLOWED_USER]
    return u_id in [OWNER_ID, ALLOWED_USER] or (m.chat and m.chat.id == GROUP_ID)

@app.on_message(filters.command("start"))
async def start_cmd(c, m: Message):
    if not is_authorized(m):
        return
    await m.reply(
        "✅ **Deep Annelise Tracker System Live!**\n\n"
        "**One-Go Usage Flow:**\n"
        "1️⃣ Upload Manga / Image (Zip / Image)\n"
        "2️⃣ Reply `/english` or `/hienglish` to it\n"
        "3️⃣ Single merged text file of **ALL pages** will be sent to your **PM**\n"
        "4️⃣ Translate words and upload `.txt` file back within 10 minutes!\n"
        "5️⃣ Bot will automatically compile and Typeset your manga!"
    )

# ====================================================================
# 📝 1. RECEIVER FOR TRANSLATED USER TXT FILES
# ====================================================================
@app.on_message(filters.document)
async def process_user_txt(c, m: Message):
    if not m.document or not m.document.file_name.endswith(".txt"):
        return

    if not is_authorized(m):
        return

    diag = await m.reply("⏳ **Syncing:** Sending translation to background engine database...")
    dl_pointer = await m.download()

    try:
        with open(dl_pointer, "r", encoding="utf-8") as rf:
            c_layer = rf.read()
    except Exception as e:
        if os.path.exists(dl_pointer): os.remove(dl_pointer)
        return await diag.edit(f"❌ **Failed to read file:** `{e}`")

    # Search the first 5 lines of the received file to find and extract the metadata config tag
    lines = c_layer.splitlines()
    task = None
    for line in lines[:5]:
        line = line.strip()
        if line.startswith("#METADATA:"):
            try:
                metadata_b64 = line.split(":", 1)[1].strip()
                metadata_json = base64.b64decode(metadata_b64.encode('utf-8')).decode('utf-8')
                task = json.loads(metadata_json)
                break
            except Exception as e:
                print("Metadata decode error:", e)

    if not task:
        if os.path.exists(dl_pointer): os.remove(dl_pointer)
        return await diag.edit("⚠️ **Metadata Error:** System configuration tag `#METADATA` at the top of the file is missing or corrupted! Please upload the original file without removing the metadata line.")

    u_id = m.from_user.id
    u_base_id = str(u_id)
    if f"{{{u_base_id}}}tutty" not in c_layer:
        if os.path.exists(dl_pointer): os.remove(dl_pointer)
        return await diag.edit("⚠️ **Integrity Error:** Tag structure not found. Keep the tag unharmed!")

    # Write translated file content directly to GitHub Repo
    github_filename = f"trans_{u_base_id}.txt"
    url = f"https://api.github.com/repos/{REPO_NAME}/contents/{github_filename}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

    try:
        r = requests.get(url, headers=headers, timeout=10)
        sha = r.json().get("sha") if r.status_code == 200 else None

        encoded_content = base64.b64encode(c_layer.encode('utf-8')).decode('utf-8')
        payload = {
            "message": f"Upload translation for user {u_base_id}",
            "content": encoded_content
        }
        if sha: payload["sha"] = sha

        res = requests.put(url, headers=headers, json=payload, timeout=10)
        if res.status_code in [200, 201]:
            # Step 2/2 progress bar initiated in group/chat where /english was run
            render_msg = await c.send_message(
                chat_id=task["chat_id"],
                text=(
                    "🎨 **Stage 2/2: Typesetting & Rendering manga...**\n"
                    "Booting Actions engine and applying your translations...\n\n"
                    "`[░░░░░░░░░░] 0% — Initiating Pipeline`"
                )
            )

            # Dispatch workflow with mode: render
            url_dispatch = f"https://api.github.com/repos/{REPO_NAME}/actions/workflows/manga.yml/dispatches"
            render_payload = {
                "ref": "main",
                "inputs": {
                    "file_id": task["file_id"],
                    "chat_id": str(task["chat_id"]),
                    "msg_id": str(render_msg.id),
                    "user_id": str(u_id),
                    "lang": task["lang"],
                    "style": task["style"],
                    "fname": task["fname"],
                    "mode": "render"
                }
            }

            response = requests.post(url_dispatch, headers=headers, json=render_payload, timeout=15)
            if response.status_code in [200, 201, 204]:
                await diag.edit("✅ **Payload Synced!** Render Engine is now initiating Typesettings. Stand-by...")
            else:
                await diag.edit(f"❌ **GitHub Dispatch Fail:** `{response.status_code}`")
        else:
            await diag.edit(f"❌ **GitHub Sync Fail:** `{res.status_code}`")
    except Exception as e:
        await diag.edit(f"❌ **Network Error:** `{e}`")

    try:
        if os.path.exists(dl_pointer): os.remove(dl_pointer)
    except: pass


# ====================================================================
# 🚀 2. INITIATING WORKFLOW TRIGGER
# ====================================================================
@app.on_message(filters.command(["english", "hienglish"]))
async def translate_cmd(c, m: Message):
    if not is_authorized(m): return

    target_msg = m.reply_to_message if m.reply_to_message else m
    if not target_msg.document and not target_msg.photo:
        return await m.reply("❌ **Error:** Please reply to a valid ZIP, PDF, or Image file.")

    lang = "hienglish" if "hienglish" in m.command[0].lower() else "english"
    uid, cid = m.from_user.id, m.chat.id

    try:
        await c.send_chat_action(uid, pyrogram.enums.ChatAction.TYPING)
    except Exception:
        return await m.reply(
            "❌ **PM Blocking Detected!**\n"
            "Please start the bot in private messages (PM) first so I can deliver the extraction text directly to your PM!"
        )

    st = await m.reply("⏳ **Verification:** Transmitting extraction core...")

    try:
        copied = await target_msg.copy(SAFE_CHANNEL_ID)
        file_id = copied.document.file_id if copied.document else copied.photo.file_id
        file_name = copied.document.file_name if copied.document else "image.jpg"
    except Exception as e:
        return await st.edit(f"❌ **Safe Channel Fault:**\nLogs: `{e}`")

    try:
        if m.reply_to_message: await m.reply_to_message.delete()
        await m.delete()
    except: pass

    task_id = f"task_{m.id}"
    pending_tasks[task_id] = {"file_id": file_id, "uid": uid, "cid": cid, "lang": lang, "fname": file_name}

    kb = [
        [InlineKeyboardButton("1️⃣ Standard Clean (Default)", callback_data=f"s_style1_{task_id}")],
        [InlineKeyboardButton("2️⃣ Bold Outline (Action)", callback_data=f"s_style2_{task_id}")],
        [InlineKeyboardButton("3️⃣ Soft Manga (Light)", callback_data=f"s_style3_{task_id}")]
    ]
    await st.edit("🎨 **Step 1: Choose Output Overlay Format**", reply_markup=InlineKeyboardMarkup(kb))


@app.on_callback_query()
async def invoke_dispatch(c, q: CallbackQuery):
    if not q.data.startswith("s_"): return
    _, style, t_id = q.data.split("_", 2)
    if t_id not in pending_tasks:
        return await q.answer("Task Expired System Frame Error!", show_alert=True)

    t = pending_tasks[t_id]
    uid = q.from_user.id

    # Clean up old translation files from GitHub
    github_filename = f"trans_{uid}.txt"
    delete_url = f"https://api.github.com/repos/{REPO_NAME}/contents/{github_filename}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

    try:
        r = requests.get(delete_url, headers=headers, timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha")
            payload = {
                "message": f"Clean up old translation for {uid}",
                "sha": sha
            }
            requests.delete(delete_url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        print("Failed to delete pre-existing file:", e)

    # Trigger Action Workflow for EXTRACT
    url = f"https://api.github.com/repos/{REPO_NAME}/actions/workflows/manga.yml/dispatches"
    payload = {
        "ref": "main",
        "inputs": {
            "file_id": t["file_id"],
            "chat_id": str(t["cid"]),
            "msg_id": str(q.message.id),
            "user_id": str(uid),
            "lang": t["lang"],
            "style": style,
            "fname": t["fname"],
            "mode": "extract"  # Triggering Stage 1
        }
    }

    await q.message.edit(
        "🚀 **Stage 1/2: Booting OCR Engine...**\n"
        "GitHub Actions workspace is starting up...\n\n"
        "`[░░░░░░░░░░] 0% — Initiating Pipeline`"
    )

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code not in [200, 201, 204]:
            await q.message.edit(
                f"❌ **GitHub Trigger Failed:**\n"
                f"Status Code: `{response.status_code}`\n"
                f"Response: `{response.text}`"
            )
    except Exception as e:
        await q.message.edit(f"❌ **Trigger Exception:** `{e}`")

    del pending_tasks[t_id]


def run_http_server():
    # ------------------------------------------------------------------
    # RENDER FIX: Render dynamically assigns the port via the $PORT
    # env var. Hardcoded 7860 se Render "no open port detected" bol kar
    # deploy fail kar dega. 7860 sirf local/HF fallback ke liye rakha hai.
    # ------------------------------------------------------------------
    port = int(os.getenv("PORT", "7860"))

    class SimpleHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Manga Internal Interface Active!")
        def log_message(self, format, *args): return

    try:
        server = HTTPServer(("0.0.0.0", port), SimpleHandler)
        print(f"Dummy HTTP server listening on 0.0.0.0:{port}")
        server.serve_forever()
    except Exception as e:
        print(f"HTTP Server Exception: {e}")

if __name__ == "__main__":
    threading.Thread(target=run_http_server, daemon=True).start()
    app.run()
