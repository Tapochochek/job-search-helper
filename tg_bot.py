"""Telegram remote control for the local app (long polling, stdlib only). Runs as a thread inside gui.py.

The program has to stay on your PC anyway (Chrome with your login + the local model live there), so the
bot is only a remote: buttons -> the same start/stop the GUI uses. Only messages from TELEGRAM_CHAT_ID
are obeyed; everything else is ignored.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

from report import build_report

logger = logging.getLogger("tg_bot")


def _b(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _kb(*rows) -> dict:
    return {"inline_keyboard": [list(r) for r in rows]}


MENU = _kb([_b("▶️ Автопоиск", "search"), _b("⏹ Стоп", "stop")],
           [_b("📊 Статус", "status"), _b("📈 Отчёт", "report")],
           [_b("🔄 Обновить резюме", "sync"), _b("📜 Лог", "log")])
BACK = _kb([_b("⬅️ Меню", "menu")])


class Bot:
    def __init__(self, token: str, chat_id, ctl, api=None):
        self.token, self.chat, self.ctl = token, int(chat_id), ctl
        self._api = api or self._http
        self.offset = 0

    # -- transport ---------------------------------------------------------------------------
    def _http(self, method: str, **params) -> dict:
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}", data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=int(params.get("timeout", 0)) + 15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:  # never put the URL (it holds the token) into messages
            raise RuntimeError(f"HTTP {e.code}" + (" (запущен второй экземпляр бота?)" if e.code == 409 else "")) from None
        except Exception as e:
            raise RuntimeError(type(e).__name__) from None

    def send(self, text: str, kb: dict | None = None, edit: int | None = None) -> None:
        p = {"chat_id": self.chat, "text": text[:4000], "disable_web_page_preview": True}
        if kb:
            p["reply_markup"] = kb
        try:
            if edit:
                self._api("editMessageText", message_id=edit, **p)
            else:
                self._api("sendMessage", **p)
        except RuntimeError:
            if edit:  # message too old / unchanged: send a fresh one
                self._api("sendMessage", **p)
            else:
                raise

    # -- logic -------------------------------------------------------------------------------
    def menu_text(self) -> str:
        return "Управление автооткликами\nСейчас: " + ("⏳ работает" if self.ctl.busy() else "💤 свободно")

    def handle(self, upd: dict) -> None:
        cb, msg = upd.get("callback_query"), upd.get("message")
        if cb:
            chat, data, mid = cb["message"]["chat"]["id"], cb.get("data", ""), cb["message"]["message_id"]
        elif msg:
            chat, data, mid = msg["chat"]["id"], (msg.get("text") or "").strip(), None
        else:
            return
        if chat != self.chat:
            logger.warning("Ignored a message from an unknown chat")
            return
        if cb:
            try:
                self._api("answerCallbackQuery", callback_query_id=cb["id"])
            except RuntimeError:
                pass
        self.route(data.lstrip("/").split("@")[0], mid)

    def route(self, cmd: str, mid: int | None) -> None:
        c = self.ctl
        if cmd in ("start", "menu", "help", ""):
            return self.send(self.menu_text(), MENU, mid)
        if cmd == "search":
            if c.busy():
                return self.send("⏳ Уже идёт задача. Сначала «Стоп» или дождись конца.", MENU, mid)
            names = sorted(c.presets())
            if not names:
                return self.send("Нет сохранённых поисков. Настрой фильтры в GUI и нажми «Сохранить поиск».", BACK, mid)
            rows = [[_b(n[:40], f"p:{i}")] for i, n in enumerate(names)] + [[_b("⬅️ Меню", "menu")]]
            return self.send("Какой поиск запустить?", _kb(*rows), mid)
        if cmd.startswith("p:"):
            names = sorted(c.presets())
            i = int(cmd[2:]) if cmd[2:].isdigit() else -1
            if not 0 <= i < len(names):
                return self.send("Поиск не найден, открой список заново.", BACK, mid)
            return self.send(f"Поиск «{names[i]}»\nКак запустить?", _kb(
                [_b("📝 Только заполнить формы", f"go:{i}:0")], [_b("📨 Отправлять отклики", f"go:{i}:1")],
                [_b("⬅️ Назад", "search")]), mid)
        if cmd.startswith("go:"):
            _, i, s = cmd.split(":")
            names = sorted(c.presets())
            if not i.isdigit() or int(i) >= len(names):
                return self.send("Поиск не найден, открой список заново.", BACK, mid)
            err = c.start_search(names[int(i)], s == "1")
            return self.send(f"❌ {err}" if err else f"🚀 Запущено: «{names[int(i)]}» ({'отправка ВКЛ' if s == '1' else 'только заполнение'}). Итог пришлю сюда.", MENU, mid)
        if cmd == "stop":
            return self.send("⏹ Остановлено." if c.stop() else "Сейчас ничего не запущено.", MENU, mid)
        if cmd == "sync":
            err = c.start_sync()
            return self.send(f"❌ {err}" if err else "🔄 Обновляю резюме с hh.ru…", MENU, mid)
        if cmd == "status":
            tail = "\n".join(c.tail(6)) or "(журнал пуст)"
            return self.send(f"{self.menu_text()}\n\nПоследние строки:\n{tail}", MENU, mid)
        if cmd == "log":
            return self.send("📜 Журнал:\n" + ("\n".join(c.tail(20)) or "(пусто)"), MENU, mid)
        if cmd == "report":
            return self.send(build_report(c.events()), MENU, mid)
        return self.send("Не понял. Вот меню:", MENU, mid)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                r = self._api("getUpdates", offset=self.offset, timeout=25, allowed_updates=["message", "callback_query"])
            except RuntimeError as e:
                logger.warning("Telegram polling: %s", e)
                stop.wait(8)
                continue
            for u in r.get("result", []):
                self.offset = u["update_id"] + 1
                try:
                    self.handle(u)
                except Exception as e:  # one bad update must not kill the bot
                    logger.warning("Bot update failed: %s", type(e).__name__)
