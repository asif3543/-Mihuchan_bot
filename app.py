import os, re, time, asyncio, threading, hashlib, requests, psutil
from pyrogram import Client, filters, idle
from pyrogram.types import Message, CallbackQuery
from pyrogram.enums import ChatType
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------- Environment ----------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
REPO_NAME = os.getenv("REPO_NAME", "").strip()                 # "owner/repo"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()     # branch that has .github/workflows/encode.yml
MAX_RUNS = int(os.getenv("MAX_RUNS", "2"))                     # how many tasks may run on GitHub at the same time
MAX_PER_USER = int(os.getenv("MAX_PER_USER", "2"))             # running + waiting tasks allowed per user (owners: unlimited)
PORT = int(os.getenv("PORT", "8080"))

OWNER_ID = 5344078567
ALLOWED_USER = 5351848105
GROUP_ID = -1003899919015
OWNERS = (OWNER_ID, ALLOWED_USER)

WORKFLOW_FILE = "encode.yml"
GH_API = "https://api.github.com"

# ---------------- Command names (change them here, nowhere else) ----------------
C_START = "start"
C_HELP = "guide"
C_STATS = "status"
C_STOP = "stop"                 # cancel a task (reply to its status message / video)
C_ADDFONT = "setfont"
C_RMFONT = "delfont"
C_HARDSUB = "burnsub"
C_ADDAUDIO = "addaudio"
C_EXTRACT = "audioextract"
RES_CMD_MAP = {"c1080": "1080p", "c720": "720p", "c480": "480p"}   # compress commands

AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".ac3", ".eac3", ".mka", ".wma", ".dts")
SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".txt")
SESSION_TTL = 600               # seconds a setup (sub/audio/rename step) may stay idle

app = Client("HarsubBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=8)

users_data = {}                 # setup conversations: user_id -> session
tasks = {}                      # task_key -> task (queued / dispatching / dispatched)
task_queue = asyncio.Queue()    # holds task keys


def gh_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}


def file_name_of(media):
    # Telegram videos often have file_name = None -> GitHub rejects null inputs (HTTP 422)
    return getattr(media, "file_name", None) or "output.mp4"


def tg_link(chat_id, msg_id):
    return f"https://t.me/c/{str(chat_id)[4:]}/{msg_id}"   # only the last number (message id) is used by the worker


def make_key(task_type, chat_id, video_msg_id):
    """Same video + same task type => same key. Used for duplicate detection and for finding
    the GitHub run again (the key is part of the run name)."""
    return hashlib.sha1(f"{task_type}:{chat_id}:{video_msg_id}".encode()).hexdigest()[:8]


def is_authorized(m: Message):
    if not m.from_user:
        return False
    if m.from_user.id in OWNERS:
        return True
    return bool(m.chat and m.chat.id == GROUP_ID)


async def check_command_privacy(c, m: Message):
    is_pm = m.chat.type == ChatType.PRIVATE
    if is_pm and m.from_user.id in OWNERS:
        return True
    if is_pm:
        try:
            chat_info = await c.get_chat(GROUP_ID)
            invite_link = chat_info.invite_link or "https://t.me/Mangajii"
        except Exception:
            invite_link = "https://t.me/Mangajii"
        await m.reply(f"❌ **Aap is Bot ko Private mein use nahi kar sakte!**\n\n👉 Humara [Official Group]({invite_link}) join karein.", disable_web_page_preview=True)
        return False
    return is_authorized(m)


# ---------------- GitHub Actions ----------------
def _gh_runs_sync():
    """Running + waiting runs of encode.yml -> [{'id', 'title'}]. None if the GitHub API failed.
    The run title is '<task_type> | <task_key>' (see run-name in the workflow)."""
    runs = []
    for status in ("in_progress", "queued"):
        url = f"{GH_API}/repos/{REPO_NAME}/actions/workflows/{WORKFLOW_FILE}/runs?status={status}&per_page=100"
        r = requests.get(url, headers=gh_headers(), timeout=15)
        if r.status_code != 200:
            print(f"GitHub runs API {r.status_code}: {r.text[:200]}")
            return None
        for w in r.json().get("workflow_runs", []):
            runs.append({"id": w["id"], "title": w.get("display_title") or w.get("name") or ""})
    return runs


