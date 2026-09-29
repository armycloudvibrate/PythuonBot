import argparse
import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.types import Channel
from telethon.errors import ChannelsTooMuchError, FloodWaitError

from opentele.td import TDesktop
from opentele.api import API, UseCurrentSession


# ============================================================
#                       КОНФИГ
# ============================================================
BOT_TOKEN = "8938803697:AAEhnDFCCZnBdrgjx1SsSH0GdmRjqHTxxBA"

TARGET_CHANNEL = "fastconnews"
REFERRAL_URL = "https://t.me/obhod_mobilniy_bot?start=ref_7799755762"

# 🔽 Текст инлайн-кнопки (регистр не важен, ищем подстроку)
BUTTON_TEXT = "я подписался"

# ⚠️ В РФ/на зарезанных сетях ПРОКСИ ОБЯЗАТЕЛЕН, иначе будет висеть на connect().
# Пример: ("socks5", "127.0.0.1", 1080)
#         ("http",   "1.2.3.4",   8080)
#         ("mtproxy", "1.2.3.4", 443, "dd1234567890abcdef1234567890abcdef")
PROXY = None
# PROXY = ("socks5", "127.0.0.1", 1080)

# Таймауты (сек)
TDATA_LOAD_TIMEOUT = 180
SESSION_CREATE_TIMEOUT = 120
CONNECT_TIMEOUT = 40

# Каналы/супергруппы, из которых НЕЛЬЗЯ выходить при автоочистке.
# Можно указывать @username, ID (int) или ссылки t.me/...
WHITELIST = {
    "fastconnews",
    "obhod_mobilniy_bot",
    # "@my_work_channel",
    # 1234567890,
}

# Сколько каналов максимум выходить за один заход (чтобы не словить FloodWait).
MAX_LEAVE_PER_RUN = 50


# ============================================================
#                       ЛОГИ
# ============================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("userbot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============================================================
#                       УТИЛИТЫ
# ============================================================
def extract_tdata_from_zip(zip_path: str, extract_to: str) -> str:
    if not zipfile.is_zipfile(zip_path):
        raise ValueError("Это не .zip архив.")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(extract_to)

    for root, dirs, files in os.walk(extract_to):
        if os.path.basename(root) == "tdata" and any(
            f.startswith("key_datas") for f in files
        ):
            return root
    for root, dirs, files in os.walk(extract_to):
        if "tdata" in dirs:
            candidate = os.path.join(root, "tdata")
            for r2, d2, f2 in os.walk(candidate):
                if any(f.startswith("key_datas") for f in f2):
                    return candidate
    for root, dirs, files in os.walk(extract_to):
        if any(f.startswith("key_datas") for f in files):
            return root
    raise ValueError("В архиве не найдена папка tdata (нет key_datas).")


def parse_referral(url: str) -> tuple[str, str | None]:
    m = re.match(r"https?://t\.me/([^/?#]+)(?:\?start=([^&\s]+))?", url.strip())
    if not m:
        raise ValueError(f"Не могу распарсить реферальную ссылку: {url}")
    return m.group(1), m.group(2)


def _normalize_username(x) -> str:
    """Приводит @username / t.me/username / username к нижнему регистру без @."""
    if isinstance(x, int):
        return str(x)
    s = str(x).strip().lower()
    s = re.sub(r"^https?://t\.me/", "", s)
    s = s.lstrip("@").split("?")[0].split("/")[0]
    return s


def _is_whitelisted(entity, whitelist: set) -> bool:
    """Проверяет, попадает ли канал/супергруппа под белый список."""
    try:
        uid = getattr(entity, "id", None)
        uname = (getattr(entity, "username", None) or "").lower()
        if uid is not None and str(uid) in whitelist:
            return True
        if uname and uname in whitelist:
            return True
    except Exception:
        pass
    return False


