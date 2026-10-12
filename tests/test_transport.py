import json

import httpx
import pytest
from astrbot_plugin_magnet.config import Settings
from astrbot_plugin_magnet.models import Job
from astrbot_plugin_magnet.transport import QQSender


def job(group="202"):
    return Job("T12345678", "a" * 40, "magnet:", "101", "origin", "platform", group, "303")


@pytest.mark.parametrize(
    "group,action,lookup",
    [("202", "upload_group_file", "get_group_file_url"), ("", "upload_private_file", "get_private_file_url")],
)
async def test_receipt_and_url_required(group, action, lookup):
    calls = []

    async def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        endpoint = request.url.path.strip("/")
        data = (
            {"user_id": 303}
            if endpoint == "get_login_info"
            else {"file_id": "receipt"}
            if endpoint == action
            else {"url": "https://example.com/file"}
        )
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})

    sender = QQSender(Settings(), None)
    sender.http = httpx.AsyncClient(base_url="http://nap/", transport=httpx.MockTransport(handler))
    assert await sender.upload(job(group), "/files/a", "a") == ("sent", "receipt")
    assert [c[0] for c in calls] == ["/get_login_info", "/" + action, "/" + lookup]
    assert calls[1][1].get("group_id" if group else "user_id") == (group or "101")
    await sender.close()


@pytest.mark.parametrize("failure", ["null_id", "lookup_failure", "timeout", "remote_error"])
async def test_ambiguous_results_do_not_replay(failure):
    uploads = []

    async def handler(request):
        if request.url.path == "/get_login_info":
            return httpx.Response(200, json={"status": "ok", "data": {"user_id": 303}})
        if request.url.path == "/upload_group_file":
            uploads.append(1)
            if failure == "timeout":
                raise httpx.ReadTimeout("timeout")
            if failure == "remote_error":
                return httpx.Response(200, json={"status": "failed", "retcode": 1200})
            return httpx.Response(
                200, json={"status": "ok", "data": {"file_id": None if failure == "null_id" else "id"}}
            )
        return httpx.Response(200, json={"status": "ok", "data": {}})

    sender = QQSender(Settings(), None)
    sender.http = httpx.AsyncClient(base_url="http://nap/", transport=httpx.MockTransport(handler))
    outcome, _ = await sender.upload(job(), "/files/a", "a")
    assert outcome == "uncertain" and len(uploads) == 1
    await sender.close()


async def test_wrong_bot_identity_never_uploads():
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "ok", "data": {"user_id": 999}})

    sender = QQSender(Settings(), None)
    sender.http = httpx.AsyncClient(base_url="http://nap/", transport=httpx.MockTransport(handler))
    assert (await sender.upload(job(), "/files/a", "a"))[0] == "failed"
    assert calls == ["/get_login_info"]
    await sender.close()