async def gh_runs():
    try:
        return await asyncio.to_thread(_gh_runs_sync)
    except Exception as e:
        print(f"API Error: {e}")
        return None


def key_in_title(key, title):
    return f"| {key}" in title


def key_of_title(title):
    return title.split("|")[-1].strip() if "|" in title else ""


def effective_count(runs):
    """Runs GitHub shows + tasks we dispatched a moment ago that GitHub does not list yet."""
    now = time.time()
    fresh = sum(1 for k, t in tasks.items()
                if t["status"] == "dispatched" and now - t["dispatched_at"] < 30
                and not any(key_in_title(k, r["title"]) for r in runs))
    return len(runs) + fresh


async def active_runs():
    runs = await gh_runs()
    return None if runs is None else effective_count(runs)


async def is_server_busy():
    n = await active_runs()
    return n is not None and n >= MAX_RUNS


async def refresh_tasks():
    """Forget tasks whose GitHub run has finished (so duplicates / user limits free up)."""
    runs = await gh_runs()
    if runs is None:
        return
    now = time.time()
    for key, t in list(tasks.items()):
        if t["status"] == "dispatched" and now - t["dispatched_at"] > 90 \
                and not any(key_in_title(key, r["title"]) for r in runs):
            tasks.pop(key, None)


def user_pending(user_id):
    return sum(1 for t in tasks.values() if t["user_id"] == user_id)


def _dispatch_worker(payload):
    url = f"{GH_API}/repos/{REPO_NAME}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    inputs = {k: str(v) for k, v in payload.items()}   # workflow_dispatch inputs must all be strings
    try:
        r = requests.post(url, headers=gh_headers(), json={"ref": GITHUB_BRANCH, "inputs": inputs}, timeout=15)
        return (True, "Success") if r.status_code == 204 else (False, f"Code {r.status_code}: {r.text}")
    except Exception as e:
        return False, str(e)


async def trigger_dispatch(payload):
    return await asyncio.to_thread(_dispatch_worker, payload)


async def _cancel_dispatched(key, t):
    """Cancel the GitHub run of a dispatched task. The run may need a few seconds to show up."""
    result = "gone"
    for _ in range(18):                                   # ~90 s
        runs = await gh_runs()
        run = next((r for r in (runs or []) if key_in_title(key, r["title"])), None)
        if run:
            try:
                r = await asyncio.to_thread(
                    requests.post, f"{GH_API}/repos/{REPO_NAME}/actions/runs/{run['id']}/cancel",
                    headers=gh_headers(), timeout=15)
                result = "run" if r.status_code == 202 else "gone"
            except Exception as e:
                print(f"Cancel error: {e}")
            break
        if runs is not None and time.time() - t["dispatched_at"] > 60:
            break                                          # never appeared -> already finished
        await asyncio.sleep(5)
    tasks.pop(key, None)
    return result


async def cancel_task(key):
    """Returns 'queue' | 'run' | 'gone' | 'none'."""
    t = tasks.get(key)
    if not t:
        return "none"
    t["cancelled"] = True
    if t["status"] == "queued":
        tasks.pop(key, None)                               # worker skips it
        return "queue"
    if t["status"] == "dispatching":
        return "queue"                                     # worker cancels the run right after dispatching
    return await _cancel_dispatched(key, t)


async def queue_worker():
    while True:
        key = await task_queue.get()
        try:
            t = tasks.get(key)
            if not t or t["cancelled"]:
                continue
            while not t["cancelled"]:
                n = await active_runs()
                if n is not None and n >= MAX_RUNS:
                    await asyncio.sleep(10)
                    continue
                break
            if t["cancelled"]:
                tasks.pop(key, None)
                continue
            t["status"] = "dispatching"
            ok, msg = await trigger_dispatch(t["payload"])
            if ok:
                t["status"] = "dispatched"
                t["dispatched_at"] = time.time()
                if t["cancelled"]:                         # /stop came while we were dispatching
                    await _cancel_dispatched(key, t)
                else:
                    # give GitHub a moment to register the run, otherwise the next
                    # check would not see it and we could start too many at once
                    await asyncio.sleep(6)
            else:
                print(f"Dispatch Error: {msg}")
                tasks.pop(key, None)
                try:
                    await app.edit_message_text(
                        t["chat_id"], t["status_msg_id"],
                        f"❌ **Task start nahi ho paya.**\n`{msg[:300]}`")
                except Exception:
                    pass
        except Exception as e:
            print(f"Worker error: {e}")
        finally:
            task_queue.task_done()


