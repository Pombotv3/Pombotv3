# -*- coding: utf-8 -*-
import os
import io
import json
import shutil
import asyncio
import logging
import urllib.parse
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime

import aiohttp
import aiosqlite
import segno
import uvicorn

from aiogram import Bot, Dispatcher, Router, F, types, BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramConflictError, TelegramRetryAfter
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, InputMediaPhoto, InputMediaVideo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from fastapi import FastAPI, Request, Form, File, UploadFile, HTTPException, Depends, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Template

# ==========================================
# CONFIG & AUTHENTICATION
# ==========================================
ADMIN_USER = os.getenv("ADMIN_USER", "nagato")
DEFAULT_PASS = os.getenv("ADMIN_PASS", "nagato@123")
AUTH_COOKIE_NAME = "session_token"
AUTH_SECRET = "admin_authenticated_session_key_99"

DATA_DIR = os.getenv("DATA_DIR", "/app/data" if os.path.exists("/app/data") else ".")
DB_NAME = os.path.join(DATA_DIR, "fireworld.db")

STATIC_DIR = os.path.join(os.getcwd(), "static")
UPLOAD_DIR = os.path.join(STATIC_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

INITIAL_BOT_TOKEN = os.getenv("BOT_TOKEN", "7110523959:AAHlYQTvoMQR1Zq8rFkM-fyWua79NMQ_r9Q")
logging.basicConfig(level=logging.INFO)

DB_WRITE_LOCK = asyncio.Lock()

async def require_admin(request: Request):
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if token != AUTH_SECRET:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return True

# ==========================================
# DATABASE LAYER
# ==========================================
async def get_db_connection():
    db = await aiosqlite.connect(DB_NAME, timeout=30.0)
    await db.execute("PRAGMA journal_mode = WAL;")
    await db.execute("PRAGMA synchronous = NORMAL;")
    await db.execute("PRAGMA busy_timeout = 30000;")
    await db.execute("PRAGMA cache_size = -64000;")
    return db

async def init_db():
    async with DB_WRITE_LOCK:
        db = await get_db_connection()
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    full_name TEXT,
                    username TEXT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    premium_status TEXT DEFAULT 'Free',
                    is_banned INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    plan_name TEXT,
                    amount REAL,
                    screenshot_file_id TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    name TEXT,
                    amount REAL,
                    validity TEXT,
                    access_link TEXT DEFAULT '',
                    color_style TEXT DEFAULT 'primary'
                )
            """)

            defaults = {
                "bot_token": INITIAL_BOT_TOKEN,
                "admin_password": DEFAULT_PASS,
                "admin_chat_id": "",
                "maintenance": "off",
                "upi_id": "anmolvlv@ibl",
                "payee_name": "Fire World",
                "first_media": json.dumps([{"type": "photo", "url": "https://picsum.photos/800/800"}]),
                "first_caption": "💎 GET PREMIUM",
                "plan_media": json.dumps([{"type": "photo", "url": "https://picsum.photos/800/700"}]),
                "plan_caption": " ══════« PRO PLAN »═════",
                "demo_videos": json.dumps([
                    "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
                    "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerEscapes.mp4"
                ]),
                "plans_text": " ══════« CHOOSE PLAN »═════"
            }

            for k, v in defaults.items():
                await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

            # Populate default 7 stylized plans matching your reference UI
            default_plans = [
                ("plan_1", "1 Month VIP", 79.0, "30 Days", "", "success"),
                ("plan_2", "2 Months VIP", 149.0, "60 Days", "", "danger"),
                ("plan_3", "3 Months VIP", 199.0, "90 Days", "", "primary"),
                ("plan_4", "6 Months VIP", 299.0, "180 Days", "", "success"),
                ("plan_5", "1 Year VIP", 399.0, "365 Days", "", "danger"),
                ("plan_6", "Lifetime Access", 499.0, "Lifetime", "", "primary"),
                ("plan_7", "Ultra Mega VIP", 799.0, "Lifetime", "", "success"),
            ]
            for p in default_plans:
                await db.execute(
                    "INSERT OR IGNORE INTO plans (plan_id, name, amount, validity, access_link, color_style) VALUES (?, ?, ?, ?, ?, ?)",
                    p
                )
            await db.commit()
        finally:
            await db.close()

async def get_setting(key: str) -> str:
    db = await get_db_connection()
    try:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else ""
    finally:
        await db.close()

async def update_setting(key: str, value: str):
    async with DB_WRITE_LOCK:
        db = await get_db_connection()
        try:
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
            await db.commit()
        finally:
            await db.close()

async def get_admin_ids() -> list[int]:
    chat_id_str = await get_setting("admin_chat_id")
    ids = []
    if chat_id_str:
        for x in chat_id_str.split(","):
            x = x.strip()
            if x.lstrip("-").isdigit():
                ids.append(int(x))
    return ids

async def get_all_plans():
    db = await get_db_connection()
    try:
        async with db.execute("SELECT plan_id, name, amount, validity, COALESCE(access_link, ''), COALESCE(color_style, 'primary') FROM plans ORDER BY plan_id ASC") as cur:
            return await cur.fetchall()
    finally:
        await db.close()

async def get_plan(plan_id: str):
    db = await get_db_connection()
    try:
        async with db.execute("SELECT plan_id, name, amount, validity, COALESCE(access_link, ''), COALESCE(color_style, 'primary') FROM plans WHERE plan_id = ?", (plan_id,)) as cur:
            return await cur.fetchone()
    finally:
        await db.close()

async def get_plan_by_name(name: str):
    db = await get_db_connection()
    try:
        async with db.execute("SELECT plan_id, name, amount, validity, COALESCE(access_link, ''), COALESCE(color_style, 'primary') FROM plans WHERE name = ?", (name,)) as cur:
            return await cur.fetchone()
    finally:
        await db.close()

async def update_plan(plan_id: str, name: str, amount: float, validity: str, access_link: str = ""):
    async with DB_WRITE_LOCK:
        db = await get_db_connection()
        try:
            await db.execute(
                "UPDATE plans SET name = ?, amount = ?, validity = ?, access_link = ? WHERE plan_id = ?",
                (name, amount, validity, access_link.strip(), plan_id),
            )
            await db.commit()
        finally:
            await db.close()

async def add_new_plan(plan_id: str, name: str, amount: float, validity: str, access_link: str = ""):
    async with DB_WRITE_LOCK:
        db = await get_db_connection()
        try:
            await db.execute(
                "INSERT INTO plans (plan_id, name, amount, validity, access_link, color_style) VALUES (?, ?, ?, ?, ?, 'primary')",
                (plan_id, name, amount, validity, access_link.strip()),
            )
            await db.commit()
        finally:
            await db.close()

async def delete_plan(plan_id: str):
    async with DB_WRITE_LOCK:
        db = await get_db_connection()
        try:
            await db.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))
            await db.commit()
        finally:
            await db.close()

async def add_or_update_user(user: types.User):
    async with DB_WRITE_LOCK:
        db = await get_db_connection()
        try:
            await db.execute(
                """
                INSERT INTO users (user_id, full_name, username)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET full_name = excluded.full_name, username = excluded.username
                """,
                (user.id, user.full_name, user.username or "N/A"),
            )
            await db.commit()
        finally:
            await db.close()

async def get_user(user_id: int):
    db = await get_db_connection()
    try:
        async with db.execute(
            "SELECT user_id, full_name, username, joined_at, premium_status, is_banned FROM users WHERE user_id = ?",
            (user_id,),
        ) as cur:
            return await cur.fetchone()
    finally:
        await db.close()

async def get_all_users_detailed():
    db = await get_db_connection()
    try:
        async with db.execute(
            "SELECT user_id, full_name, username, joined_at, premium_status, is_banned FROM users ORDER BY joined_at DESC"
        ) as cur:
            return await cur.fetchall()
    finally:
        await db.close()

async def update_user_subscription(user_id: int, plan_name: str):
    async with DB_WRITE_LOCK:
        db = await get_db_connection()
        try:
            await db.execute("UPDATE users SET premium_status = ? WHERE user_id = ?", (plan_name, user_id))
            await db.commit()
        finally:
            await db.close()

async def set_user_ban_status(user_id: int, is_banned: int):
    async with DB_WRITE_LOCK:
        db = await get_db_connection()
        try:
            await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (int(is_banned), int(user_id)))
            await db.commit()
        finally:
            await db.close()

async def get_dashboard_metrics():
    db = await get_db_connection()
    try:
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM payments WHERE status='approved'") as cur:
            row = await cur.fetchone()
            paid_orders = row[0] if row else 0
            revenue = row[1] if row else 0.0

        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            total_users = (await cur.fetchone())[0]

        async with db.execute("""
            SELECT p.id, p.user_id, COALESCE(u.username, 'N/A'), p.plan_name, p.amount, p.status, p.created_at, p.screenshot_file_id
            FROM payments p
            LEFT JOIN users u ON p.user_id = u.user_id
            ORDER BY p.id DESC
        """) as cur:
            all_orders = await cur.fetchall()

        return {
            "paid_orders": paid_orders,
            "revenue": f"{revenue:,.2f}",
            "total_users": total_users,
            "recent_orders": all_orders[:20],
            "all_orders": all_orders,
        }
    finally:
        await db.close()

# ==========================================
# NON-BLOCKING QR GENERATION
# ==========================================
def _generate_qr_sync(upi_url: str) -> io.BytesIO:
    qr = segno.make(upi_url, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=8, border=2)
    buf.seek(0)
    return buf

async def generate_upi_qr(plan_name: str, amount: float) -> io.BytesIO:
    upi_id = await get_setting("upi_id") or "anmolvlv@ibl"
    payee_name = await get_setting("payee_name") or "Fire World"
    upi_params = {
        "pa": upi_id,
        "pn": payee_name,
        "am": f"{amount:.2f}",
        "cu": "INR",
        "tn": f"Order {plan_name}",
    }
    upi_url = "upi://pay?" + urllib.parse.urlencode(upi_params)
    return await asyncio.to_thread(_generate_qr_sync, upi_url)

# ==========================================
# DYNAMIC BOT MANAGER
# ==========================================
class BotManager:
    def __init__(self):
        self.bot: Bot | None = None
        self.dp: Dispatcher = Dispatcher(storage=MemoryStorage())
        self.polling_task: asyncio.Task | None = None
        self.session: AiohttpSession | None = None
        self.current_token: str | None = None

    async def send_raw(self, method: str, payload: dict):
        if not self.current_token:
            return None
        url = f"https://api.telegram.org/bot{self.current_token}/{method}"
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=payload) as resp:
                return await resp.json()

    async def start(self, token: str):
        if not token or token == "YOUR_BOT_TOKEN_HERE":
            logging.warning("[BotManager] No valid token set. Bot is idle.")
            return

        self.current_token = token.strip()
        self.session = AiohttpSession(timeout=20.0)
        self.bot = Bot(
            token=self.current_token,
            session=self.session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )

        try:
            await self.bot.delete_webhook(drop_pending_updates=True)
            await self.bot.session.close()
            self.session = AiohttpSession(timeout=20.0)
            self.bot.session = self.session
        except Exception as e:
            logging.warning(f"[BotManager] Initial reset notice: {e}")

        async def runner():
            while True:
                try:
                    logging.info("[BotManager] Starting isolated polling loop...")
                    await self.dp.start_polling(
                        self.bot,
                        drop_pending_updates=True,
                        allowed_updates=["message", "callback_query"],
                    )
                    break
                except TelegramConflictError:
                    logging.warning("[BotManager] Polling collision: duplicate session detected. Retrying in 5s...")
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logging.error(f"[BotManager] Polling error: {e}. Retrying in 3s...")
                    await asyncio.sleep(3)

        self.polling_task = asyncio.create_task(runner())

    async def stop(self):
        if self.polling_task and not self.polling_task.done():
            self.polling_task.cancel()
            try:
                await self.polling_task
            except asyncio.CancelledError:
                pass
            self.polling_task = None

        if self.dp:
            try:
                await self.dp.stop_polling()
            except Exception:
                pass

        if self.bot:
            try:
                if self.bot.session:
                    await self.bot.session.close()
            except Exception:
                pass
            self.bot = None

        self.current_token = None
        logging.info("[BotManager] Bot stopped cleanly.")

    async def restart(self, new_token: str):
        await self.stop()
        await self.start(new_token)

manager = BotManager()

# ==========================================
# KEYBOARDS & CONSTANTS
# ==========================================
class PaymentStates(StatesGroup):
    waiting_for_screenshot = State()

BOTTOM_REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "💎 GET PREMIUM", "style": "success"}],
        [{"text": "🥵 DEMO", "style": "danger"}]
    ],
    "resize_keyboard": True
}

THREE_OPTIONS_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "💎 GET PREMIUM", "callback_data": "menu_get_premium", "style": "success"}],
        [{"text": "🥵 DEMO", "callback_data": "menu_demo", "style": "danger"}],
        [{"text": "✅ HOW TO GET PREMIUM", "callback_data": "menu_how_to", "style": "primary"}]
    ]
}

DEMO_ITEM_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "💎 GET PREMIUM", "callback_data": "menu_get_premium", "style": "success"}]
    ]
}

def make_get_link_keyboard(order_id: int):
    return {
        "inline_keyboard": [
            [{"text": "GET LINK", "callback_data": f"submit_{order_id}", "style": "success"}]
        ]
    }

async def build_dynamic_plans_keyboard():
    plans = await get_all_plans()
    rows = []
    # Palette sequence: Green, Red, Blue
    palette = ["success", "danger", "primary"]
    for i, p in enumerate(plans):
        pid, name, amount, validity, _, style = p
        btn_style = style if style in palette else palette[i % len(palette)]
        rows.append([
            {
                "text": f"{name} - ₹{int(amount)}",
                "callback_data": f"buy_plan:{pid}",
                "style": btn_style
            }
        ])
    return {"inline_keyboard": rows}

# ==========================================
# BOT CONTROLLER HELPERS & FLOW
# ==========================================
async def send_first_media_with_bottom_bar(chat_id: int, media_list: list, caption: str):
    """Sends first message media with the persistent bottom bar and zero placeholder text."""
    if not media_list:
        await manager.send_raw("sendMessage", {
            "chat_id": chat_id,
            "text": caption,
            "reply_markup": BOTTOM_REPLY_KEYBOARD
        })
        return

    if len(media_list) == 1:
        item = media_list[0]
        method = "sendVideo" if item["type"] == "video" else "sendPhoto"
        key = "video" if item["type"] == "video" else "photo"
        await manager.send_raw(method, {
            "chat_id": chat_id,
            key: item["url"],
            "caption": caption,
            "reply_markup": BOTTOM_REPLY_KEYBOARD
        })
    else:
        chunks = [media_list[i:i + 10] for i in range(0, len(media_list), 10)]
        for chunk_idx, chunk in enumerate(chunks):
            media_group = []
            is_last_chunk = (chunk_idx == len(chunks) - 1)
            for item_idx, item in enumerate(chunk):
                is_last_item = is_last_chunk and (item_idx == len(chunk) - 1)
                cap = caption if is_last_item else None
                if item["type"] == "video":
                    media_group.append(InputMediaVideo(media=item["url"], caption=cap))
                else:
                    media_group.append(InputMediaPhoto(media=item["url"], caption=cap))
            await manager.bot.send_media_group(chat_id=chat_id, media=media_group)

async def send_plan_presentation(chat_id: int):
    # 1. First intermediate message
    plans_banner = await get_setting("plans_text") or " ══════« CHOOSE PLAN »═════"
    await manager.bot.send_message(chat_id, plans_banner)

    # 2. Main plan photo with the styled buttons
    plan_media_raw = await get_setting("plan_media")
    plan_caption = await get_setting("plan_caption") or " ══════« PRO PLAN »═════"
    try:
        plan_media = json.loads(plan_media_raw) if plan_media_raw else []
    except Exception:
        plan_media = []

    photo_url = plan_media[0]["url"] if plan_media else "https://picsum.photos/800/700"
    plans_kbd = await build_dynamic_plans_keyboard()

    await manager.send_raw("sendPhoto", {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": plan_caption,
        "reply_markup": plans_kbd
    })

async def play_demo_videos_one_by_one(chat_id: int):
    raw_demos = await get_setting("demo_videos")
    try:
        videos = json.loads(raw_demos) if raw_demos else []
    except Exception:
        videos = [line.strip() for line in raw_demos.splitlines() if line.strip()]

    if not videos:
        await manager.bot.send_message(chat_id, "📺 No demo videos available right now.")
        return

    for vid in videos:
        await manager.send_raw("sendVideo", {
            "chat_id": chat_id,
            "video": vid,
            "reply_markup": DEMO_ITEM_KEYBOARD
        })
        await asyncio.sleep(0.5)

async def notify_payment_approved(user_id: int, plan_name: str):
    if not manager.bot:
        return
    plan_info = await get_plan_by_name(plan_name)
    access_link = plan_info[4] if plan_info and len(plan_info) > 4 else ""

    builder = InlineKeyboardBuilder()
    if access_link and access_link.strip().startswith(("http://", "https://", "t.me/")):
        link_url = access_link.strip()
        if link_url.startswith("t.me/"):
            link_url = "https://" + link_url
        builder.button(text="🔗 Claim VIP Channel Access", url=link_url)
    builder.adjust(1)

    caption = (
        f"🎉 <b>Payment Approved!</b>\n\n"
        f"Your access for <b>{plan_name}</b> is now fully activated!\n"
    )
    if access_link:
        caption += "\nClick the button below to join the private channel:"

    try:
        await manager.bot.send_message(
            chat_id=user_id,
            text=caption,
            reply_markup=builder.as_markup() if access_link else None,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"Could not deliver approval to {user_id}: {e}")

# ==========================================
# BOT DISPATCHER & EVENT ROUTING
# ==========================================
bot_router = Router()

@bot_router.message(CommandStart())
async def handle_start(message: types.Message):
    await add_or_update_user(message.from_user)

    first_media_raw = await get_setting("first_media")
    first_caption = await get_setting("first_caption") or "💎 GET PREMIUM"

    try:
        media_list = json.loads(first_media_raw) if first_media_raw else []
    except Exception:
        media_list = []

    # 1. First Message: Images/Videos with caption + bottom persistent keyboard attached directly
    await send_first_media_with_bottom_bar(message.chat.id, media_list, first_caption)

    # 2. Second Message: Choose option with 3 colored buttons
    await manager.send_raw("sendMessage", {
        "chat_id": message.chat.id,
        "text": "👇 Choose an option:",
        "reply_markup": THREE_OPTIONS_KEYBOARD
    })

    # 3. Third Message: Hand pointing emoji
    await message.answer("👇")

@bot_router.callback_query(F.data == "menu_get_premium")
async def on_get_premium_click(callback: types.CallbackQuery):
    await callback.answer()
    await send_plan_presentation(callback.message.chat.id)

@bot_router.callback_query(F.data == "menu_demo")
async def on_demo_click(callback: types.CallbackQuery):
    await callback.answer()
    await play_demo_videos_one_by_one(callback.message.chat.id)

@bot_router.callback_query(F.data == "menu_how_to")
async def on_how_to_click(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "📖 <b>How to Get Premium:</b>\n\n"
        "1. Click <b>💎 GET PREMIUM</b>\n"
        "2. Choose your preferred plan from the list.\n"
        "3. Scan the generated UPI QR code & pay.\n"
        "4. Click <b>GET LINK</b> and upload your payment screenshot.\n"
        "5. The admin will verify and your private access link arrives instantly!",
        parse_mode="HTML"
    )

@bot_router.message(F.text == "💎 GET PREMIUM")
async def on_text_get_premium(message: types.Message):
    await send_plan_presentation(message.chat.id)

@bot_router.message(F.text == "🥵 DEMO")
async def on_text_demo(message: types.Message):
    await play_demo_videos_one_by_one(message.chat.id)

@bot_router.callback_query(F.data.startswith("buy_plan:"))
async def process_plan_choice(callback: types.CallbackQuery):
    await callback.answer()
    pid = callback.data.split(":")[1]
    plan = await get_plan(pid)
    if not plan:
        await callback.message.answer("Plan not found.")
        return

    _, plan_name, amount, validity, _, _ = plan
    upi_id = await get_setting("upi_id") or "anmolvlv@ibl"

    async with DB_WRITE_LOCK:
        db = await get_db_connection()
        try:
            cur = await db.execute(
                "INSERT INTO payments (user_id, plan_name, amount, status) VALUES (?, ?, ?, 'pending')",
                (callback.from_user.id, plan_name, amount)
            )
            order_id = cur.lastrowid
            await db.commit()
        finally:
            await db.close()

    qr_buf = await generate_upi_qr(plan_name, amount)
    photo_file = BufferedInputFile(qr_buf.getvalue(), filename="qr.png")

    caption = (
        f"🏷 Price : ₹{int(amount)}\n\n"
        f"🏦 UPI ID: <code>{upi_id}</code>\n\n"
        f"1️⃣ Scan | 2️⃣ Pay | 3️⃣ Click 'GET LINK'"
    )

    await callback.message.answer_photo(
        photo=photo_file,
        caption=caption,
        parse_mode="HTML"
    )

    await manager.send_raw("sendMessage", {
        "chat_id": callback.message.chat.id,
        "text": "Click below after completing payment:",
        "reply_markup": make_get_link_keyboard(order_id)
    })

@bot_router.callback_query(F.data.startswith("submit_"))
async def prompt_screenshot(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    order_id = int(callback.data.split("_")[1])
    await state.update_data(order_id=order_id)
    await state.set_state(PaymentStates.waiting_for_screenshot)
    await callback.message.answer("📸 Please send your payment screenshot.")

@bot_router.message(PaymentStates.waiting_for_screenshot, F.photo)
async def receive_screenshot_proof(message: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    file_id = message.photo[-1].file_id

    async with DB_WRITE_LOCK:
        db = await get_db_connection()
        try:
            await db.execute("UPDATE payments SET screenshot_file_id = ? WHERE id = ?", (file_id, order_id))
            async with db.execute("SELECT plan_name, amount FROM payments WHERE id = ?", (order_id,)) as cur:
                row = await cur.fetchone()
            await db.commit()
        finally:
            await db.close()

    await state.clear()
    await message.reply("✅ Payment screenshot received. Please wait while an admin verifies it.")

    # Push verification card to telegram admin IDs if configured
    admin_ids = await get_admin_ids()
    if admin_ids and row:
        plan_name, amount = row
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Approve", callback_data=f"adm_pay:{order_id}:approved")
        builder.button(text="❌ Reject", callback_data=f"adm_pay:{order_id}:rejected")
        builder.adjust(2)

        caption = (
            f"🔔 <b>New Payment Verification</b>\n\n"
            f"<b>Order ID:</b> #{order_id}\n"
            f"<b>User:</b> <code>{message.from_user.id}</code> (@{message.from_user.username or 'N/A'})\n"
            f"<b>Plan:</b> {plan_name}\n"
            f"<b>Amount:</b> ₹{amount}"
        )
        for aid in admin_ids:
            try:
                await manager.bot.send_photo(
                    chat_id=aid,
                    photo=file_id,
                    caption=caption,
                    reply_markup=builder.as_markup()
                )
            except Exception:
                pass

@bot_router.callback_query(F.data.startswith("adm_pay:"))
async def handle_telegram_admin_pay_approval(callback: types.CallbackQuery):
    admin_ids = await get_admin_ids()
    if callback.from_user.id in admin_ids:
        _, pid, act = callback.data.split(":")
        async with DB_WRITE_LOCK:
            db = await get_db_connection()
            try:
                async with db.execute("SELECT user_id, plan_name FROM payments WHERE id = ?", (int(pid),)) as cur:
                    p = await cur.fetchone()
                if not p:
                    await callback.answer("Order not found.")
                    return
                t_uid, pl = p
                if act == "approved":
                    await db.execute("UPDATE payments SET status='approved' WHERE id=?", (int(pid),))
                    await db.execute("UPDATE users SET premium_status=? WHERE user_id=?", (pl, t_uid))
                    await db.commit()
                    await notify_payment_approved(t_uid, pl)
                    await callback.message.edit_caption(caption=callback.message.caption + "\n\nSTATUS: APPROVED ✅")
                else:
                    await db.execute("UPDATE payments SET status='rejected' WHERE id=?", (int(pid),))
                    await db.commit()
                    try:
                        await manager.bot.send_message(t_uid, "❌ Payment verification failed. Your proof could not be verified.")
                    except Exception:
                        pass
                    await callback.message.edit_caption(caption=callback.message.caption + "\n\nSTATUS: REJECTED ❌")
            finally:
                await db.close()
        await callback.answer("Order updated.")

manager.dp.include_router(bot_router)

# ==========================================
# FASTAPI APPLICATION & WEB ADMIN INTERFACE
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    token = await get_setting("bot_token")
    if token and token != "YOUR_BOT_TOKEN_HERE":
        await manager.start(token)
    yield
    await manager.stop()

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ==========================================
# TEMPLATES (NAGATO CYBERPUNK ADMIN THEME)
# ==========================================
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Login &mdash; Nagato Panel</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;700;800;900&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Plus Jakarta Sans', sans-serif; background-color: #030108; }
        .font-tech { font-family: 'Orbitron', monospace; }
        .exact-login-card {
            background: linear-gradient(180deg, rgba(16, 12, 34, 0.94) 0%, rgba(10, 8, 22, 0.96) 100%);
            border: 1px solid rgba(168, 85, 247, 0.5);
            box-shadow: 0 0 28px rgba(168, 85, 247, 0.35), 0 0 70px rgba(168, 85, 247, 0.15);
            border-radius: 26px;
        }
        .custom-input {
            background-color: #080613;
            border: 1px solid rgba(147, 51, 234, 0.25);
            transition: all 0.2s ease;
        }
        .custom-input:focus {
            outline: none;
            border-color: #38bdf8;
            box-shadow: 0 0 12px rgba(56, 189, 248, 0.3);
        }
    </style>
</head>
<body class="text-slate-100 min-h-screen flex items-center justify-center p-4 relative overflow-hidden">
    <div class="w-full max-w-[370px] relative z-10">
        <div class="exact-login-card p-8 space-y-6">
            <div class="space-y-1">
                <div class="flex items-center gap-2.5">
                    <svg class="w-6 h-6 text-fuchsia-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/>
                        <path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/>
                    </svg>
                    <h1 class="text-xl font-bold tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-purple-300 via-fuchsia-300 to-cyan-300">
                        Nagato Panel
                    </h1>
                </div>
                <div class="font-tech text-[10px] tracking-[0.25em] text-cyan-400/90 font-bold uppercase pl-8">
                    ADMIN LOGIN
                </div>
            </div>

            {% if error %}
            <div class="p-3 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-400 text-xs font-mono">
                {{ error }}
            </div>
            {% endif %}

            <form method="POST" action="/login" class="space-y-4 pt-1">
                <div>
                    <label class="block text-xs font-medium text-slate-300 mb-2">Username</label>
                    <input type="text" name="username" required autofocus class="custom-input w-full h-11 rounded-xl px-4 text-sm text-white">
                </div>

                <div>
                    <label class="block text-xs font-medium text-slate-300 mb-2">Password</label>
                    <input type="password" name="password" required class="custom-input w-full h-11 rounded-xl px-4 text-sm text-white">
                </div>

                <button type="submit" class="w-full h-11 mt-3 bg-gradient-to-r from-purple-500 via-fuchsia-500 to-cyan-400 hover:opacity-95 text-white font-semibold rounded-xl text-sm transition shadow-lg shadow-purple-600/30">
                    Sign in &rarr;
                </button>
            </form>
        </div>
    </div>
</body>
</html>"""

