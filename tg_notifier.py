"""Lightweight async Telegram notifier based on httpx."""
import json
import logging
import time

import httpx

import config

logger = logging.getLogger(__name__)

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _event(kind: str, url: str = "", title: str = "", note: str = "") -> None:
    """Append to data/events.jsonl: the journal behind the bot's /report. Never raises."""
    try:
        config.DATA_DIR.mkdir(exist_ok=True)
        with open(config.EVENTS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": int(time.time()), "kind": kind, "url": url, "title": title[:150], "note": note[:300]},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass


async def send_photo(png: bytes, caption: str) -> bool:
    """Send a screenshot (e.g. the captcha page) to the chat. Best effort."""
    if config.TELEGRAM_BOT_TOKEN.startswith("PUT_YOUR") or not config.TELEGRAM_BOT_TOKEN or str(config.TELEGRAM_CHAT_ID).startswith("PUT_YOUR"):
        return False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=20.0)) as client:
            resp = await client.post(_API_URL.format(token=config.TELEGRAM_BOT_TOKEN).replace("sendMessage", "sendPhoto"),
                                     data={"chat_id": config.TELEGRAM_CHAT_ID, "caption": caption[:900]},
                                     files={"photo": ("page.png", png, "image/png")})
        return resp.status_code == 200
    except httpx.HTTPError as exc:
        logger.error("Failed to send Telegram photo: %s", type(exc).__name__)
        return False


async def send_telegram(text: str) -> bool:
    """Send a message to the configured Telegram chat. Returns True on success."""
    if not config.TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN.startswith("PUT_YOUR"):
        logger.warning("Telegram token is not configured; skipping notification.")
        return False
    if not config.TELEGRAM_CHAT_ID or config.TELEGRAM_CHAT_ID.startswith("PUT_YOUR"):
        logger.warning("TELEGRAM_CHAT_ID is not configured (see .env); skipping notification.")
        return False

    url = _API_URL.format(token=config.TELEGRAM_BOT_TOKEN)
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text[:4000], "disable_web_page_preview": True}
    for attempt in (1, 2):  # api.telegram.org is flaky from some networks: retry once
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=20.0)) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                # Telegram explains the 400 in the body (e.g. "chat not found") - retrying won't help
                logger.error("Telegram rejected the message (%s): %s", resp.status_code, resp.text[:300])
                return False
            return True
        except httpx.HTTPError as exc:
            # Don't log the exception text: httpx puts the full URL (with the bot token) in it
            logger.error("Failed to send Telegram message (attempt %d/2): %s", attempt, type(exc).__name__)
    return False


async def notify_applied(vacancy_title: str, vacancy_url: str, letter: str, resume_title: str = "", submitted: bool = True) -> bool:
    _event("submitted" if submitted else "prepared", vacancy_url, vacancy_title, resume_title)
    head = "✅ Отклик отправлен" if submitted else "📝 Отклик подготовлен (не отправлен)"
    resume_line = f"\nРезюме: {resume_title}" if resume_title else ""
    return await send_telegram(f"{head}\n{vacancy_title}\n{vacancy_url}{resume_line}\n\n{letter}")


async def notify_skipped(vacancy_title: str, vacancy_url: str, reason: str) -> bool:
    _event("skipped", vacancy_url, vacancy_title, reason)
    return await send_telegram(f"⏭ Пропущено\n{vacancy_title}\n{vacancy_url}\n{reason}")


async def notify_error(vacancy_url: str, error: str) -> bool:
    _event("error", vacancy_url, note=error)
    logger.error("%s | %s", vacancy_url, error)  # also visible in the terminal if Telegram is down
    return await send_telegram(f"⚠️ Ошибка\n{vacancy_url}\n{error}")


async def notify_captcha(vacancy_url: str) -> bool:
    _event("captcha", vacancy_url)
    return await send_telegram(f"🛑 Обнаружена капча. Требуется ручное вмешательство.\n{vacancy_url}")


async def notify_manual(vacancy_url: str, questions: list[str]) -> bool:
    _event("manual", vacancy_url, note="; ".join(questions))
    return await send_telegram("✍️ Нужны ручные ответы на вопросы работодателя:\n- " + "\n- ".join(questions) + f"\n{vacancy_url}")
