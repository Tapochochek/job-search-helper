"""All AI logic: choose the best resume for a vacancy, score fit, write a grounded Russian letter."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import config
from llm_providers import LLMProvider, get_provider
from resumes import Resume

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# 1. Resume choice + fit score
# ----------------------------------------------------------------------------
_CHOOSE_SYSTEM = (
    "Ты — рекрутер-ассистент. Тебе дают вакансию и пронумерованный список резюме одного кандидата. "
    "Выбери резюме, которое лучше всего подходит под вакансию (смотри на главный стек и название роли в вакансии), "
    "и оцени соответствие кандидата вакансии по шкале от 0 до 10. "
    "Оценивай строго, ориентируясь только на то, что реально написано в резюме. "
    'Ответь ТОЛЬКО JSON именно в таком порядке полей: {"reason": "<одно короткое предложение по-русски '
    'о ВЫБРАННОМ резюме>", "resume": <номер выбранного резюме из квадратных скобок>, "fit": <0-10>}'
)

_GENERIC = {"разработчик", "программист", "инженер", "специалист", "developer", "engineer", "senior",
            "middle", "junior", "lead", "ведущий", "старший", "младший", "backend", "frontend", "fullstack"}


@dataclass
class Choice:
    resume: Resume
    fit: int
    reason: str


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-zа-я0-9+#]{2,}", s.lower().replace("ё", "е"))}


def keyword_choice(vacancy_title: str, vacancy_text: str, resumes: list[Resume]) -> Choice:
    """Offline fallback: token overlap, title weighted x3. Used if the LLM call/parse fails."""
    v_title, v_all = _tokens(vacancy_title), _tokens(vacancy_title + " " + vacancy_text)
    best, best_score = resumes[0], -1.0
    for r in resumes:
        score = 3 * len(v_title & _tokens(r.title)) + len(v_all & _tokens(r.text))
        if score > best_score:
            best, best_score = r, score
    return Choice(best, fit=5, reason="выбрано по совпадению ключевых слов (AI недоступен)")


def title_match(vacancy_title: str, pool: list[Resume]) -> Resume | None:
    """The one resume whose distinctive title words (c#, net, 1с, python...) appear in the VACANCY
    TITLE. None if no resume or several resumes match (then the LLM decides)."""
    head = _tokens(vacancy_title)
    hits = [(len((_tokens(r.title) - _GENERIC) & head), r) for r in pool]
    top = max((h for h, _ in hits), default=0)
    winners = [r for h, r in hits if h == top]
    return winners[0] if top > 0 and len(winners) == 1 else None


def _parse_json(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def choose_resume(
    vacancy_title: str,
    vacancy_text: str,
    resumes: list[Resume],
    allowed: list[Resume] | None = None,
    provider: LLMProvider | None = None,
) -> Choice:
    """`allowed` = resumes actually offered in hh.ru's dropdown for this vacancy."""
    pool = allowed or resumes
    if not pool:
        raise ValueError("No resumes available. Run: python app.py --sync-resumes")
    hint = title_match(vacancy_title, pool)
    if hint is not None and len(pool) > 1:
        logger.info("Resume fixed by the vacancy title: '%s'", hint.title)
        pool = [hint]  # the LLM still scores the fit, but for the right resume
    # Even with a single resume we still ask the LLM: the fit score lets us skip hopeless vacancies.
    listing = "\n\n".join(f"[{i}] {r.title}\n{r.full_text(2500)}" for i, r in enumerate(pool))
    user = (
        f"ВАКАНСИЯ: {vacancy_title}\n{vacancy_text[:4000]}\n\n"
        f"РЕЗЮМЕ КАНДИДАТА:\n{listing}"
    )
    try:
        provider = provider or get_provider()
        raw = await provider.complete(_CHOOSE_SYSTEM, user, max_tokens=200, temperature=0.0, json_mode=True)
        data = _parse_json(raw)
        if data is None:
            raise ValueError(f"unparseable answer: {raw[:120]!r}")
        idx = int(data["resume"]) if len(pool) > 1 else 0  # one candidate: nothing to pick
        fit = max(0, min(10, int(data["fit"])))
        return Choice(pool[idx], fit, str(data.get("reason", "")).strip())
    except Exception as exc:
        logger.warning("LLM resume choice failed (%s); using keyword fallback", exc)
        return keyword_choice(vacancy_title, vacancy_text, pool)


# ----------------------------------------------------------------------------
# 2. Cover letter
# ----------------------------------------------------------------------------
_GENDER_RULE = {
    "male": "Кандидат — мужчина: глаголы прошедшего времени от первого лица пиши в мужском роде.",
    "female": "Кандидат — женщина: глаголы прошедшего времени от первого лица пиши в женском роде.",
    "neutral": (
        "Не используй глаголы прошедшего времени от первого лица (я работал/работала) — "
        "пиши так, чтобы род не требовался: «имею опыт…», «занимаюсь…», «опыт — 4 года», «знаком с…»."
    ),
}

_LETTER_SYSTEM = """Ты помогаешь соискателю писать сопроводительные письма для hh.ru.

ЯЗЫК: пиши ТОЛЬКО по-русски. Английские слова допустимы лишь как названия технологий и инструментов (Python, FastAPI, PostgreSQL, 1C, Docker). Не пиши ни одного английского предложения.

ФАКТЫ: используй ТОЛЬКО то, что написано в резюме. Ничего не выдумывай: не называй годы опыта, технологии, компании, проекты и достижения, которых нет в резюме. Если какое-то требование вакансии в резюме не подтверждено — просто не упоминай его. Не пиши о зарплатных ожиданиях.

СТРУКТУРА (3–5 предложений, примерно 400–700 символов):
1. Что именно привлекло в этой вакансии (назови роль и одну конкретную деталь из описания).
2. Два-три самых релевантных факта из резюме, сопоставленных с требованиями вакансии.
3. Короткое завершение: готовность обсудить детали.

ПОДТВЕРЖДЁННЫЕ СОВПАДЕНИЯ: если в запросе есть блок «Совпадения вакансии и резюме», строй письмо именно на них.

ПРИМЕР ТОНА И ДЛИНЫ (другая профессия — факты из примера НЕ использовать, копировать нельзя):
«Заинтересовала вакансия логиста: вы ищете человека для выстраивания маршрутов по региону, и это близко к моей текущей работе. Имею опыт 5 лет в транспортной компании: планирование рейсов, работа с перевозчиками, ведение документооборота в 1С. Готов обсудить детали и ответить на вопросы по опыту.»

СТИЛЬ: деловой, спокойный, конкретный. Без приветствия и подписи (они добавятся автоматически), без markdown, без кавычек вокруг текста, без клише («командный игрок», «стрессоустойчивый», «быстро обучаюсь»). {gender_rule}

Верни ТОЛЬКО текст письма, без пояснений."""


def validate_letter(text: str) -> str | None:
    """Return None if OK, otherwise a short Russian reason (fed back to the model on retry)."""
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", text)
    if not letters:
        return "в тексте нет слов"
    latin = sum(1 for c in letters if re.match(r"[A-Za-z]", c))
    if latin / len(letters) > 0.20:
        return "слишком много английского текста; пиши только по-русски"
    if len(text) < config.LETTER_MIN_CHARS:
        return "письмо слишком короткое"
    if len(text) > config.LETTER_MAX_CHARS:
        return "письмо слишком длинное, сократи до 5 предложений"
    if re.search(r"```|\*\*|^#", text, re.M):
        return "не используй markdown"
    return None


def _norm_facts(s: str) -> str:
    # Latin "c" next to "1" is how people type "1С"; unify so "1C" == "1С"
    return re.sub(r"1[cс]", "1с", s.lower().replace("ё", "е"))


def check_grounding(letter: str, resume_text: str, vacancy_text: str = "") -> str | None:
    """Cheap anti-hallucination guard: every English term and every "N лет" in the letter
    must literally occur in the resume (English terms may also come from the vacancy)."""
    resume_n = _norm_facts(resume_text)
    allowed_n = resume_n + " " + _norm_facts(vacancy_text)
    for term in sorted(set(re.findall(r"[A-Za-z][A-Za-z0-9+#]*(?:[.\-][A-Za-z0-9+#]+)*", re.sub(r"1[CcСс]", "1с", letter)))):
        if _norm_facts(term) not in allowed_n:
            return f"упомянуто «{term}», которого нет в резюме и вакансии; убери это"
    for num in re.findall(r"(\d+)\s*(?:лет|года|год)\b", letter.lower()):
        if not re.search(rf"(?<!\d){num}(?!\d)", resume_n):
            return f"указан опыт «{num}», которого нет в резюме; убери эту цифру"
    return None


_MATCH_SYSTEM = (
    "Ты анализируешь вакансию и резюме. Выпиши, какие требования вакансии ПОДТВЕРЖДЕНЫ текстом резюме. "
    "Не додумывай: если в резюме нет прямого подтверждения, не включай пункт. "
    'Ответь ТОЛЬКО JSON: {"matches": ["<требование вакансии — чем подтверждено в резюме>", ...], '
    '"missing": ["<требование, которого нет в резюме>", ...]}. Не больше 5 пунктов в каждом списке, по-русски.'
)


async def extract_matches(vacancy_title: str, vacancy_text: str, resume: Resume, provider: LLMProvider) -> list[str]:
    """Step 1 of letter writing: ground the letter in explicit evidence. Returns [] on any failure."""
    user = (
        f"ВАКАНСИЯ: {vacancy_title}\n{vacancy_text[: config.VACANCY_MAX_CHARS]}\n\n"
        f"РЕЗЮМЕ:\n{resume.full_text()}"
    )
    try:
        data = _parse_json(await provider.complete(_MATCH_SYSTEM, user, max_tokens=400, temperature=0.0, json_mode=True))
        return [str(m).strip() for m in (data or {}).get("matches", []) if str(m).strip()][:5]
    except Exception as exc:
        logger.warning("Match extraction failed (%s); writing the letter without it", exc)
        return []


def clean_letter(text: str) -> str:
    text = text.strip().strip('"«»“”').strip()
    text = re.sub(r"^(письмо|сопроводительное письмо|текст письма)\s*:\s*", "", text, flags=re.I)
    # drop a greeting the model added anyway; we add our own
    text = re.sub(r"^(здравствуйте|добрый день|уважаемые [^\n]*?)[!,.]*\s*\n+", "", text, flags=re.I)
    return text.strip()


async def generate_cover_letter(
    vacancy_title: str,
    vacancy_text: str,
    resume: Resume,
    provider: LLMProvider | None = None,
) -> str:
    vacancy_text = (vacancy_text or "").strip()
    if not vacancy_text:
        raise ValueError("Vacancy text is empty")
    provider = provider or get_provider()

    system = _LETTER_SYSTEM.format(gender_rule=_GENDER_RULE.get(config.CANDIDATE_GENDER, _GENDER_RULE["neutral"]))
    matches = await extract_matches(vacancy_title, vacancy_text, resume, provider)
    matches_block = ("Совпадения вакансии и резюме:\n" + "\n".join(f"- {m}" for m in matches) + "\n\n") if matches else ""
    user = (
        f"ВАКАНСИЯ: {vacancy_title}\n{vacancy_text[: config.VACANCY_MAX_CHARS]}\n\n"
        f"РЕЗЮМЕ КАНДИДАТА («{resume.title}»):\n{resume.full_text()}\n\n"
        f"{matches_block}"
        "Напиши сопроводительное письмо на русском языке."
    )

    problem = None
    for attempt in range(1, config.LETTER_MAX_ATTEMPTS + 1):
        prompt = user if not problem else f"{user}\n\nПредыдущий вариант отклонён: {problem}. Исправь и напиши заново."
        raw = await provider.complete(system, prompt, max_tokens=700, temperature=0.4)
        body = clean_letter(raw)
        problem = validate_letter(body) or check_grounding(body, resume.full_text(), vacancy_text)
        if problem is None:
            return f"{config.LETTER_GREETING}\n\n{body}"
        logger.warning("Letter attempt %d rejected: %s", attempt, problem)
    raise RuntimeError(f"Could not produce a valid letter after {config.LETTER_MAX_ATTEMPTS} attempts ({problem})")
