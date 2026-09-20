# -*- coding: utf-8 -*-
import os
import io
import json
import math
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
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, InputMediaPhoto, InputMediaVideo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from fastapi import FastAPI, Request, Form, File, UploadFile, HTTPException, Depends, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
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
DB_NAME = os.path.join(DATA_DIR, "pompom_v3.db")

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
                    access_link TEXT DEFAULT ''
                )
            """)

            await db.execute("CREATE INDEX IF NOT EXISTS idx_users_joined ON users(joined_at DESC);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id);")

            defaults = {
                "bot_token": INITIAL_BOT_TOKEN,
                "admin_password": DEFAULT_PASS,
                "admin_chat_id": "",
                "maintenance": "off",
                "upi_id": "anmolvlv@ibl",
                "payee_name": "POM POM BOT V3",
                "first_media": "https://picsum.photos/800/800",
                "first_caption": "💎 GET PREMIUM",
                "plan_media": "https://picsum.photos/800/700",
                "plan_caption": " ══════« PRO PLAN »═════",
                "demo_videos": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4\nhttps://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerEscapes.mp4"
            }

            for k, v in defaults.items():
                await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

            default_plans = [
                ("plan_1", "1 Month VIP", 79.0, "30 Days", ""),
                ("plan_2", "2 Months VIP", 149.0, "60 Days", ""),
                ("plan_3", "3 Months VIP", 199.0, "90 Days", ""),
                ("plan_4", "6 Months VIP", 299.0, "180 Days", ""),
                ("plan_5", "1 Year VIP", 399.0, "365 Days", ""),
                ("plan_6", "Lifetime Access", 499.0, "Lifetime", ""),
                ("plan_7", "Ultra Mega VIP", 799.0, "Lifetime", ""),
            ]
            for p in default_plans:
                await db.execute(
                    "INSERT OR IGNORE INTO plans (plan_id, name, amount, validity, access_link) VALUES (?, ?, ?, ?, ?)",
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

# Strictly selects 5 columns to eliminate Jinja unpacking errors
async def get_all_plans():
    db = await get_db_connection()
    try:
        async with db.execute("SELECT plan_id, name, amount, validity, COALESCE(access_link, '') FROM plans ORDER BY plan_id ASC") as cur:
            return await cur.fetchall()
    finally:
        await db.close()

async def get_plan(plan_id: str):
    db = await get_db_connection()
    try:
        async with db.execute("SELECT plan_id, name, amount, validity, COALESCE(access_link, '') FROM plans WHERE plan_id = ?", (plan_id,)) as cur:
            return await cur.fetchone()
    finally:
        await db.close()

async def get_plan_by_name(name: str):
    db = await get_db_connection()
    try:
        async with db.execute("SELECT plan_id, name, amount, validity, COALESCE(access_link, '') FROM plans WHERE name = ?", (name,)) as cur:
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
                "INSERT INTO plans (plan_id, name, amount, validity, access_link) VALUES (?, ?, ?, ?, ?)",
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

async def get_paginated_users(limit: int = 50, offset: int = 0, search: str = ""):
    db = await get_db_connection()
    try:
        search_query = f"%{search.strip().lstrip('@')}%"
        if search.strip():
            async with db.execute(
                "SELECT COUNT(*) FROM users WHERE CAST(user_id AS TEXT) LIKE ? OR username LIKE ? OR full_name LIKE ?",
                (search_query, search_query, search_query)
            ) as cur:
                total_count = (await cur.fetchone())[0]

            async with db.execute(
                """
                SELECT user_id, full_name, username, joined_at, premium_status, is_banned 
                FROM users 
                WHERE CAST(user_id AS TEXT) LIKE ? OR username LIKE ? OR full_name LIKE ?
                ORDER BY joined_at DESC LIMIT ? OFFSET ?
                """,
                (search_query, search_query, search_query, limit, offset),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute("SELECT COUNT(*) FROM users") as cur:
                total_count = (await cur.fetchone())[0]

            async with db.execute(
                "SELECT user_id, full_name, username, joined_at, premium_status, is_banned FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ) as cur:
                rows = await cur.fetchall()

        return rows, total_count
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
            ORDER BY p.id DESC LIMIT 50
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
# NON-BLOCKING QR GENERATOR
# ==========================================
def _generate_qr_sync(upi_url: str) -> io.BytesIO:
    qr = segno.make(upi_url, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=8, border=2)
    buf.seek(0)
    return buf

async def generate_upi_qr(plan_name: str, amount: float) -> io.BytesIO:
    upi_id = await get_setting("upi_id") or "anmolvlv@ibl"
    payee_name = await get_setting("payee_name") or "POM POM BOT V3"
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
# DYNAMIC BOT RUNTIME CONTROLLER
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
                res_data = await resp.json()
                if not res_data.get("ok"):
                    logging.warning(f"[Telegram API Fail] {method}: {res_data}")
                return res_data

    async def start(self, token: str):
        if not token or token == "YOUR_BOT_TOKEN_HERE":
            logging.warning("[BotManager] No token set.")
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
        except Exception:
            pass

        async def runner():
            while True:
                try:
                    await self.dp.start_polling(
                        self.bot,
                        drop_pending_updates=True,
                        allowed_updates=["message", "callback_query"],
                    )
                    break
                except TelegramConflictError:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logging.error(f"[BotManager] Polling error: {e}")
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

    async def restart(self, new_token: str):
        await self.stop()
        await self.start(new_token)

manager = BotManager()

# ==========================================
# KEYBOARDS WITH SOLID COLORS
# ==========================================
class PaymentStates(StatesGroup):
    waiting_for_screenshot = State()

# Bottom Persistent Keyboard with Solid Green and Red
BOTTOM_REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "💎 GET PREMIUM", "style": "success"}],
        [{"text": "🥵 DEMO", "style": "danger"}]
    ],
    "resize_keyboard": True
}

# The 3-Options Menu with Solid Colors (Green, Red, Blue)
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
    """
    Builds the original styled colored inline buttons with permanent plan_id routing.
    Changing the plan name, price, or validity days will NOT break these buttons.
    """
    plans = await get_all_plans()
    rows = []
    palette = ["success", "danger", "primary"]
    for i, p in enumerate(plans):
        pid = p[0]
        name = p[1]
        amount = p[2]
        color = palette[i % len(palette)]
        rows.append([
            {
                "text": f"{name} - ₹{int(amount)}",
                "callback_data": f"buy_plan:{pid}",
                "style": color
            }
        ])
    return {"inline_keyboard": rows}

# ==========================================
# DIRECT LINK & FILE ID SANITIZER HELPER
# ==========================================
def parse_direct_links(text_block: str, default_type: str = "photo") -> list[dict]:
    if not text_block or not text_block.strip():
        return []
    text_block = text_block.strip()
    if text_block.startswith("[") and text_block.endswith("]"):
        try:
            return json.loads(text_block)
        except Exception:
            pass

    items = []
    for line in text_block.splitlines():
        clean_target = line.strip()
        if not clean_target:
            continue
        
        # Strip internal quotes or accidental formatting noise
        clean_target = clean_target.replace('"', '').replace("'", "")
        
        m_type = default_type
        lower_line = clean_target.lower()
        if any(lower_line.endswith(ext) for ext in [".mp4", ".mov", ".avi", ".mkv", ".webm"]):
            m_type = "video"
        elif clean_target.startswith("BAACAg"): # Standard Telegram video file_id prefix
            m_type = "video"
        elif clean_target.startswith("AgACAg"): # Standard Telegram photo file_id prefix
            m_type = "photo"

        items.append({"type": m_type, "url": clean_target})
    return items

# ==========================================
# UNIVERSAL MEDIA SENDER (CRASH-PROOF FILE ID DISPATCHER)
# ==========================================
async def send_universal_media(chat_id: int, target: str, caption: str = None, reply_markup: dict = None, preferred: str = "photo"):
    """
    Tries sending via Photo, Video, or Document. 
    If Telegram rejects a file_id because of type mismatch (e.g. document sent to photo),
    it automatically falls back gracefully without crashing or hanging the bot.
    """
    target = target.strip()
    order = ["sendPhoto", "sendVideo", "sendDocument"] if preferred == "photo" else ["sendVideo", "sendPhoto", "sendDocument"]
    key_map = {
        "sendPhoto": "photo",
        "sendVideo": "video",
        "sendDocument": "document"
    }

    for method in order:
        key = key_map[method]
        payload = {
            "chat_id": chat_id,
            key: target,
            "caption": caption,
            "parse_mode": "HTML"
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        res = await manager.send_raw(method, payload)
        if res and res.get("ok"):
            return True

    # If all direct media senders fail, fall back cleanly to text
    text_msg = caption if caption else "Attached File"
    payload = {
        "chat_id": chat_id,
        "text": f"{text_msg}\n\n📎 <code>{target}</code>",
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await manager.send_raw("sendMessage", payload)
    return False

# ==========================================
# BOT DISPATCHER FLOW
# ==========================================
bot_router = Router()

async def send_first_media_with_bottom_bar(chat_id: int, media_list: list, caption: str):
    if not media_list:
        await manager.send_raw("sendMessage", {
            "chat_id": chat_id,
            "text": caption,
            "parse_mode": "HTML",
            "reply_markup": BOTTOM_REPLY_KEYBOARD
        })
        return

    if len(media_list) == 1:
        item = media_list[0]
        await send_universal_media(
            chat_id=chat_id,
            target=item["url"],
            caption=caption,
            reply_markup=BOTTOM_REPLY_KEYBOARD,
            preferred=item["type"]
        )
        return

    # Multiple items album handling
    chunks = [media_list[i:i + 10] for i in range(0, len(media_list), 10)]
    for chunk_idx, chunk in enumerate(chunks):
        if len(chunk) == 1:
            item = chunk[0]
            await send_universal_media(
                chat_id=chat_id,
                target=item["url"],
                caption=caption if chunk_idx == len(chunks) - 1 else None,
                preferred=item["type"]
            )
            continue

        media_group = []
        is_last_chunk = (chunk_idx == len(chunks) - 1)
        for item_idx, item in enumerate(chunk):
            is_last_item = is_last_chunk and (item_idx == len(chunk) - 1)
            cap = caption if is_last_item else None
            if item["type"] == "video":
                media_group.append(InputMediaVideo(media=item["url"], caption=cap, parse_mode="HTML"))
            else:
                media_group.append(InputMediaPhoto(media=item["url"], caption=cap, parse_mode="HTML"))
        try:
            await manager.bot.send_media_group(chat_id=chat_id, media=media_group)
        except Exception as e:
            logging.error(f"[Media Group Fallback]: {e}")
            await manager.send_raw("sendMessage", {
                "chat_id": chat_id,
                "text": caption,
                "parse_mode": "HTML"
            })

    # Note: Duplicate 👇 removed from here to ensure clean flow with no duplicate messages

async def send_plan_presentation(chat_id: int):
    # 1. Send intermediate banner notice
    await manager.send_raw("sendMessage", {
        "chat_id": chat_id,
        "text": " ══════« CHOOSE PLAN »═════"
    })

    # 2. Extract plan image and caption
    plan_media_raw = await get_setting("plan_media")
    plan_caption = await get_setting("plan_caption") or " ══════« PRO PLAN »═════"
    plan_media = parse_direct_links(plan_media_raw, default_type="photo")

    photo_target = plan_media[0]["url"] if plan_media else "https://picsum.photos/800/700"
    plans_kbd = await build_dynamic_plans_keyboard()

    # 3. Universal crash-proof sender (automatically handles file_id mismatches)
    await send_universal_media(
        chat_id=chat_id,
        target=photo_target,
        caption=plan_caption,
        reply_markup=plans_kbd,
        preferred="photo"
    )

async def play_demo_videos_fast(chat_id: int):
    raw_demos = await get_setting("demo_videos")
    demo_items = parse_direct_links(raw_demos, default_type="video")

    if not demo_items:
        await manager.send_raw("sendMessage", {
            "chat_id": chat_id,
            "text": "📺 No demo videos available."
        })
        return

    for item in demo_items:
        await send_universal_media(
            chat_id=chat_id,
            target=item["url"],
            caption="📺 <b>Demo Video Preview</b>",
            reply_markup=DEMO_ITEM_KEYBOARD,
            preferred="video"
        )

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
        f"Your membership for <b>{plan_name}</b> is now fully activated!\n"
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
        logging.warning(f"Delivery error to {user_id}: {e}")

# ==========================================
# ADMIN FILE ID EXTRACTOR (SUPPORTS PHOTOS, VIDEOS, DOCUMENTS)
# ==========================================
@bot_router.message(F.video | F.photo | F.document, StateFilter("*"))
async def admin_get_file_id(message: types.Message, state: FSMContext):
    admin_ids = await get_admin_ids()
    if message.from_user.id not in admin_ids:
        return

    current_state = await state.get_state()
    if current_state is not None:
        return

    if message.video:
        file_id = message.video.file_id
        media_type = "Video 🎬"
    elif message.photo:
        file_id = message.photo[-1].file_id
        media_type = "Photo 📸"
    elif message.document:
        file_id = message.document.file_id
        media_type = f"Document / File 📁 ({message.document.file_name or 'file'})"
    else:
        return

    reply_text = (
        f"⚡ <b>Admin File ID Grabber</b>\n\n"
        f"<b>Type:</b> {media_type}\n"
        f"<b>Telegram File ID:</b>\n"
        f"<code>{file_id}</code>\n\n"
        f"<i>💡 Direct paste enabled! Simply copy this code and paste it directly into your Admin Panel.</i>"
    )
    await message.reply(reply_text, parse_mode="HTML")

# ==========================================
# CORE BOT COMMANDS
# ==========================================
@bot_router.message(CommandStart(), StateFilter("*"))
async def handle_start(message: types.Message, state: FSMContext):
    await state.clear()
    await add_or_update_user(message.from_user)

    first_media_raw = await get_setting("first_media")
    first_caption = await get_setting("first_caption") or "💎 GET PREMIUM"
    media_list = parse_direct_links(first_media_raw, default_type="photo")

    # 1. First Message: Media with multiline caption + persistent bottom bar
    await send_first_media_with_bottom_bar(message.chat.id, media_list, first_caption)

    # 2. Second Message: Choose option with the 3 solid-colored buttons
    await manager.send_raw("sendMessage", {
        "chat_id": message.chat.id,
        "text": "👇 Choose an option:",
        "reply_markup": THREE_OPTIONS_KEYBOARD
    })

    # 3. Third Message: Hand pointing emoji (single instance)
    await manager.send_raw("sendMessage", {
        "chat_id": message.chat.id,
        "text": "👇"
    })

# Handles clicking inline green "GET PREMIUM"
@bot_router.callback_query(F.data == "menu_get_premium", StateFilter("*"))
async def on_get_premium_click(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await send_plan_presentation(callback.message.chat.id)

# Handles tapping bottom persistent green "GET PREMIUM"
@bot_router.message(F.text == "💎 GET PREMIUM", StateFilter("*"))
async def on_text_get_premium(message: types.Message, state: FSMContext):
    await state.clear()
    await send_plan_presentation(message.chat.id)

# Handles clicking inline red "DEMO"
@bot_router.callback_query(F.data == "menu_demo", StateFilter("*"))
async def on_demo_click(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await play_demo_videos_fast(callback.message.chat.id)

# Handles tapping bottom persistent red "DEMO"
@bot_router.message(F.text == "🥵 DEMO", StateFilter("*"))
async def on_text_demo(message: types.Message, state: FSMContext):
    await state.clear()
    await play_demo_videos_fast(message.chat.id)

@bot_router.callback_query(F.data == "menu_how_to", StateFilter("*"))
async def on_how_to_click(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "📖 <b>How to Get Premium:</b>\n\n"
        "1. Click <b>💎 GET PREMIUM</b>\n"
        "2. Choose your preferred plan from the list.\n"
        "3. Scan the generated UPI QR code & pay.\n"
        "4. Click <b>GET LINK</b> and upload your payment screenshot.\n"
        "5. The admin will verify and your access link arrives instantly!",
        parse_mode="HTML"
    )

# Robust Plan Choice Handler (Uses Immutable plan_id)
@bot_router.callback_query(F.data.startswith("buy_plan:"), StateFilter("*"))
async def process_plan_choice_stable(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()

    pid = callback.data.split(":", 1)[1]
    plan = await get_plan(pid)
    if not plan:
        await callback.message.answer("⚠️ Selected plan is no longer available.")
        return

    pid, plan_name, amount, validity, _ = plan
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

@bot_router.callback_query(F.data.startswith("submit_"), StateFilter("*"))
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
    await message.reply("✅ Payment screenshot received. Verification in progress.", reply_markup=BOTTOM_REPLY_KEYBOARD)

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

@bot_router.callback_query(F.data.startswith("adm_pay:"), StateFilter("*"))
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
                        await manager.bot.send_message(t_uid, "❌ Payment verification failed.")
                    except Exception:
                        pass
                    await callback.message.edit_caption(caption=callback.message.caption + "\n\nSTATUS: REJECTED ❌")
            finally:
                await db.close()
        await callback.answer("Order updated.")

manager.dp.include_router(bot_router)

# ==========================================
# FASTAPI APPLICATION & CONTROLLERS
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
# TEMPLATES (NAGATO CYBERPUNK PANEL)
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
                    <svg class="w-7 h-7 text-fuchsia-400 drop-shadow-[0_0_8px_rgba(232,121,249,0.6)]" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M15.59 14.37a6 6 0 01-5.84 7.38v-4.8m5.84-2.58a14.98 14.98 0 006.16-12.12A14.98 14.98 0 009.631 8.41m5.96 5.96a14.926 14.926 0 01-5.841 2.58m-.119-8.54a6 6 0 00-7.381 5.84h4.8m2.581-5.84a14.927 14.927 0 00-2.58 5.84m2.699 2.7c-.103.021-.207.041-.311.06a15.09 15.09 0 01-2.448-2.448 14.9 14.9 0 01.06-.312m-2.24 2.39a4.493 4.493 0 00-1.757 4.306 4.493 4.493 0 004.306-1.758M16.5 9a1.5 1.5 0 11-3 0 1.5 1.5 0 013 0z"/>
                    </svg>
                    <h1 class="text-xl font-bold tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-purple-300 via-fuchsia-300 to-cyan-300">
                        Nagato Panel
                    </h1>
                </div>
                <div class="font-tech text-[10px] tracking-[0.25em] text-cyan-400/90 font-bold uppercase pl-9">
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
            <h4 class="font-tech text-sm font-bold text-white flex items-center gap-2">
                <svg class="w-5 h-5 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" /></svg>
                Reset Revenue
            </h4>
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
            <h4 class="font-tech text-sm font-bold text-white flex items-center gap-2">
                <svg class="w-5 h-5 text-rose-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" /></svg>
                Confirm Deletion
            </h4>
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
                    <svg class="w-7 h-7 text-fuchsia-400 drop-shadow-[0_0_8px_rgba(232,121,249,0.7)]" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M15.59 14.37a6 6 0 01-5.84 7.38v-4.8m5.84-2.58a14.98 14.98 0 006.16-12.12A14.98 14.98 0 009.631 8.41m5.96 5.96a14.926 14.926 0 01-5.841 2.58m-.119-8.54a6 6 0 00-7.381 5.84h4.8m2.581-5.84a14.927 14.927 0 00-2.58 5.84m2.699 2.7c-.103.021-.207.041-.311.06a15.09 15.09 0 01-2.448-2.448 14.9 14.9 0 01.06-.312m-2.24 2.39a4.493 4.493 0 00-1.757 4.306 4.493 4.493 0 004.306-1.758M16.5 9a1.5 1.5 0 11-3 0 1.5 1.5 0 013 0z"/>
                    </svg>
                    <div>
                        <span class="font-tech font-bold text-sm text-transparent bg-clip-text bg-gradient-to-r from-fuchsia-400 to-cyan-300 tracking-wider block">Nagato Panel</span>
                        <span class="font-tech text-[10px] tracking-wider text-cyan-300 uppercase block">POM POM BOT V3</span>
                    </div>
                </div>
                <button type="button" onclick="toggleSidebar()" class="md:hidden text-purple-400 p-1">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
                </button>
            </div>

            <nav class="space-y-1.5 text-xs">
                <button type="button" onclick="switchTab('tab-dashboard')" id="nav-tab-dashboard" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-semibold bg-purple-900/40 border border-fuchsia-500/30 text-cyan-400">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"/></svg>
                    Dashboard
                </button>
                <button type="button" onclick="switchTab('tab-orders')" id="nav-tab-orders" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/></svg>
                    Customer Orders
                </button>
                <button type="button" onclick="switchTab('tab-users')" id="nav-tab-users" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z"/></svg>
                    Manage Users
                </button>
                <button type="button" onclick="switchTab('tab-broadcast')" id="nav-tab-broadcast" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5.882V19.24a1.76 1.76 0 01-3.417.592l-2.147-6.15M18 13a3 3 0 100-6M5.436 13.683A4.001 4.001 0 017 6h1.832c4.1 0 7.625-1.234 9.168-3v14c-1.543-1.766-5.067-3-9.168-3H7a3.988 3.988 0 01-1.564-.317z"/></svg>
                    Broadcast
                </button>
                <button type="button" onclick="switchTab('tab-settings')" id="nav-tab-settings" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                    Setting &amp; UPI
                </button>
                <button type="button" onclick="switchTab('tab-bot')" id="nav-tab-bot" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z"/></svg>
                    Bot Token Config
                </button>
                <button type="button" onclick="switchTab('tab-media')" id="nav-tab-media" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
                    Media &amp; Greetings
                </button>
                <button type="button" onclick="switchTab('tab-plans')" id="nav-tab-plans" class="nav-btn w-full flex items-center gap-3 px-3 py-2.5 rounded-xl font-medium text-slate-400">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                    Subscription Plans
                </button>
            </nav>
        </div>

        <div class="pt-4 border-t border-purple-900/40">
            <a href="/logout" class="w-full flex items-center justify-center gap-2 py-2 rounded-xl text-xs font-mono font-semibold text-rose-400 hover:bg-rose-500/10 transition">
                <svg class="w-4 h-4 text-rose-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/>
                </svg>
                Sign Out
            </a>
        </div>
    </aside>

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

                <!-- Recent Orders (Properly unpacks 8 values) -->
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

            <!-- TAB: CUSTOMER ORDERS (Properly unpacks 8 values) -->
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
                                            <a href="/admin/photo/{{ oid }}" target="_blank" class="text-cyan-400 hover:text-cyan-300 underline font-semibold">View Screenshot</a>
                                        {% else %}
                                            <span class="text-gray-500 italic">No upload</span>
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
            <div id="tab-users" class="tab-content space-y-4 hidden">
                <div class="glass-card rounded-2xl p-5 space-y-4">
                    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-purple-900/40 pb-3">
                        <div>
                            <h3 class="font-tech text-base font-bold text-white tracking-wide">Registered Users</h3>
                            <p class="text-[11px] text-purple-400 font-mono">Showing {{ users_list|length }} of {{ users_total }} accounts</p>
                        </div>
                        <form method="GET" action="/" class="flex items-center gap-2">
                            <input type="hidden" name="tab" value="tab-users">
                            <input type="text" name="user_search" value="{{ user_search }}" placeholder="Search user ID or @username..."
                                   class="bg-[#070410] border border-purple-900/60 rounded-xl px-3 py-1.5 text-xs text-white placeholder-purple-400/50 font-mono focus:outline-none focus:border-cyan-400 w-48 sm:w-56">
                            <button type="submit" class="bg-purple-900/60 hover:bg-cyan-600 text-white font-tech text-xs px-3 py-1.5 rounded-xl transition">
                                Find
                            </button>
                            {% if user_search %}
                            <a href="/?tab=tab-users" class="text-rose-400 hover:text-white text-xs font-mono px-1">✕</a>
                            {% endif %}
                        </form>
                    </div>

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
                                                {% for p_id, p_name, _, _, _ in plans %}
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

                    {% if total_pages > 1 %}
                    <div class="flex items-center justify-between pt-3 border-t border-purple-900/40 font-mono text-xs text-purple-300">
                        <div>Page {{ current_page }} of {{ total_pages }}</div>
                        <div class="flex gap-2">
                            {% if current_page > 1 %}
                            <a href="/?tab=tab-users&page={{ current_page - 1 }}&user_search={{ user_search }}" 
                               class="px-3 py-1.5 rounded-lg bg-purple-900/40 hover:bg-cyan-600 text-white font-tech transition">&larr; Prev</a>
                            {% endif %}
                            {% if current_page < total_pages %}
                            <a href="/?tab=tab-users&page={{ current_page + 1 }}&user_search={{ user_search }}" 
                               class="px-3 py-1.5 rounded-lg bg-purple-900/40 hover:bg-cyan-600 text-white font-tech transition">Next &rarr;</a>
                            {% endif %}
                        </div>
                    </div>
                    {% endif %}
                </div>
            </div>

            <!-- TAB: BROADCAST -->
            <div id="tab-broadcast" class="tab-content space-y-6 hidden">
                <form method="POST" action="/admin/broadcast/send" class="glass-card rounded-2xl p-6 space-y-4">
                    <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3 flex items-center gap-2">
                        <svg class="w-5 h-5 text-fuchsia-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5.882V19.24a1.76 1.76 0 01-3.417.592l-2.147-6.15M18 13a3 3 0 100-6M5.436 13.683A4.001 4.001 0 017 6h1.832c4.1 0 7.625-1.234 9.168-3v14c-1.543-1.766-5.067-3-9.168-3H7a3.988 3.988 0 01-1.564-.317z"/></svg>
                        Transmit Broadcast
                    </h3>
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
                    <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3 flex items-center gap-2">
                        <svg class="w-5 h-5 text-fuchsia-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                        UPI Settings
                    </h3>
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
                    <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3 flex items-center gap-2">
                        <svg class="w-5 h-5 text-fuchsia-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>
                        Change Admin Password
                    </h3>
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
                        <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3 flex items-center gap-2">
                            <svg class="w-5 h-5 text-fuchsia-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z"/></svg>
                            Bot Token Manager
                        </h3>
                        <div>
                            <label class="block text-xs text-purple-300 mb-2 font-mono uppercase">Telegram Bot Token</label>
                            <input type="text" name="bot_token" value="{{ bot_token }}" required class="w-full bg-[#070410] border border-purple-900/60 rounded-xl px-4 py-3 text-sm text-white font-mono focus:outline-none">
                            <span class="text-[11px] text-gray-500 mt-1 block">Updating this token automatically restarts polling without restarting Railway.</span>
                        </div>
                    </div>
                </div>

                <!-- TAB: MEDIA & GREETINGS (MULTILINE FIRST MESSAGE CAPTION) -->
                <div id="tab-media" class="tab-content space-y-5 hidden">
                    <div class="glass-card rounded-2xl p-6 space-y-4">
                        <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3 flex items-center gap-2">
                            <svg class="w-5 h-5 text-fuchsia-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
                            Media &amp; Direct Links
                        </h3>
                        <div>
                            <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">First Message Caption (Supports Multiple Lines, Spaces &amp; Emojis)</label>
                            <textarea name="first_caption" rows="4" required class="w-full bg-[#070410] border border-purple-900/60 rounded-xl p-3 text-sm text-white font-sans focus:outline-none focus:border-cyan-400">{{ first_caption }}</textarea>
                        </div>
                        <div>
                            <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">First Message Media (Direct URLs or File IDs &mdash; 1 per line)</label>
                            <textarea id="first_media_txt" name="first_media" rows="3" required placeholder="https://example.com/photo1.jpg&#10;https://example.com/video.mp4&#10;TELEGRAM_FILE_ID" class="w-full bg-[#070410] border border-purple-900/60 rounded-xl p-3 text-sm text-white font-mono">{{ first_media }}</textarea>
                        </div>
                        <div>
                            <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">Plan Banner Caption</label>
                            <input type="text" name="plan_caption" value="{{ plan_caption }}" required class="w-full bg-[#070410] border border-purple-900/60 rounded-xl p-3 text-sm text-white">
                        </div>
                        <div>
                            <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">Plan Banner Media (Direct Image URL or File ID)</label>
                            <textarea name="plan_media" rows="2" required placeholder="https://example.com/banner.jpg or Telegram File ID" class="w-full bg-[#070410] border border-purple-900/60 rounded-xl p-3 text-sm text-white font-mono">{{ plan_media }}</textarea>
                        </div>
                        <div>
                            <label class="block text-xs text-purple-300 mb-1 font-mono uppercase">Demo Videos (Direct Video URLs or File IDs &mdash; 1 per line)</label>
                            <textarea name="demo_videos" rows="4" required placeholder="https://example.com/demo1.mp4&#10;TELEGRAM_FILE_ID" class="w-full bg-[#070410] border border-purple-900/60 rounded-xl p-3 text-sm text-white font-mono">{{ demo_videos }}</textarea>
                        </div>
                    </div>

                    <!-- Direct First Message Media Upload -->
                    <div class="glass-card rounded-2xl p-5 space-y-3">
                        <div class="flex items-center justify-between">
                            <h4 class="font-tech text-xs font-bold text-fuchsia-300 uppercase tracking-wider flex items-center gap-2">
                                <svg class="w-4 h-4 text-fuchsia-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"/></svg>
                                Direct First Message Media Upload
                            </h4>
                            <span id="firstUploadBadge" class="text-[10px] font-mono text-purple-400 hidden">Uploading...</span>
                        </div>
                        
                        <div class="flex flex-col sm:flex-row items-stretch sm:items-center gap-3">
                            <label class="flex-1 cursor-pointer">
                                <input type="file" id="first_media_input" multiple accept="image/*,video/*" 
                                       class="block w-full text-xs text-slate-300 font-mono file:mr-3 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-xs file:font-tech file:bg-purple-900/60 file:text-fuchsia-400 hover:file:bg-purple-900/90 bg-[#070410] border border-purple-900/60 rounded-xl p-1.5 focus:outline-none">
                            </label>
                            <button type="button" id="firstUploadBtn" onclick="uploadFirstMediaFiles()" 
                                    class="bg-gradient-to-r from-fuchsia-600 to-purple-600 hover:opacity-90 text-white font-tech font-bold text-xs py-2.5 px-6 rounded-xl uppercase tracking-wider shadow-lg shadow-fuchsia-600/30 transition shrink-0">
                                Upload Media
                            </button>
                        </div>
                        <div id="firstUploadStatus" class="text-xs font-mono empty:hidden transition-all"></div>
                    </div>

                    <!-- Direct Demo Video File Upload -->
                    <div class="glass-card rounded-2xl p-5 space-y-3">
                        <div class="flex items-center justify-between">
                            <h4 class="font-tech text-xs font-bold text-cyan-300 uppercase tracking-wider flex items-center gap-2">
                                <svg class="w-4 h-4 text-cyan-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
                                Direct Demo Video Upload
                            </h4>
                            <span id="uploadBadge" class="text-[10px] font-mono text-purple-400 hidden">Uploading...</span>
                        </div>
                        
                        <div class="flex flex-col sm:flex-row items-stretch sm:items-center gap-3">
                            <label class="flex-1 cursor-pointer">
                                <input type="file" id="demo_file_input" multiple accept="video/mp4,video/*" 
                                       class="block w-full text-xs text-slate-300 font-mono file:mr-3 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-xs file:font-tech file:bg-purple-900/60 file:text-cyan-400 hover:file:bg-purple-900/90 bg-[#070410] border border-purple-900/60 rounded-xl p-1.5 focus:outline-none">
                            </label>
                            <button type="button" id="uploadBtn" onclick="uploadDemoVideoFile()" 
                                    class="bg-gradient-to-r from-cyan-600 to-blue-600 hover:from-cyan-500 hover:to-blue-500 text-white font-tech font-bold text-xs py-2.5 px-6 rounded-xl uppercase tracking-wider shadow-lg shadow-cyan-600/30 transition shrink-0">
                                Upload Videos
                            </button>
                        </div>
                        <div id="uploadStatus" class="text-xs font-mono empty:hidden transition-all"></div>
                    </div>
                </div>

                <div id="saveBar" class="pt-4 hidden">
                    <button type="submit" class="w-full bg-gradient-to-r from-fuchsia-600 via-purple-600 to-cyan-600 text-white font-tech font-bold py-3 rounded-xl uppercase tracking-wider flex items-center justify-center gap-2">
                        <svg class="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
                        Save Interface Parameters
                    </button>
                </div>
            </form>

            <!-- TAB: SUBSCRIPTION PLANS (Unpacks exactly 5 values) -->
            <div id="tab-plans" class="tab-content space-y-5 hidden">
                <div class="glass-card rounded-2xl p-6 space-y-4">
                    <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3 flex items-center gap-2">
                        <svg class="w-5 h-5 text-fuchsia-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v3m0 0v3m0-3h3m-3 0H9m12 0a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                        Add Subscription Tier
                    </h3>
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
                    <h3 class="font-tech text-base font-bold text-white border-b border-purple-900/40 pb-3 flex items-center gap-2">
                        <svg class="w-5 h-5 text-fuchsia-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 10h16M4 14h16M4 18h16"/></svg>
                        Active Subscription Plans
                    </h3>
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
                                {% for pid, name, amount, validity, access_link in plans %}
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

        async function uploadFirstMediaFiles() {
            var input = document.getElementById('first_media_input');
            var statusEl = document.getElementById('firstUploadStatus');
            var btn = document.getElementById('firstUploadBtn');
            var badge = document.getElementById('firstUploadBadge');

            if (!input.files || input.files.length === 0) {
                statusEl.innerText = "⚠️ Please select at least one image or video.";
                statusEl.className = "text-xs text-rose-400 font-mono mt-1";
                return;
            }

            btn.disabled = true;
            btn.innerText = "Uploading...";
            badge.classList.remove('hidden');
            statusEl.innerText = "⏳ Uploading " + input.files.length + " media item(s)...";
            statusEl.className = "text-xs text-fuchsia-400 font-mono mt-1 animate-pulse";

            var formData = new FormData();
            for (var i = 0; i < input.files.length; i++) {
                formData.append("files", input.files[i]);
            }

            try {
                var res = await fetch('/admin/upload-first-media', {
                    method: 'POST',
                    body: formData
                });
                var data = await res.json();
                btn.disabled = false;
                btn.innerText = "Upload Media";
                badge.classList.add('hidden');

                if (res.ok && data.status === 'success') {
                    statusEl.innerText = "✅ Successfully added " + data.urls.length + " direct link(s)!";
                    statusEl.className = "text-xs text-emerald-400 font-mono mt-1";

                    var txtArea = document.getElementById('first_media_txt');
                    if (txtArea) {
                        var existing = txtArea.value.trim();
                        var newUrls = data.urls.join('\\n');
                        txtArea.value = (existing ? existing + '\\n' + newUrls : newUrls).trim();
                    }
                    input.value = "";
                } else {
                    statusEl.innerText = "❌ Upload failed: " + (data.message || "Server error");
                    statusEl.className = "text-xs text-rose-400 font-mono mt-1";
                }
            } catch(e) {
                btn.disabled = false;
                btn.innerText = "Upload Media";
                badge.classList.add('hidden');
                statusEl.innerText = "❌ Connection interrupted.";
                statusEl.className = "text-xs text-rose-400 font-mono mt-1";
            }
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
                btn.innerText = "Upload Videos";
                badge.classList.add('hidden');

                if (res.ok && data.status === 'success') {
                    statusEl.innerText = "✅ Successfully uploaded " + (data.urls ? data.urls.length : count) + " video(s)!";
                    statusEl.className = "text-xs text-emerald-400 font-mono mt-1";

                    var txtArea = document.querySelector('textarea[name="demo_videos"]');
                    if (txtArea && data.urls) {
                        var existing = txtArea.value.trim();
                        var newUrls = data.urls.join('\\n');
                        txtArea.value = (existing ? existing + '\\n' + newUrls : newUrls).trim();
                    }
                    input.value = "";
                } else {
                    statusEl.innerText = "❌ Upload failed: " + (data.message || "Server error");
                    statusEl.className = "text-xs text-rose-400 font-mono mt-1";
                }
            } catch(e) {
                btn.disabled = false;
                btn.innerText = "Upload Videos";
                badge.classList.add('hidden');
                statusEl.innerText = "❌ Upload failed: Connection interrupted or file too large.";
                statusEl.className = "text-xs text-rose-400 font-mono mt-1";
            }
        }
    </script>
</body>
</html>"""

