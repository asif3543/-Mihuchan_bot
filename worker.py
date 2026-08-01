import os
import sys
import zipfile
import shutil
import asyncio
import time
import base64
import requests
import re
import json
from pyrogram import Client
import pyrogram.utils

# Pyrogram Utils Interception
pyrogram.utils.get_peer_type = lambda p: "channel" if str(p).startswith("-100") else "chat" if str(p).startswith("-") else "user"

FILE_ID = os.getenv("FILE_ID", "").strip()
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
MSG_ID = int(os.getenv("MSG_ID", "0"))
USER_ID = int(os.getenv("USER_ID", "0"))
LANG = os.getenv("LANG", "english").strip().lower()
STYLE = os.getenv("STYLE", "style1").strip()
FNAME = os.getenv("FNAME", "translated_manga.zip").strip()
MODE = os.getenv("MODE", "extract").strip().lower()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
REPO_NAME = os.getenv("REPO_NAME", "aasifhusenaasifkhan-beep/tamatar-laal-").strip()

# System tuning
os.environ["OMP_NUM_THREADS"] = "4"
os.environ["MKL_NUM_THREADS"] = "4"
os.environ["OPENBLAS_NUM_THREADS"] = "4"
os.environ["VECLIB_MAXIMUM_THREADS"] = "4"
os.environ["NUMEXPR_NUM_THREADS"] = "4"

try:
    import torch
    torch.set_num_threads(4)
except ImportError:
    pass

# ============================================================================
ROUTINE_SCRIPT_BYPASSER = r"""
import os
import json
from .common import CommonTranslator

class HumanInterventionTranslator(CommonTranslator):
    supported_src_languages = ['auto', 'ENG', 'JPN', 'CHS', 'CHT', 'KOR', 'FRA', 'DEU', 'RUS', 'SPA', 'ITA', 'POR', 'TRK', 'VIE', 'NLD', 'PLK', 'UKR', 'ARA', 'THA', 'IND', 'FIL']
    supported_target_languages = ['auto', 'ENG', 'JPN', 'CHS', 'CHT', 'KOR', 'FRA', 'DEU', 'RUS', 'SPA', 'ITA', 'POR', 'TRK', 'VIE', 'NLD', 'PLK', 'UKR', 'ARA', 'THA', 'IND', 'FIL']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.translations_map = {}

        mode = os.environ.get("ENV_TRANSLATE_MODE", "EXTRACT")
        if mode == "RENDER":
            json_path = os.environ.get("ENV_TRANSLATIONS_JSON")
            if json_path and os.path.exists(json_path):
                try:
                    with open(json_path, "r", encoding="utf-8") as rf:
                        self.translations_map = json.load(rf)
                except Exception as e:
                    print("Error loading translation JSON in bypasser:", e)

    def supports_languages(self, from_lang, to_lang, fatal=False):
        return True

    async def _translate(self, from_lang, to_lang, queries, *args, **kwargs):
        return await self.do_custom_workflow(queries)

    async def translate(self, from_lang, to_lang, queries, *args, **kwargs):
        return await self.do_custom_workflow(queries)

    async def do_custom_workflow(self, queries):
        if not queries:
            return queries

        mode = os.environ.get("ENV_TRANSLATE_MODE", "EXTRACT")
        current_page = int(os.environ.get("ENV_CURRENT_PAGE_IDX", "1"))

        if mode == "EXTRACT":
            os.makedirs("../manga_workspace", exist_ok=True)
            page_file = f"../manga_workspace/page_{current_page}_queries.txt"
            with open(page_file, "w", encoding="utf-8") as wf:
                wf.write("\n".join(queries))
            return queries

        elif mode == "RENDER":
            translated_layer_dump = []
            for idx, qrs in enumerate(queries, 1):
                key_str = f"{current_page}_{idx}"
                translated_text = self.translations_map.get(key_str, qrs)
                translated_layer_dump.append(translated_text)
            return translated_layer_dump

class ChatGPTTranslator(HumanInterventionTranslator): pass
class ChatGPT2StageTranslator(HumanInterventionTranslator): pass
class GPT3Translator(HumanInterventionTranslator): pass
class GPT35TurboTranslator(HumanInterventionTranslator): pass
class GPT4Translator(HumanInterventionTranslator): pass
"""


