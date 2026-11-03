"""Main entry point: connects to an already opened Chrome via CDP and processes vacancies.

Usage:
    python app.py --sync-resumes                 # import your resumes from hh.ru -> data/resumes.json
    python app.py https://hh.ru/vacancy/123      # fill the response form (review, don't send)
    python app.py --submit --file urls.txt       # fill AND send
    python app.py                                # use the vacancy open in the current tab
    python app.py --debug URL                    # also dump the modal HTML + screenshot to ./debug
"""
import argparse
import asyncio
import json
import logging
import random
import re
import sys
import time

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright

import config
from ai_handler import choose_resume, generate_cover_letter
from resumes import Resume, load_resumes, match_option_to_resume, sync_from_hh
from urllib.parse import quote_plus

from extras import answer_questions, find_letter_field, handle_popups
from tg_notifier import send_photo, send_telegram, notify_applied, notify_captcha, notify_error, notify_manual, notify_skipped

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("hh_autoapply")
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines contain the bot token

SEL_DESCRIPTION = '[data-qa="vacancy-description"]'
SEL_TITLE = '[data-qa="vacancy-title"]'
SEL_SKILLS = '[data-qa="skills-element"]'
SEL_SERP = 'a[data-qa^="serp-item__title"]'  # vacancy links on a search-results page
SEARCH_MODE = False  # in search mode low-fit vacancies are remembered so they are not re-scored every run
SEL_APPLY = '[data-qa="vacancy-response-link-top"]'
SEL_COVER_LETTER = 'textarea[name="cover_letter"]'
SEL_CAPTCHA = (
    'iframe[src*="captcha"], [data-qa*="captcha"], '
    '.g-recaptcha, #captcha, [class*="captcha"]'
)
SEL_COVER_LETTER_ANY = (
    f'{SEL_COVER_LETTER}, [data-qa="vacancy-response-popup-form-letter-input"], [role="dialog"] textarea'
)
ADD_COVER_LETTER_TEXT = "Добавить сопроводительное"
SUBMIT_TEXT = "Откликнуться"

# Candidates for the resume dropdown inside the response modal (best-effort; see --debug).
RESUME_TRIGGER_SELECTORS = [
    '[data-qa*="resume-select"]',
    '[data-qa*="resume"][role="combobox"]',
    '[role="combobox"]',
    '[aria-haspopup="listbox"]',
    "select",
]
RESUME_OPTION_SELECTORS = '[role="option"], [role="listbox"] li, [role="menuitem"]'
# Radio-style resume lists (hh.ru shows these when you have several resumes)
RESUME_RADIO_SELECTOR = 'input[type="radio"]'


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
async def human_delay(min_s: float = config.MIN_DELAY, max_s: float = config.MAX_DELAY) -> None:
    delay = random.uniform(min_s, max_s)
    logger.info("Sleeping %.1f s (human-like delay)", delay)
    await asyncio.sleep(delay)