# ==========================================
# FASTAPI HTTP ROUTES & API
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
async def admin_dashboard(
    request: Request,
    message: str | None = None,
    page: int = 1,
    user_search: str = "",
    is_auth: bool = Depends(require_admin)
):
    try:
        metrics = await get_dashboard_metrics()
        
        limit = 50
        page = max(1, page)
        offset = (page - 1) * limit
        users_list, users_total = await get_paginated_users(limit=limit, offset=offset, search=user_search)
        total_pages = max(1, math.ceil(users_total / limit))

        context = {
            "message": message,
            "admin_username": ADMIN_USER,
            "is_online": manager.bot is not None,
            "paid_orders": metrics.get("paid_orders", 0),
            "revenue": metrics.get("revenue", "0.00"),
            "total_users": metrics.get("total_users", 0),
            "recent_orders": metrics.get("recent_orders", []),
            "all_orders": metrics.get("all_orders", []),
            "users_list": users_list or [],
            "users_total": users_total,
            "current_page": page,
            "total_pages": total_pages,
            "user_search": user_search,
            "bot_token": await get_setting("bot_token"),
            "admin_chat_id": await get_setting("admin_chat_id"),
            "upi_id": await get_setting("upi_id"),
            "payee_name": await get_setting("payee_name"),
            "maintenance": await get_setting("maintenance"),
            "first_media": await get_setting("first_media"),
            "first_caption": await get_setting("first_caption"),
            "plan_media": await get_setting("plan_media"),
            "plan_caption": await get_setting("plan_caption"),
            "demo_videos": await get_setting("demo_videos"),
            "plans": await get_all_plans(),
        }

        tmpl = Template(DASHBOARD_PAGE)
        html = await asyncio.to_thread(tmpl.render, **context)
        return HTMLResponse(content=html)
    except Exception as err:
        logging.error(f"Dashboard render error: {err}")
        return HTMLResponse(f"<h3>Dashboard Error: {err}</h3>", status_code=500)