async def get_pinned_file_link(chat_id, target_name):
    try:
        chat = await app.get_chat(chat_id)
        if chat.pinned_message and chat.pinned_message.text and f"Name – {target_name}" in chat.pinned_message.text:
            match = re.search(r"Link – (https://\S+)", chat.pinned_message.text)
            if match:
                return match.group(1)
        async for msg in app.get_chat_history(chat_id, limit=50):
            if msg.text and f"Name – {target_name}" in msg.text:
                match = re.search(r"Link – (https://\S+)", msg.text)
                if match:
                    return match.group(1)
    except Exception:
        pass
    return "none"


# ---------------- Submit a task (duplicate check + per-user limit + queue) ----------------
DUP_TEXT = ("⚠️ **Ye task pehle se queue/process me hai.**\n"
            f"Rokne ke liye uske status message par reply karke `/{C_STOP}` likhein.")


async def reject_if_blocked(msg: Message, user_id, key):
    """Returns True (and tells the user) if this task must not be started."""
    if key in tasks:
        await msg.reply(DUP_TEXT)
        return True
    if user_id not in OWNERS and user_pending(user_id) >= MAX_PER_USER:
        await msg.reply(f"⚠️ Aapke {MAX_PER_USER} task pehle se chal rahe / queue me hain. "
                        f"Khatam hone ka wait karein ya `/{C_STOP}` se roken.")
        return True
    return False


async def submit_task(msg: Message, user_id, chat_id, task_type, video_msg_id,
                      extra_id="none", rename="none", resolution="none"):
    await refresh_tasks()
    key = make_key(task_type, chat_id, video_msg_id)
    if await reject_if_blocked(msg, user_id, key):
        return

    # reserve the slot right away (no await between the check above and this line)
    t = {"key": key, "status": "queued", "user_id": user_id, "chat_id": chat_id,
         "video_msg_id": video_msg_id, "status_msg_id": None, "payload": None,
         "dispatched_at": 0, "cancelled": False}
    tasks[key] = t
    try:
        busy = task_queue.qsize() > 0 or await is_server_busy()
        st = await msg.reply("⏳ **Task Queued!**\nServer busy hain, aapka task queue me lag gaya hai aur turn aane par automatic start hoga."
                             if busy else "⏳ **Task Dispatched to Cloud Processing Node...**")
        t["status_msg_id"] = st.id
        font_link = await get_pinned_file_link(chat_id, "file") if task_type == "hardsub" else "none"
        t["payload"] = {
            "task_type": task_type, "video_id": tg_link(chat_id, video_msg_id), "extra_id": extra_id,
            "rename": rename, "chat_id": str(chat_id), "user_id": str(user_id), "resolution": resolution,
            "font_link": font_link, "trigger_msg_id": str(st.id), "task_key": key,
        }
    except Exception as e:
        print(f"Submit error: {e}")
        if tasks.get(key) is t:
            tasks.pop(key, None)
        return
    if t["cancelled"] or tasks.get(key) is not t:
        return                                             # stopped while we were still preparing
    await task_queue.put(key)


