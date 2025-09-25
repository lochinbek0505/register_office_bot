import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
import pandas as pd
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from aiogram.types import InputFile
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest  # <— qo‘shildi
from aiogram.types.input_file import FSInputFile

import asyncpg
import httpx

# ---------- Timezone ----------
TZ_TASHKENT = timezone(timedelta(hours=5))

# ---------- Env ----------
# API_TOKEN = "8098944544:AAFis8r0qm4IDqrEfO_mZV5gVnPbTt-uORQ"
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing")

ADMIN_GROUP_ID_RAW = (os.getenv("ADMIN_GROUP_ID") or "").strip()

def _parse_chat_id(raw: str):
    if not raw:
        return None
    if raw.startswith("@"):
        return raw
    try:
        return int(raw)
    except ValueError:
        return None

ADMIN_CHAT_ID = _parse_chat_id(ADMIN_GROUP_ID_RAW)

# DB_CONFIG = {
#     "user": 'postgres',
#     "password": '1960m1997a1999n',
#     "database": 'appeals_db',
#     "host": 'localhost',
#     "port": 5432,
# }

DB_CONFIG = {
    "user": os.getenv("DB_USER", "appeals_user"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "appeals_db"),
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "5432")),
}

HEMIS_LOGIN_URL = "https://student.samdu.uz/rest/v1/auth/login"
HEMIS_STUDENT_INFO_URL = "https://student.samdu.uz/rest/v1/account/me"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("appeals_bot")

user_state = {}

async def create_pool():
    return await asyncpg.create_pool(**DB_CONFIG)

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

async def validate_admin_chat(bot: Bot) -> bool:
    if not ADMIN_CHAT_ID:
        logger.error("ADMIN_GROUP_ID bo‘sh yoki noto‘g‘ri format: %r", ADMIN_GROUP_ID_RAW)
        return False
    try:
        chat = await bot.get_chat(ADMIN_CHAT_ID)
        logger.info("[OK] Admin chat: %s (%s)", getattr(chat, "title", None), chat.id)
        return True
    except TelegramBadRequest as e:
        logger.error("[ERR] ADMIN_GROUP_ID noto‘g‘ri yoki bot a’zo emas: %s; raw=%r", e, ADMIN_GROUP_ID_RAW)
        return False

async def send_to_admin(bot: Bot, text: str):
    if not ADMIN_CHAT_ID:
        logger.error("[send_to_admin] ADMIN_GROUP_ID yo‘q/yaroqsiz. Matn: %r", text)
        return
    try:
        await bot.send_message(ADMIN_CHAT_ID, text)
    except TelegramBadRequest as e:
        logger.error("[send_to_admin] chat not found / bad id: %s; target=%r; text=%r", e, ADMIN_GROUP_ID_RAW, text)