@app.post("/admin/upload-first-media")
async def upload_first_media(
    request: Request,
    files: list[UploadFile] = File(...),
    is_auth: bool = Depends(require_admin),
):
    try:
        base_url = str(request.base_url).rstrip("/")
        if "railway.app" in base_url and base_url.startswith("http://"):
            base_url = base_url.replace("http://", "https://")

        raw = await get_setting("first_media")
        existing_lines = [l.strip() for l in raw.splitlines() if l.strip()]

        new_urls = []
        for f in files:
            clean_name = f"{int(datetime.now().timestamp())}_{f.filename.replace(' ', '_')}"
            dest_path = os.path.join(UPLOAD_DIR, clean_name)
            with open(dest_path, "wb") as buffer:
                shutil.copyfileobj(f.file, buffer)

            file_url = f"{base_url}/static/uploads/{clean_name}"
            new_urls.append(file_url)
            existing_lines.append(file_url)
            await asyncio.sleep(0.01)

        await update_setting("first_media", "\n".join(existing_lines))
        return JSONResponse({"status": "success", "urls": new_urls})
    except Exception as e:
        logging.error(f"First media upload error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

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
        existing_lines = [l.strip() for l in raw.splitlines() if l.strip()]

        new_urls = []
        for f in files:
            clean_name = f"{int(datetime.now().timestamp())}_{f.filename.replace(' ', '_')}"
            dest_path = os.path.join(UPLOAD_DIR, clean_name)
            with open(dest_path, "wb") as buffer:
                shutil.copyfileobj(f.file, buffer)

            file_url = f"{base_url}/static/uploads/{clean_name}"
            new_urls.append(file_url)
            existing_lines.append(file_url)
            await asyncio.sleep(0.01)

        await update_setting("demo_videos", "\n".join(existing_lines))
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
                        await manager.bot.send_message(user_id, "❌ Payment verification failed. Proof was rejected.")
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
        url=f"/?message=Broadcast+sent+to+{sent}+users!&tab=tab-broadcast",
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
                await manager.bot.send_message(user_id, "Your premium subscription has expired.")
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
    plan_caption: str = Form(...),
    plan_media: str = Form(...),
    demo_videos: str = Form(...),
    is_auth: bool = Depends(require_admin),
):
    old_token = await get_setting("bot_token")
    cleaned_token = bot_token.strip()

    await update_setting("bot_token", cleaned_token)
    await update_setting("first_caption", first_caption)
    await update_setting("first_media", first_media.strip())
    await update_setting("plan_caption", plan_caption)
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