# ---------------- Commands ----------------
@app.on_message(filters.command([C_START, C_HELP, C_STATS, C_ADDFONT, C_RMFONT]))
async def general_cmds(c, m: Message):
    cmd = m.command[0].lower()
    if cmd == C_START and m.chat.type == ChatType.PRIVATE:
        if m.from_user.id in OWNERS:
            return await m.reply("🙋‍♂️ Welcome Master!")
        return await check_command_privacy(c, m)
    if not await check_command_privacy(c, m):
        return

    if cmd == C_HELP:
        await m.reply(
            "🤖 **Bot Usage Guide:**\n\n"
            f"1️⃣ **Compress:** Video par reply karein `/c1080`, `/c720` ya `/c480`\n"
            f"2️⃣ **Hardsub:** Video par reply karein `/{C_HARDSUB}` aur subtitle send karein.\n"
            f"3️⃣ **Audio badlo:** Video par reply karein `/{C_ADDAUDIO}` aur nayi audio send karein "
            "(purani audio hat jayegi).\n"
            f"4️⃣ **Audio alag karo:** Video par reply karein `/{C_EXTRACT}` — audio aur bina-audio video alag milenge.\n"
            f"5️⃣ **Custom Font:** Font file par reply karke `/{C_ADDFONT}` (hatane ke liye `/{C_RMFONT}`)\n"
            f"6️⃣ **Task rokna:** Uske status message (ya video) par reply karke `/{C_STOP}`. "
            "Setup beech me chhodna ho to bhi `/stop`.")

    elif cmd == C_STATS:
        await refresh_tasks()
        ram = psutil.virtual_memory()
        runs = await active_runs()
        waiting = sum(1 for t in tasks.values() if t["status"] in ("queued", "dispatching"))
        await m.reply(
            "📊 **System Status:**\n"
            f"🖥️ CPU: `{psutil.cpu_percent()}%`  💾 RAM: `{ram.percent}%`\n"
            f"⚙️ Processing now: `{'?' if runs is None else runs}` / `{MAX_RUNS}`\n"
            f"📂 Waiting in queue: `{waiting}`")

    elif cmd == C_ADDFONT:
        if not m.reply_to_message or not (m.reply_to_message.photo or m.reply_to_message.document):
            return await m.reply("❌ File par reply karein.")
        msg_link = tg_link(m.chat.id, m.reply_to_message.id)
        pinned = await m.reply(f"ID – {m.from_user.id}\nLink – {msg_link}\nName – file")
        try:
            await pinned.pin()
        except Exception:
            pass
        await m.reply("✅ Configuration saved successfully.")

    elif cmd == C_RMFONT:
        chat = await c.get_chat(m.chat.id)
        if chat.pinned_message and "Name – file" in (chat.pinned_message.text or ""):
            await chat.pinned_message.unpin()
            await m.reply("🗑️ Config removed.")
        else:
            await m.reply("❌ Config nahi mila.")


def _replied_media(m: Message):
    r = m.reply_to_message
    return (r.video or r.document or r.animation) if r else None


@app.on_message(filters.command(list(RES_CMD_MAP)))
async def compress_cmd(c, m: Message):
    if not await check_command_privacy(c, m):
        return
    media = _replied_media(m)
    if not media:
        return await m.reply("❌ Kisi valid video par reply karein.")
    await submit_task(m, m.from_user.id, m.chat.id, "compress", m.reply_to_message.id,
                      rename=file_name_of(media), resolution=RES_CMD_MAP[m.command[0].lower()])


@app.on_message(filters.command(C_EXTRACT))
async def extract_cmd(c, m: Message):
    if not await check_command_privacy(c, m):
        return
    media = _replied_media(m)
    if not media:
        return await m.reply("❌ Kisi valid video par reply karein.")
    await submit_task(m, m.from_user.id, m.chat.id, "audioextract", m.reply_to_message.id,
                      rename=file_name_of(media))


async def begin_session(c, m: Message, kind, prompt):
    if not await check_command_privacy(c, m):
        return
    media = _replied_media(m)
    if not media:
        return await m.reply("❌ Pehle kisi video par reply karke command likhein.")
    await refresh_tasks()
    if await reject_if_blocked(m, m.from_user.id, make_key(kind, m.chat.id, m.reply_to_message.id)):
        return                                             # don't make the user go through the steps for nothing
    await m.reply(prompt)
    users_data[m.from_user.id] = {
        "kind": kind, "video_msg_id": m.reply_to_message.id, "chat_id": m.chat.id,
        "state": "WAIT_SUB" if kind == "hardsub" else "WAIT_AUDIO",
        "rename": "none", "orig_name": file_name_of(media), "extra_link": "none", "ts": time.time()}


