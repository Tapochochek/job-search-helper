import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import pytest
from filters import build_search_url, parse_search_url

URL = ("https://orenburg.hh.ru/search/vacancy?text=C%23+.NET&area=70&experience=between1And3&work_format=REMOTE"
       "&work_format=HYBRID&search_field=name&work_schedule_by_days=OTHER&hhtmFrom=main&page=2&foo=bar&salary=100000")

def test_roundtrip_keeps_unknown_params_and_drops_noise():
    f = parse_search_url(URL)
    b = build_search_url(f)
    assert parse_search_url(b) == f
    assert "foo=bar" in b and "work_schedule_by_days=OTHER" in b and "page=" not in b and "hhtmFrom" not in b

def test_rejects_foreign_hosts_and_pages():
    for bad in ("https://evil.com/search/vacancy?x=1", "https://hh.ru/vacancy/1", "javascript:1"):
        with pytest.raises(ValueError):
            parse_search_url(bad)
    with pytest.raises(ValueError):
        build_search_url({"host": "evil.com"})

def test_build_sanitizes():
    u = build_search_url({"text": "  Python   dev ", "work_format": ["REMOTE", "<x>"], "area": ["1", "a2"], "period": "99",
                          "order_by": "zzz", "extra": [["a b", "1"], ["text", "hack"], ["ok", "1"]]})
    assert u == "https://hh.ru/search/vacancy?text=Python+dev&work_format=REMOTE&area=1&order_by=publication_time&ok=1"