async def _leave_some_channels(client, report, whitelist: set, limit: int) -> int:
    """
    Выходит из каналов/супергрупп (кроме белого списка).
    Возвращает количество, из которых удалось выйти.
    """
    left = 0
    try:
        dialogs = await client.get_dialogs(limit=300)
    except Exception as e:
        await report(f"⚠️ Не смог получить список диалогов: {e}")
        return 0

    candidates = []
    for d in dialogs:
        ent = getattr(d, "entity", None)
        if not isinstance(ent, Channel):
            continue  # только каналы и супергруппы, обычные группы не трогаем
        if _is_whitelisted(ent, whitelist):
            continue
        candidates.append(ent)

    await report(f"🧹 Нашёл {len(candidates)} каналов/супергрупп для выхода (лимит: {limit}).")

    for ent in candidates[:limit]:
        if left >= limit:
            break
        try:
            await client(LeaveChannelRequest(ent))
            left += 1
            if left % 10 == 0:
                await report(f"   ...вышел из {left}")
            await asyncio.sleep(0.8)  # анти-флуд
        except FloodWaitError as e:
            await report(f"⏳ FloodWait {e.seconds}с — ждём...")
            await asyncio.sleep(e.seconds + 1)
            try:
                await client(LeaveChannelRequest(ent))
                left += 1
            except Exception as e2:
                log.warning(f"leave after flood failed: {e2}")
        except Exception as e:
            log.warning(f"leave {getattr(ent, 'id', '?')} failed: {e}")

    await report(f"🧹 Вышел из {left} каналов/супергрупп.")
    return left


async def ensure_subscribed(client, target: str, report) -> None:
    """
    Гарантирует подписку на target.
    Если аккаунт упёрся в лимит — выходит из чужих каналов и пробует снова.
    """
    whitelist = {_normalize_username(x) for x in WHITELIST}

    async def _try_join() -> bool:
        try:
            channel = await client.get_entity(target)
            await client(JoinChannelRequest(channel))
            return True
        except ChannelsTooMuchError:
            return False
        except Exception as e:
            # Если уже подписаны — Telegram вернёт UserAlreadyParticipantError,
            # его можно считать успехом.
            if "already" in str(e).lower():
                return True
            raise

    # Первая попытка
    if await _try_join():
        await report(f"✅ Подписка на @{target}")
        return

    # Упёрлись в лимит — чистим и пробуем снова
    await report(
        f"⚠️ Лимит каналов достигнут. Чищу подписки "
        f"(белый список: {sorted(whitelist) or '—'})..."
    )
    left = await _leave_some_channels(client, report, whitelist, MAX_LEAVE_PER_RUN)
    if left == 0:
        raise RuntimeError(
            "Не удалось освободить ни одного слота. "
            "Проверь WHITELIST или освободи каналы вручную."
        )

    await asyncio.sleep(2)
    if await _try_join():
        await report(f"✅ Подписка на @{target} (после очистки {left} шт.)")
        return

    raise RuntimeError(
        f"Всё ещё не могу подписаться на @{target} даже после выхода из {left} каналов."
    )