@app.on_message(filters.command(C_HARDSUB))
async def hsub_cmd(c, m: Message):
    await begin_session(c, m, "hardsub", "Send subtitle file (.srt, .ass, .vtt) ya skip karne ke liye `S` type karein.")


@app.on_message(filters.command(C_ADDAUDIO))
async def addaudio_cmd(c, m: Message):
    await begin_session(c, m, "addaudio", "🎧 Nayi audio send karein (audio file / voice note).\n"
                                          "Video ki purani audio hat jayegi.")


# ---------------- Stop / cancel (runs here on Render, it talks to the GitHub API) ----------------
@app.on_message(filters.command(C_STOP))
async def stop_cmd(c, m: Message):
    if not await check_command_privacy(c, m):
        return
    uid, chat_id = m.from_user.id, m.chat.id
    had_setup = users_data.pop(uid, None) is not None
    await refresh_tasks()

    r = m.reply_to_message
    if r:
        match = [(k, t) for k, t in tasks.items()
                 if t["chat_id"] == chat_id and r.id in (t["status_msg_id"], t["video_msg_id"])]
        if not match:
            return await m.reply("✅ Setup cancel kar diya gaya." if had_setup
                                 else "❌ Is message ka koi active task nahi mila.")
        key, t = match[0]
        if uid not in OWNERS and t["user_id"] != uid:
            return await m.reply("❌ Ye task aapka nahi hai.")
    else:
        mine = [(k, t) for k, t in tasks.items()
                if t["chat_id"] == chat_id and (uid in OWNERS or t["user_id"] == uid)]
        if not mine:
            return await m.reply("✅ Setup cancel kar diya gaya." if had_setup
                                 else "❌ Koi active task ya setup nahi mila.")
        if len(mine) > 1:
            return await m.reply(f"Ek se zyada task chal rahe hain. Jisko rokna hai uske status message par reply karke `/{C_STOP}` likhein.")
        key, t = mine[0]

    status_id = t["status_msg_id"]
    res = await cancel_task(key)
    if res == "none":
        return await m.reply("❌ Task pehle hi khatam ho chuka hai.")
    if status_id:
        try:
            await c.edit_message_text(chat_id, status_id, "🛑 **Task cancelled.**")
        except Exception:
            pass
    await m.reply("🛑 Task queue se hata diya gaya." if res == "queue" else "🛑 Task cancel kar diya gaya.")


# ---------------- Setup conversation (subtitle / audio / rename) ----------------
def _is_audio_msg(m: Message):
    if m.audio or m.voice:
        return True
    d = m.document
    if d and ((d.mime_type or "").startswith("audio/") or (d.file_name or "").lower().endswith(AUDIO_EXTS)):
        return True
    return bool(m.video)                                   # use the audio of another video


@app.on_message(filters.text | filters.document | filters.audio | filters.voice | filters.video)
async def replies_controller(c, m: Message):
    if not m.from_user or (m.text and m.text.startswith("/")):
        return
    user_id = m.from_user.id
    session = users_data.get(user_id)
    if not session or session["chat_id"] != m.chat.id:
        return
    if time.time() - session["ts"] > SESSION_TTL:
        users_data.pop(user_id, None)
        return await m.reply("⌛ Setup time out ho gaya. Command dobara chalayein.")
    session["ts"] = time.time()

    state = session.get("state")
    text = m.text.strip().upper() if m.text else ""
    rename_prompt = "Rename ke liye `R` / Same name ke liye `S` type karein."

    if state == "WAIT_SUB":
        if m.document and m.document.file_name and m.document.file_name.lower().endswith(SUB_EXTS):
            session["extra_link"] = tg_link(m.chat.id, m.id)
            session["state"] = "WAIT_RENAME_CHOICE"
            await m.reply(rename_prompt)
        elif text == "S":
            session["extra_link"] = "none"
            session["state"] = "WAIT_RENAME_CHOICE"
            await m.reply(rename_prompt)
        else:
            await m.reply("❌ Invalid format! Please send a valid subtitle file ya type `S`.")

    elif state == "WAIT_AUDIO":
        if _is_audio_msg(m):
            session["extra_link"] = tg_link(m.chat.id, m.id)
            session["state"] = "WAIT_RENAME_CHOICE"
            await m.reply(rename_prompt)
        else:
            await m.reply(f"❌ Audio file ya voice note bhejein (cancel: `/{C_STOP}`).")

    elif state == "WAIT_RENAME_CHOICE":
        if text == "R":
            session["state"] = "WAIT_RENAME_VALUE"
            await m.reply("Send new file name:")
        elif text == "S":
            session["rename"] = session["orig_name"]
            await execute_dispatch(user_id, m)
        else:
            await m.reply("❌ Type `R` to rename ya `S` to skip.")

    elif state == "WAIT_RENAME_VALUE":
        if not m.text or not m.text.strip():
            return await m.reply("❌ Invalid name.")
        raw_name = m.text.strip()
        if raw_name.lower().endswith(".mp4"):
            raw_name = raw_name[:-4]
        clean_name = re.sub(r'[\\/:*?"<>|]', '', raw_name).strip()
        if not clean_name:
            return await m.reply("❌ Invalid name.")
        session["rename"] = clean_name + ".mp4"
        await execute_dispatch(user_id, m)


