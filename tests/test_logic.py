import asyncio, json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import httpx
import config
from resumes import Resume, match_option_to_resume
import ai_handler as ai
from llm_providers import AnthropicProvider, OllamaProvider, LLMProvider

R1C = Resume("1", "Программист 1С", "Программист 1С. 3 года. 1С:ERP, УТ 11, обмены данными, отчёты СКД, доработка конфигураций.")
RPY = Resume("2", "Python-разработчик", "Python backend. 4 года. FastAPI, asyncio, PostgreSQL, Docker, pytest.")

class Fake(LLMProvider):
    name, model = "fake", "fake"
    def __init__(self, answers): self.answers, self.calls = list(answers), []
    async def complete(self, system, user, **kw):
        self.calls.append(user); return self.answers.pop(0)

GOOD = ("Заинтересовала вакансия программиста 1С: вы ищете человека для разработки и сопровождения конфигураций. "
        "Имею опыт 3 года в 1С:ERP и УТ 11: доработка конфигураций, обмены данными, отчёты на СКД. "
        "Понимаю, как устроена работа с ТЗ и самостоятельная сдача результата. Готов обсудить детали на собеседовании.")

def run(c): return asyncio.run(c)

def test_validate():
    assert ai.validate_letter(GOOD) is None
    assert "английск" in ai.validate_letter("I have 4 years of experience in Python development and automation of processes. " * 3)
    assert ai.validate_letter("Коротко.") is not None

def test_retry_on_english():
    eng = "My experience as a Python developer for 4 years allows me to offer expertise in backend development and automation. " * 2
    f = Fake(['{"matches": ["1С — 3 года опыта"]}', eng, GOOD])
    out = run(ai.generate_cover_letter("Программист 1С", "Нужен 1С программист", R1C, provider=f))
    assert out.startswith("Здравствуйте!") and "1С" in out and len(f.calls) == 3
    assert "Совпадения вакансии и резюме" in f.calls[1] and "отклонён" in f.calls[2]

def test_greeting_stripped():
    assert ai.clean_letter("Здравствуйте!\n\nТекст").startswith("Текст")

def test_choose_llm_and_fallback():
    f = Fake(['Вот ответ: {"resume": 1, "fit": 8, "reason": "Python подходит"}'])
    c = run(ai.choose_resume("Backend Python", "FastAPI", [R1C, RPY], provider=f))
    assert c.resume.id == "2" and c.fit == 8
    bad = Fake(["не знаю"])
    c = run(ai.choose_resume("Программист 1С", "нужен опыт 1С ERP", [RPY, R1C], provider=bad))
    assert c.resume.id == "1"

def test_match_option():
    assert match_option_to_resume("Программист 1С\nОбновлено 2 ноября", [R1C, RPY]).id == "1"
    assert match_option_to_resume("python разработчик", [R1C, RPY]).id == "2"
    assert match_option_to_resume("Повар", [R1C, RPY]) is None

def test_anthropic_wire():
    seen = {}
    def handler(req):
        seen["h"], seen["b"] = req.headers, json.loads(req.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": " привет "}]})
    p = AnthropicProvider("anthropic", {"api_key": "k", "model": "m", "base_url": "https://x/v1"}, transport=httpx.MockTransport(handler))
    assert run(p.complete("sys", "usr")) == "привет"
    assert seen["h"]["x-api-key"] == "k" and seen["b"]["system"] == "sys" and seen["b"]["messages"][0]["content"] == "usr"


def test_grounding_rejects_invented_facts():
    g = ai.check_grounding
    assert g(GOOD, R1C.text) is None
    assert "Python" in g("Имею опыт Senior Python разработчика, 3 года в 1С.", R1C.text, "Нужен 1С")
    assert "10" in g("Имею опыт 10 лет в 1С:ERP.", R1C.text)
    assert g("Знаком с FastAPI и PostgreSQL.", RPY.text) is None
    assert g("Опыт 1C 3 года.", R1C.text) is None  # Latin C == Cyrillic С


def test_no_matches_step_failure_still_writes_letter():
    f = Fake(["не json", GOOD])
    assert ai.generate_cover_letter and run(ai.generate_cover_letter("Программист 1С", "Нужен 1С", R1C, provider=f)).endswith("детали на собеседовании.")


def test_ollama_wire_sets_context():
    seen = {}
    def handler(req):
        seen["b"] = json.loads(req.content); seen["u"] = str(req.url)
        return httpx.Response(200, json={"message": {"content": " ок "}, "prompt_eval_count": 100})
    p = OllamaProvider("ollama", {"model": "m", "base_url": "http://h:11434", "num_ctx": 8192}, transport=httpx.MockTransport(handler))
    assert run(p.complete("s", "u", json_mode=True)) == "ок"
    assert seen["u"].endswith("/api/chat") and seen["b"]["options"]["num_ctx"] == 8192
    assert seen["b"]["format"] == "json" and seen["b"]["think"] is False
