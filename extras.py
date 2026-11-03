"""Screening questions of the response form + unexpected pop-ups ("other country", ...)."""
from __future__ import annotations

import json
import logging
import re

import config
from ai_handler import _GENDER_RULE, _parse_json
from llm_providers import get_provider

logger = logging.getLogger(__name__)
LETTER_RE = re.compile(r"сопроводит|письм|cover", re.I)
REMOTE_RE = re.compile(r"удал[её]н|remote|из дома|дистанционн", re.I)
POPUP_SEL = ('[role="dialog"]:visible:not(:has(textarea)):not([data-qa="drop-base"]), '
             '[data-qa="modal"]:visible:not(:has(textarea))')

_LABEL_JS = """el => {
  const ids = (el.getAttribute('aria-labelledby') || '').split(' ').filter(Boolean);
  let t = ids.map(i => (document.getElementById(i) || {}).innerText || '').join(' ').trim();
  for (let n = el, i = 0; !t && n && i < 4; i++) {
    n = n.parentElement; const l = n && n.querySelector('label,legend,[class*=label]');
    t = l ? l.innerText.trim() : '';
  }
  return t;
}"""

async def field_label(loc) -> str:
    return (await loc.evaluate(_LABEL_JS)).strip()


async def find_letter_field(scope):
    """The cover-letter textarea on a full-page form (no stable selector): a visible textarea whose
    label / placeholder / name mentions a letter. None if the form has no such field."""
    areas = scope.locator("textarea:visible")
    for i in range(await areas.count()):
        t = areas.nth(i)
        hay = " ".join([await field_label(t), await t.get_attribute("placeholder") or "", await t.get_attribute("name") or ""])
        if LETTER_RE.search(hay):
            return t
    return None


_QUESTION_SYSTEM = (
    "Ты отвечаешь за соискателя на вопрос работодателя в форме отклика. Технические вопросы на знания "
    "(что такое, чем отличается, как работает, напиши запрос/код) отвечай своими знаниями — кратко и точно. "
    "Личные факты (опыт, проекты, игры, зарплата, переезд, сроки выхода) бери ТОЛЬКО из резюме и вакансии, "
    "ничего не выдумывай; если их там нет — known=false. Вопросы вроде «как понимаете роль» отвечай по вакансии "
    "и резюме. Ответ по-русски, 1–4 коротких предложения (код — как есть), без приветствий. {gender}\n"
    'Только JSON: {{"known": true|false, "answer": "<текст>"}}'
)
_CHOICE_SYSTEM = (
    "Ты проходишь тест при отклике на вакансию. Для технических вопросов выбирай верный ответ по своим знаниям; "
    "для вопросов про личный опыт кандидата опирайся ТОЛЬКО на резюме. Если вопрос не технический и ответа нет "
    "в резюме (зарплата, переезд, график и т.п.) — confident=false. {rule} Сначала кратко рассуждай. "
    'Только JSON: {{"reasoning": "<до 2 предложений>", "choice": [<номера вариантов>], "confident": true|false}}'
)
CONSENT_RE = re.compile(r"согласен|согласие|персональн|обработк|политик|подтверждаю", re.I)

# Collect radio/checkbox groups; tags every input with data-hhq="<group>-<option>" for later clicks.
_GROUPS_JS = """root => {
  const groups = new Map();
  root.querySelectorAll('input[type=radio],input[type=checkbox]').forEach(el => {
    if (el.closest('[data-qa="drop-base"]')) return;
    let row = null;
    for (let n = el.parentElement; n && n !== root; n = n.parentElement) {
      if (n.querySelectorAll('input[type=' + el.type + ']').length !== 1) break;
      if (n.innerText.trim()) { row = n; break; }  // nearest ancestor that has the option text
    }
    if (!row || !row.getClientRects().length) return;
    const key = el.type + ':' + (el.name || 'noname');
    if (!groups.has(key)) groups.set(key, {kind: el.type, els: [], opts: [], rows: []});
    const g = groups.get(key); g.els.push(el); g.rows.push(row);
    g.opts.push(row.innerText.trim().split('\\n')[0]);
  });
  const out = [];
  [...groups.values()].forEach((g, gi) => {
    g.els.forEach((el, oi) => el.setAttribute('data-hhq', gi + '-' + oi));
    let lca = g.rows[0].parentElement;
    while (lca && !g.rows.every(r => lca.contains(r))) lca = lca.parentElement;
    let q = '';
    for (let n = lca, i = 0; n && n !== root && i < 4 && !q; n = n.parentElement, i++) {
      q = n.innerText.split('\\n').map(x => x.trim()).filter(x => x && !g.opts.some(o => x.startsWith(o))).join(' ');
    }
    out.push({kind: g.kind, q: q.slice(0, 500), opts: g.opts, checked: g.els.some(e => e.checked)});
  });
  return out;
}"""