async def main():
    bot = Bot(token=API_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    db_pool = await create_pool()

    await validate_admin_chat(bot)

    @dp.message(Command("start"))
    async def cmd_start(msg: Message):
        uid = msg.from_user.id
        user_state[uid] = {"stage": "await_hemis"}
        await msg.answer("👋 Salom! Iltimos HEMIS login ID yuboring.")

    @dp.message(lambda m: (ADMIN_CHAT_ID is None or m.chat.id != ADMIN_CHAT_ID) and m.text and not m.text.startswith('/'))
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
                "full_name": f"{student_info.get('full_name', '')}".strip() or "Noma'lum",
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
            tashkent_tz = pytz.timezone("Asia/Tashkent")
            text_for_admin = (
                "📥 Yangi murojaat:\n"
                f"👤 F.I.Sh: {student['full_name']}\n"
                f"🆔 ID raqam: {student['student_id']}\n"
                f"🏫 Fakultet: {student['faculty']}\n"
                f"👥 Guruh: {student['group']}\n"
                f"📝 Murojaat: {appeal_text}\n"
                f"🕒 {datetime.now(timezone.utc).astimezone(tashkent_tz).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                "👉 Javob berish uchun reply qiling."
            )

            sent = None
            try:
                if ADMIN_CHAT_ID:
                    sent = await bot.send_message(ADMIN_CHAT_ID, text_for_admin)
                else:
                    logger.error("ADMIN_CHAT_ID yo‘q — admin xabarini yuborib bo‘lmadim.")
            except TelegramBadRequest as e:
                logger.error("Admin chatga yuborishda xato: %s", e)

            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO appeals (
                        telegram_id, hemis_id, murojaat, time,
                        group_msg_id, group_chat_id, fish, fakultet, guruh
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    uid,
                    student["student_id"],
                    appeal_text,
                    datetime.utcnow(),
                    (sent.message_id if sent else None),
                    (sent.chat.id if sent else None),
                    student["full_name"],
                    student["faculty"],
                    student["group"],
                )

            await msg.answer("✅ Murojaat muvaffaqiyatli jo‘natildi.")
            user_state.pop(uid, None)

    @dp.message(lambda m: ADMIN_CHAT_ID is not None and m.chat.id == (ADMIN_CHAT_ID if isinstance(ADMIN_CHAT_ID, int) else m.chat.id) and m.reply_to_message is not None)
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
                        "UPDATE appeals SET admin_reply=$1, answered=$2 WHERE group_msg_id=$3",
                        msg.text, True, replied_msg_id
                    )
        except Exception:
            logger.exception("Xatolik (admin reply)")

    logger.info("Bot ishga tushdi ✅")

    scheduler = AsyncIOScheduler(timezone=TZ_TASHKENT)
    tashkent_tz = pytz.timezone("Asia/Tashkent")
    scheduler.add_job(
        generate_and_send_report,
        args=(db_pool, bot, "daily"),
        trigger=CronTrigger(hour=12, minute=35, timezone=tashkent_tz)
    )

    scheduler.add_job(
        generate_and_send_report,
        args=(db_pool, bot, "monthly"),
        trigger=CronTrigger(day='last', hour=20, minute=0, timezone=tashkent_tz)
    )
    for job in scheduler.get_jobs():
        logger.info(f"JOB mavjud: {job}")
    scheduler.start()

    await dp.start_polling(bot)

async def generate_and_send_report(db_pool, bot: Bot, report_type: str):
    logger.info(f"Hisobot funksiyasi ishga tushdi: {report_type}")
    now = datetime.now(timezone.utc)
    async with db_pool.acquire() as conn:
        if report_type == "daily":
            start_time = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            end_time = now
            filename = f"hisobot_kunlik_{now.strftime('%Y-%m-%d')}.xlsx"
        elif report_type == "monthly":
            start_time = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end_time = now
            filename = f"hisobot_oylik_{now.strftime('%Y-%m')}.xlsx"
        else:
            return

        rows = await conn.fetch("""
            SELECT hemis_id, murojaat, time, answered, admin_reply,
                fish, fakultet, guruh
                FROM appeals
                WHERE time BETWEEN $1 AND $2
                ORDER BY time DESC
        """, start_time, end_time)

        if not rows:
            await send_to_admin(bot, f"ℹ️ {report_type.capitalize()} hisobot: Yangi murojaatlar yo‘q.")
            return

        data = []
        for row in rows:
            # time may be offset-aware, agar yo'q bo'lsa to'liq offset bilan o'rnatish kerak
            time_val = row['time']
            if time_val.tzinfo is None:
                time_val = time_val.replace(tzinfo=timezone.utc).astimezone(TZ_TASHKENT)
            else:
                time_val = time_val.astimezone(TZ_TASHKENT)

            data.append({
                "HEMIS ID": row['hemis_id'],
                "F.I.SH.": row['fish'] or "Noma'lum",
                "Fakultet": row['fakultet'] or "Noma'lum",
                "Guruh": row['guruh'] or "Noma'lum",
                "Murojaat": row['murojaat'],
                "Vaqti": time_val.strftime('%Y-%m-%d %H:%M:%S'),
                "Javob berilganmi": "Ha" if row['answered'] else "Yo‘q",
                "Admin javobi": row['admin_reply'] or "",
            })

        df = pd.DataFrame(data)
        df.to_excel(filename, index=False)

        try:
            file = FSInputFile(path=filename, filename=filename)
            await bot.send_document(chat_id=ADMIN_CHAT_ID, document=file,
                                    caption=f"📊 {report_type.capitalize()} hisobot")
        except Exception:
            logger.exception("Hisobot yuborishda xatolik")
        finally:
            os.remove(filename)


if __name__ == "__main__":
    asyncio.run(main())