async def execute_dispatch(user_id, msg: Message):
    data = users_data.pop(user_id, None)
    if not data:
        return
    await submit_task(msg, user_id, data["chat_id"], data["kind"], data["video_msg_id"],
                      extra_id=data.get("extra_link", "none"), rename=data.get("rename", "none"))


# ---------------- Cancel button (cancels ONLY that task's run) ----------------
@app.on_callback_query(filters.regex(r"^cancel_(\d+)_(\d+)$"))
async def cancel_run_callback(c, q: CallbackQuery):
    run_id, owner_id = q.matches[0].group(1), q.matches[0].group(2)
    if q.from_user.id not in OWNERS and str(q.from_user.id) != owner_id:
        return await q.answer("❌ Ye task aapka nahi hai.", show_alert=True)
    try:
        r = await asyncio.to_thread(
            requests.post, f"{GH_API}/repos/{REPO_NAME}/actions/runs/{run_id}/cancel",
            headers=gh_headers(), timeout=15)
        if r.status_code == 202:
            runs = await gh_runs()                         # free the duplicate / user-limit slot right away
            for run in runs or []:
                if str(run["id"]) == run_id:
                    tasks.pop(key_of_title(run["title"]), None)
            await q.message.edit("🛑 **Process Cancelled Successfully!**")
            await q.answer("Task Aborted", show_alert=True)
        else:
            await q.answer("Task pehle hi khatam ho chuka hai.", show_alert=True)
    except Exception as e:
        await q.answer(f"Abort Exception: {e}", show_alert=True)


@app.on_callback_query(filters.regex(r"^cancel_active_run$"))
async def old_cancel_button(c, q: CallbackQuery):
    await q.answer(f"Ye purana button hai. Task rokne ke liye status message par reply karke /{C_STOP} likhein.", show_alert=True)


# ---------------- Render: health server + keep-alive ----------------
class Health(BaseHTTPRequestHandler):
    def _ok(self, body=True):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if body:
            self.wfile.write(b"Server Active & Running")

    def do_GET(self):
        self._ok()

    def do_HEAD(self):        # UptimeRobot & co. often use HEAD
        self._ok(body=False)

    def log_message(self, *a):
        pass


async def keep_alive():
    """Render's free plan sleeps a web service after ~15 min without web traffic.
    RENDER_EXTERNAL_URL is set by Render itself; we ping it every 10 minutes."""
    url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if not url:
        return
    while True:
        await asyncio.sleep(600)
        try:
            await asyncio.to_thread(requests.get, url, timeout=20)
        except Exception:
            pass


async def main():
    threading.Thread(target=lambda: ThreadingHTTPServer(("0.0.0.0", PORT), Health).serve_forever(), daemon=True).start()
    print(f"📡 Web server running on port {PORT}")

    await app.start()
    print("🚀 Bot Client Connected Successfully!")
    asyncio.create_task(queue_worker())
    asyncio.create_task(keep_alive())
    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
