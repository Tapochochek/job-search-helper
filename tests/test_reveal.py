"""The response modal: the cover-letter field may appear late, or only after "Добавить сопроводительное"."""
import asyncio, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import pytest
pw = pytest.importorskip("playwright.async_api")
from app import reveal_cover_letter_field

# modal "loads" for 1.2 s, then shows a button (not <button>: a span, like many hh widgets); click reveals the textarea after 400 ms
LATE_BUTTON = """<div role=dialog id=m><h2>Отклик на вакансию</h2></div><script>
setTimeout(()=>{const s=document.createElement('span');s.textContent='Добавить сопроводительное';s.style.cursor='pointer';
 s.onclick=()=>setTimeout(()=>{const t=document.createElement('textarea');t.name='cover_letter';document.getElementById('m').append(t)},400);
 document.getElementById('m').append(s)},1200)</script>"""
DIRECT = "<div role=dialog><textarea name=cover_letter></textarea></div>"
NOTHING = "<div role=dialog><h2>Отклик на вакансию</h2></div>"

async def _run(html):
    async with pw.async_playwright() as p:
        b = await p.chromium.launch(); page = await b.new_page(); await page.set_content(html)
        t0 = asyncio.get_event_loop().time()
        r = await reveal_cover_letter_field(page)
        res = (r is not None and await r.is_visible(), asyncio.get_event_loop().time() - t0)
        await b.close(); return res

def test_late_button_then_field():
    ok, _ = asyncio.run(_run(LATE_BUTTON)); assert ok

def test_field_already_there_is_instant():
    ok, dt = asyncio.run(_run(DIRECT)); assert ok and dt < 2

def test_no_field_returns_none():
    ok, _ = asyncio.run(_run(NOTHING)); assert not ok
