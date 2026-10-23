"""Error bodies that are not gateway JSON, and the Retry-After header."""

import pytest

import crx

NGINX_502 = (
    "<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n<body>\r\n"
    "<center><h1>502 Bad Gateway</h1></center>\r\n<hr><center>nginx/1.24.0</center>\r\n</body>\r\n</html>\r\n"
)


def fail_balance(make_client, session, status, body, headers=None):
    session.routes[("GET", "/balance")] = (status, body, headers or {})
    c = make_client()
    c._sleep = lambda s: None  # skip the real pause before the one GET retry on a 5xx
    with pytest.raises(crx.CrxError) as ei:
        c.balance()
    return ei.value


@pytest.mark.parametrize("status,body,headers,cls,code,details", [
    (502, NGINX_502, {"content-type": "text/html"}, crx.ServerError, "server_error", {"body": "502 Bad Gateway"}),
    (400, "\n  " + "a" * 1000 + "\nsecond line\n", {"content-type": "text/plain"}, crx.CrxError, "http_400",
     {"body": "a" * 200}),
    (429, "Too Many Requests", {"Retry-After": "7", "content-type": "text/plain"}, crx.RateLimited, "rate_limited",
     {"body": "Too Many Requests", "retry_after_secs": 7}),
], ids=["nginx-502", "text-400", "edge-429"])
def test_a_body_that_is_not_gateway_json_names_status_and_host(
        make_client, session, status, body, headers, cls, code, details):
    e = fail_balance(make_client, session, status, body, headers)
    assert type(e) is cls and e.code == code and e.status == status
    assert str(e) == f"HTTP {status} from gateway.test"
    assert e.details == details and e.gateway_code is None
