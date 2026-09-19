import os
import io
import json
import asyncio
import datetime
import uvicorn
import qrcode
import aiohttp
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, BigInteger, DateTime, Float, Text, select

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    BufferedInputFile,
    InputMediaPhoto,
    InputMediaVideo
)

# ---------------- CONFIGURATION ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "7110523959:AAHlYQTvoMQR1Zq8rFkM-fyWua79NMQ_r9Q")
UPI_ID = os.getenv("UPI_ID", "anmolvlv@ibl")
UPI_NAME = os.getenv("UPI_NAME", "Fire World")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot_database.db")

# ---------------- DATABASE ----------------
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String, nullable=True)
    plan_name: Mapped[str] = mapped_column(String)
    amount: Mapped[float] = mapped_column(Float)
    screenshot_file_id: Mapped[str] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="PENDING")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

class BotConfig(Base):
    __tablename__ = "bot_config"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    async with AsyncSessionLocal() as session:
        first_media = await session.get(BotConfig, "first_media")
        if not first_media:
            default_media = [{"type": "photo", "url": "https://picsum.photos/800/800"}]
            session.add(BotConfig(key="first_media", value=json.dumps(default_media)))

        plan_media = await session.get(BotConfig, "plan_media")
        if not plan_media:
            default_plan_media = [{"type": "photo", "url": "https://picsum.photos/800/700"}]
            session.add(BotConfig(key="plan_media", value=json.dumps(default_plan_media)))

        plan_caption = await session.get(BotConfig, "plan_caption")
        if not plan_caption:
            session.add(BotConfig(key="plan_caption", value="═« PRO PLAN »═"))

        first_caption = await session.get(BotConfig, "first_caption")
        if not first_caption:
            session.add(BotConfig(key="first_caption", value="💎 GET PREMIUM"))

        await session.commit()

# ---------------- BOT HELPERS ----------------
class OrderState(StatesGroup):
    waiting_for_screenshot = State()

def make_bottom_reply_keyboard():
    return {
        "keyboard": [
            [{"text": "💎 GET PREMIUM", "style": "success"}],
            [{"text": "🥵 DEMO", "style": "danger"}]
        ],
        "resize_keyboard": True
    }

# 3 Buttons: Green, Red, Blue matching screenshot
THREE_OPTIONS_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "💎 GET PREMIUM", "callback_data": "menu_get_premium", "style": "success"}],
        [{"text": "🥵 DEMO", "callback_data": "menu_demo", "style": "danger"}],
        [{"text": "✅ HOW TO GET PREMIUM", "callback_data": "menu_how_to", "style": "primary"}]
    ]
}

# 7 Plan Buttons
PLANS_RAW_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "1 Month VIP - ₹79", "callback_data": "buy_79_1Month", "style": "success"}],
        [{"text": "2 Months VIP - ₹149", "callback_data": "buy_149_2Months", "style": "danger"}],
        [{"text": "3 Months VIP - ₹199", "callback_data": "buy_199_3Months", "style": "primary"}],
        [{"text": "6 Months VIP - ₹299", "callback_data": "buy_299_6Months", "style": "success"}],
        [{"text": "1 Year VIP - ₹399", "callback_data": "buy_399_1Year", "style": "danger"}],
        [{"text": "Lifetime Access - ₹499", "callback_data": "buy_499_Lifetime", "style": "primary"}],
        [{"text": "Ultra Mega VIP - ₹799", "callback_data": "buy_799_UltraMega", "style": "success"}]
    ]
}

def make_get_link_keyboard(order_id: int):
    return {
        "inline_keyboard": [
            [{"text": "GET LINK", "callback_data": f"submit_{order_id}", "style": "success"}]
        ]
    }

def generate_upi_qr(amount: float, note: str) -> io.BytesIO:
    upi_url = f"upi://pay?pa={UPI_ID}&pn={UPI_NAME}&am={amount}&cu=INR&tn={note}"
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(upi_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, "PNG")
    bio.seek(0)
    return bio

async def send_raw_telegram_method(method: str, payload: dict):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            return await resp.json()

async def send_dynamic_media_group(bot_instance: Bot, chat_id: int, media_list: list, caption: str):
    if not media_list: return
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
        await bot_instance.send_media_group(chat_id=chat_id, media=media_group)

# ---------------- BOT ROUTER ----------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
bot_router = Router()