async def _run_single_page(cli_cmd, cwd_dir):
    """Runs manga_translator for ONE page and returns (returncode, last_log_lines)."""
    proc = await asyncio.create_subprocess_exec(
        *cli_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=cwd_dir
    )
    log_lines = []
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        log_lines.append(line.decode('utf-8', errors='ignore').strip())
    await proc.wait()
    return proc.returncode, log_lines


async def run_translator_with_fallback(input_dir, output_dir, ws, bot_client, mode):
    cwd_dir = "manga-image-translator" if os.path.exists("manga-image-translator") else None

    os.environ["ENV_USER_ID"] = str(USER_ID)
    os.environ["ENV_BOT_TOKEN"] = str(BOT_TOKEN)
    os.environ["ENV_CHAT_ID"] = str(CHAT_ID)
    os.environ["ENV_MSG_ID"] = str(MSG_ID)

    if cwd_dir:
        core_lib_node = os.path.join(cwd_dir, "manga_translator", "translators", "chatgpt.py")
        if os.path.exists(os.path.dirname(core_lib_node)):
            with open(core_lib_node, "w", encoding="utf-8") as injectn:
                injectn.write(ROUTINE_SCRIPT_BYPASSER)

    style_flags = [
        "--manga2eng",
        "--mask-dilation-offset", "5",
        "--text-threshold", "0.3",
        "--box-threshold", "0.3",
        "--unclip-ratio", "2.5",
    ]

    pages = sorted([
        os.path.join(r, f) for r, _, fs in os.walk(input_dir)
        for f in fs if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp'))
    ])
    total_pages = len(pages)
    if total_pages == 0:
        return False, "Failed", "No pages found to process."

    start_time = time.time()

    # ------------------------------------------------------------------
    # PHASE 1: EXTRACTION
    # ------------------------------------------------------------------
    if mode == "extract":
        os.environ["ENV_TRANSLATE_MODE"] = "EXTRACT"

        for page_idx, page_path in enumerate(pages, 1):
            os.environ["ENV_CURRENT_PAGE_IDX"] = str(page_idx)
            cli_cmd = ["python", "-m", "manga_translator", "-i", page_path,
                       "--dest", output_dir, "--translator", "gpt3", "-l", "FRA"] + style_flags

            returncode, log_lines = await _run_single_page(cli_cmd, cwd_dir)

            if returncode != 0:
                err_log = "\n".join(log_lines[-10:])
                return False, "Failed", (
                    f"OCR failed on page {page_idx} ({os.path.basename(page_path)}), "
                    f"exit code {returncode}.\nLogs:\n{err_log}"
                )

            elapsed = time.time() - start_time
            speed = page_idx / elapsed if elapsed > 0 else 0
            percent = int((page_idx / total_pages) * 100)
            bar = "█" * (percent // 10) + "░" * (10 - (percent // 10))
            status_text = (
                f"🔍 **Stage 1/2: Extracting speech bubbles (OCR)**\n"
                f"Analyzing bubble structures...\n\n"
                f"**Progress:** Page `{page_idx}` of `{total_pages}` finished.\n"
                f"**Speed:** `{speed:.2f} pages/sec` | **Percentage:** `{percent}%`\n"
                f"`[{bar}]`"
            )
            try:
                await bot_client.edit_message_text(CHAT_ID, MSG_ID, status_text)
            except Exception:
                pass

        # Compile master file with metadata header
        master_lines = []
        # ✅ मेटाडेटा में user_id जोड़ा गया
        metadata = {
            "file_id": FILE_ID,
            "chat_id": CHAT_ID,
            "lang": LANG,
            "style": STYLE,
            "fname": FNAME,
            "user_id": USER_ID
        }
        metadata_json = json.dumps(metadata)
        metadata_b64 = base64.b64encode(metadata_json.encode('utf-8')).decode('utf-8')

        master_lines.append(f"#METADATA:{metadata_b64}")
        master_lines.append("# DO NOT EDIT OR DELETE THE FIRST LINE! IT CONTAINS BOT PROCESS SYSTEM CONFIGS.")
        master_lines.append("")

        for i in range(1, total_pages + 1):
            page_file = os.path.join(ws, f"page_{i}_queries.txt")
            if os.path.exists(page_file):
                master_lines.append(f"[Page {i:02d}]")
                with open(page_file, "r", encoding="utf-8") as rf:
                    queries = rf.read().splitlines()
                for idx, q in enumerate(queries, 1):
                    master_lines.append(f"{idx}")
                    master_lines.append(f"{{{USER_ID}}}tutty_{i}_{idx} ==> {q}\n")
                master_lines.append("")

        master_txt_path = os.path.join(ws, f"FrameExtr_{USER_ID}.txt")
        with open(master_txt_path, "w", encoding="utf-8") as wf:
            wf.write("\n".join(master_lines))

        caption_pm = (
            f"📝 **Manga Consolidated Translation File Ready!**\n\n"
            f"**Images Extracted:** `{total_pages}` Pages\n"
            f"**Instructions:**\n"
            f"1️⃣ Translate the dialogues written after the `==>` arrow.\n"
            f"2️⃣ DO NOT alter the `{{{USER_ID}}}tutty` tags, the top `#METADATA` line, or the `==>` arrow.\n"
            f"3️⃣ Send this edited file back to the bot in PM.\n\n"
            f"⏳ **Timeout Alarm:** You have exactly **10 minutes** to translate and return this file!"
        )

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
        try:
            with open(master_txt_path, 'rb') as doc:
                requests.post(
                    url,
                    data={'chat_id': USER_ID, 'caption': caption_pm, 'parse_mode': 'Markdown'},
                    files={'document': doc},
                    timeout=15
                )
        except Exception as e:
            print("Failed to deliver document via HTTP:", e)

        return True, "ExtractSuccess", ""

    # ------------------------------------------------------------------
    # PHASE 2: RENDERING
    # ------------------------------------------------------------------
    elif mode == "render":
        os.environ["ENV_TRANSLATE_MODE"] = "RENDER"

        trans_file_path = f"trans_{USER_ID}.txt"
        translations = {}
        if os.path.exists(trans_file_path):
            with open(trans_file_path, "r", encoding="utf-8") as rf:
                txt_val = rf.read()
            pattern = r"\{(\d+)\}tutty_(\d+)_(\d+) ==> (.*)"
            for line in txt_val.splitlines():
                line = line.strip()
                match = re.search(pattern, line)
                if match:
                    _, p_idx, b_idx, text = match.groups()
                    translations[f"{p_idx}_{b_idx}"] = text.strip()

            translations_json_path = os.path.join(ws, "translations.json")
            with open(translations_json_path, "w", encoding="utf-8") as wf:
                json.dump(translations, wf, ensure_ascii=False, indent=4)

            os.environ["ENV_TRANSLATIONS_JSON"] = translations_json_path
        else:
            return False, "Failed", f"Missing translation file: {trans_file_path}"

        for page_idx, page_path in enumerate(pages, 1):
            os.environ["ENV_CURRENT_PAGE_IDX"] = str(page_idx)
            cli_cmd = ["python", "-m", "manga_translator", "-i", page_path,
                       "--dest", output_dir, "--translator", "gpt3", "-l", "FRA"] + style_flags

            returncode, log_lines = await _run_single_page(cli_cmd, cwd_dir)

            if returncode != 0:
                err_log2 = "\n".join(log_lines[-10:])
                return False, "Failed", (
                    f"Render failed on page {page_idx} ({os.path.basename(page_path)}), "
                    f"exit code {returncode}.\nLogs:\n{err_log2}"
                )

            elapsed_render = time.time() - start_time
            speed_render = page_idx / elapsed_render if elapsed_render > 0 else 0
            percent_render = int((page_idx / total_pages) * 100)
            bar_render = "█" * (percent_render // 10) + "░" * (10 - (percent_render // 10))

            render_text = (
                f"🎨 **Stage 2/2: Rendering completed typesetting**\n"
                f"Erasing bubbles and adjusting fonts...\n\n"
                f"**Render Progress:** Page `{page_idx}` of `{total_pages}` finished.\n"
                f"**Speed:** `{speed_render:.2f} pages/sec` | **Percentage:** `{percent_render}%`\n"
                f"`[{bar_render}]`"
            )
            try:
                await bot_client.edit_message_text(CHAT_ID, MSG_ID, render_text)
            except Exception:
                pass

        cnt_results = 0
        if os.path.exists(output_dir):
            base_results = [
                f for r, _, fx in os.walk(output_dir)
                for f in fx if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))
            ]
            cnt_results = len(base_results)

        if cnt_results > 0:
            return True, "RenderSuccess", ""
        return False, "Failed", "Rendering failed to compile images."

    return False, "InvalidMode", ""


