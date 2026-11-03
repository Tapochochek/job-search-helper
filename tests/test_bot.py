import pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from report import build_report
from tg_bot import Bot

class Ctl:
    def __init__(self): self.started, self._busy, self.stopped = [], False, False
    def busy(self): return self._busy
    def presets(self): return {"C# удалёнка": {}, "1С Оренбург": {}}
    def start_search(self, name, submit): self.started.append((name, submit)); return None
    def start_sync(self): return None
    def stop(self): self.stopped = True; return True
    def tail(self, n): return ["line1", "line2"]
    def events(self): return [{"ts": time.time(), "kind": "submitted", "note": "Программист 1С", "url": "u"},
                              {"ts": time.time(), "kind": "manual", "note": "Зарплата?", "url": "https://hh.ru/vacancy/1"}]

def make():
    calls = []
    bot = Bot("1:x", 555, Ctl(), api=lambda m, **p: calls.append((m, p)) or {"result": []})
    return bot, calls

def cbq(data, chat=555): return {"update_id": 1, "callback_query": {"id": "c", "data": data, "message": {"chat": {"id": chat}, "message_id": 7}}}
def msg(text, chat=555): return {"update_id": 2, "message": {"chat": {"id": chat}, "text": text}}

def test_ignores_foreign_chat():
    bot, calls = make()
    bot.handle(msg("/start", chat=999)); bot.handle(cbq("stop", chat=999))
    assert calls == [] and not bot.ctl.stopped

def test_menu_and_start_search_flow():
    bot, calls = make()
    bot.handle(msg("/start"))
    assert calls[-1][0] == "sendMessage" and "свободно" in calls[-1][1]["text"]
    bot.handle(cbq("search"))  # sorted: "1С Оренбург" = 0, "C# удалёнка" = 1 (Cyrillic/digits sort deterministic)
    names = sorted(bot.ctl.presets())
    kb = calls[-1][1]["reply_markup"]["inline_keyboard"]
    assert calls[-1][0] == "editMessageText" and kb[0][0]["callback_data"] == "p:0"
    bot.handle(cbq("p:1")); assert names[1] in calls[-1][1]["text"]
    bot.handle(cbq("go:1:1"))
    assert bot.ctl.started == [(names[1], True)] and "Запущено" in calls[-1][1]["text"]

def test_busy_blocks_new_search_and_stop_works():
    bot, calls = make(); bot.ctl._busy = True
    bot.handle(cbq("search")); assert "Уже идёт" in calls[-1][1]["text"] and not bot.ctl.started
    bot.handle(cbq("stop")); assert bot.ctl.stopped

def test_stale_buttons_do_not_crash():
    bot, calls = make()
    bot.handle(cbq("p:99")); bot.handle(cbq("go:99:1")); bot.handle(cbq("p:abc"))
    assert not bot.ctl.started

def test_report_has_counts_and_pending():
    r = build_report(Ctl().events())
    assert "Сегодня: отправлено 1, нужны ручные ответы 1" in r and "Зарплата?" in r and "Программист 1С — 1" in r