# ============================================================
#                 ОСНОВНАЯ ЛОГИКА USERBOTA
# ============================================================
async def run_userbot(tdata_path: str, report, password: str | None = None) -> None:
    # ---------- 1. Чтение tdata ----------
    await report("🔐 Открываю tdata...")
    t0 = time.time()

    def _load_tdesk():
        try:
            return TDesktop(tdata_path, password=password) if password else TDesktop(tdata_path)
        except TypeError:
            return TDesktop(tdata_path)

    try:
        tdesk = await asyncio.wait_for(
            asyncio.to_thread(_load_tdesk),
            timeout=TDATA_LOAD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"Таймаут чтения tdata (>{TDATA_LOAD_TIMEOUT}с).")

    # В opentele >= 0.2 LoadTData может быть отдельным async-методом
    load_tdata = getattr(tdesk, "LoadTData", None)
    if callable(load_tdata):
        try:
            r = load_tdata()
            if asyncio.iscoroutine(r):
                await asyncio.wait_for(r, timeout=TDATA_LOAD_TIMEOUT)
        except TypeError:
            pass  # требует аргументов — пропускаем
        except Exception as e:
            log.warning(f"LoadTData: {e}")

    # Проверка загрузки (isLoaded есть не во всех версиях)
    is_loaded = getattr(tdesk, "isLoaded", None)
    if callable(is_loaded):
        loaded = is_loaded()
        if asyncio.iscoroutine(loaded):
            loaded = await loaded
        if not loaded:
            raise RuntimeError("Не удалось прочитать tdata (повреждена или защищена паролем).")

    await report(f"📖 tdata прочитан за {time.time()-t0:.1f}с. Готовлю сессию...")

    # ---------- 2. Получаем строку сессии из tdata ----------
    t1 = time.time()
    api = API.TelegramDesktop

    async def _get_session_str() -> str:
        # --- Вариант A: ToSession (opentele >= 0.2 — async) ---
        to_session = getattr(tdesk, "ToSession", None)
        if callable(to_session):
            try:
                sess = await to_session(flag=UseCurrentSession, api=api)
                if isinstance(sess, str):
                    return sess
                if isinstance(sess, StringSession):
                    return StringSession.save(sess)
                if hasattr(sess, "save"):
                    return sess.save()
            except (AttributeError, TypeError) as e:
                log.warning(f"ToSession не сработал: {e}, пробую ToTelethon")

        # --- Вариант B: ToTelethon (возвращает уже подключённый клиент) ---
        to_telethon = getattr(tdesk, "ToTelethon", None)
        if to_telethon is None:
            raise RuntimeError("opentele: нет ни ToSession, ни ToTelethon")

        client_tmp = await to_telethon(session=None, flag=UseCurrentSession, api=api)
        try:
            return StringSession.save(client_tmp.session)
        finally:
            try:
                await client_tmp.disconnect()
            except Exception:
                pass

    try:
        session_str = await asyncio.wait_for(
            _get_session_str(),
            timeout=SESSION_CREATE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"Таймаут создания сессии (>{SESSION_CREATE_TIMEOUT}с).")

    if not isinstance(session_str, str):
        raise RuntimeError(f"Ожидалась строка сессии, получено: {type(session_str)}")

    await report(f"🔑 Сессия готова за {time.time()-t1:.1f}с. Подключаюсь...")

    # ---------- 3. Клиент сразу с прокси ----------
    client = TelegramClient(
        StringSession(session_str),
        api_id=api.api_id,
        api_hash=api.api_hash,
        proxy=PROXY,
        connection_retries=3,
        retry_delay=1,
        timeout=15,
        request_retries=3,
        auto_reconnect=False,
        flood_sleep_threshold=0,
    )
    client.parse_mode = None

    t2 = time.time()
    try:
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise RuntimeError(
            f"Таймаут подключения к Telegram (>{CONNECT_TIMEOUT}с).\n"
            "Скорее всего нужен прокси — задай PROXY в начале файла."
        )

    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Сессия не авторизована (аккаунт разлогинен).")

        dc_id = None
        try:
            dc_id = client.session.dc_id
        except Exception:
            pass

        me = await asyncio.wait_for(client.get_me(), timeout=30)
        uname = f"@{me.username}" if me.username else f"{me.first_name} (id={me.id})"
        await report(f"👤 Аккаунт: {uname} | DC={dc_id} | подключение {time.time()-t2:.1f}с")

        # ---------- 4. Реферальная ссылка ----------
        bot_username, start_param = parse_referral(REFERRAL_URL)
        await report(f"🔗 @{bot_username} | start={start_param}")

        # ---------- 5. Подписка на канал (с автоочисткой) ----------
        await ensure_subscribed(client, TARGET_CHANNEL, report)

        await asyncio.sleep(1.5)

        # ---------- 6. Сохраняем ссылку в «Избранное» ----------
        try:
            await client.send_message("me", REFERRAL_URL)
            await report("✅ Ссылка в «Избранном»")
        except Exception as e:
            await report(f"⚠️ Не смог сохранить в «Избранное»: {e}")

        await asyncio.sleep(1)

        # ---------- 7. Переход по ссылке ----------
        try:
            target = await client.get_entity(bot_username)
            start_text = f"/start {start_param}" if start_param else "/start"
            await client.send_message(target, start_text)
            await report(f"✅ Переход → @{bot_username} ({start_text})")
        except Exception as e:
            await report(f"❌ Переход в @{bot_username}: {e}")
            raise

        await asyncio.sleep(2)

        # ---------- 8. Нажатие инлайн-кнопки ----------
        clicked = False
        btn_text_found = ""
        for _ in range(5):
            messages = await client.get_messages(target, limit=10)
            for msg in messages:
                if not msg.reply_markup:
                    continue
                rows = getattr(msg.reply_markup, "rows", None) or []
                for row in rows:
                    for button in row.buttons:
                        text = getattr(button, "text", "") or ""
                        if BUTTON_TEXT.lower() in text.lower():
                            await msg.click(text=text)
                            clicked = True
                            btn_text_found = text
                            break
                    if clicked:
                        break
                if clicked:
                    break
            if clicked:
                break
            await asyncio.sleep(1.5)

        if clicked:
            await report(f"✅ Кнопка «{btn_text_found}» нажата")
        else:
            await report(f"⚠️ Кнопка «{BUTTON_TEXT}» не найдена")

        await report("🏁 Готово.")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ============================================================
