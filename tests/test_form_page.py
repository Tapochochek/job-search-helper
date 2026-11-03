"""Full-page questions form (hh.ru sometimes opens it instead of the modal). Needs playwright + chromium."""
import asyncio, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import pytest
pw = pytest.importorskip("playwright.async_api")
import extras
from app import find_letter_field, submit_response
from llm_providers import LLMProvider
from resumes import Resume

FORM = """<html><body><header><input type=text placeholder="Поиск"></header><main>
<h1>Ответьте на вопросы работодателя</h1>
%s
<div><label for=q1>Сколько лет опыта с .NET?</label><input type=text id=q1 name=q1></div>
<fieldset><legend>Готовы к удалённой работе?</legend>
 <label><input type=radio name=r value=y> Да</label><label><input type=radio name=r value=n> Нет</label></fieldset>
<label><input type=checkbox name=pd> Даю согласие на обработку персональных данных</label>
<button type=button onclick="document.title='SENT'">Откликнуться</button></main></body></html>"""
LETTER = '<div><label for=t>Сопроводительное письмо</label><textarea id=t name=text></textarea></div>'
RES = Resume("1", "C#/.NET-разработчик", "C#, .NET 4 года, ASP.NET Core, PostgreSQL", "")

class Fake(LLMProvider):
    name = model = "fake"
    async def complete(self, system, user, **kw):
        if "проходишь тест" in system:
            return '{"reasoning": "ok", "choice": [0], "confident": true}'
        return '{"known": true, "answer": "4 года"}'

async def _run(html):
    async with pw.async_playwright() as p:
        b = await p.chromium.launch(); page = await b.new_page()
        await page.set_content(html)
        scope = page.locator("main").first
        letter = await find_letter_field(scope)
        manual = await extras.answer_questions(page, scope, letter, "Dev", "Нужен .NET", RES, provider=Fake())
        out = dict(letter=letter is not None, manual=manual, q1=await page.input_value("#q1"),
                   radio=await page.evaluate("document.querySelector('input[name=r]:checked')?.value"),
                   consent=await page.evaluate("document.querySelector('input[name=pd]').checked"))
        if letter: out["letter_untouched"] = await letter.input_value() == ""
        await submit_response(scope, letter)
        out["sent"] = await page.title() == "SENT"
        await b.close()
        return out

def test_form_with_letter_field():
    o = asyncio.run(_run(FORM % LETTER))
    assert o["letter"] and o["letter_untouched"] and o["q1"] == "4 года" and o["radio"] == "y"
    assert not o["consent"] and len(o["manual"]) == 1 and "согласие" in o["manual"][0].lower() and o["sent"]

def test_form_without_letter_field():
    o = asyncio.run(_run(FORM % ""))
    assert not o["letter"] and o["q1"] == "4 года" and o["radio"] == "y" and not o["consent"] and o["sent"]
