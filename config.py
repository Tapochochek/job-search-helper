"""Central configuration. Everything can be overridden via environment variables or a .env file."""
import os
from pathlib import Path

try:  # optional: pip install python-dotenv
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEBUG_DIR = BASE_DIR / "debug"
RESUMES_FILE = DATA_DIR / "resumes.json"
APPLIED_FILE = DATA_DIR / "applied.json"
EVENTS_FILE = DATA_DIR / "events.jsonl"  # journal for the Telegram bot report

HH_BASE_URL = os.getenv("HH_BASE_URL", "https://hh.ru")
CHROME_CDP_URL = os.getenv("CHROME_CDP_URL", "http://localhost:9222")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "PUT_YOUR_TELEGRAM_CHAT_ID_HERE")

# ----------------------------------------------------------------------------
# AI providers. Pick one with LLM_PROVIDER=ollama|openai|anthropic|openrouter|deepseek
# "kind" = wire protocol: "openai" (any OpenAI-compatible API) or "anthropic".
# Adding a new provider = adding one dict here. Later a UI can write this to a file.
# ----------------------------------------------------------------------------
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama")

PROVIDERS: dict[str, dict] = {
    "ollama": {
        # Native Ollama API (NOT /v1): only it lets us set the context window. See OllamaProvider.
        "kind": "ollama",
        "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        # RTX 3060 12GB: gemma3:12b (best Russian) or qwen2.5:14b (best at JSON/instructions)
        "model": os.getenv("OLLAMA_MODEL", "qwen3.6:27b"),
        "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192")),
        "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
        "timeout": 300.0,
    },
    "openai": {
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "token_param": "max_completion_tokens",
        "timeout": 60.0,
    },
    "openrouter": {
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": os.getenv("OPENROUTER_API_KEY", ""),
        "model": os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat"),
        "token_param": "max_tokens",
        "timeout": 60.0,
    },
    "deepseek": {
        "kind": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": os.getenv("DEEPSEEK_API_KEY", ""),
        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        "token_param": "max_tokens",
        "timeout": 60.0,
    },
    "anthropic": {
        "kind": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "api_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5"),
        "timeout": 60.0,
    },
}

# ----------------------------------------------------------------------------
# Candidate / letter settings
# ----------------------------------------------------------------------------
# Used so the model doesn't write "я работал" / "я работала" incorrectly.
# male | female | neutral (neutral = avoid gendered past-tense verbs)
CANDIDATE_GENDER = os.getenv("CANDIDATE_GENDER", "neutral")

# Skip a vacancy if the best resume scores below this (0-10) for the vacancy.
MIN_FIT = int(os.getenv("MIN_FIT", "5"))  # skip vacancies scored below this (0-10)
SEARCH_LIMIT = int(os.getenv("SEARCH_LIMIT", "20"))  # max new vacancies per auto-search run

LETTER_GREETING = os.getenv("LETTER_GREETING", "Здравствуйте!")
LETTER_MIN_CHARS = 150
LETTER_MAX_CHARS = 1100
LETTER_MAX_ATTEMPTS = 3

VACANCY_MAX_CHARS = int(os.getenv("VACANCY_MAX_CHARS", "6000"))
RESUME_MAX_CHARS = int(os.getenv("RESUME_MAX_CHARS", "5000"))

# Final "Откликнуться" click. Off by default; enable with AUTO_SUBMIT=1 or --submit.
FOREIGN_APPLY = os.getenv("FOREIGN_APPLY", "remote")  # remote | always | never: vacancies in another country
CAPTCHA_WAIT_S = int(os.getenv("CAPTCHA_WAIT_S", "180"))  # how long to wait for you to solve a captcha
AUTO_SUBMIT = os.getenv("AUTO_SUBMIT", "0").lower() in ("1", "true", "yes")

# Human-like delay range (seconds) between actions
MIN_DELAY = 5
MAX_DELAY = 15

# Timeouts (milliseconds)
PAGE_LOAD_TIMEOUT_MS = 30_000
SELECTOR_TIMEOUT_MS = 15_000
