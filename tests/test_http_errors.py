"""Error bodies that are not gateway JSON: edge HTML, plain text, empty. Retry-After."""

import os
import time

import pytest

import crx
from crx._bind import refused

from .conftest import BASE, Resp

NGINX_502 = (
    "<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n<body>\r\n"
    "<center><h1>502 Bad Gateway</h1></center>\r\n<hr><center>nginx/1.24.0</center>\r\n</body>\r\n</html>\r\n"
)
CLOUDFLARE_520 = (
    "<!DOCTYPE html>\n<!--[if lt IE 7]> <html class=\"no-js ie6 oldie\" lang=\"en-US\"> <![endif]-->\n"
    "<!--[if gt IE 8]><!--> <html class=\"no-js\" lang=\"en-US\"> <!--<![endif]-->\n<head>\n"
    "<title>gateway.test | 520: Web server is returning an unknown error</title>\n"
    "<meta charset=\"UTF-8\" />\n<style>body{margin:0}</style>\n"
    "<script>(function(){window._cf_chl_opt={cRay:'8c1d'};})();</script>\n</head>\n"
    "<body><div id=\"cf-wrapper\"><h1>Web server is returning an unknown error</h1>" + "x" * 5000 + "</div></body></html>"
)


def fail_balance(make_client, session, status, body, headers=None):
    session.routes[("GET", "/balance")] = (status, body, headers or {})
    c = make_client()
    c._sleep = lambda s: None  # a 5xx GET is sent once more: no real pause
    with pytest.raises(crx.CrxError) as ei:
        c.balance()
    return ei.value


def test_nginx_html_names_status_and_host(make_client, session):
    e = fail_balance(make_client, session, 502, NGINX_502, {"content-type": "text/html"})
    assert type(e) is crx.ServerError and e.code == "server_error" and e.status == 502
    assert str(e) == "HTTP 502 from gateway.test"
    assert e.details["body"] == "502 Bad Gateway"
    assert e.gateway_code is None


def test_cloudflare_html_keeps_one_short_line(make_client, session):
    e = fail_balance(make_client, session, 520, CLOUDFLARE_520, {"content-type": "text/html"})
    assert type(e) is crx.ServerError and e.status == 520
    assert str(e) == "HTTP 520 from gateway.test"
    assert e.details["body"] == "gateway.test | 520: Web server is returning an unknown error"
    assert "<" not in str(e) and "<" not in e.details["body"]


def test_text_body_first_line_capped(make_client, session):
    e = fail_balance(make_client, session, 400, "\n  " + "a" * 1000 + "\nsecond line\n", {"content-type": "text/plain"})
    assert type(e) is crx.CrxError and e.code == "http_400" and e.status == 400
    assert str(e) == "HTTP 400 from gateway.test"
    assert e.details["body"] == "a" * 200


def test_text_body_strips_control_chars(make_client, session):
    e = fail_balance(make_client, session, 502, "bad\x1b[31m gateway\r\nmore", {})
    assert "\x1b" not in e.details["body"] and e.details["body"].startswith("bad")
    assert "more" not in e.details["body"]


def test_edge_429_reads_retry_after(make_client, session):
    e = fail_balance(make_client, session, 429, "Too Many Requests", {"Retry-After": "7", "content-type": "text/plain"})
    assert type(e) is crx.RateLimited and e.code == "rate_limited" and e.status == 429
    assert str(e) == "HTTP 429 from gateway.test"
    assert e.details == {"body": "Too Many Requests", "retry_after_secs": 7}


def test_json_429_header_fills_missing_retry_after(make_client, session):
    body = {"code": "rate_limited", "error": "slow down", "details": {"scope": "seat"}}
    e = fail_balance(make_client, session, 429, body, {"retry-after": "5"})
    assert type(e) is crx.RateLimited and str(e) == "slow down" and e.gateway_code == "rate_limited"
    assert e.details == {"scope": "seat", "retry_after_secs": 5}


@pytest.mark.parametrize("value,want", [
    ("0", 0), (" 12 ", 12), ("Wed, 21 Oct 2015 07:28:00 GMT", 0), ("soon", None), ("-3", None),
    ("\xb2", None), ("\xb9", None), ("\xb3", None), ("\u0663", None),
    ("99999999999999999999999", 86400), pytest.param("9" * 5000, 86400, id="5000-digits"), ("Fri, 31 Dec 9999 23:59:59 GMT", 86400),
])
def test_retry_after_forms(make_client, session, value, want):
    e = fail_balance(make_client, session, 503, "", {"Retry-After": value})
    assert e.details.get("retry_after_secs") == want


def test_retry_after_date_without_zone_is_utc(make_client, session, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 4070908800.0 - 60)  # 2099-01-01 00:00:00 UTC less 60 s
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        e = fail_balance(make_client, session, 503, "", {"Retry-After": "Thu, 01 Jan 2099 00:00:00 -0000"})
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
    assert e.details["retry_after_secs"] == 60


def test_bind_refusal_html_names_host():
    e = refused(Resp(502, NGINX_502, {"Retry-After": "3"}, url=BASE + "/rfqs/r1/accept?secret=1"))
    assert type(e) is crx.ServerError and str(e) == "HTTP 502 from gateway.test"
    assert e.details == {"body": "502 Bad Gateway", "retry_after_secs": 3}
