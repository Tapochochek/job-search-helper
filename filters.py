"""hh.ru search filters <-> search URL.

The GUI keeps filters as a dict; this module turns it into a https://hh.ru/search/vacancy?... link
(which app.py --search already understands) and parses a pasted link back into the dict.
Parameters we don't know are kept verbatim in `extra`, so a link copied from hh.ru with ANY filter
survives a round trip - the most reliable way to get exactly the same results as on the site.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

HOST_RE = re.compile(r"^([a-z0-9-]+\.)?hh\.ru$")
KEY_RE = re.compile(r"^[A-Za-z0-9_.\[\]-]{1,40}$")

# GUI key -> (hh.ru URL parameter, allowed values)
MULTI = {
    "search_field": ("search_field", ["name", "company_name", "description"]),
    "experience": ("experience", ["noExperience", "between1And3", "between3And6", "moreThan6"]),
    "work_format": ("work_format", ["ON_SITE", "REMOTE", "HYBRID", "FIELD_WORK"]),
    "schedule": ("work_schedule_by_days", ["FIVE_ON_TWO_OFF", "TWO_ON_TWO_OFF", "SIX_ON_ONE_OFF"]),
}
ORDERS = ["publication_time", "relevance", "salary_desc", "salary_asc"]
KNOWN = {"text", "area", "salary", "only_with_salary", "period", "order_by"} | {p for p, _ in MULTI.values()}
SINGLE = {"text", "area", "salary", "only_with_salary", "period", "order_by"}  # modelled one-to-one, never via extra
DROP = {"page", "from", "hhtmFrom", "hhtmFromLabel", "L_save_area", "clusters", "enable_snippets"}


def _digits(v, limit=None) -> str:
    v = re.sub(r"\D", "", str(v or ""))
    return v[:9] if limit is None else (v if v and int(v) <= limit else "")


def build_search_url(f: dict) -> str:
    host = f.get("host") or "hh.ru"
    if not HOST_RE.match(host):
        raise ValueError("Неверный адрес hh.ru")
    q: list[tuple[str, str]] = []
    text = " ".join(str(f.get("text") or "").split())
    if len(text) > 200:
        raise ValueError("Слишком длинный запрос (максимум 200 символов)")
    if text:
        q.append(("text", text))
    for key, (param, allowed) in MULTI.items():
        q += [(param, v) for v in f.get(key) or [] if v in allowed]
    q += [("area", str(a)) for a in f.get("area") or [] if str(a).isdigit() and len(str(a)) <= 9]
    if _digits(f.get("salary")):
        q.append(("salary", _digits(f.get("salary"))))
        if f.get("only_with_salary"):
            q.append(("only_with_salary", "true"))
    elif f.get("only_with_salary"):
        q.append(("only_with_salary", "true"))
    if _digits(f.get("period"), 30):
        q.append(("period", _digits(f.get("period"), 30)))
    q.append(("order_by", f.get("order_by") if f.get("order_by") in ORDERS else "publication_time"))
    for pair in f.get("extra") or []:
        k, v = (list(pair) + [""])[:2]
        if KEY_RE.match(str(k)) and k not in SINGLE and k not in DROP and len(str(v)) <= 200:
            q.append((str(k), str(v)))
    return f"https://{host}/search/vacancy?{urlencode(q)}"


def parse_search_url(url: str) -> dict:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not HOST_RE.match(parts.hostname or "") or not parts.path.startswith("/search/vacancy"):
        raise ValueError("Нужна ссылка поиска вида https://hh.ru/search/vacancy?…")
    f: dict = {"host": parts.hostname, "text": "", "area": [], "extra": [], "salary": "", "only_with_salary": False,
               "period": "", "order_by": "publication_time", **{k: [] for k in MULTI}}
    by_param = {p: (k, allowed) for k, (p, allowed) in MULTI.items()}
    for k, v in parse_qsl(parts.query, keep_blank_values=False):
        if k in DROP:
            continue
        if k == "text":
            f["text"] = v
        elif k == "area" and v.isdigit():
            f["area"].append(v)
        elif k == "salary" and v.isdigit():
            f["salary"] = v
        elif k == "only_with_salary":
            f["only_with_salary"] = v.lower() == "true"
        elif k == "period" and v.isdigit():
            f["period"] = v
        elif k == "order_by" and v in ORDERS:
            f["order_by"] = v
        elif k in by_param and v in by_param[k][1]:
            f[by_param[k][0]].append(v)
        else:  # unknown parameter, or a known one with a value we don't model (another schedule): keep as is
            f["extra"].append([k, v])
    return f
