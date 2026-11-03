"""Local store of the candidate's REAL resumes (imported from hh.ru, editable by hand).

data/resumes.json:
[
  {"id": "abc123...", "title": "Программист 1С", "text": "<full resume text>", "extra": "<optional facts you add by hand>"},
  ...
]
`extra` is never overwritten by --sync-resumes: put things hh's resume page doesn't show
(projects, numbers, preferred tone) there.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import asdict, dataclass

import config

logger = logging.getLogger(__name__)

RESUME_ID_RE = re.compile(r"/resume/([0-9a-f]{20,})")


@dataclass
class Resume:
    id: str
    title: str
    text: str
    extra: str = ""

    def full_text(self, limit: int | None = None) -> str:
        body = self.text.strip()
        if self.extra.strip():
            body += "\n\nДополнительно:\n" + self.extra.strip()
        return body[: limit or config.RESUME_MAX_CHARS]


def load_resumes() -> list[Resume]:
    if not config.RESUMES_FILE.exists():
        return []
    raw = json.loads(config.RESUMES_FILE.read_text(encoding="utf-8"))
    return [Resume(**{k: r.get(k, "") for k in ("id", "title", "text", "extra")}) for r in raw]


def save_resumes(resumes: list[Resume]) -> None:
    config.DATA_DIR.mkdir(exist_ok=True)
    config.RESUMES_FILE.write_text(
        json.dumps([asdict(r) for r in resumes], ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я+#]+", " ", s).strip()


def match_option_to_resume(option_text: str, resumes: list[Resume]) -> Resume | None:
    """Map a dropdown label from the hh.ru modal to one of our stored resumes."""
    opt = _norm(option_text)
    if not opt:
        return None
    best, best_score = None, 0.0
    for r in resumes:
        t = _norm(r.title)
        if not t:
            continue
        if opt == t:
            return r
        score = 0.9 if (t in opt or opt in t) else difflib.SequenceMatcher(None, opt, t).ratio()
        if score > best_score:
            best, best_score = r, score
    return best if best_score >= 0.75 else None


# ----------------------------------------------------------------------------
# Import from hh.ru through the already logged-in Chrome session
# ----------------------------------------------------------------------------
async def sync_from_hh(context) -> list[Resume]:
    """Open the applicant's resume list, then each resume, and store the visible text.
    Selectors are best-effort; if something is off, edit data/resumes.json manually."""
    old = {r.id: r for r in load_resumes()}
    page = await context.new_page()
    try:
        await page.goto(f"{config.HH_BASE_URL}/applicant/resumes", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        hrefs = await page.eval_on_selector_all(
            'a[href*="/resume/"]', "els => els.map(e => e.getAttribute('href'))"
        )
        ids: list[str] = []
        for h in hrefs:
            m = RESUME_ID_RE.search(h or "")
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
        logger.info("Found %d resume(s) on hh.ru", len(ids))

        result: list[Resume] = []
        for rid in ids:
            await page.goto(f"{config.HH_BASE_URL}/resume/{rid}", wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            title = ""
            for sel in ('[data-qa="resume-block-title-position"]', "h1"):
                loc = page.locator(sel).first
                if await loc.count():
                    title = (await loc.inner_text()).strip()
                    if title:
                        break
            container = page.locator("main").first
            if not await container.count():
                container = page.locator("body")
            text = (await container.inner_text()).strip()
            result.append(
                Resume(id=rid, title=title or rid, text=text, extra=old[rid].extra if rid in old else "")
            )
            logger.info("Imported resume '%s' (%d chars)", title, len(text))
        if result:
            save_resumes(result)
        return result
    finally:
        await page.close()
