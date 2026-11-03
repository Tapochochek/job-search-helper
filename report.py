"""Event journal (data/events.jsonl, written by tg_notifier) -> text report for the Telegram bot."""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

LABELS = [("submitted", "отправлено"), ("prepared", "подготовлено"), ("skipped", "пропущено"),
          ("manual", "нужны ручные ответы"), ("error", "ошибок"), ("captcha", "капч")]


def load_events(path: Path) -> list[dict]:
    out = []
    try:
        for ln in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue  # a half-written line must not break the report
    except OSError:
        pass
    return out


def _line(events: list[dict]) -> str:
    c = Counter(e.get("kind") for e in events)
    parts = [f"{label} {c[k]}" for k, label in LABELS if c[k]]
    return ", ".join(parts) or "ничего не было"


def build_report(events: list[dict], now: float | None = None) -> str:
    now = now or time.time()
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = [e for e in events if e.get("ts", 0) >= midnight]
    week = [e for e in events if e.get("ts", 0) >= midnight - 6 * 86400]
    sent = [e for e in events if e.get("kind") == "submitted"]
    by_resume = Counter(e.get("note") for e in sent if e.get("note"))
    lines = ["📈 Отчёт", f"Сегодня: {_line(today)}", f"7 дней: {_line(week)}", f"Всего: {_line(events)}"]
    if by_resume:
        lines.append("Отправлено по резюме: " + "; ".join(f"{r} — {n}" for r, n in by_resume.most_common(5)))
    pending = [e for e in events if e.get("kind") == "manual"][-5:]
    if pending:
        lines.append("\n✍️ Ждут твоих ответов (последние):")
        lines += [f"• {e.get('note', '')[:120]}\n  {e.get('url', '')}" for e in pending]
    errors = [e for e in events if e.get("kind") == "error"][-3:]
    if errors:
        lines.append("\n⚠️ Последние ошибки:")
        lines += [f"• {e.get('note', '')[:120]}\n  {e.get('url', '')}" for e in errors]
    return "\n".join(lines)[:3900]