async def send_plan_presentation(chat_id: int):
    async with AsyncSessionLocal() as session:
        res_m2 = await session.get(BotConfig, "plan_media")
        res_c2 = await session.get(BotConfig, "plan_caption")

    media_2 = json.loads(res_m2.value) if res_m2 else []
    caption_2 = res_c2.value if res_c2 else "═« PRO PLAN »═"
    photo_url = media_2[0]["url"] if media_2 else "https://picsum.photos/800/700"

    await send_raw_telegram_method("sendPhoto", {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption_2,
        "reply_markup": PLANS_RAW_KEYBOARD
    })

@bot_router.message(CommandStart())
async def start_handler(message: types.Message):
    async with AsyncSessionLocal() as session:
        res_m1 = await session.get(BotConfig, "first_media")
        res_c1 = await session.get(BotConfig, "first_caption")

    media_1 = json.loads(res_m1.value) if res_m1 else []
    caption_1 = res_c1.value if res_c1 else "💎 GET PREMIUM"

    # Set bottom keyboard silently
    await send_raw_telegram_method("sendMessage", {
        "chat_id": message.chat.id,
        "text": ".",
        "reply_markup": make_bottom_reply_keyboard()
    })
    # Remove the dot message immediately so no welcome text appears
    # (Or keep keyboard activated)

    # 1. First Message: Images/Videos managed by admin panel with caption "💎 GET PREMIUM"
    if media_1:
        await send_dynamic_media_group(bot, message.chat.id, media_1, caption_1)
    else:
        await message.answer(caption_1)

    # 2. Second Message: "👇 Choose an option:" with the 3 colored buttons
    await send_raw_telegram_method("sendMessage", {
        "chat_id": message.chat.id,
        "text": "👇 Choose an option:",
        "reply_markup": THREE_OPTIONS_KEYBOARD
    })

    # 3. Third Message: Big pointing hand emoji
    await message.answer("👇")

@bot_router.callback_query(F.data == "menu_get_premium")
async def on_get_premium_click(callback: types.CallbackQuery):
    await send_plan_presentation(callback.message.chat.id)
    await callback.answer()

@bot_router.callback_query(F.data == "menu_demo")
async def on_demo_click(callback: types.CallbackQuery):
    await callback.message.answer("Here is your free demo preview: https://t.me/example")
    await callback.answer()

@bot_router.callback_query(F.data == "menu_how_to")
async def on_how_to_click(callback: types.CallbackQuery):
    await callback.message.answer(
        "📖 *How to Get Premium:*\n\n"
        "1. Click '💎 GET PREMIUM'.\n"
        "2. Choose your preferred plan from the list.\n"
        "3. Scan the generated UPI QR code & pay.\n"
        "4. Click 'GET LINK' and send the payment screenshot.\n"
        "5. The admin will verify and your private access link will be sent automatically!",
        parse_mode="Markdown"
    )
    await callback.answer()

@bot_router.message(F.text == "💎 GET PREMIUM")
async def show_plans_text(message: types.Message):
    await send_plan_presentation(message.chat.id)

@bot_router.message(F.text == "🥵 DEMO")
async def show_demo(message: types.Message):
    await message.answer("Here is your free demo preview: https://t.me/example")

@bot_router.callback_query(F.data.startswith("buy_"))
async def process_plan_choice(callback: types.CallbackQuery):
    _, amt_str, plan_name = callback.data.split("_")
    amount = float(amt_str)

    async with AsyncSessionLocal() as session:
        order = Order(
            user_id=callback.from_user.id,
            username=callback.from_user.username or "Anonymous",
            plan_name=plan_name,
            amount=amount
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)

    qr_io = generate_upi_qr(amount, f"Order_{order.id}")
    file = BufferedInputFile(qr_io.read(), filename="qr.png")

    caption = (
        f"🏷 Price : ₹{int(amount)}\n\n"
        f"🏦 UPI ID: `{UPI_ID}`\n\n"
        f"1️⃣ Scan | 2️⃣ Pay | 3️⃣ Click 'GET LINK'"
    )

    await callback.message.answer_photo(
        photo=file,
        caption=caption,
        parse_mode="Markdown"
    )
    
    await send_raw_telegram_method("sendMessage", {
        "chat_id": callback.message.chat.id,
        "text": "Click below after payment:",
        "reply_markup": make_get_link_keyboard(order.id)
    })
    await callback.answer()

