# hh.ru Auto-Apply Helper (Chrome CDP)

## 1. Launch Chrome with remote debugging
```
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\chrome-hh-profile"
```
(Linux: `google-chrome --remote-debugging-port=9222 --user-data-dir=$HOME/chrome-hh-profile`)
Log in to hh.ru there (solve captchas manually).

## 2. Install & configure
```
pip install -r requirements.txt
playwright install chromium   # only if Playwright asks for it
cp .env.example .env          # then edit
```

## 3. Import your resumes (once, and after you edit them on hh.ru)
```
python app.py --sync-resumes
```
Creates `data/resumes.json`. Open it and check the text is sensible. The `extra` field is yours:
add projects, numbers, or anything the hh resume page doesn't show — it is never overwritten.

## 4. Run
```
python app.py --debug https://hh.ru/vacancy/12345678   # fills the form, does NOT send
python app.py --submit --file urls.txt                 # fills and SENDS
```
Per vacancy: read vacancy -> open response modal -> read the resume dropdown -> AI picks the best
resume + fit score (0-10) -> select it -> write the letter from THAT resume -> fill -> (submit).
Vacancies with fit < `MIN_FIT` are skipped and reported to Telegram. Handled vacancies are
remembered in `data/applied.json`.

Safety: with `--submit`, the click happens only if the chosen resume was verified in the dropdown.

## GUI (python gui.py)
Left: filters like on hh.ru (query, where to search, region, work format, experience, schedule,
salary, freshness, sorting). They are turned into a hh.ru search link shown above the Run button -
open it on hh.ru to check it. Already set filters up on the site? Paste that link into "Разобрать ссылку":
the form fills in, and parameters the GUI doesn't know are passed through unchanged.
Tab "Конкретные вакансии": paste vacancy links. Settings: AI provider/model/key, gender, minimum
score, Telegram - saved to `.env`. Filter values are remembered in the browser.

## Telegram bot (control + reports)
`python gui.py` also starts the bot when `TELEGRAM_BOT_TOKEN` and a NUMERIC `TELEGRAM_CHAT_ID` are set
(GUI -> Settings -> Telegram). Write /start to the bot: buttons for Автопоиск (your saved searches, either
"only fill forms" or "send"), Стоп, Статус, Отчёт, Обновить резюме, Лог. Only your chat is obeyed.
The PC with Chrome and the model must stay on: the bot is a remote control, not a server.
Save searches in the GUI ("Сохранить поиск"). Report = data/events.jsonl (sent / prepared / skipped /
manual answers needed / errors / captchas, by day, with links to what waits for you). `python gui.py --no-open`
starts without opening a browser tab.

## Captcha
It is not solved automatically. The script pings Telegram with a screenshot of the page and waits
(`CAPTCHA_WAIT_S`, default 180 s) while you solve it in the Chrome window, then continues by itself.

## Employer questions
Handled in the modal AND on the separate full page hh.ru opens for some vacancies: text fields,
radio/checkbox groups and lists are answered by the model (technical questions from its own knowledge,
personal facts only from the resume). Consents (personal data etc.) are never ticked and anything the
model isn't sure about is sent to Telegram as "нужны ручные ответы"; such a form is left open and not submitted.
A page dump of that layout is saved to `debug/questions_page_*.html` each time.

## Choosing an AI
Set `LLM_PROVIDER` in `.env`. Adding a provider = one dict in `config.PROVIDERS`.

Local (RTX 3060 12GB): `ollama pull gemma3:12b` (best Russian) or `ollama pull qwen2.5:14b`
(best at instructions/JSON). Don't go above ~14B: it spills into RAM and gets very slow.
The Ollama provider talks to the native API and sets `OLLAMA_NUM_CTX` (default 8192). If the log
says "prompt may have been truncated", raise it (16384 fits on 12GB for 12-14B models, slower).

Letter pipeline: (1) AI lists which vacancy requirements the resume really confirms,
(2) AI writes the letter from those, (3) code rejects the letter if it is not Russian, has
markdown, or mentions an English term / "N лет" that is not in the resume or vacancy; up to 3 retries.

## If something doesn't click
hh.ru selectors for the resume dropdown are best-effort. Run with `--debug`, then send the files
from `debug/` (`*.html` + `*.png`) and the selectors can be fixed against the real page.
