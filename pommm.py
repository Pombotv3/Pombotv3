import os
import io
import asyncio
import datetime
import uvicorn
import qrcode
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, BigInteger, DateTime, Float, select

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
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
    status: Mapped[str] = mapped_column(String, default="PENDING")  # PENDING, APPROVED, REJECTED
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# ---------------- BOT HELPERS & KEYBOARDS ----------------
class OrderState(StatesGroup):
    waiting_for_screenshot = State()

def make_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💎 GET PREMIUM")],
            [KeyboardButton(text="🥵 DEMO")]
        ],
        resize_keyboard=True
    )

def make_plans_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🟢 Basic Plan - ₹79", callback_data="plan_79_Basic")],
            [InlineKeyboardButton(text="🔴 Standard Plan - ₹149", callback_data="plan_149_Standard")],
            [InlineKeyboardButton(text="🔵 Super Plan - ₹299", callback_data="plan_299_Super")],
            [InlineKeyboardButton(text="🟢 Pro Plan - ₹499", callback_data="plan_499_Pro")]
        ]
    )

def make_get_link_keyboard(order_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="GET LINK", callback_data=f"submit_{order_id}")]
        ]
    )

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

# ---------------- BOT ROUTER ----------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
bot_router = Router()

@bot_router.message(CommandStart())
async def start_handler(message: types.Message):
    await message.answer("Welcome to Fire World Bot! Choose an option below:", reply_markup=make_main_keyboard())

@bot_router.message(F.text == "💎 GET PREMIUM")
async def show_plans(message: types.Message):
    await message.answer("👇 Choose an option:", reply_markup=make_plans_keyboard())

@bot_router.message(F.text == "🥵 DEMO")
async def show_demo(message: types.Message):
    await message.answer("Here is your demo preview: https://t.me/example")

@bot_router.callback_query(F.data.startswith("plan_"))
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
        parse_mode="Markdown",
        reply_markup=make_get_link_keyboard(order.id)
    )
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

# ---------------- ADMIN PANEL HTML TEMPLATE ----------------
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Admin Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-gray-100 p-8">
  <div class="max-w-6xl mx-auto">
    <div class="flex justify-between items-center mb-6">
      <h1 class="text-2xl font-bold">Fire World Bot - Payment Approvals</h1>
      <a href="/admin" class="bg-gray-700 px-4 py-2 rounded text-sm hover:bg-gray-600 transition">Refresh</a>
    </div>
    <div class="overflow-x-auto bg-gray-800 rounded-lg shadow border border-gray-700">
      <table class="min-w-full divide-y divide-gray-700 text-left text-sm">
        <thead class="bg-gray-700/50 text-gray-400 uppercase text-xs">
          <tr>
            <th class="p-3">Order ID</th>
            <th class="p-3">User</th>
            <th class="p-3">Plan</th>
            <th class="p-3">Amount</th>
            <th class="p-3">Screenshot</th>
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
                <a href="/admin/photo/{{ o.id }}" target="_blank" class="text-blue-400 hover:text-blue-300 underline font-medium">View Image</a>
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
                  <button class="bg-green-600 hover:bg-green-500 px-3 py-1 rounded text-xs font-medium transition">Approve</button>
                </form>
                <form method="POST" action="/admin/order/{{ o.id }}/update">
                  <input type="hidden" name="action" value="reject">
                  <button class="bg-red-600 hover:bg-red-500 px-3 py-1 rounded text-xs font-medium transition">Reject</button>
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

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard():
    from jinja2 import Template
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Order).order_by(Order.id.desc()))
        orders = result.scalars().all()
    template = Template(ADMIN_HTML)
    return template.render(orders=orders)

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
                text="❌ *Payment Rejected.*\nThe screenshot could not be verified. Please try again with valid proof."
            )
        await session.commit()

    return RedirectResponse(url="/admin", status_code=303)

# ---------------- ENTRYPOINT ----------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