async def collect_vacancy_urls(context, search: str, pages: int, limit: int) -> list[str]:
    """Walk hh.ru search results (query text or a ready search URL) and return NEW vacancy URLs."""
    if search.startswith("http"):
        base = re.sub(r"[?&]page=\d+", "", search).rstrip("?&")
    else:
        base = f"{config.HH_BASE_URL}/search/vacancy?text={quote_plus(search)}&order_by=publication_time"
    sep = "&" if "?" in base else "?"
    done, found = load_applied(), []
    page = await context.new_page()
    try:
        for n in range(pages):
            await page.goto(f"{base}{sep}page={n}", wait_until="domcontentloaded", timeout=config.PAGE_LOAD_TIMEOUT_MS)
            if not await wait_for_manual_captcha(page, page.url):
                break
            try:
                await page.wait_for_selector(SEL_SERP, timeout=config.SELECTOR_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                logger.warning("No vacancies on search page %d (end of results?)", n + 1)
                break
            for href in await page.locator(SEL_SERP).evaluate_all("els => els.map(e => e.href)"):
                m = re.match(r"(https?://[^/]+/vacancy/\d+)", href)
                if m and vacancy_id(m.group(1)) not in done and m.group(1) not in found:
                    found.append(m.group(1))
            logger.info("Search page %d: %d new vacancies so far", n + 1, len(found))
            if len(found) >= limit:
                break
            await human_delay(2, 5)
    finally:
        await page.close()
    return found[:limit]


async def wait_for_manual_captcha(page: Page, url: str | None) -> bool:
    """On a captcha: ping Telegram and wait (up to CAPTCHA_WAIT_S) while the user solves it by
    hand in the Chrome window. Returns True when the page is clear, False on timeout."""
    if not await has_captcha(page):
        return True
    logger.warning("Captcha! Solve it in the Chrome window (waiting up to %d s)", config.CAPTCHA_WAIT_S)
    await notify_captcha(url or page.url)
    try:  # show what the page looks like, so you know whether to walk to the PC
        await send_photo(await page.screenshot(), "Так выглядит страница с капчей. Реши её в окне Chrome — скрипт продолжит сам.")
    except Exception:
        pass
    for _ in range(max(1, config.CAPTCHA_WAIT_S // 3)):
        await asyncio.sleep(3)
        if not await has_captcha(page):
            logger.info("Captcha cleared, continuing")
            await asyncio.sleep(2)
            return True
    logger.error("Captcha was not solved in time; skipping this vacancy")
    return False


async def has_captcha(page: Page) -> bool:
    if "captcha" in page.url.lower():
        return True
    try:
        return await page.locator(SEL_CAPTCHA).count() > 0
    except Exception:
        return False


def vacancy_id(url: str) -> str:
    m = re.search(r"/vacancy/(\d+)", url)
    return m.group(1) if m else url


def load_applied() -> dict:
    if config.APPLIED_FILE.exists():
        return json.loads(config.APPLIED_FILE.read_text(encoding="utf-8"))
    return {}


def mark_applied(vid: str, resume_title: str) -> None:
    config.DATA_DIR.mkdir(exist_ok=True)
    data = load_applied()
    data[vid] = {"resume": resume_title, "ts": int(time.time())}
    config.APPLIED_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def debug_dump(page: Page, scope, tag: str) -> None:
    """Save modal HTML + screenshot so selectors can be fixed against the real DOM."""
    config.DEBUG_DIR.mkdir(exist_ok=True)
    stamp = f"{tag}_{int(time.time())}"
    try:
        html = await scope.evaluate("el => el.outerHTML")
        (config.DEBUG_DIR / f"{stamp}.html").write_text(html, encoding="utf-8")
        await page.screenshot(path=str(config.DEBUG_DIR / f"{stamp}.png"))
        logger.info("Debug dump saved: debug/%s.(html|png)", stamp)
    except Exception as exc:
        logger.warning("Debug dump failed: %s", exc)


# ----------------------------------------------------------------------------
# response modal
# ----------------------------------------------------------------------------
LETTER_BTN_RE = re.compile(r"(добавить|написать|приложить)\s+(сопроводительное|письмо)", re.I)
LETTER_FALLBACK_SEL = '[role="dialog"] textarea:visible, [data-qa*="response"] textarea:visible, main textarea:visible'


async def _is_visible(loc) -> bool:
    try:
        return await loc.is_visible()
    except Exception:
        return False


async def reveal_cover_letter_field(page: Page):
    """Returns a visible cover-letter textarea locator, or None.

    hh.ru's modal is not always ready at once: it may still be loading, or show only an
    "Добавить сопроводительное" button that reveals the field. So poll up to the timeout: field
    visible -> done; else click that button (button / link / bare text, up to 3 times); wait; repeat."""
    textarea = page.locator(SEL_COVER_LETTER_ANY).first
    deadline = time.monotonic() + config.SELECTOR_TIMEOUT_MS / 1000 + 5
    clicks = 0
    while time.monotonic() < deadline:
        if await _is_visible(textarea):
            return textarea
        if clicks < 3:
            btn = (page.get_by_role("button", name=LETTER_BTN_RE)
                   .or_(page.get_by_role("link", name=LETTER_BTN_RE))
                   .or_(page.get_by_text(LETTER_BTN_RE))).first
            if await _is_visible(btn):
                try:
                    await btn.click(timeout=3_000)
                    clicks += 1
                    logger.info("Clicked '%s' (%d)", ADD_COVER_LETTER_TEXT, clicks)
                    await page.wait_for_timeout(900)
                    continue
                except Exception as exc:
                    logger.warning("Could not click '%s': %s", ADD_COVER_LETTER_TEXT, str(exc)[:120])
        await page.wait_for_timeout(500)
    fallback = page.locator(LETTER_FALLBACK_SEL).first  # field exists but with attributes we don't know
    if await _is_visible(fallback):
        logger.info("Cover-letter field found by the generic selector")
        return fallback
    logger.error("Cover-letter field did not appear (button clicked %d times)", clicks)
    return None


def modal_scope(page: Page, textarea):
    """The dialog that contains the textarea (falls back to the whole page)."""
    dlg = textarea.locator('xpath=ancestor::*[@role="dialog"][1]')
    return dlg


async def find_resume_trigger(scope):
    for sel in RESUME_TRIGGER_SELECTORS:
        loc = scope.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible():
                return loc, sel
        except Exception:
            continue
    return None, None


async def radio_rows(root, resumes: list[Resume]):
    """Rows of the resume list rendered as radio buttons: [(label, row_locator), ...].
    Row = outermost ancestor that contains exactly this one radio and has text."""
    items = []
    radios = root.locator(RESUME_RADIO_SELECTOR)
    for i in range(await radios.count()):
        row = radios.nth(i).locator(
            'xpath=ancestor::*[count(.//input[@type="radio"])=1 and normalize-space(.)!=""][last()]'
        )
        try:
            if not await row.count() or not await row.is_visible():
                continue  # list collapsed -> its rows are not visible/clickable
            lines = [ln.strip() for ln in (await row.inner_text()).splitlines() if ln.strip()]
        except Exception:
            continue
        label = next((ln for ln in lines if match_option_to_resume(ln, resumes)), "")
        if label and all(label != l for l, _ in items):
            items.append((label, row))
    return items


async def _visible_text(page: Page, text: str) -> bool:
    loc = page.get_by_text(text, exact=True)
    try:
        for i in range(await loc.count()):
            if await loc.nth(i).is_visible():
                return True
    except Exception:
        pass
    return False


async def open_resume_list(page: Page, scope, resumes: list[Resume]):
    """hh.ru shows only the CURRENT resume (a collapsed header) until you click it; the radio
    list exists only while it is expanded. Returns the radio rows, expanding the list if needed."""
    items = await radio_rows(scope, resumes)
    if len(items) >= 2:
        return items
    for r in resumes:  # the header shows exactly one stored resume title
        header = scope.get_by_text(r.title, exact=True).first
        try:
            if await header.count() and await header.is_visible():
                await header.click(timeout=3_000)
                await page.wait_for_timeout(700)
                # the expanded list is a floating layer, maybe outside the modal's DOM subtree
                return await radio_rows(page.locator("body"), resumes)
        except Exception as exc:
            logger.warning("Could not expand the resume list: %s", exc)
    return items


async def selected_resume_shown(page: Page, chosen: Resume, resumes: list[Resume]) -> bool:
    """After the list is collapsed the header must show the chosen title and no other one."""
    if not await _visible_text(page, chosen.title):
        return False
    for r in resumes:
        if r.id != chosen.id and r.title != chosen.title and await _visible_text(page, r.title):
            return False
    return True


async def close_resume_popup(page: Page) -> None:
    """hh.ru keeps the resume list open as a floating layer that covers the cover-letter
    textarea. Click a neutral spot of the modal (its heading) to collapse it. NOT Escape:
    that would close the whole response modal."""
    popup = page.locator('[data-qa="drop-base"]:visible')
    try:
        if await popup.count() == 0:
            return
        await page.get_by_text("Отклик на вакансию", exact=True).first.click(timeout=3_000)
        await page.wait_for_timeout(500)
    except Exception as exc:
        logger.warning("Could not close the resume list popup: %s", exc)


async def read_resume_options(page: Page, scope):
    """Open the resume dropdown and return (trigger, current_label, [(label, locator), ...]).
    Returns (None, '', []) if the dropdown can't be found (e.g. only one resume)."""
    trigger, sel = await find_resume_trigger(scope)
    if trigger is None:
        return None, "", []
    current = (await trigger.inner_text()).strip() if sel != "select" else ""

    if sel == "select":
        opts = trigger.locator("option")
        items = []
        for i in range(await opts.count()):
            label = (await opts.nth(i).inner_text()).strip()
            if label:
                items.append((label, opts.nth(i)))
        cur = await trigger.evaluate("el => el.selectedOptions[0]?.textContent || ''")
        return trigger, cur.strip(), items

    await trigger.click()
    await page.wait_for_timeout(600)
    options = page.locator(RESUME_OPTION_SELECTORS)
    items = []
    for i in range(await options.count()):
        o = options.nth(i)
        if await o.is_visible():
            label = (await o.inner_text()).strip()
            if label and len(label) < 200:
                items.append((label, o))
    return trigger, current, items


async def pick_resume_in_modal(page: Page, scope, vacancy_title: str, vacancy_text: str,
                               resumes: list[Resume], debug: bool, pre=None):
    """Choose the best resume for this vacancy AND select it in hh.ru's dropdown.
    Returns (Choice | None, selection_ok: bool)."""
    trigger, current, items = await read_resume_options(page, scope)
    if (trigger is None or not items) and len(resumes) > 1:
        items = await open_resume_list(page, scope, resumes)
        if items:
            logger.info("Resume list opened: %d options", len(items))
            trigger = None  # no dropdown to open; options are clicked directly
            current = ""
    if debug:
        await debug_dump(page, scope, "modal_resume")

    if not items:
        logger.warning("Resume dropdown not found/readable (single resume, or selectors need fixing)")
        await debug_dump(page, scope, "modal_no_dropdown")
        # Fallback: whatever resume is currently shown
        cur_resume = match_option_to_resume(current, resumes) if current else None
        if cur_resume is None and len(resumes) == 1:
            cur_resume = resumes[0]
        if cur_resume is None:
            return None, False
        return await choose_resume(vacancy_title, vacancy_text, resumes, allowed=[cur_resume]), True

    # Map dropdown labels -> stored resumes
    mapped: list[tuple[Resume, object, str]] = []
    for label, loc in items:
        r = match_option_to_resume(label, resumes)
        if r and all(r.id != m[0].id for m in mapped):
            mapped.append((r, loc, label))
        elif r is None:
            logger.warning("Dropdown option '%s' has no match in data/resumes.json", label[:60])
    if not mapped:
        await page.keyboard.press("Escape")
        return None, False

    choice = pre if pre and any(pre.resume.id == m[0].id for m in mapped) else await choose_resume(
        vacancy_title, vacancy_text, resumes, allowed=[m[0] for m in mapped])
    logger.info("Chosen resume: '%s' (fit %d/10) — %s", choice.resume.title, choice.fit, choice.reason)

    target = next(m for m in mapped if m[0].id == choice.resume.id)
    if trigger is None:
        await target[1].click()  # radio / row list
    elif (await trigger.evaluate("el => el.tagName")).lower() == "select":
        await trigger.select_option(label=target[2])  # native <select>
    else:
        await target[1].click()  # custom dropdown
    await page.wait_for_timeout(600)
    await close_resume_popup(page)

    # Verify the dropdown / header now shows the chosen resume
    try:
        if trigger is None:
            ok = await selected_resume_shown(page, choice.resume, resumes)
            if not ok:  # one retry: tick the radio itself
                await open_resume_list(page, scope, resumes)
                radio = target[1].locator("xpath=.//input[@type='radio']").first
                if await radio.count():
                    await radio.check(force=True)
                    await page.wait_for_timeout(600)
                await close_resume_popup(page)
                ok = await selected_resume_shown(page, choice.resume, resumes)
        else:
            shown = (await trigger.inner_text()).strip()
            ok = match_option_to_resume(shown, [choice.resume]) is not None
    except Exception:
        ok = False
    if not ok:
        logger.warning("Could not verify the selected resume in the dropdown")
    return choice, ok


async def is_form_page(page: Page) -> bool:
    """After clicking "Откликнуться" hh.ru sometimes opens a separate full page with the employer's
    questions instead of the modal. Its URL no longer looks like /vacancy/<id>."""
    await page.wait_for_timeout(1500)
    return "vacancy_response" in page.url or not re.search(r"/vacancy/\d+", page.url)


async def submit_response(scope, textarea=None) -> None:
    """Click the final button. With a letter field: the button of its form/modal; without (a
    questions-only page): the button inside `scope`, falling back to the whole page."""
    name = re.compile(rf"^({SUBMIT_TEXT}|Отправить( отклик)?)$")
    container = scope
    if textarea is not None:
        container = textarea.locator(f'xpath=ancestor::*[.//button[contains(normalize-space(.), "{SUBMIT_TEXT}")]][1]')
    elif await container.get_by_role("button", name=name).count() == 0:
        container = scope.page.locator("body")
    submit_btn = container.get_by_role("button", name=name).last
    await submit_btn.wait_for(state="visible", timeout=config.SELECTOR_TIMEOUT_MS)
    await submit_btn.click()


# ----------------------------------------------------------------------------
# main per-vacancy flow
# ----------------------------------------------------------------------------
async def process_vacancy(page: Page, url: str | None, submit: bool, debug: bool) -> bool:
    """Returns True when the form was left open for the user (tab must stay)."""
    if url:
        logger.info("Opening %s", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=config.PAGE_LOAD_TIMEOUT_MS)
    else:
        url = page.url
        logger.info("Using current tab: %s", url)

    vid = vacancy_id(url)
    if vid in load_applied():
        logger.info("Vacancy %s already handled earlier; skipping", vid)
        return

    resumes = load_resumes()
    if not resumes:
        await notify_error(url, "Нет сохранённых резюме. Запусти: python app.py --sync-resumes")
        logger.error("data/resumes.json is empty. Run: python app.py --sync-resumes")
        return

    await human_delay()
    if not await wait_for_manual_captcha(page, url):
        return

    # 1. Extract vacancy
    try:
        await page.wait_for_selector(SEL_DESCRIPTION, timeout=config.SELECTOR_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        await notify_error(url, f"Selector {SEL_DESCRIPTION} not found on page")
        return

    description = (await page.locator(SEL_DESCRIPTION).first.inner_text()).strip()
    if not description:
        await notify_error(url, "Vacancy description is empty")
        return

    title = url
    if await page.locator(SEL_TITLE).count() > 0:
        title = (await page.locator(SEL_TITLE).first.inner_text()).strip() or url
    skills = []
    try:
        skills = [s.strip() for s in await page.locator(SEL_SKILLS).all_inner_texts() if s.strip()]
    except Exception:
        pass
    vacancy_text = description + (f"\n\nКлючевые навыки: {', '.join(skills)}" if skills else "")

    # 1b. Score ALL resumes against the vacancy first; low fit -> skip without touching the form
    pre = await choose_resume(title, vacancy_text, resumes)
    logger.info("Pre-check: '%s' fit %d/10 — %s", pre.resume.title, pre.fit, pre.reason)
    if pre.fit < config.MIN_FIT:
        reason = f"Низкое соответствие {pre.fit}/10 (порог {config.MIN_FIT}) для «{pre.resume.title}»: {pre.reason}"
        logger.info("Skipping: %s", reason)
        if SEARCH_MODE:
            mark_applied(vid, f"skipped, fit {pre.fit}")
        await notify_skipped(title, url, reason)
        return False

    # 2. Open the response modal (skip if it is already open, e.g. left over in this tab)
    modal_open = form_page = False
    try:
        modal_open = await page.locator(SEL_COVER_LETTER_ANY).first.is_visible()
    except Exception:
        pass
    if modal_open:
        logger.info("Response modal is already open; reusing it")
    else:
        try:
            await page.wait_for_selector(SEL_APPLY, timeout=config.SELECTOR_TIMEOUT_MS)
            await page.locator(SEL_APPLY).first.click(timeout=config.SELECTOR_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            await notify_error(url, f"Apply button {SEL_APPLY} not found or not clickable (already applied, or a modal blocks it?)")
            return
        stop = await handle_popups(page, vacancy_text, title)
        if stop:
            logger.info("Skipping: %s", stop)
            await notify_skipped(title, url, stop)
            return
        form_page = await is_form_page(page)
    await human_delay(2, 5)

    if not await wait_for_manual_captcha(page, url):
        return

    if form_page:
        logger.info("Separate questions page: %s", page.url)
        await page.wait_for_load_state("domcontentloaded")
        scope = page.locator("main").first if await page.locator("main").count() else page.locator("body")
        textarea = await find_letter_field(scope)  # None = questions only, no letter field
        await debug_dump(page, scope, "questions_page")  # a layout we have little data on: always keep a dump
    else:
        textarea = await reveal_cover_letter_field(page)
        if textarea is None:
            await debug_dump(page, page.locator("body"), "no_textarea")
            await notify_error(url, "Cover letter field did not appear (modal handling failed)")
            return
        scope = modal_scope(page, textarea)
        if await scope.count() == 0:
            scope = page.locator("body")

    # 3. Pick + select the best resume, then score the fit
    choice, selection_ok = await pick_resume_in_modal(page, scope, title, vacancy_text, resumes, debug, pre)
    if choice is None:
        await notify_error(url, "Не удалось определить резюме в окне отклика. Проверь debug/ и data/resumes.json")
        return
    if choice.fit < config.MIN_FIT:
        reason = f"Низкое соответствие {choice.fit}/10 для «{choice.resume.title}»: {choice.reason}"
        logger.info("Skipping: %s", reason)
        await page.keyboard.press("Escape")
        await notify_skipped(title, url, reason)
        return

    # 4. Letter grounded in the chosen resume (skipped when the form has no letter field)
    letter = ""
    if textarea is not None:
        try:
            letter = await generate_cover_letter(title, vacancy_text, choice.resume)
        except Exception as exc:
            logger.exception("AI generation failed")
            await notify_error(url, f"AI generation failed: {exc}")
            return
        logger.info("Letter generated (%d chars):\n%s", len(letter), letter)
        await textarea.fill(letter)
        await human_delay()
    else:
        logger.info("No cover-letter field on this form; answering the questions only")

    manual = await answer_questions(page, scope, textarea, title, vacancy_text, choice.resume)
    if manual:
        logger.warning("Нужны ручные ответы: %s", "; ".join(manual))
        await notify_manual(url, manual)

    # 5. Submit only if explicitly enabled AND the resume selection was verified
    submitted = False
    if submit and selection_ok and not manual:
        try:
            await submit_response(scope, textarea)
            stop = await handle_popups(page, vacancy_text, title)
            if stop:
                logger.info("Not submitted: %s", stop)
                await notify_skipped(title, url, stop)
                return
            submitted = True
            mark_applied(vid, choice.resume.title)
            logger.info("Application submitted")
        except PlaywrightTimeoutError:
            await notify_error(url, f"Submit button '{SUBMIT_TEXT}' not found in the response modal")
            return
    elif submit and (manual or not selection_ok):
        logger.warning("Not auto-submitting (resume not verified or manual answers needed). Check the form manually.")
    else:
        logger.info("Submit is off: review the form and click '%s' manually", SUBMIT_TEXT)

    await notify_applied(title, url, letter, choice.resume.title, submitted)
    logger.info("Done: %s", url)
    return not submitted


def load_urls(args: argparse.Namespace) -> list[str]:
    urls = list(args.urls)
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            urls.extend(line.strip() for line in fh if line.strip() and not line.startswith("#"))
    return urls


async def main() -> int:
    parser = argparse.ArgumentParser(description="hh.ru application helper (Chrome CDP)")
    parser.add_argument("urls", nargs="*", help="Vacancy URLs")
    parser.add_argument("--file", help="Text file with one vacancy URL per line")
    parser.add_argument("--sync-resumes", action="store_true", help="Import your resumes from hh.ru and exit")
    parser.add_argument("--submit", action="store_true", help="Actually click the final 'Откликнуться'")
    parser.add_argument("--search", help="Auto-search: query text or a hh.ru search URL (with your filters)")
    parser.add_argument("--pages", type=int, default=3, help="Search result pages to scan (default 3)")
    parser.add_argument("--limit", type=int, default=config.SEARCH_LIMIT, help="Max new vacancies per run")
    parser.add_argument("--debug", action="store_true", help="Dump modal HTML/screenshot into ./debug")
    args = parser.parse_args()
    urls = load_urls(args)
    submit = args.submit or config.AUTO_SUBMIT

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(config.CHROME_CDP_URL)
        except Exception as exc:
            logger.error("Cannot connect to Chrome at %s: %s", config.CHROME_CDP_URL, exc)
            await notify_error(config.CHROME_CDP_URL, f"CDP connection failed: {exc}")
            return 1
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        if args.sync_resumes:
            res = await sync_from_hh(context)
            if not res:
                logger.error("No resumes imported. Make sure you are logged in; or fill data/resumes.json by hand.")
                return 1
            logger.info("Saved %d resume(s) to %s", len(res), config.RESUMES_FILE)
            return 0

        if args.search:
            global SEARCH_MODE
            SEARCH_MODE = True
            urls = await collect_vacancy_urls(context, args.search, args.pages, args.limit)
            logger.info("Auto-search: %d new vacancies to check", len(urls))
            await send_telegram(f"🔎 Автопоиск «{args.search[:60]}»: новых вакансий — {len(urls)}")
            if not urls:
                return 0

        if not urls:
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await process_vacancy(page, None, submit, args.debug)
            except Exception as exc:
                logger.exception("Unexpected error")
                await notify_error(page.url, f"Unexpected error: {exc}")
            return 0

        for index, url in enumerate(urls, start=1):
            logger.info("=== Vacancy %d/%d ===", index, len(urls))
            page = await context.new_page()
            keep = False
            try:
                keep = await process_vacancy(page, url, submit, args.debug)
            except Exception as exc:
                logger.exception("Unexpected error")
                await notify_error(url, f"Unexpected error: {exc}")
            if not keep:  # don't pile up tabs; keep only forms waiting for the user
                try:
                    await page.close()
                except Exception:
                    pass
            if index < len(urls):
                await human_delay()
        if args.search:
            await send_telegram(f"✅ Автопоиск завершён: проверено {len(urls)} вакансий")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