DASHBOARD_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard &mdash; Nagato Panel</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Plus Jakarta Sans', sans-serif; background-color: #06040c; margin: 0; padding: 0; overflow-x: hidden; }
        .font-tech { font-family: 'Orbitron', monospace; }
        .glass-card {
            background: linear-gradient(135deg, rgba(22, 12, 42, 0.85) 0%, rgba(13, 8, 25, 0.92) 100%);
            border: 1px solid rgba(139, 92, 246, 0.25);
        }
        .neon-border-pink {
            border-color: rgba(255, 0, 127, 0.5) !important;
            box-shadow: 0 0 15px rgba(255, 0, 127, 0.2);
        }
        #sidebar {
            position: fixed;
            top: 0;
            left: 0;
            bottom: 0;
            width: 260px;
            background-color: #090614;
            border-right: 1px solid rgba(139, 92, 246, 0.25);
            z-index: 50;
            transition: transform 0.25s ease;
            transform: translateX(-100%);
        }
        #sidebar.open { transform: translateX(0); }
        @media (min-width: 768px) {
            #sidebar { position: static; transform: translateX(0) !important; height: 100vh; }
        }
        #sidebarBackdrop { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.7); backdrop-filter: blur(4px); z-index: 40; }
        #sidebarBackdrop.open { display: block; }
        .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0, 0, 0, 0.75); backdrop-filter: blur(6px); z-index: 99; align-items: center; justify-content: center; padding: 1rem; }
        .modal-overlay.active { display: flex; }
    </style>