#                 TELEGRAM-БОТ (управляющий)
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет!\n\n"
        "Пришли мне <b>.zip</b> архив с папкой <b>tdata</b>.\n\n"
        "Что сделаю от имени аккаунта:\n"
        f"1) Подпишусь на <b>@{TARGET_CHANNEL}</b>\n"
        "   (если лимит — выйду из чужих каналов и повторю)\n"
        "2) Сохраню реферальную ссылку в «Избранное»\n"
        "3) Перейду по ней в бота\n"
        f"4) Нажму кнопку «<b>{BUTTON_TEXT}</b>»\n"
        "5) Пришлю отчёт\n\n"
        "Если у tdata стоит пароль — укажи его в подписи к архиву.\n\n"
        "⚠️ tdata = полный доступ к аккаунту. Только для личного использования.",
        parse_mode="HTML",
    )


@dp.message(F.document)
async def handle_document(message: types.Message):
    doc = message.document
    fname = (doc.file_name or "").lower()
    if not fname.endswith(".zip"):
        await message.answer("⚠️ Нужен .zip архив с папкой tdata.")
        return

    password = (message.caption or "").strip() or None
    status = await message.answer("⬇️ Скачиваю архив...")
    tmp = tempfile.mkdtemp(prefix="td_upload_")

    try:
        zip_path = os.path.join(tmp, doc.file_name or "tdata.zip")
        await bot.download(doc, destination=zip_path)

        await status.edit_text("📦 Распаковываю...")
        tdata_path = await asyncio.to_thread(extract_tdata_from_zip, zip_path, tmp)

        await status.edit_text("🚀 Работаю от имени аккаунта...")

        async def report(text: str):
            try:
                await message.answer(text)
            except Exception as e:
                log.error(f"send report failed: {e}")

        await run_userbot(tdata_path, report, password=password)
        await status.edit_text("🏁 Завершено. Отчёт выше ☝️")

    except Exception as e:
        log.exception("handle_document error")
        try:
            await status.edit_text(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")
        except Exception:
            await message.answer(f"❌ Ошибка: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
#                       ЗАПУСК
# ============================================================
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--tdata", help="Путь к папке tdata (локальный режим).")
    p.add_argument("--password", help="Пароль от tdata (если есть).", default=None)
    return p.parse_args()


async def main():
    args = parse_args()

    if args.tdata:
        async def report(text: str):
            print(text)

        print(f"▶️ Локальный режим. tdata: {args.tdata}")
        await run_userbot(args.tdata, report, password=args.password)
    else:
        log.info("Запускаю управляющего бота...")
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
