from urllib.parse import parse_qs

import httpx
import pytest
from astrbot_plugin_magnet.config import Settings
from astrbot_plugin_magnet.models import BTError, Job
from astrbot_plugin_magnet.qb import QB


async def test_auth_metadata_only_add_and_delete_without_files():
    calls = []
    authorized = False

    async def handler(request):
        nonlocal authorized
        data = parse_qs(request.content.decode()) if request.content else {}
        calls.append((request.url.path, data))
        if request.url.path.endswith("/auth/login"):
            authorized = True
            return httpx.Response(200, text="Ok.")
        if not authorized:
            return httpx.Response(403)
        if request.url.path.endswith("/app/version"):
            return httpx.Response(200, text="v5.1.4")
        if request.url.path.endswith("/app/webapiVersion"):
            return httpx.Response(200, text="2.11.0")
        return httpx.Response(200, text="Ok.")

    q = QB(Settings())
    await q.http.aclose()
    q.http = httpx.AsyncClient(base_url="http://qb/api/v2/", transport=httpx.MockTransport(handler))
    assert await q.check() == "5.1.4"
    j = Job("T12345678", "a" * 40, "magnet:?xt=urn:btih:" + "a" * 40, "1", "o", "p", "", "2")
    await q.add(j, "tag")
    await q.remove(j.hash)
    payload = next(d for p, d in calls if p.endswith("/torrents/add"))
    assert payload["stopCondition"] == ["MetadataReceived"]
    assert payload["autoTMM"] == ["false"] and payload["stopped"] == ["false"]
    assert payload["useDownloadPath"] == ["false"]
    assert payload["savepath"] == ["/bt-downloads/T12345678"]
    assert calls[-1][1]["deleteFiles"] == ["false"]
    await q.close()


async def test_unsupported_major_rejected():
    async def handler(request):
        return httpx.Response(200, text="v4.6.0" if request.url.path.endswith("/version") else "2.9.0")

    q = QB(Settings())
    await q.http.aclose()
    q.http = httpx.AsyncClient(base_url="http://qb/", transport=httpx.MockTransport(handler))
    with pytest.raises(BTError, match="5.x"):
        await q.check()
    await q.close()