</head>
<body class="text-slate-100 min-h-screen flex">
    <div id="sidebarBackdrop" onclick="toggleSidebar()"></div>

    <!-- Revenue Reset Modal -->
    <div id="confirmResetModal" class="modal-overlay">
        <div class="glass-card max-w-sm w-full p-6 rounded-2xl border border-amber-500/40 space-y-5">
            <h4 class="font-tech text-sm font-bold text-white">Reset Revenue</h4>
            <p class="text-xs text-slate-300">Are you sure you want to reset all revenue counters? This will delete all order histories permanently.</p>
            <form method="POST" action="/admin/revenue/reset" class="flex gap-3 pt-2">
                <button type="button" onclick="closeResetModal()" class="flex-1 bg-purple-900/40 text-slate-300 font-tech text-xs py-2.5 rounded-xl">Cancel</button>
                <button type="submit" class="flex-1 bg-amber-600 text-white font-tech font-bold text-xs py-2.5 rounded-xl uppercase">Confirm</button>
            </form>
        </div>
    </div>

    <!-- Delete Plan Modal -->
    <div id="confirmDeleteModal" class="modal-overlay">
        <div class="glass-card max-w-sm w-full p-6 rounded-2xl border border-rose-500/40 space-y-5">
            <h4 class="font-tech text-sm font-bold text-white">Confirm Deletion</h4>
            <p class="text-xs text-slate-300">Delete <span id="modalPlanName" class="text-cyan-400 font-semibold"></span> (<span id="modalPlanId" class="text-purple-300 font-mono"></span>)?</p>
            <form id="modalDeleteForm" method="POST" action="/admin/plans/delete" class="flex gap-3 pt-2">
                <input type="hidden" id="modalPlanIdInput" name="plan_id" value="">
                <button type="button" onclick="closeDeleteModal()" class="flex-1 bg-purple-900/40 text-slate-300 font-tech text-xs py-2.5 rounded-xl">Cancel</button>
                <button type="submit" class="flex-1 bg-rose-600 text-white font-tech font-bold text-xs py-2.5 rounded-xl uppercase">Delete</button>
            </form>
        </div>
    </div>

    <!-- Sidebar Navigation -->
    <aside id="sidebar" class="p-5 flex flex-col justify-between overflow-y-auto">
        <div class="space-y-6">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-2.5">
                    <svg class="w-6 h-6 text-fuchsia-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/>
                        <path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/>
                    </svg>
                    <div>
                        <span class="font-tech font-bold text-sm text-transparent bg-clip-text bg-gradient-to-r from-fuchsia-400 to-cyan-300 tracking-wider block">Nagato Panel</span>
                        <span class="font-tech text-[10px] tracking-wider text-cyan-300 uppercase block">Fire World Bot</span>
                    </div>
                </div>
                <button type="button" onclick="toggleSidebar()" class="md:hidden text-purple-400 p-1">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
                </button>
            </div>

            <nav class="space-y-1.5 text-xs">
                <button type="button" onclick="switchTab('tab-dashboard')" id="nav-tab-dashboard" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-semibold bg-purple-900/40 border border-fuchsia-500/30 text-cyan-400">Dashboard</button>
                <button type="button" onclick="switchTab('tab-orders')" id="nav-tab-orders" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400">Customer Orders</button>
                <button type="button" onclick="switchTab('tab-users')" id="nav-tab-users" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400">Manage Users</button>
                <button type="button" onclick="switchTab('tab-broadcast')" id="nav-tab-broadcast" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400">Broadcast</button>
                <button type="button" onclick="switchTab('tab-settings')" id="nav-tab-settings" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400">Setting &amp; UPI</button>
                <button type="button" onclick="switchTab('tab-bot')" id="nav-tab-bot" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400">Bot Token Config</button>
                <button type="button" onclick="switchTab('tab-media')" id="nav-tab-media" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400">Media &amp; Greetings</button>
                <button type="button" onclick="switchTab('tab-plans')" id="nav-tab-plans" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400">Subscription Plans</button>
            </nav>
        </div>

        <div class="pt-4 border-t border-purple-900/40">
            <a href="/logout" class="w-full flex items-center justify-center gap-2 py-2 rounded-xl text-xs font-mono font-semibold text-rose-400 hover:bg-rose-500/10 transition">
                Sign Out
            </a>
        </div>
    </aside>

    <!-- Main Content -->
    <main class="flex-1 flex flex-col min-w-0 h-screen overflow-y-auto">
        <header class="sticky top-0 z-20 bg-[#06040c]/95 backdrop-blur-md border-b border-purple-900/40 px-4 md:px-8 py-3.5 flex items-center justify-between">
            <div class="flex items-center gap-3">
                <button type="button" onclick="toggleSidebar()" class="p-2 rounded-lg bg-[#120b22] border border-purple-900/40 text-purple-300">
                    <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"/></svg>
                </button>
                <h2 id="sectionTitle" class="font-tech text-base md:text-lg font-bold text-white tracking-wider">Dashboard</h2>
            </div>
            
            <div class="flex items-center gap-3">
                <span class="flex items-center gap-1.5 px-3 py-1 rounded-full bg-[#120b22] border border-cyan-500/40 text-xs font-mono text-cyan-300">
                    <span class="w-2 h-2 rounded-full {{ 'bg-cyan-400' if is_online else 'bg-amber-400' }} animate-pulse inline-block"></span>
                    {{ 'ONLINE' if is_online else 'STANDBY' }}
                </span>
            </div>
        </header>

        <div class="p-4 md:p-8 max-w-5xl w-full mx-auto space-y-6">
            {% if message %}
            <div class="p-4 rounded-xl bg-cyan-500/10 border border-cyan-500/40 text-cyan-300 text-xs font-mono">
                {{ message }}
            </div>
            {% endif %}

            <!-- TAB: DASHBOARD -->
            <div id="tab-dashboard" class="tab-content space-y-6">
                <div class="grid grid-cols-2 lg:grid-cols-4 gap-3 md:gap-4">
                    <div class="glass-card rounded-2xl p-4 flex flex-col justify-between">
                        <span class="font-tech text-2xl md:text-3xl font-extrabold text-white">{{ paid_orders }}</span>
                        <p class="text-[11px] text-purple-300/80 font-mono mt-0.5 uppercase">Paid orders</p>
                    </div>

                    <div class="glass-card rounded-2xl p-4 flex flex-col justify-between">
                        <div class="flex justify-between items-center">
                            <span class="font-tech text-2xl md:text-3xl font-extrabold text-white">&#8377;{{ revenue }}</span>
                            <button type="button" onclick="openResetModal()" class="text-[10px] text-purple-400 hover:text-fuchsia-300 font-mono transition">Reset</button>
                        </div>
                        <p class="text-[11px] text-purple-300/80 font-mono mt-0.5 uppercase">Revenue</p>
                    </div>

                    <div class="glass-card rounded-2xl p-4 flex flex-col justify-between">
                        <span class="font-tech text-2xl md:text-3xl font-extrabold text-white">{{ total_users }}</span>
                        <p class="text-[11px] text-purple-300/80 font-mono mt-0.5 uppercase">Total users</p>
                    </div>

                    <div class="glass-card rounded-2xl p-4 flex flex-col justify-between neon-border-pink">
                        <div class="flex justify-between items-center mb-1">
                            <span class="text-[10px] font-tech px-2 py-0.5 rounded-md bg-fuchsia-500/20 text-fuchsia-300 border border-fuchsia-500/40">ADMIN</span>
                        </div>
                        <div>
                            <span class="font-tech text-base md:text-lg font-black text-transparent bg-clip-text bg-gradient-to-r from-fuchsia-400 to-cyan-300 truncate block">{{ admin_username }}</span>
                            <p class="text-[10px] text-purple-400 mt-0.5 font-mono">Control Center</p>
                        </div>
                    </div>
                </div>

                <!-- Recent Orders -->
                <div class="glass-card rounded-2xl p-5 space-y-4">
                    <div class="flex items-center justify-between border-b border-purple-900/40 pb-3">
                        <h3 class="font-tech text-base font-bold text-white tracking-wide">Recent orders</h3>
                        <button type="button" onclick="switchTab('tab-orders')" class="text-xs text-fuchsia-400 hover:text-fuchsia-300 font-mono">View All &rarr;</button>
                    </div>

                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs font-mono">
                            <thead class="text-purple-400 uppercase text-[10px] border-b border-purple-900/40">
                                <tr>
                                    <th class="py-2.5 px-3">Order</th>
                                    <th class="py-2.5 px-3">User</th>
                                    <th class="py-2.5 px-3">Item</th>
                                    <th class="py-2.5 px-3">Amount</th>
                                    <th class="py-2.5 px-3 text-right">Status</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y divide-purple-900/30 text-slate-300">
                                {% for oid, uid, uname, item, amt, st, dt, proof in recent_orders %}
                                <tr>
                                    <td class="py-2.5 px-3 text-cyan-400">#{{ oid }}</td>
                                    <td class="py-2.5 px-3">{{ uid }}<span class="block text-[10px] text-purple-400">@{{ uname }}</span></td>
                                    <td class="py-2.5 px-3 text-white">{{ item }}</td>
                                    <td class="py-2.5 px-3 text-white font-semibold">&#8377;{{ "%.2f"|format(amt) }}</td>
                                    <td class="py-2.5 px-3 text-right">
                                        <span class="px-2 py-0.5 rounded text-[10px] uppercase font-tech {{ 'text-emerald-400 bg-emerald-500/10' if st == 'approved' else ('text-amber-400 bg-amber-500/10' if st == 'pending' else 'text-rose-400 bg-rose-500/10') }}">{{ st }}</span>
                                    </td>
                                </tr>
                                {% else %}
                                <tr><td colspan="5" class="py-6 text-center text-purple-400">No orders recorded yet.</td></tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- TAB: CUSTOMER ORDERS (WITH SCREENSHOT VIEWER & APPROVE / REJECT) -->
            <div id="tab-orders" class="tab-content space-y-6 hidden">
                <div class="glass-card rounded-2xl p-5 space-y-4">
                    <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3">Customer Orders &amp; Screenshot Proofs</h3>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs font-mono text-slate-300 border-collapse">
                            <thead class="text-purple-400 uppercase text-[10px] border-b border-purple-900/40">
                                <tr>
                                    <th class="py-2.5 px-3">Order</th>
                                    <th class="py-2.5 px-3">User</th>
                                    <th class="py-2.5 px-3">Plan</th>
                                    <th class="py-2.5 px-3">Amount</th>
                                    <th class="py-2.5 px-3">Proof</th>
                                    <th class="py-2.5 px-3">Status</th>
                                    <th class="py-2.5 px-3 text-right">Action</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y divide-purple-900/30">
                                {% for oid, uid, uname, item, amt, st, dt, proof in all_orders %}
                                <tr class="hover:bg-purple-900/20 transition">
                                    <td class="py-2.5 px-3 text-cyan-400 align-middle">#{{ oid }}</td>
                                    <td class="py-2.5 px-3 align-middle">{{ uid }}<span class="block text-[10px] text-purple-400">@{{ uname }}</span></td>
                                    <td class="py-2.5 px-3 text-white align-middle">{{ item }}</td>
                                    <td class="py-2.5 px-3 font-semibold text-white align-middle">&#8377;{{ "%.2f"|format(amt) }}</td>
                                    <td class="py-2.5 px-3 align-middle">
                                        {% if proof %}
                                            <a href="/admin/photo/{{ oid }}" target="_blank" class="text-cyan-400 hover:text-cyan-300 underline">View Photo</a>
                                        {% else %}
                                            <span class="text-gray-500 italic">No proof</span>
                                        {% endif %}
                                    </td>
                                    <td class="py-2.5 px-3 align-middle">
                                        <span class="px-2 py-0.5 rounded text-[10px] uppercase font-tech {{ 'text-emerald-400 bg-emerald-500/10' if st == 'approved' else ('text-amber-400 bg-amber-500/10' if st == 'pending' else 'text-rose-400 bg-rose-500/10') }}">
                                            {{ st }}
                                        </span>
                                    </td>
                                    <td class="py-2.5 px-3 text-right align-middle">
                                        {% if st == 'pending' %}
                                        <div class="flex items-center justify-end gap-1.5">
                                            <form method="POST" action="/admin/orders/status">
                                                <input type="hidden" name="order_id" value="{{ oid }}">
                                                <input type="hidden" name="action" value="approved">
                                                <button type="submit" class="bg-emerald-500/20 hover:bg-emerald-600 text-emerald-400 hover:text-white font-tech text-[10px] px-2.5 py-1 rounded-lg uppercase tracking-wider transition">Accept</button>
                                            </form>
                                            <form method="POST" action="/admin/orders/status">
                                                <input type="hidden" name="order_id" value="{{ oid }}">
                                                <input type="hidden" name="action" value="rejected">
                                                <button type="submit" class="bg-rose-500/20 hover:bg-rose-600 text-rose-400 hover:text-white font-tech text-[10px] px-2.5 py-1 rounded-lg uppercase tracking-wider transition">Reject</button>
                                            </form>
                                        </div>
                                        {% else %}
                                        <span class="text-purple-400/50 text-[11px] uppercase font-tech font-bold">Processed</span>
                                        {% endif %}
                                    </td>
                                </tr>
                                {% else %}
                                <tr><td colspan="7" class="py-8 text-center text-purple-400">No orders recorded in database.</td></tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- TAB: MANAGE USERS -->
            <div id="tab-users" class="tab-content space-y-6 hidden">
                <div class="glass-card rounded-2xl p-5 space-y-4">
                    <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3">Registered Users</h3>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs font-mono">
                            <thead class="text-purple-400 uppercase text-[10px] border-b border-purple-900/40">
                                <tr>
                                    <th class="py-2.5 px-3">User ID</th>
                                    <th class="py-2.5 px-3">Username</th>
                                    <th class="py-2.5 px-3">Subscription</th>
                                    <th class="py-2.5 px-3 text-right">Actions</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y divide-purple-900/30 text-slate-300">
                                {% for u_id, u_fname, u_uname, u_joined, u_status, u_banned in users_list %}
                                <tr>
                                    <td class="py-2.5 px-3 text-cyan-400">{{ u_id }}<span class="block text-[10px] text-white">{{ u_fname }}</span></td>
                                    <td class="py-2.5 px-3"><a href="https://t.me/{{ u_uname }}" target="_blank" class="text-fuchsia-400">@{{ u_uname }}</a></td>
                                    <td class="py-2.5 px-3">
                                        <form method="POST" action="/admin/users/subscription" class="flex items-center gap-1.5">
                                            <input type="hidden" name="user_id" value="{{ u_id }}">
                                            <select name="plan_name" class="bg-[#070410] border border-purple-900/60 rounded px-2 py-1 text-[11px] text-white">
                                                <option value="Free" {{ 'selected' if u_status == 'Free' else '' }}>Free</option>
                                                {% for p_id, p_name, _, _, _, _ in plans %}
                                                <option value="{{ p_name }}" {{ 'selected' if u_status == p_name else '' }}>{{ p_name }}</option>
                                                {% endfor %}
                                            </select>
                                            <button type="submit" class="bg-purple-900/60 px-2 py-1 rounded text-[10px] font-tech text-white">SET</button>
                                        </form>
                                    </td>
                                    <td class="py-2.5 px-3 text-right">
                                        <form method="POST" action="/admin/users/ban">
                                            <input type="hidden" name="user_id" value="{{ u_id }}">
                                            <input type="hidden" name="status" value="{{ 0 if u_banned else 1 }}">
                                            <button type="submit" class="px-2.5 py-1 rounded text-[10px] font-tech {{ 'bg-emerald-500/20 text-emerald-400' if u_banned else 'bg-rose-500/20 text-rose-400' }}">
                                                {{ 'UNBAN' if u_banned else 'BAN' }}
                                            </button>
                                        </form>
                                    </td>
                                </tr>
                                {% else %}
                                <tr><td colspan="4" class="py-8 text-center text-purple-400">No users found.</td></tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- TAB: BROADCAST -->
            <div id="tab-broadcast" class="tab-content space-y-6 hidden">
                <form method="POST" action="/admin/broadcast/send" class="glass-card rounded-2xl p-6 space-y-4">
                    <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3">Transmit Broadcast</h3>
                    <div>
                        <label class="block text-xs text-purple-300 mb-2 font-mono uppercase">Message Payload</label>
                        <textarea name="broadcast_message" rows="4" required class="w-full bg-[#070410] border border-purple-900/60 rounded-xl p-3 text-sm text-white focus:outline-none"></textarea>
                    </div>
                    <div>
                        <label class="block text-xs text-purple-300 mb-2 font-mono uppercase">Photo URL (Optional)</label>
                        <input type="url" name="broadcast_photo" class="w-full bg-[#070410] border border-purple-900/60 rounded-xl px-4 py-2 text-sm text-white focus:outline-none">
                    </div>
                    <button type="submit" class="bg-gradient-to-r from-fuchsia-600 to-purple-600 text-white font-tech font-bold px-6 py-2.5 rounded-xl text-xs uppercase">Send Broadcast</button>
                </form>
            </div>

            <!-- TAB: SETTINGS & UPI -->
            <div id="tab-settings" class="tab-content space-y-6 hidden">
                <form method="POST" action="/admin/settings/upi" class="glass-card rounded-2xl p-6 space-y-4">
                    <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3">UPI Settings</h3>
                    <div>
                        <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">UPI ID</label>
                        <input type="text" name="upi_id" value="{{ upi_id }}" required class="w-full bg-[#070410] border border-purple-900/60 rounded-xl px-4 py-2 text-sm text-white font-mono focus:outline-none">
                    </div>
                    <div>
                        <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">Payee Name</label>
                        <input type="text" name="payee_name" value="{{ payee_name }}" required class="w-full bg-[#070410] border border-purple-900/60 rounded-xl px-4 py-2 text-sm text-white focus:outline-none">
                    </div>
                    <div>
                        <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">Admin Telegram Chat ID(s)</label>
                        <input type="text" name="admin_chat_id" value="{{ admin_chat_id }}" placeholder="e.g. 123456789, 987654321" class="w-full bg-[#070410] border border-purple-900/60 rounded-xl px-4 py-2 text-sm text-white font-mono focus:outline-none">
                    </div>
                    <button type="submit" class="bg-gradient-to-r from-fuchsia-600 to-purple-600 text-white font-tech font-bold px-5 py-2.5 rounded-xl text-xs uppercase">Save UPI</button>
                </form>

                <form method="POST" action="/admin/password/update" class="glass-card rounded-2xl p-6 space-y-4">
                    <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3">Change Admin Password</h3>
                    <div>
                        <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">Current Password</label>
                        <input type="password" name="current_password" required class="w-full bg-[#070410] border border-purple-900/60 rounded-xl px-4 py-2 text-sm text-white focus:outline-none">
                    </div>
                    <div>
                        <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">New Password</label>
                        <input type="password" name="new_password" required class="w-full bg-[#070410] border border-purple-900/60 rounded-xl px-4 py-2 text-sm text-white focus:outline-none">
                    </div>
                    <button type="submit" class="bg-purple-900/60 text-white font-tech px-5 py-2 rounded-xl text-xs">Update Password</button>
                </form>
            </div>

            <!-- TAB: BOT TOKEN CONFIG -->
            <form method="POST" action="/admin/save">
                <div id="tab-bot" class="tab-content space-y-5 hidden">
                    <div class="glass-card rounded-2xl p-6 space-y-4">
                        <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3">Bot Token Manager</h3>
                        <div>
                            <label class="block text-xs text-purple-300 mb-2 font-mono uppercase">Telegram Bot Token</label>
                            <input type="text" name="bot_token" value="{{ bot_token }}" required class="w-full bg-[#070410] border border-purple-900/60 rounded-xl px-4 py-3 text-sm text-white font-mono focus:outline-none">
                            <span class="text-[11px] text-gray-500 mt-1 block">Updating this token automatically restarts polling without restarting Railway.</span>
                        </div>
                    </div>
                </div>

                <!-- TAB: MEDIA & GREETINGS -->
                <div id="tab-media" class="tab-content space-y-5 hidden">
                    <div class="glass-card rounded-2xl p-6 space-y-4">
                        <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3">Media &amp; Captions</h3>
                        <div>
                            <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">First Message Caption</label>
                            <input type="text" name="first_caption" value="{{ first_caption }}" required class="w-full bg-[#070410] border border-purple-900/60 rounded-xl p-3 text-sm text-white">
                        </div>
                        <div>
                            <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">First Message Media (JSON)</label>
                            <textarea name="first_media" rows="3" required class="w-full bg-[#070410] border border-purple-900/60 rounded-xl p-3 text-sm text-white font-mono">{{ first_media }}</textarea>
                        </div>
                        <div>
                            <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">Plan Intermediate Text</label>
                            <input type="text" name="plans_text" value="{{ plans_text }}" required class="w-full bg-[#070410] border border-purple-900/60 rounded-xl p-3 text-sm text-white">
                        </div>
                        <div>
                            <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">Plan Banner Caption</label>
                            <input type="text" name="plan_caption" value="{{ plan_caption }}" required class="w-full bg-[#070410] border border-purple-900/60 rounded-xl p-3 text-sm text-white">
                        </div>
                        <div>
                            <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">Plan Banner Media (JSON)</label>
                            <textarea name="plan_media" rows="3" required class="w-full bg-[#070410] border border-purple-900/60 rounded-xl p-3 text-sm text-white font-mono">{{ plan_media }}</textarea>
                        </div>
                        <div>
                            <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">Demo Videos (JSON Array or 1 URL per line)</label>
                            <textarea name="demo_videos" rows="4" required class="w-full bg-[#070410] border border-purple-900/60 rounded-xl p-3 text-sm text-white font-mono">{{ demo_videos }}</textarea>
                        </div>
                    </div>

                    <!-- Direct Video File Upload -->
                    <div class="glass-card rounded-2xl p-5 space-y-3">
                        <div class="flex items-center justify-between">
                            <h4 class="font-tech text-xs font-bold text-cyan-300 uppercase tracking-wider">Direct Demo Video Upload</h4>
                            <span id="uploadBadge" class="text-[10px] font-mono text-purple-400 hidden">Uploading...</span>
                        </div>
                        
                        <div class="flex flex-col sm:flex-row items-stretch sm:items-center gap-3">
                            <label class="flex-1 cursor-pointer">
                                <input type="file" id="demo_file_input" multiple accept="video/mp4,video/*" 
                                       class="block w-full text-xs text-slate-300 font-mono file:mr-3 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-xs file:font-tech file:bg-purple-900/60 file:text-cyan-400 hover:file:bg-purple-900/90 bg-[#070410] border border-purple-900/60 rounded-xl p-1.5 focus:outline-none">
                            </label>
                            <button type="button" id="uploadBtn" onclick="uploadDemoVideoFile()" 
                                    class="bg-gradient-to-r from-cyan-600 to-blue-600 hover:from-cyan-500 hover:to-blue-500 text-white font-tech font-bold text-xs py-2.5 px-6 rounded-xl uppercase tracking-wider shadow-lg shadow-cyan-600/30 transition shrink-0">
                                Upload
                            </button>
                        </div>
                        <div id="uploadStatus" class="text-xs font-mono empty:hidden transition-all"></div>
                    </div>
                </div>

                <div id="saveBar" class="pt-4 hidden">
                    <button type="submit" class="w-full bg-gradient-to-r from-fuchsia-600 via-purple-600 to-cyan-600 text-white font-tech font-bold py-3 rounded-xl uppercase tracking-wider">
                        Save Interface Parameters
                    </button>
                </div>
            </form>

            <!-- TAB: SUBSCRIPTION PLANS -->
            <div id="tab-plans" class="tab-content space-y-5 hidden">
                <div class="glass-card rounded-2xl p-6 space-y-4">
                    <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3">Add Subscription Tier</h3>
                    <form method="POST" action="/admin/plans/add" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3 text-xs font-mono">
                        <div>
                            <label class="block text-purple-300 mb-1 uppercase">Plan Key</label>
                            <input type="text" name="plan_id" required placeholder="plan_8" class="w-full bg-[#070410] border border-purple-900/60 rounded px-3 py-2 text-white">
                        </div>
                        <div>
                            <label class="block text-purple-300 mb-1 uppercase">Tier Title</label>
                            <input type="text" name="name" required placeholder="Mega VIP" class="w-full bg-[#070410] border border-purple-900/60 rounded px-3 py-2 text-white">
                        </div>
                        <div>
                            <label class="block text-purple-300 mb-1 uppercase">Price (Rs.)</label>
                            <input type="number" step="any" name="amount" required placeholder="599" class="w-full bg-[#070410] border border-purple-900/60 rounded px-3 py-2 text-white">
                        </div>
                        <div>
                            <label class="block text-purple-300 mb-1 uppercase">Validity</label>
                            <input type="text" name="validity" required placeholder="60 Days" class="w-full bg-[#070410] border border-purple-900/60 rounded px-3 py-2 text-white">
                        </div>
                        <div>
                            <label class="block text-purple-300 mb-1 uppercase">Access Link</label>
                            <input type="text" name="access_link" placeholder="https://t.me/+..." class="w-full bg-[#070410] border border-purple-900/60 rounded px-3 py-2 text-white">
                        </div>
                        <div class="sm:col-span-2 lg:col-span-5">
                            <button type="submit" class="bg-cyan-600 text-white font-tech font-bold py-2.5 px-6 rounded-xl uppercase tracking-wider">+ Add Plan</button>
                        </div>
                    </form>
                </div>

                <div class="glass-card rounded-2xl p-6 space-y-4">
                    <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3">Active Subscription Plans</h3>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-xs font-mono text-slate-300 border-collapse">
                            <thead class="text-purple-400 uppercase text-[10px] border-b border-purple-900/40">
                                <tr>
                                    <th class="p-3">Plan Key</th>
                                    <th class="p-3">Title</th>
                                    <th class="p-3">Price (Rs.)</th>
                                    <th class="p-3">Validity</th>
                                    <th class="p-3">Premium Link</th>
                                    <th class="p-3 text-center">Update</th>
                                    <th class="p-3 text-center">Delete</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y divide-purple-900/30">
                                {% for pid, name, amount, validity, access_link, style in plans %}
                                <tr class="hover:bg-purple-900/20 transition">
                                    <td class="p-3 text-cyan-400 align-middle">{{ pid }}</td>
                                    <form method="POST" action="/admin/plans/update">
                                        <input type="hidden" name="plan_id" value="{{ pid }}">
                                        <td class="p-3 align-middle">
                                            <input type="text" name="name" value="{{ name }}" class="bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white min-w-[130px]">
                                        </td>
                                        <td class="p-3 align-middle">
                                            <input type="number" step="any" name="amount" value="{{ amount }}" class="bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white w-20">
                                        </td>
                                        <td class="p-3 align-middle">
                                            <input type="text" name="validity" value="{{ validity }}" class="bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white w-24">
                                        </td>
                                        <td class="p-3 align-middle">
                                            <input type="text" name="access_link" value="{{ access_link }}" placeholder="https://t.me/+..." class="bg-[#070410] border border-purple-900/60 rounded-lg px-2.5 py-1.5 text-white min-w-[140px]">
                                        </td>
                                        <td class="p-3 text-center align-middle">
                                            <button type="submit" class="bg-purple-900/60 hover:bg-cyan-600 text-white font-tech text-[10px] px-3.5 py-1.5 rounded-lg uppercase tracking-wider transition">Update</button>
                                        </td>
                                    </form>
                                    <td class="p-3 text-center align-middle">
                                        <button type="button" onclick="openDeleteModal('{{ pid }}', '{{ name }}')" class="bg-rose-500/20 hover:bg-rose-600 text-rose-400 hover:text-white font-tech text-[10px] px-3.5 py-1.5 rounded-lg uppercase tracking-wider transition">Delete</button>
                                    </td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

        </div>
    </main>

    <script>
        function toggleSidebar() {
            var sidebar = document.getElementById('sidebar');
            var backdrop = document.getElementById('sidebarBackdrop');
            if (!sidebar) return;
            sidebar.classList.toggle('open');
            if (backdrop) backdrop.classList.toggle('open');
        }

        var titles = {
            'tab-dashboard': 'Dashboard',
            'tab-orders': 'Customer Orders',
            'tab-users': 'Manage Users',
            'tab-broadcast': 'Mass Broadcast',
            'tab-settings': 'Setting & UPI',
            'tab-bot': 'Bot Token Config',
            'tab-media': 'Media & Greetings',
            'tab-plans': 'Subscription Plans'
        };

        function switchTab(tabId) {
            var contents = document.querySelectorAll('.tab-content');
            for (var i = 0; i < contents.length; i++) {
                contents[i].classList.add('hidden');
            }

            var target = document.getElementById(tabId);
            if (target) target.classList.remove('hidden');

            var saveBar = document.getElementById('saveBar');
            if (saveBar) {
                if (tabId === 'tab-bot' || tabId === 'tab-media') {
                    saveBar.classList.remove('hidden');
                } else {
                    saveBar.classList.add('hidden');
                }
            }

            var titleEl = document.getElementById('sectionTitle');
            if (titleEl) titleEl.innerText = titles[tabId] || 'Nagato Panel';

            var navBtns = document.querySelectorAll('.nav-btn');
            for (var j = 0; j < navBtns.length; j++) {
                navBtns[j].classList.remove('bg-purple-900/40', 'border', 'border-fuchsia-500/30', 'text-cyan-400');
                navBtns[j].classList.add('text-slate-400');
            }

            var activeNav = document.getElementById('nav-' + tabId);
            if (activeNav) {
                activeNav.classList.add('bg-purple-900/40', 'border', 'border-fuchsia-500/30', 'text-cyan-400');
                activeNav.classList.remove('text-slate-400');
            }

            if (window.innerWidth < 768) {
                var sidebar = document.getElementById('sidebar');
                if (sidebar && sidebar.classList.contains('open')) {
                    toggleSidebar();
                }
            }
        }

        var urlParams = new URLSearchParams(window.location.search);
        var requestedTab = urlParams.get('tab');
        if (requestedTab && titles[requestedTab]) {
            switchTab(requestedTab);
        }

        function openDeleteModal(planId, planName) {
            document.getElementById('modalPlanIdInput').value = planId;
            document.getElementById('modalPlanId').innerText = planId;
            document.getElementById('modalPlanName').innerText = planName;
            document.getElementById('confirmDeleteModal').classList.add('active');
        }

        function closeDeleteModal() {
            document.getElementById('confirmDeleteModal').classList.remove('active');
        }

        function openResetModal() {
            document.getElementById('confirmResetModal').classList.add('active');
        }

        function closeResetModal() {
            document.getElementById('confirmResetModal').classList.remove('active');
        }

        async function uploadDemoVideoFile() {
            var input = document.getElementById('demo_file_input');
            var statusEl = document.getElementById('uploadStatus');
            var btn = document.getElementById('uploadBtn');
            var badge = document.getElementById('uploadBadge');

            if (!input.files || input.files.length === 0) {
                statusEl.innerText = "⚠️ Please select at least one video file.";
                statusEl.className = "text-xs text-rose-400 font-mono mt-1";
                return;
            }

            var count = input.files.length;
            btn.disabled = true;
            btn.innerText = "Uploading...";
            badge.classList.remove('hidden');
            statusEl.innerText = "⏳ Uploading " + count + " video(s)... Please wait.";
            statusEl.className = "text-xs text-cyan-400 font-mono mt-1 animate-pulse";

            var formData = new FormData();
            for (var i = 0; i < count; i++) {
                formData.append("files", input.files[i]);
            }

            try {
                var res = await fetch('/admin/upload-demo-video', {
                    method: 'POST',
                    body: formData
                });

                var data = await res.json();
                btn.disabled = false;
                btn.innerText = "Upload";
                badge.classList.add('hidden');

                if (res.ok && data.status === 'success') {
                    statusEl.innerText = "✅ Successfully uploaded " + (data.urls ? data.urls.length : count) + " video(s)!";
                    statusEl.className = "text-xs text-emerald-400 font-mono mt-1";

                    var txtArea = document.querySelector('textarea[name="demo_videos"]');
                    if (txtArea && data.urls) {
                        var existing = txtArea.value.trim();
                        try {
                            var parsed = JSON.parse(existing);
                            if (Array.isArray(parsed)) {
                                parsed.push(...data.urls);
                                txtArea.value = JSON.stringify(parsed, null, 2);
                            } else {
                                txtArea.value = (existing ? existing + "\\n" + data.urls.join("\\n") : data.urls.join("\\n")).trim();
                            }
                        } catch(err) {
                            txtArea.value = (existing ? existing + "\\n" + data.urls.join("\\n") : data.urls.join("\\n")).trim();
                        }
                    }
                    input.value = "";
                } else {
                    statusEl.innerText = "❌ Upload failed: " + (data.message || "Server rejected request");
                    statusEl.className = "text-xs text-rose-400 font-mono mt-1";
                }
            } catch(e) {
                btn.disabled = false;
                btn.innerText = "Upload";
                badge.classList.add('hidden');
                statusEl.innerText = "❌ Upload failed: Connection interrupted or file too large.";
                statusEl.className = "text-xs text-rose-400 font-mono mt-1";
            }
        }
    </script>