async def main():
    if not FILE_ID:
        print("Empty File Matrix ID Found")
        return

    tg_bot = Client("WorkerMaster", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, no_updates=True)
    await tg_bot.start()

    def get_progress_bar(percent, status):
        filled_length = int(percent // 10)
        bar = "█" * filled_length + "░" * (10 - filled_length)
        return f"⚡ **Status:** {status}\n`[{bar}] {percent}%`"

    await tg_bot.edit_message_text(CHAT_ID, MSG_ID, get_progress_bar(10, f"Target pull sequence initiated ({MODE.upper()})..."))

    dl_path = None
    for attempt in range(1, 6):
        try:
            dl_path = await tg_bot.download_media(FILE_ID)
            if dl_path and os.path.exists(dl_path) and os.path.getsize(dl_path) > 1024:
                break
            await asyncio.sleep(2)
        except Exception as e:
            await tg_bot.edit_message_text(CHAT_ID, MSG_ID, f"⚠️ **Network Dropout {attempt}/5:** `{e}`")
            await asyncio.sleep(3)

    if not dl_path or not os.path.exists(dl_path):
        await tg_bot.edit_message_text(CHAT_ID, MSG_ID, "❌ **Critical Error:** Failed to download manga file from Telegram servers.")
        return await tg_bot.stop()

    await tg_bot.edit_message_text(CHAT_ID, MSG_ID, get_progress_bar(20, "Extracting layouts & images..."))

    ext = os.path.splitext(FNAME)[1].lower() or ".zip"
    ws = os.path.abspath("manga_workspace")
    inp = os.path.join(ws, "input")
    out = os.path.join(ws, "output")

    if os.path.exists(ws):
        shutil.rmtree(ws)
    os.makedirs(inp, exist_ok=True)
    os.makedirs(out, exist_ok=True)

    try:
        if ext in [".zip", ".cbz"]:
            with zipfile.ZipFile(dl_path, 'r') as z:
                z.extractall(inp)
        elif ext == ".pdf":
            import fitz
            pdf_layer = fitz.open(dl_path)
            for znc_n in range(len(pdf_layer)):
                pdf_pg = pdf_layer.load_page(znc_n)
                pdf_pg.get_pixmap(dpi=150).save(os.path.join(inp, f"page_{znc_n:03d}.png"))
            pdf_layer.close()
        else:
            shutil.copy(dl_path, inp)
    except Exception as e:
        await tg_bot.edit_message_text(CHAT_ID, MSG_ID, f"❌ **Extraction Failed:** `{e}`")
        return await tg_bot.stop()

    # ------------------------------------------------------------------
    # SPEED FIX: bade scan pages ko downscale karo
    # ------------------------------------------------------------------
    try:
        from PIL import Image
        MAX_WIDTH = 1600
        for r, _, fs in os.walk(inp):
            for f in fs:
                if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp')):
                    fp = os.path.join(r, f)
                    try:
                        with Image.open(fp) as im:
                            if im.width > MAX_WIDTH:
                                ratio = MAX_WIDTH / im.width
                                new_size = (MAX_WIDTH, int(im.height * ratio))
                                im = im.convert("RGB") if im.mode in ("P", "RGBA") else im
                                im = im.resize(new_size, Image.LANCZOS)
                                im.save(fp)
                    except Exception as resize_err:
                        print(f"Resize skip for {fp}: {resize_err}")
    except ImportError:
        pass

    pages = [
        os.path.join(r, f) for r, _, fs in os.walk(inp)
        for f in fs if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp'))
    ]
    if not pages:
        await tg_bot.edit_message_text(CHAT_ID, MSG_ID, "❌ **Formatting Error:** No supported images found in your document.")
        return await tg_bot.stop()

    success_bool, prvd_ui, full_core_log = await run_translator_with_fallback(inp, out, ws, tg_bot, MODE)

    if not success_bool:
        err_out = f"❌ **FATAL SYSTEM FAIL:** Processes crashed.\n\n`{full_core_log[-450:]}`"
        await tg_bot.edit_message_text(CHAT_ID, MSG_ID, err_out)
        return await tg_bot.stop()

    # Stops worker once Stage 1 completes
    if MODE == "extract":
        try:
            await tg_bot.edit_message_text(
                CHAT_ID, MSG_ID,
                "📝 **Stage 1/2 Complete: Speech Bubbles Extracted!**\n\n"
                "Consolidated subtitle file has been sent directly to your **PM**.\n"
                "Please translate the file and send it back to the bot to execute typesetting!"
            )
        except Exception:
            pass
        shutil.rmtree(ws, ignore_errors=True)
        try:
            os.remove(dl_path)
        except Exception:
            pass
        await tg_bot.stop()
        return

    # Phase 3 packaging (Triggered in "render" mode)
    await tg_bot.edit_message_text(CHAT_ID, MSG_ID, get_progress_bar(90, "Rebuilding completed typesetting output..."))

    try:
        finals_l = sorted([
            os.path.join(r, f) for r, _, fs in os.walk(out)
            for f in fs if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))
        ])
        if not finals_l:
            raise ValueError("Rendering phase compiled but no output images found.")

        # ✅ हमेशा ZIP बनाएँ, इमेज के नाम 01.png, 02.png...
        zipx_out = f"translated_{os.path.splitext(FNAME)[0]}.zip"
        with zipfile.ZipFile(zipx_out, 'w', zipfile.ZIP_DEFLATED) as z:
            for idx, img_path in enumerate(finals_l, 1):
                new_name = f"{idx:02d}{os.path.splitext(img_path)[1]}"
                z.write(img_path, new_name)

        endcap_caption = "✅ **Processing Repacked Successfully!**\n⚡ Control Type: Manual Human Output Render Logic MTPE"

        await tg_bot.send_document(CHAT_ID, zipx_out, caption=endcap_caption)
        await tg_bot.delete_messages(CHAT_ID, MSG_ID)

    except Exception as e:
        err_msg = f"❌ **Rebuild Error at 90%:** `{e}`"
        await tg_bot.edit_message_text(CHAT_ID, MSG_ID, err_msg)

    shutil.rmtree(ws, ignore_errors=True)
    try:
        os.remove(dl_path)
    except Exception:
        pass
    try:
        os.remove(zipx_out)
    except Exception:
        pass
    await tg_bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
