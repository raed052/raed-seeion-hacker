# bot_sessions_telethon_v4.py
# نسخة معدّلة: إدارة بروكسيات لكل 10 جلسات + إعادة عرض الأجهزة بعد الخروج
import asyncio
import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import socks
from telethon import TelegramClient, errors, events
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from telethon.tl.functions.auth import LogOutRequest

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from config import Config

# --- إعدادات ومسارات ---
SESSIONS_DIR = Path("uploaded_sessions")
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

PROXIES_FILE = Path("/mnt/data/Webshare 10 proxies.txt")  # هذا هو ملف البروكسيات الذي رفعته. يمكنك تغييره.
PROXIES_STATE_PATH = Path("proxies_state.json")

MAX_DEVICES = 20
WATCH_CODE_TIMEOUT = 300
PROTECTED_DEVICE_NAME = "Samsung Galaxy M12"

API_ID = Config.API_ID
API_HASH = Config.API_HASH
BOT_TOKEN = Config.TOKEN
START_PIC = getattr(Config, "START_PIC", None)

# --- حالة الذاكرة ---
SESSIONS: Dict[int, Dict[str, Any]] = {}   # owner_id -> { client, session_path, ... }
CREATE_FLOW: Dict[int, Dict[str, Any]] = {}

# --- مدير البروكسيات ---
class ProxyManager:
    def __init__(self, proxies_file: Path, state_file: Path, max_uses: int = 10):
        self.proxies_file = proxies_file
        self.state_file = state_file
        self.max_uses = max_uses
        self.proxies: List[Dict[str, Any]] = []  # كل عنصر: {host, port, user, pass, uses}
        self._load()

    def _load(self):
        # load proxies from file
        if self.proxies_file.exists():
            lines = [l.strip() for l in self.proxies_file.read_text().splitlines() if l.strip()]
            parsed = []
            for L in lines:
                parts = L.split(":")
                # توقع الشكل host:port:user:pass
                if len(parts) >= 4:
                    host, port, user, pwd = parts[0], parts[1], parts[2], ":".join(parts[3:])
                    parsed.append({"host": host, "port": int(port), "user": user, "pass": pwd})
                else:
                    # لو السطر مختلف، حاول تفكيكه بذكاء
                    continue
            # load saved state (usages)
            state = {}
            if self.state_file.exists():
                try:
                    state = json.loads(self.state_file.read_text())
                except Exception:
                    state = {}
            self.proxies = []
            for p in parsed:
                key = f"{p['host']}:{p['port']}:{p['user']}"
                uses = int(state.get(key, 0))
                self.proxies.append({**p, "uses": uses, "key": key})
            # شطب أي بروكسي تجاوز الحد
            self.proxies = [p for p in self.proxies if p["uses"] < self.max_uses]
            self._save_state()
        else:
            self.proxies = []
            self._save_state()

    def _save_state(self):
        state = {p["key"]: p["uses"] for p in self.proxies}
        try:
            self.state_file.write_text(json.dumps(state, indent=2))
        except Exception:
            pass

    def get_proxy_tuple(self) -> Optional[Tuple]:
        """
        يرجع tuple مناسب لتمريره إلى Telethon كـ proxy parameter.
        مثال: (socks.SOCKS5, host, port, True, username, password)
        """
        if not self.proxies:
            return None
        p = random.choice(self.proxies)
        # بعد الاختيار نزيد العداد ونحفظ الحالة؛ لو وصل للحد نحذفه من القائمة
        p["uses"] += 1
        key = p["key"]
        if p["uses"] >= self.max_uses:
            # شطب
            self.proxies = [pp for pp in self.proxies if pp["key"] != key]
        self._save_state()
        try:
            return (socks.SOCKS5, p["host"], int(p["port"]), True, p["user"], p["pass"])
        except Exception:
            # fallback لو في أي مشكلة
            return (socks.SOCKS5, p["host"], int(p["port"]), False)

    def available_count(self) -> int:
        return len(self.proxies)