</body>
</html>"""

# ==========================================
# FASTAPI HTTP ROUTES & CONTROLLERS
# ==========================================
@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str | None = None):
    if request.cookies.get(AUTH_COOKIE_NAME) == AUTH_SECRET:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    tmpl = Template(LOGIN_PAGE)
    return HTMLResponse(content=tmpl.render(error=error))

@app.post("/login")
async def process_login(username: str = Form(...), password: str = Form(...)):
    stored_password = await get_setting("admin_password") or DEFAULT_PASS
    if username == ADMIN_USER and password == stored_password:
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key=AUTH_COOKIE_NAME, value=AUTH_SECRET, httponly=True, max_age=86400 * 7)
        return response
    tmpl = Template(LOGIN_PAGE)
    return HTMLResponse(content=tmpl.render(error="Invalid administrator credentials."), status_code=status.HTTP_401_UNAUTHORIZED)

@app.get("/logout")
async def logout_admin():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key=AUTH_COOKIE_NAME)
    return response

@app.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request, message: str | None = None, is_auth: bool = Depends(require_admin)):
    try:
        metrics = await get_dashboard_metrics()
        users_list = await get_all_users_detailed()
        tmpl = Template(DASHBOARD_PAGE)
        html = tmpl.render(
            message=message,
            admin_username=ADMIN_USER,
            is_online=manager.bot is not None,
            paid_orders=metrics.get("paid_orders", 0),
            revenue=metrics.get("revenue", "0.00"),
            total_users=metrics.get("total_users", 0),
            recent_orders=metrics.get("recent_orders", []),
            all_orders=metrics.get("all_orders", []),
            users_list=users_list or [],
            bot_token=await get_setting("bot_token"),
            admin_chat_id=await get_setting("admin_chat_id"),
            upi_id=await get_setting("upi_id"),
            payee_name=await get_setting("payee_name"),
            maintenance=await get_setting("maintenance"),
            first_media=await get_setting("first_media"),
            first_caption=await get_setting("first_caption"),
            plan_media=await get_setting("plan_media"),
            plan_caption=await get_setting("plan_caption"),
            plans_text=await get_setting("plans_text"),
            demo_videos=await get_setting("demo_videos"),
            plans=await get_all_plans(),
        )
        return HTMLResponse(content=html)
    except Exception as err:
        logging.error(f"Dashboard render error: {err}")
        return HTMLResponse(f"<h3>Dashboard Error: {err}</h3>", status_code=500)

@app.post("/admin/upload-demo-video")
async def upload_demo_video_file(
    request: Request,
    files: list[UploadFile] = File(...),
    is_auth: bool = Depends(require_admin),
):
    try:
        base_url = str(request.base_url).rstrip("/")
        if "railway.app" in base_url and base_url.startswith("http://"):
            base_url = base_url.replace("http://", "https://")

        raw = await get_setting("demo_videos")
        try:
            curr = json.loads(raw) if raw else []
        except Exception:
            curr = [line.strip() for line in raw.splitlines() if line.strip()]

        new_urls = []
        for f in files:
            clean_name = f"{int(datetime.now().timestamp())}_{f.filename.replace(' ', '_')}"
            dest_path = os.path.join(UPLOAD_DIR, clean_name)
            with open(dest_path, "wb") as buffer:
                shutil.copyfileobj(f.file, buffer)

            file_url = f"{base_url}/static/uploads/{clean_name}"
            new_urls.append(file_url)
            curr.append(file_url)
            await asyncio.sleep(0.01)

        await update_setting("demo_videos", json.dumps(curr))
        return JSONResponse({"status": "success", "urls": new_urls})
    except Exception as e:
        logging.error(f"Upload error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/admin/photo/{order_id}")
async def view_screenshot_proof(order_id: int, is_auth: bool = Depends(require_admin)):
    async with DB_WRITE_LOCK:
        db = await get_db_connection()
        try:
            async with db.execute("SELECT screenshot_file_id FROM payments WHERE id = ?", (int(order_id),)) as cur:
                row = await cur.fetchone()
        finally:
            await db.close()

    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="Proof not found")

    file_id = row[0]
    if not manager.bot:
        raise HTTPException(status_code=500, detail="Bot is offline")

    f = await manager.bot.get_file(file_id)
    photo_bytes = await manager.bot.download_file(f.file_path)
    return Response(content=photo_bytes.read(), media_type="image/jpeg")

@app.post("/admin/orders/status")
async def handle_order_status_change(
    order_id: int = Form(...),
    action: str = Form(...),
    is_auth: bool = Depends(require_admin),
):
    async with DB_WRITE_LOCK:
        db = await get_db_connection()
        try:
            async with db.execute("SELECT user_id, plan_name FROM payments WHERE id = ?", (int(order_id),)) as cur:
                row = await cur.fetchone()
            if not row:
                return RedirectResponse(url="/?message=Order+not+found&tab=tab-orders", status_code=status.HTTP_303_SEE_OTHER)

            user_id, plan_name = row
            if action == "approved":
                await db.execute("UPDATE payments SET status = 'approved' WHERE id = ?", (int(order_id),))
                await db.execute("UPDATE users SET premium_status = ? WHERE user_id = ?", (plan_name, user_id))
                await db.commit()
                await notify_payment_approved(user_id, plan_name)
                msg = f"Order #{order_id} approved!"
            else:
                await db.execute("UPDATE payments SET status = 'rejected' WHERE id = ?", (int(order_id),))
                await db.commit()
                if manager.bot:
                    try:
                        await manager.bot.send_message(user_id, "❌ Payment verification failed. Your proof was rejected.")
                    except Exception:
                        pass
                msg = f"Order #{order_id} rejected."
        finally:
            await db.close()

    return RedirectResponse(url=f"/?message={urllib.parse.quote_plus(msg)}&tab=tab-orders", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/broadcast/send")
async def handle_broadcast_send(
    broadcast_message: str = Form(...),
    broadcast_photo: str = Form(""),
    is_auth: bool = Depends(require_admin),
):
    if not manager.bot:
        return RedirectResponse(
            url="/?message=Error:+Bot+is+offline.+Add+token+first.&tab=tab-broadcast",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    db = await get_db_connection()
    try:
        async with db.execute("SELECT user_id FROM users WHERE is_banned=0") as cur:
            users = await cur.fetchall()
    finally:
        await db.close()

    sent = 0
    cleaned_photo = broadcast_photo.strip()

    for (uid,) in users:
        try:
            if cleaned_photo:
                await manager.bot.send_photo(chat_id=uid, photo=cleaned_photo, caption=broadcast_message)
            else:
                await manager.bot.send_message(chat_id=uid, text=broadcast_message)
            sent += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                if cleaned_photo:
                    await manager.bot.send_photo(chat_id=uid, photo=cleaned_photo, caption=broadcast_message)
                else:
                    await manager.bot.send_message(chat_id=uid, text=broadcast_message)
                sent += 1
            except Exception:
                pass
        except Exception:
            pass

    return RedirectResponse(
        url=f"/?message=Broadcast+successfully+sent+to+{sent}+users!&tab=tab-broadcast",
        status_code=status.HTTP_303_SEE_OTHER,
    )

@app.post("/admin/users/ban")
async def handle_ban_toggle(request: Request, is_auth: bool = Depends(require_admin)):
    form = await request.form()
    user_id = int(form.get("user_id"))
    status_int = int(form.get("status"))
    await set_user_ban_status(user_id, status_int)
    action_text = "banned" if status_int == 1 else "unbanned"
    return RedirectResponse(url=f"/?message=User+{user_id}+{action_text}&tab=tab-users", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/users/subscription")
async def handle_subscription_change(request: Request, is_auth: bool = Depends(require_admin)):
    form = await request.form()
    user_id = int(form.get("user_id"))
    plan_name = str(form.get("plan_name", "Free")).strip()
    await update_user_subscription(user_id, plan_name)
    if manager.bot:
        if plan_name == "Free":
            try:
                await manager.bot.send_message(user_id, "Your premium subscription has expired. You are on the Free tier.")
            except Exception:
                pass
        else:
            await notify_payment_approved(user_id, plan_name)
    return RedirectResponse(url=f"/?message=User+{user_id}+updated+to+{urllib.parse.quote_plus(plan_name)}&tab=tab-users", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/settings/upi")
async def save_upi(
    upi_id: str = Form(...),
    payee_name: str = Form(...),
    admin_chat_id: str = Form(""),
    is_auth: bool = Depends(require_admin),
):
    await update_setting("upi_id", upi_id.strip())
    await update_setting("payee_name", payee_name.strip())
    await update_setting("admin_chat_id", admin_chat_id.strip())
    return RedirectResponse(url="/?message=UPI+settings+saved&tab=tab-settings", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/password/update")
async def update_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    is_auth: bool = Depends(require_admin),
):
    stored = await get_setting("admin_password") or DEFAULT_PASS
    if current_password != stored:
        return RedirectResponse(url="/?message=Error:+Incorrect+current+password&tab=tab-settings", status_code=status.HTTP_303_SEE_OTHER)
    await update_setting("admin_password", new_password.strip())
    return RedirectResponse(url="/?message=Password+updated&tab=tab-settings", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/save")
async def save_general_config(
    bot_token: str = Form(...),
    first_caption: str = Form(...),
    first_media: str = Form(...),
    plans_text: str = Form(...),
    plan_caption: str = Form(...),
    plan_media: str = Form(...),
    demo_videos: str = Form(...),
    is_auth: bool = Depends(require_admin),
):
    old_token = await get_setting("bot_token")
    cleaned_token = bot_token.strip()

    await update_setting("bot_token", cleaned_token)
    await update_setting("first_caption", first_caption.strip())
    await update_setting("first_media", first_media.strip())
    await update_setting("plans_text", plans_text.strip())
    await update_setting("plan_caption", plan_caption.strip())
    await update_setting("plan_media", plan_media.strip())
    await update_setting("demo_videos", demo_videos.strip())

    if cleaned_token and (cleaned_token != old_token or not manager.bot):
        await manager.restart(cleaned_token)

    return RedirectResponse(url="/?message=Parameters+saved+and+applied&tab=tab-media", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/revenue/reset")
async def reset_revenue_counters(is_auth: bool = Depends(require_admin)):
    async with DB_WRITE_LOCK:
        db = await get_db_connection()
        try:
            await db.execute("DELETE FROM payments")
            await db.commit()
        finally:
            await db.close()
    return RedirectResponse(url="/?message=Revenue+reset+to+zero&tab=tab-dashboard", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/plans/add")
async def add_plan_entry(
    plan_id: str = Form(...),
    name: str = Form(...),
    amount: float = Form(...),
    validity: str = Form(...),
    access_link: str = Form(""),
    is_auth: bool = Depends(require_admin),
):
    await add_new_plan(plan_id.strip(), name.strip(), amount, validity.strip(), access_link.strip())
    return RedirectResponse(url="/?message=New+plan+added&tab=tab-plans", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/plans/update")
async def update_plan_entry(
    plan_id: str = Form(...),
    name: str = Form(...),
    amount: float = Form(...),
    validity: str = Form(...),
    access_link: str = Form(""),
    is_auth: bool = Depends(require_admin),
):
    await update_plan(plan_id, name.strip(), amount, validity.strip(), access_link.strip())
    return RedirectResponse(url="/?message=Plan+updated&tab=tab-plans", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/admin/plans/delete")
async def delete_plan_entry(plan_id: str = Form(...), is_auth: bool = Depends(require_admin)):
    await delete_plan(plan_id.strip())
    return RedirectResponse(url="/?message=Plan+deleted&tab=tab-plans", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/health")
async def health_check():
    return {"status": "ok", "bot_online": manager.bot is not None}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