_DIALOG_SYSTEM = (
    "Ты автоматизируешь отклики на hh.ru. Тебе показывают всплывающее окно и его кнопки. Если окно лишь "
    "предупреждает/информирует — выбери кнопку, которая продолжает отклик. Верни NONE, если окно требует оплату, "
    "подписку, согласие на что-либо, загрузку или изменение данных, удаление, подтверждение личности, или ты не "
    'уверен. Только JSON: {"button": "<текст кнопки ровно как в списке или NONE>"}'
)


async def answer_questions(page, scope, textarea, title: str, vacancy_text: str, resume, provider=None) -> list[str]:
    """Fill the employer's free-text questions from the resume. Returns the questions that need
    a human (unknown answer / unsupported field type); empty list = everything answered."""
    manual: list[str] = []
    me = await textarea.element_handle() if textarea is not None else None  # None: form without a letter field
    fields = scope.locator('textarea:visible, input[type="text"]:visible, input[type="number"]:visible')
    for i in range(await fields.count()):
        f = fields.nth(i)
        try:
            if await f.evaluate("(el, me) => el === me || !!el.value || el.disabled", me):
                continue  # the letter itself, or already filled
            q = (await f.evaluate(_LABEL_JS)).strip()
            if not q:
                manual.append("(вопрос без подписи)")
                continue
            provider = provider or get_provider()
            raw = await provider.complete(
                _QUESTION_SYSTEM.format(gender=_GENDER_RULE.get(config.CANDIDATE_GENDER, _GENDER_RULE["neutral"])),
                f"ВАКАНСИЯ: {title}\n{vacancy_text[:2500]}\n\nРЕЗЮМЕ:\n{resume.full_text(3000)}\n\nВОПРОС: {q}",
                max_tokens=300, temperature=0.2, json_mode=True)
            data = _parse_json(raw) or {}
            ans = str(data.get("answer", "")).strip()
            num = (await f.get_attribute("type")) == "number"
            if not data.get("known") or not ans or (num and not re.fullmatch(r"\d+([.,]\d+)?", ans)):
                manual.append(q)
                continue
            await f.fill(ans)
            logger.info("Question answered: %s -> %s", q[:70], ans[:100])
        except Exception as exc:
            logger.warning("Question handling failed: %s", exc)
            manual.append("(не удалось обработать поле)")
    try:
        await _answer_choices(scope, title, vacancy_text, resume, provider, manual)
    except Exception as exc:
        logger.warning("Choice questions failed: %s", exc)
        manual.append("(не удалось обработать вопросы с выбором)")
    return manual