# أنشئ مدير البروكسيات
proxy_manager = ProxyManager(PROXIES_FILE, PROXIES_STATE_PATH, max_uses=10)

# --- دوال مساعدة ---
def fmt_time(ts: Optional[int]) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)

def auth_label(auth) -> str:
    parts = []
    if getattr(auth, "device_model", None):
        parts.append(str(auth.device_model))
    if getattr(auth, "platform", None):
        parts.append(str(auth.platform))
    if getattr(auth, "app_name", None):
        parts.append(str(auth.app_name))
    if getattr(auth, "ip", None):
        parts.append(f"IP:{auth.ip}")
    if getattr(auth, "date_active", None):
        parts.append(f"{fmt_time(auth.date_active)}")
    if parts:
        return " | ".join(parts)
    return f"hash={getattr(auth, 'hash', 'unknown')}"

def build_session_keyboard(auths, owner_id: int):
    keyboard = []
    shown = 0
    current_key_id = None
    try:
        state = SESSIONS.get(owner_id, {})
        current_key_id = state.get("current_key_id")
    except Exception:
        current_key_id = None

    for auth in auths:
        try:
            auth_hash = int(getattr(auth, "hash", 0))
        except Exception:
            auth_hash = None

        if current_key_id is not None and auth_hash == current_key_id:
            continue

        if shown >= MAX_DEVICES:
            break

        label = auth_label(auth)
        cb = f"kill:{auth_hash}:{owner_id}"
        keyboard.append([InlineKeyboardButton(f"🗑 حذف — {label}", callback_data=cb)])
        shown += 1

    keyboard.append([InlineKeyboardButton("🗑 حذف الجلسة الحالية (مسح ملف الجلسة)", callback_data=f"kill_current:{owner_id}")])
    keyboard.append([InlineKeyboardButton("🔍 راقب كود تسجيل الدخول", callback_data=f"watch_code:{owner_id}")])
    keyboard.append([InlineKeyboardButton("➕ إنشاء جلسة جديدة", callback_data=f"create_session:{owner_id}")])
    keyboard.append([InlineKeyboardButton(f"🧨 حذف كل الجلسات (ماعدا {PROTECTED_DEVICE_NAME})", callback_data=f"kill_all_except:{owner_id}")])
    return InlineKeyboardMarkup(keyboard)

# --- handlers ---
async def start(update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if START_PIC:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=START_PIC,
                caption="سلام 👋\nابعت ملف .session علشان اعرض الاجهزة."
            )
            return
        except Exception:
            pass
    await context.bot.send_message(
        chat_id=chat_id,
        text="سلام 👋\nابعت ملف .session علشان اعرض الاجهزة. البوت يدعم حذف جلسات، مراقبة الكود، وانشاء جلسة جديدة."
    )