@bot_router.callback_query(F.data.startswith("submit_"))
async def prompt_screenshot_upload(callback: types.CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[1])
    await state.update_data(order_id=order_id)
    await state.set_state(OrderState.waiting_for_screenshot)
    await callback.message.answer("📸 Please send your payment screenshot.")
    await callback.answer()

@bot_router.message(OrderState.waiting_for_screenshot, F.photo)
async def receive_screenshot(message: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    file_id = message.photo[-1].file_id

    async with AsyncSessionLocal() as session:
        order = await session.get(Order, order_id)
        if order:
            order.screenshot_file_id = file_id
            await session.commit()

    await state.clear()
    await message.reply("✅ Payment screenshot received.")

dp.include_router(bot_router)

# ---------------- ADMIN PANEL HTML ----------------
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Admin Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-gray-100 p-6 md:p-10">
  <div class="max-w-6xl mx-auto space-y-10">
    <div class="flex justify-between items-center border-b border-gray-800 pb-4">
      <h1 class="text-3xl font-bold text-emerald-400">Fire World Bot Management</h1>
      <a href="/admin" class="bg-gray-800 px-4 py-2 rounded text-sm hover:bg-gray-700">Refresh</a>
    </div>

    <!-- Media Controls -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-8">
      <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 space-y-4">
        <h2 class="text-lg font-bold text-blue-400">1️⃣ First Message Media</h2>
        <form method="POST" action="/admin/config/update" class="space-y-3">
          <input type="hidden" name="target" value="first">
          <div>
            <label class="block text-xs uppercase font-medium text-gray-400 mb-1">Caption</label>
            <input type="text" name="caption" value="{{ first_caption }}" class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-sm">
          </div>
          <div>
            <label class="block text-xs uppercase font-medium text-gray-400 mb-1">Media JSON</label>
            <textarea name="media_json" rows="5" class="w-full bg-gray-900 border border-gray-700 rounded p-2 font-mono text-xs">{{ first_media_json }}</textarea>
          </div>
          <button type="submit" class="w-full bg-blue-600 hover:bg-blue-500 py-2 rounded text-sm font-semibold">Save First Message</button>
        </form>
      </div>

      <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 space-y-4">
        <h2 class="text-lg font-bold text-purple-400">2️⃣ Second Message (Plan Card Image)</h2>
        <form method="POST" action="/admin/config/update" class="space-y-3">
          <input type="hidden" name="target" value="plan">
          <div>
            <label class="block text-xs uppercase font-medium text-gray-400 mb-1">Caption</label>
            <input type="text" name="caption" value="{{ plan_caption }}" class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-sm">
          </div>
          <div>
            <label class="block text-xs uppercase font-medium text-gray-400 mb-1">Image URL (JSON)</label>
            <textarea name="media_json" rows="5" class="w-full bg-gray-900 border border-gray-700 rounded p-2 font-mono text-xs">{{ plan_media_json }}</textarea>
          </div>
          <button type="submit" class="w-full bg-purple-600 hover:bg-purple-500 py-2 rounded text-sm font-semibold">Save Plan Media</button>
        </form>
      </div>
    </div>

    <!-- Verification Table -->
    <div class="space-y-4">
      <h2 class="text-xl font-bold">Pending Payment Verifications</h2>
      <div class="overflow-x-auto bg-gray-800 rounded-lg shadow border border-gray-700">
        <table class="min-w-full divide-y divide-gray-700 text-left text-sm">
          <thead class="bg-gray-700/50 text-gray-400 uppercase text-xs">
            <tr>
              <th class="p-3">Order ID</th>
              <th class="p-3">User</th>
              <th class="p-3">Plan</th>
              <th class="p-3">Amount</th>
              <th class="p-3">Proof</th>
              <th class="p-3">Status</th>
              <th class="p-3">Action</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-700">
            {% for o in orders %}
            <tr>
              <td class="p-3 font-mono">#{{ o.id }}</td>
              <td class="p-3">@{{ o.username }}<br><span class="text-xs text-gray-500">ID: {{ o.user_id }}</span></td>
              <td class="p-3">{{ o.plan_name }}</td>
              <td class="p-3 font-semibold text-emerald-400">₹{{ o.amount }}</td>
              <td class="p-3">
                {% if o.screenshot_file_id %}
                  <a href="/admin/photo/{{ o.id }}" target="_blank" class="text-blue-400 hover:text-blue-300 underline font-medium">View Screenshot</a>
                {% else %}
                  <span class="text-gray-500 italic">No upload</span>
                {% endif %}
              </td>
              <td class="p-3">
                <span class="px-2 py-1 rounded text-xs font-bold 
                  {% if o.status == 'APPROVED' %}bg-green-900/60 text-green-300 border border-green-700
                  {% elif o.status == 'REJECTED' %}bg-red-900/60 text-red-300 border border-red-700
                  {% else %}bg-yellow-900/60 text-yellow-300 border border-yellow-700{% endif %}">
                  {{ o.status }}
                </span>
              </td>
              <td class="p-3">
                {% if o.status == 'PENDING' and o.screenshot_file_id %}
                <div class="flex gap-2">
                  <form method="POST" action="/admin/order/{{ o.id }}/update">
                    <input type="hidden" name="action" value="approve">
                    <button class="bg-green-600 hover:bg-green-500 px-3 py-1 rounded text-xs font-medium">Approve</button>
                  </form>
                  <form method="POST" action="/admin/order/{{ o.id }}/update">
                    <input type="hidden" name="action" value="reject">
                    <button class="bg-red-600 hover:bg-red-500 px-3 py-1 rounded text-xs font-medium">Reject</button>
                  </form>
                </div>
                {% endif %}
              </td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
</body>
</html>
"""

# ---------------- FASTAPI APPLICATION ----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    polling_task = asyncio.create_task(dp.start_polling(bot))
    yield
    polling_task.cancel()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/admin")

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard():
    from jinja2 import Template
    async with AsyncSessionLocal() as session:
        orders_res = await session.execute(select(Order).order_by(Order.id.desc()))
        orders = orders_res.scalars().all()
        m1 = await session.get(BotConfig, "first_media")
        c1 = await session.get(BotConfig, "first_caption")
        m2 = await session.get(BotConfig, "plan_media")
        c2 = await session.get(BotConfig, "plan_caption")

    template = Template(ADMIN_HTML)
    return template.render(
        orders=orders,
        first_media_json=m1.value if m1 else "[]",
        first_caption=c1.value if c1 else "💎 GET PREMIUM",
        plan_media_json=m2.value if m2 else "[]",
        plan_caption=c2.value if c2 else "═« PRO PLAN »═"
    )

@app.post("/admin/config/update")
async def update_media_config(target: str = Form(...), caption: str = Form(...), media_json: str = Form(...)):
    try:
        json.loads(media_json)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON array")

    async with AsyncSessionLocal() as session:
        if target == "first":
            m = await session.get(BotConfig, "first_media")
            c = await session.get(BotConfig, "first_caption")
            if m: m.value = media_json
            if c: c.value = caption
        elif target == "plan":
            m = await session.get(BotConfig, "plan_media")
            c = await session.get(BotConfig, "plan_caption")
            if m: m.value = media_json
            if c: c.value = caption
        await session.commit()

    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/photo/{order_id}")
async def view_screenshot(order_id: int):
    async with AsyncSessionLocal() as session:
        order = await session.get(Order, order_id)
        if not order or not order.screenshot_file_id:
            raise HTTPException(status_code=404, detail="Photo not found")
        file = await bot.get_file(order.screenshot_file_id)
        photo_bytes = await bot.download_file(file.file_path)
        return StreamingResponse(io.BytesIO(photo_bytes.read()), media_type="image/jpeg")

@app.post("/admin/order/{order_id}/update")
async def update_order_status(order_id: int, action: str = Form(...)):
    async with AsyncSessionLocal() as session:
        order = await session.get(Order, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if action == "approve":
            order.status = "APPROVED"
            await bot.send_message(
                chat_id=order.user_id,
                text="🎉 *Payment Approved!*\n\nHere is your private channel link: https://t.me/+YourPrivateChannelLink",
                parse_mode="Markdown"
            )
        elif action == "reject":
            order.status = "REJECTED"
            await bot.send_message(
                chat_id=order.user_id,
                text="❌ *Payment Rejected.*\nThe screenshot could not be verified. Please retry with valid proof."
            )
        await session.commit()

    return RedirectResponse(url="/admin", status_code=303)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