async def _ask_choice(q: str, opts: list[str], kind: str, title: str, vacancy_text: str, resume, provider):
    """LLM picks option indexes; None = not confident / invalid -> a human should answer."""
    if CONSENT_RE.search(q + " " + " ".join(opts)):
        return None  # legal consents are never ticked automatically
    rule = "Выбери ровно один вариант." if kind != "checkbox" else "Можно несколько вариантов: выбери все верные."
    listing = "\n".join(f"[{i}] {o}" for i, o in enumerate(opts))
    provider = provider or get_provider()
    raw = await provider.complete(
        _CHOICE_SYSTEM.format(rule=rule),
        f"ВАКАНСИЯ: {title}\n{vacancy_text[:1200]}\n\nРЕЗЮМЕ:\n{resume.full_text(1500)}\n\nВОПРОС: {q}\nВАРИАНТЫ:\n{listing}",
        max_tokens=400, temperature=0.0, json_mode=True)
    data = _parse_json(raw) or {}
    try:
        idx = sorted({int(i) for i in data.get("choice", [])})
    except (TypeError, ValueError):
        return None
    if not data.get("confident") or not idx or any(not 0 <= i < len(opts) for i in idx) or (kind != "checkbox" and len(idx) != 1):
        return None
    logger.info("Test answered: %s -> %s (%s)", q[:70], [opts[i] for i in idx], str(data.get("reasoning", ""))[:100])
    return idx


async def _answer_choices(scope, title, vacancy_text, resume, provider, manual) -> None:
    for gi, g in enumerate(await scope.evaluate(_GROUPS_JS)):
        if g["checked"]:
            continue  # already has an answer
        q = g["q"] or " / ".join(g["opts"])[:200]  # lone checkbox: its own text is the question
        idx = await _ask_choice(q, g["opts"], g["kind"], title, vacancy_text, resume, provider)
        if idx is None:
            manual.append(q)
            continue
        for oi in idx:  # el.click() fires the real events even for visually hidden inputs
            await scope.locator(f'[data-hhq="{gi}-{oi}"]').evaluate("el => el.click()")
        checks = [await scope.locator(f'[data-hhq="{gi}-{oi}"]').evaluate("el => el.checked") for oi in idx]
        if not all(checks):
            manual.append(q + " (не удалось отметить)")
    selects = scope.locator("select:visible")
    for i in range(await selects.count()):
        sel = selects.nth(i)
        if await sel.evaluate("el => !!el.value"):
            continue
        q = (await sel.evaluate(_LABEL_JS)).strip() or "(список без подписи)"
        real = [(j, t.strip()) for j, t in enumerate(await sel.locator("option").all_inner_texts())
                if t.strip() and (await sel.locator("option").nth(j).get_attribute("value") or "")]
        idx = await _ask_choice(q, [t for _, t in real], "select", title, vacancy_text, resume, provider) if real else None
        if idx is None:
            manual.append(q)
            continue
        await sel.select_option(index=real[idx[0]][0])


async def handle_popups(page, vacancy_text: str, title: str, rounds: int = 3, provider=None) -> str | None:
    """Deal with warning pop-ups. Returns None if clear, else the reason to stop this vacancy."""
    for _ in range(rounds):
        await page.wait_for_timeout(1500)
        dlg = page.locator(POPUP_SEL).last
        if not await dlg.count():
            return None
        text = " ".join((await dlg.inner_text()).split())
        btns = [b.strip() for b in await dlg.locator("button").all_inner_texts() if b.strip()]
        logger.info("Pop-up: %s | buttons: %s", text[:120], btns)
        if "в другой стране" in text.lower():
            remote = bool(REMOTE_RE.search(vacancy_text))
            if config.FOREIGN_APPLY == "always" or (config.FOREIGN_APPLY == "remote" and remote):
                btn = next((b for b in btns if "Все равно" in b), None)
            else:
                await page.keyboard.press("Escape")
                return "вакансия в другой стране и не удалённая (FOREIGN_APPLY в .env)"
        else:
            provider = provider or get_provider()
            raw = await provider.complete(_DIALOG_SYSTEM, f"ВАКАНСИЯ: {title}\nОКНО: {text[:600]}\nКНОПКИ: {json.dumps(btns, ensure_ascii=False)}",
                                          max_tokens=80, temperature=0.0, json_mode=True)
            btn = (_parse_json(raw) or {}).get("button")
            btn = btn if btn in btns else None
        if not btn:
            return f"неизвестное окно, нужен ручной выбор: {text[:150]}"
        await dlg.get_by_role("button", name=btn, exact=True).first.click()
        logger.info("Pop-up: clicked '%s'", btn)
    return None