async def handle_document(update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("ابعث ملف .session")
        return

    fname = doc.file_name or "session.session"
    if not (fname.endswith(".session") or "session" in fname):
        await update.message.reply_text("الملف مش ملف جلسة صالح (.session). ابعت ملف الجلسة.")
        return

    user_id = update.effective_user.id
    saved_path = SESSIONS_DIR / f"{user_id}__{fname}"

    file_obj = await doc.get_file()
    await file_obj.download_to_drive(custom_path=str(saved_path))

    await update.message.reply_text("⚠️ تحذير: ملف الجلسة يعطي وصول كامل للحساب. لا ترفعه إلا لو انت واثق.")

    # احصل على بروكسي إن وُجد
    proxy_tuple = proxy_manager.get_proxy_tuple()
    client_kwargs = {}
    if proxy_tuple:
        client_kwargs["proxy"] = proxy_tuple

    client = TelegramClient(str(saved_path), API_ID, API_HASH, **client_kwargs)

    try:
        await client.connect()
    except Exception as e:
        await update.message.reply_text(f"خطأ في الاتصال بـ Telethon: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
        return

    try:
        is_auth = await client.is_user_authorized()
    except Exception as e:
        await update.message.reply_text(f"فشل التحقق من الجلسة: {e}")
        await client.disconnect()
        return

    if not is_auth:
        await update.message.reply_text("الجلسة غير مفوّضة (مطلوب تسجيل/كود). تأكد الملف صحيح.")
        await client.disconnect()
        return

    try:
        res = await client(GetAuthorizationsRequest())
        auths = list(res.authorizations or [])
    except Exception as e:
        await update.message.reply_text(f"فشل في جلب الاجهزة: {e}")
        await client.disconnect()
        return

    current_key_id = None
    try:
        if client.session and client.session.auth_key:
            current_key_id = client.session.auth_key.key_id
    except Exception:
        current_key_id = None

    # لا نحذف الـ SESSIONS تلقائيًا — نخزن الحالة ونُعيد عرض الأجهزة لاحقًا حتى تحذف صراحة.
    SESSIONS[user_id] = {
        "client": client,
        "session_path": str(saved_path),
        "authorizations": auths,
        "current_key_id": current_key_id,
        "watch_task": None,
    }

    # بناء لوحة أزرار ودفعها للمستخدم
    kb = build_session_keyboard(auths, user_id)
    await update.message.reply_text(
        f"عرضت لك حتى {MAX_DEVICES} أجهزة (ما عدا الجلسة الحالية). اضغط الأزرار للتصرف.\n\nملاحظة: عدد البروكسيات المتاحة الآن: {proxy_manager.available_count()}",
        reply_markup=kb,
    )

async def refresh_session_keyboard(owner_id: int, context: ContextTypes.DEFAULT_TYPE, query=None):
    """
    يعيد جلب authorizations ويحدث رسالة الزرّ للمستخدم.
    إذا تم تمرير `query` (الـ CallbackQuery) نستخدمه للتعديل؛ وإلا نرسل رسالة جديدة.
    """
    state = SESSIONS.get(owner_id)
    if not state:
        try:
            await context.bot.send_message(chat_id=owner_id, text="الحالة غير موجودة، اعد رفع ملف الجلسة لو سمحت.")
        except Exception:
            pass
        return

    client: TelegramClient = state["client"]
    try:
        res = await client(GetAuthorizationsRequest())
        auths = list(res.authorizations or [])
        state["authorizations"] = auths
    except Exception as e:
        auths = state.get("authorizations", [])

    kb = build_session_keyboard(auths, owner_id)
    text = f"تحديث الأجهزة. عدد البروكسيات المتاحة الآن: {proxy_manager.available_count()}"

    try:
        if query:
            await query.edit_message_text(text, reply_markup=kb)
        else:
            await context.bot.send_message(chat_id=owner_id, text=text, reply_markup=kb)
    except Exception:
        try:
            await context.bot.send_message(chat_id=owner_id, text=text)
        except Exception:
            pass

async def callback_q(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    parts = (data.split(":") if data else [])

    owner_id = None
    if parts and parts[-1].isdigit():
        owner_id = int(parts[-1])

    if owner_id is None:
        await query.edit_message_text("بيانات غير صحيحة في الزر.")
        return

    if update.effective_user.id != owner_id:
        await query.edit_message_text("غير مسموح. هذا الزر ليه صاحب الجلسة فقط.")
        return

    state = SESSIONS.get(owner_id)
    if not state:
        await query.edit_message_text("انتهت صلاحية الجلسة أو لم يتم تحميلها. اعد رفع ملف الجلسة.")
        return

    client: TelegramClient = state["client"]

    if data.startswith("kill:"):
        try:
            _, hash_str, _ = parts
            target_hash = int(hash_str)
        except Exception:
            await query.edit_message_text("بيانات غير صحيحة.")
            return

        if state.get("current_key_id") is not None and target_hash == state.get("current_key_id"):
            await query.edit_message_text("لا يمكن حذف الجلسة الحالية هنا.")
            return

        try:
            await client(ResetAuthorizationRequest(hash=target_hash))
            # بعد الحذف نعيد تحديث لوحة الأزرار ونعرضها مجدداً حتى المستخدم يمسح الملف يدوياً
            await refresh_session_keyboard(owner_id, context, query)
        except Exception as e:
            await query.edit_message_text(f"فشل حذف الجلسة: {e}")
        return

    if data.startswith("kill_current:"):
        try:
            await client(LogOutRequest())
            try:
                await client.disconnect()
            except Exception:
                pass

            session_path = state.get("session_path")
            try:
                if session_path and Path(session_path).exists():
                    Path(session_path).unlink()
            except Exception:
                pass

            # هنا المستخدم طلب مسح ملف الجلسة، فنمحي الحالة
            SESSIONS.pop(owner_id, None)
            await query.edit_message_text("✅ تم تسجيل خروج الجلسة الحالية من تيليجرام وتم مسح ملف الجلسة.")
        except Exception as e:
            await query.edit_message_text(f"فشل تسجيل الخروج: {e}")
        return

    if data.startswith("watch_code:"):
        if state.get("watch_task"):
            await query.edit_message_text("المراقبة شغالة بالفعل. انتظر أو أوقفها أولاً.")
            return

        async def watcher():
            done = asyncio.Event()
            found = {"code": None, "msg": None}

            async def _handler(ev):
                try:
                    msg = ev.message
                    sid = getattr(msg, "sender_id", None)
                    text = getattr(msg, "message", "") or ""
                    m = re.search(r"\b(\d{4,7})\b", text)
                    if sid == 777000 or m:
                        found["code"] = m.group(1) if m else None
                        found["msg"] = text
                        done.set()
                except Exception:
                    pass

            client.add_event_handler(_handler, events.NewMessage(incoming=True))

            try:
                await asyncio.wait_for(done.wait(), timeout=WATCH_CODE_TIMEOUT)
            except asyncio.TimeoutError:
                try:
                    client.remove_event_handler(_handler)
                except Exception:
                    pass
                return None

            try:
                client.remove_event_handler(_handler)
            except Exception:
                pass
            return found

        loop = asyncio.get_event_loop()
        task = loop.create_task(watcher())
        state["watch_task"] = task

        await query.edit_message_text(f"🔍 بدأت مراقبة كود تسجيل الدخول لمدة {WATCH_CODE_TIMEOUT} ثانية. سأرسل لك الكود لو وصَل.")

        async def monitor_and_notify():
            try:
                res = await task
                state["watch_task"] = None
                if res is None:
                    await context.bot.send_message(chat_id=owner_id, text="⏱ انتهت المهلة ولم يصل كود تسجيل الدخول.")
                else:
                    code = res.get("code") or res.get("msg")
                    await context.bot.send_message(chat_id=owner_id, text=f"✅ وصل كود:\n{code}")
            except Exception as e:
                state["watch_task"] = None
                await context.bot.send_message(chat_id=owner_id, text=f"حدث خطأ في مراقبة الكود: {e}")

        asyncio.create_task(monitor_and_notify())
        return

    if data.startswith("create_session:"):
        CREATE_FLOW[owner_id] = {"state": "await_phone", "tmp": {}}
        await query.edit_message_text("➕ اكتب رقم الهاتف (بالصيغة الدولية، مثال: +201234567890) لبدء انشاء الجلسة:")
        return

    if data.startswith("kill_all_except:"):
        removed = 0
        protected_found = False
        try:
            res = await client(GetAuthorizationsRequest())
            auths = list(res.authorizations or [])
        except Exception as e:
            await query.edit_message_text(f"فشل جلب الاجهزة قبل الحذف: {e}")
            return

        for auth in auths:
            dev_name = getattr(auth, "device_model", None)
            auth_hash = getattr(auth, "hash", None)
            if dev_name == PROTECTED_DEVICE_NAME:
                protected_found = True
                continue
            try:
                await client(ResetAuthorizationRequest(hash=int(auth_hash)))
                removed += 1
            except Exception:
                pass

        # بعد الحذف — نعيد تحديث لوحة الأزرار ونبقي على ملف الجلسة حتى تضغط مسح صراحة
        await refresh_session_keyboard(owner_id, context, query)
        await context.bot.send_message(chat_id=owner_id, text=f"✅ تم حذف {removed} جلسة. المحمية: {PROTECTED_DEVICE_NAME} {'(موجودة)' if protected_found else '(غير موجودة)'}")
        return

    await query.edit_message_text("أمر غير معروف.")

async def text_handler(update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    txt = (update.message.text or "").strip()

    if user_id not in CREATE_FLOW:
        return

    flow = CREATE_FLOW[user_id]
    state = flow["state"]

    if state == "await_phone":
        phone = txt
        if not re.match(r"^\+\d{7,15}$", phone):
            await update.message.reply_text("رقم الهاتف غير صالح. استخدم الصيغة الدولية مثل +201234567890")
            return

        session_name = f"created_{user_id}_{int(datetime.now().timestamp())}.session"
        session_path = SESSIONS_DIR / session_name

        # استخدم بروكسي عند إنشاء الجلسة إن وُجد
        proxy_tuple = proxy_manager.get_proxy_tuple()
        client_kwargs = {}
        if proxy_tuple:
            client_kwargs["proxy"] = proxy_tuple

        client = TelegramClient(str(session_path), API_ID, API_HASH, **client_kwargs)

        try:
            await client.connect()
        except Exception as e:
            await update.message.reply_text(f"فشل الاتصال: {e}")
            return

        try:
            await client.send_code_request(phone)
        except Exception as e:
            await client.disconnect()
            await update.message.reply_text(f"فشل إرسال الكود: {e}")
            return

        flow["state"] = "await_code"
        flow["tmp"] = {"phone": phone, "session_path": str(session_path), "client": client}
        await update.message.reply_text("تم إرسال كود إلى رقم الهاتف. ابعته هنا (أو استخدم زر راقب الكود لو تريده).")
        return

    if state == "await_code":
        code = txt
        tmp = flow.get("tmp", {})
        client: TelegramClient = tmp.get("client")
        phone = tmp.get("phone")
        session_path = tmp.get("session_path")

        try:
            await client.sign_in(phone=phone, code=code)
        except errors.SessionPasswordNeededError:
            flow["state"] = "await_2fa"
            await update.message.reply_text("الحساب محمي بكلمة سر (2FA). ابعت الباسورد الآن.")
            return
        except Exception as e:
            await update.message.reply_text(f"فشل تسجيل الدخول بالكود: {e}")
            return

        try:
            await client.disconnect()
        except Exception:
            pass

        try:
            await context.bot.send_document(chat_id=user_id, document=InputFile(session_path))
            await update.message.reply_text("✅ تم إنشاء الجلسة وأرسلتها لك.")
        except Exception as e:
            await update.message.reply_text(f"تعذر ارسال ملف الجلسة: {e}")

        CREATE_FLOW.pop(user_id, None)
        return

    if state == "await_2fa":
        pwd = txt
        tmp = flow.get("tmp", {})
        client: TelegramClient = tmp.get("client")
        session_path = tmp.get("session_path")
        try:
            await client.sign_in(password=pwd)
        except Exception as e:
            await update.message.reply_text(f"فشل التحقق من كلمة المرور: {e}")
            return

        try:
            await client.disconnect()
        except Exception:
            pass

        try:
            await context.bot.send_document(chat_id=user_id, document=InputFile(session_path))
            await update.message.reply_text("✅ تم إنشاء الجلسة مع 2FA وأرسلتها لك.")
        except Exception as e:
            await update.message.reply_text(f"تعذر ارسال ملف الجلسة: {e}")

        CREATE_FLOW.pop(user_id, None)
        return

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, handle_document))
    app.add_handler(CallbackQueryHandler(callback_q))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("Running bot_sessions_telethon_v4...")
    app.run_polling()

if __name__ == "__main__":
    main()
