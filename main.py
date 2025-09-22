# app.py (main.py o'rniga shunday tutsangiz ham bo'ladi)
import asyncio
import os
from datetime import datetime, date

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# .env ni yuklash
API_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "0"))

DB_CONFIG = {
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "appeals_db"),
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "5432")),
}

# Bot/DP
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# Connection pool
pool: asyncpg.Pool | None = None

async def init_db():
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS appeals (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT,
            fish TEXT,
            hemis_id TEXT,
            yonalish TEXT,
            guruh TEXT,
            bosqich TEXT,
            murojaat TEXT,
            time TIMESTAMP,
            answered INTEGER DEFAULT 0,
            group_msg_id BIGINT
        )
        """)

user_data: dict[int, dict] = {}

@dp.message(Command("start"))
async def start_cmd(msg: Message):
    user_data[msg.from_user.id] = {}
    await msg.answer("Salom! FISH (Familiya Ism Sharif) yuboring.")

@dp.message(F.text, lambda m: m.from_user.id in user_data and "fish" not in user_data[m.from_user.id])
async def get_fish(msg: Message):
    user_data[msg.from_user.id]["fish"] = msg.text
    await msg.answer("HEMIS ID yuboring.")

@dp.message(F.text, lambda m: m.from_user.id in user_data and "hemis_id" not in user_data[m.from_user.id])
async def get_hemis(msg: Message):
    user_data[msg.from_user.id]["hemis_id"] = msg.text
    await msg.answer("Yo‘nalishingizni yuboring.")

@dp.message(F.text, lambda m: m.from_user.id in user_data and "yonalish" not in user_data[m.from_user.id])
async def get_yonalish(msg: Message):
    user_data[msg.from_user.id]["yonalish"] = msg.text
    await msg.answer("Guruhni yuboring.")

@dp.message(F.text, lambda m: m.from_user.id in user_data and "guruh" not in user_data[m.from_user.id])
async def get_guruh(msg: Message):
    user_data[msg.from_user.id]["guruh"] = msg.text
    await msg.answer("Bosqichni yuboring.")

@dp.message(F.text, lambda m: m.from_user.id in user_data and "bosqich" not in user_data[m.from_user.id])
async def get_bosqich(msg: Message):
    user_data[msg.from_user.id]["bosqich"] = msg.text
    await msg.answer("Endi murojaat matnini yuboring.")

@dp.message(F.text, lambda m: m.from_user.id in user_data and "murojaat" not in user_data[m.from_user.id])
async def get_murojaat(msg: Message):
    user_id = msg.from_user.id
    data = user_data[user_id]
    data["murojaat"] = msg.text

    async with pool.acquire() as conn:
        appeal_id = await conn.fetchval("""
            INSERT INTO appeals (telegram_id, fish, hemis_id, yonalish, guruh, bosqich, murojaat, time)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id
        """, user_id, data["fish"], data["hemis_id"], data["yonalish"],
             data["guruh"], data["bosqich"], data["murojaat"], datetime.now())

    appeal_text = (f"📝 Yangi murojaat:\n\n"
                   f"👤 FISH: {data['fish']}\n"
                   f"🆔 HEMIS: {data['hemis_id']}\n"
                   f"📚 Yo‘nalish: {data['yonalish']}\n"
                   f"👥 Guruh: {data['guruh']}\n"
                   f"🎓 Bosqich: {data['bosqich']}\n"
                   f"🕒 Vaqt: {str(datetime.now())[:-7]}\n\n"
                   f"✍️ Murojaat:\n{data['murojaat']}")

    sent = await bot.send_message(ADMIN_GROUP_ID, appeal_text)

    async with pool.acquire() as conn:
        await conn.execute("UPDATE appeals SET group_msg_id=$1 WHERE id=$2", sent.message_id, appeal_id)

    await msg.answer("✅ Murojaatingiz qabul qilindi. Adminlar tez orada javob berishadi. Yangi murojaat uchun /start ni bosing")
    user_data.pop(user_id, None)

@dp.message(F.chat.id == int(os.getenv("ADMIN_GROUP_ID", "0")), F.reply_to_message)
async def admin_reply(msg: Message):
    group_msg_id = msg.reply_to_message.message_id
    text = msg.text

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT telegram_id FROM appeals WHERE group_msg_id=$1", group_msg_id)

    if row:
        user_id = row["telegram_id"]
        try:
            await bot.send_message(user_id, f"✉️ Admin javobi:\n\n{text}\n Yangi murojaat uchun /start ni bosing")
            async with pool.acquire() as conn:
                await conn.execute("UPDATE appeals SET answered=1 WHERE group_msg_id=$1", group_msg_id)
            await msg.reply("✅ Javob foydalanuvchiga yuborildi.")
        except Exception as e:
            await msg.reply(f"❌ Foydalanuvchiga yuborib bo‘lmadi.\n{e}")
    else:
        await msg.reply("⚠️ Bu xabar bazada topilmadi.")

async def send_stats():
    today = date.today()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*), SUM(answered) FROM appeals WHERE DATE(time)=$1", today)

    total, answered = (row[0] or 0), (row[1] or 0)
    await bot.send_message(
        ADMIN_GROUP_ID,
        f"📊 Statistika ({today}):\nUmumiy murojaatlar: {total}\nJavob berilgan: {answered}"
    )

async def on_startup():
    global pool
    pool = await asyncpg.create_pool(**DB_CONFIG, min_size=1, max_size=5)
    await init_db()

async def on_shutdown():
    if pool:
        await pool.close()

async def main():
    # Linuxda ishlash tezligi uchun (ixtiyoriy)
    try:
        import uvloop
        uvloop.install()
    except Exception:
        pass

    await on_startup()

    scheduler = AsyncIOScheduler(timezone=os.getenv("TZ", "Asia/Tashkent"))
    scheduler.add_job(send_stats, "cron", hour=20, minute=0)
    scheduler.start()

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await on_shutdown()

if __name__ == "__main__":
    asyncio.run(main())
