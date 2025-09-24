import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.storage.memory import MemoryStorage

import asyncpg
import httpx

# ---------- Timezone ----------
TZ_TASHKENT = timezone(timedelta(hours=5))

# ---------- Env ----------
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing")

ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID", "0"))

DB_CONFIG = {
    "user": os.getenv("DB_USER", "appeals_user"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "appeals_db"),
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "5432")),
}

# ---------- URLs ----------
HEMIS_LOGIN_URL = "https://student.samdu.uz/rest/v1/auth/login"
HEMIS_STUDENT_INFO_URL = "https://student.samdu.uz/rest/v1/account/me"

# ---------- Logger ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("appeals_bot")

# Foydalanuvchi holati (oddiy in-memory)
user_state = {}

# ------------------- DB POOL ------------------- #
async def create_pool():
    return await asyncpg.create_pool(**DB_CONFIG)

# ------------------- HEMIS FUNCTIONS ------------------- #
async def hemis_login(hemis_id: str, password: str) -> str | None:
    headers = {
        "accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(HEMIS_LOGIN_URL, json={"login": hemis_id, "password": password}, headers=headers)
            if r.status_code == 200:
                data = r.json().get("data", {})
                return data.get("token")
            logger.warning("HEMIS login failed: %s %s", r.status_code, r.text)
    except Exception:
        logger.exception("HEMIS login xatosi")
    return None

async def hemis_get_student_info(token: str) -> dict | None:
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0"
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(HEMIS_STUDENT_INFO_URL, headers=headers)
            if r.status_code == 200:
                return r.json().get("data", {})
            logger.warning("HEMIS me failed: %s %s", r.status_code, r.text)
    except Exception:
        logger.exception("HEMIS student info xatosi")
    return None

# ------------------- MAIN ------------------- #
async def main():
    bot = Bot(token=API_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    db_pool = await create_pool()

    # /start
    @dp.message(Command("start"))
    async def cmd_start(msg: Message):
        uid = msg.from_user.id
        user_state[uid] = {"stage": "await_hemis"}
        await msg.answer("👋 Salom! Iltimos HEMIS login ID yuboring.")

    # Foydalanuvchi xabarlari
    @dp.message(lambda m: m.chat.id != ADMIN_GROUP_ID and m.text and not m.text.startswith('/'))
    async def handle_user(msg: Message):
        uid = msg.from_user.id
        if uid not in user_state:
            return

        stage = user_state[uid].get("stage")

        if stage == "await_hemis":
            user_state[uid]["hemis_id"] = msg.text.strip()
            user_state[uid]["stage"] = "await_pass"
            await msg.answer("🔑 Parol yuboring (faqat HEMIS ga yuboriladi).")
            return

        if stage == "await_pass":
            hemis_id = user_state[uid]["hemis_id"]
            password = msg.text.strip()

            await msg.answer("⏳ Tekshirilmoqda...")
            token = await hemis_login(hemis_id, password)
            if not token:
                await msg.answer("❌ HEMIS login xatosi. /start bilan qayta urinib ko‘ring.")
                user_state.pop(uid, None)
                return

            student_info = await hemis_get_student_info(token)
            if not student_info:
                await msg.answer("❌ Student ma’lumotini olishda xatolik.")
                user_state.pop(uid, None)
                return

            user_state[uid]["student"] = {
                "full_name": f"{student_info.get('first_name', '')} {student_info.get('last_name', '')}".strip() or "Noma'lum",
                "student_id": student_info.get("student_id_number", "Noma'lum"),
                "faculty": (student_info.get("faculty") or {}).get("name", "Noma'lum"),
                "group": (student_info.get("group") or {}).get("name", "Noma'lum"),
            }
            user_state[uid]["stage"] = "await_appeal"
            await msg.answer(
                f"✅ Login muvaffaqiyatli!\n"
                f"👤 {user_state[uid]['student']['full_name']}\n"
                f"🏫 {user_state[uid]['student']['faculty']} - {user_state[uid]['student']['group']}\n"
                f"✍️ Endi murojaat matnini yuboring."
            )
            return

        if stage == "await_appeal":
            student = user_state[uid]["student"]
            appeal_text = msg.text.strip()

            sent = await bot.send_message(
                ADMIN_GROUP_ID,
                (
                    "📥 Yangi murojaat:\n"
                    f"👤 F.I.Sh: {student['full_name']}\n"
                    f"🆔 ID raqam: {student['student_id']}\n"
                    f"🏫 Fakultet: {student['faculty']}\n"
                    f"👥 Guruh: {student['group']}\n"
                    f"📝 Murojaat: {appeal_text}\n"
                    f"🕒 {datetime.now(TZ_TASHKENT).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    "👉 Javob berish uchun reply qiling."
                )
            )

            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO appeals (telegram_id, hemis_id, murojaat, time, group_msg_id, group_chat_id)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    uid, student["student_id"], appeal_text, datetime.utcnow(), sent.message_id, sent.chat.id
                )

            await msg.answer("✅ Murojaat muvaffaqiyatli jo‘natildi.")
            user_state.pop(uid, None)

    # Admin reply
    @dp.message(lambda m: m.chat.id == ADMIN_GROUP_ID and m.reply_to_message is not None)
    async def handle_group_reply(msg: Message):
        try:
            me = await bot.get_me()
            if msg.reply_to_message.from_user.id != me.id:
                return

            replied_msg_id = msg.reply_to_message.message_id

            async with db_pool.acquire() as conn:
                record = await conn.fetchrow(
                    "SELECT telegram_id FROM appeals WHERE group_msg_id = $1",
                    replied_msg_id
                )

            if record:
                telegram_id = record['telegram_id']
                await bot.send_message(telegram_id, f"✅ Admin javobi:\n{msg.text}")

                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE appeals SET admin_reply=$1, answered=1 WHERE group_msg_id=$2",
                        msg.text, replied_msg_id
                    )

        except Exception:
            logger.exception("Xatolik (admin reply)")

    logger.info("Bot ishga tushdi ✅")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
