from __future__ import annotations

import httpx

from app.opencode.health import check_opencode_health


def transport(handler):
    return httpx.MockTransport(handler)


async def test_healthy_with_version():
    def h(request):
        return httpx.Response(200, json={"healthy": True, "version": "1.2.3"})

    r = await check_opencode_health("http://x:1", timeout_s=1, transport=transport(h))
    assert r.healthy and r.version == "1.2.3"


async def test_unhealthy_flag():
    def h(request):
        return httpx.Response(200, json={"healthy": False})

    r = await check_opencode_health("http://x:1", timeout_s=1, transport=transport(h))
    assert not r.healthy and r.detail == "unhealthy"


async def test_http_500():
    def h(request):
        return httpx.Response(500)

    r = await check_opencode_health("http://x:1", timeout_s=1, transport=transport(h))
    assert not r.healthy and r.detail == "HTTP 500"


async def test_non_json_and_non_dict():
    r = await check_opencode_health(
        "http://x:1", timeout_s=1, transport=transport(lambda req: httpx.Response(200, text="hi"))
    )
    assert not r.healthy and r.detail == "malformed response"
    r = await check_opencode_health(
        "http://x:1", timeout_s=1, transport=transport(lambda req: httpx.Response(200, json=[1]))
    )
    assert not r.healthy and r.detail == "malformed response"


async def test_connect_error():
    def h(request):
        raise httpx.ConnectError("nope")

    r = await check_opencode_health("http://x:1", timeout_s=1, transport=transport(h))
    assert not r.healthy and r.detail == "ConnectError"


async def test_path_exact_with_trailing_slash():
    seen = []

    def h(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"healthy": True})

    r = await check_opencode_health("http://x:1/", timeout_s=1, transport=transport(h))
    assert r.healthy and r.version is None
    assert seen == ["/global/health"]
