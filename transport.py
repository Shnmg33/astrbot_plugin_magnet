"""NapCat OneBot file delivery; file_id + URL lookup is required for auto-cleanup."""

import asyncio

import httpx

from .models import BTError


class QQSender:
    def __init__(self, settings, context):
        self.s, self.context = settings, context
        self.http = None
        if settings.napcat_http_url:
            self.http = httpx.AsyncClient(
                base_url=settings.napcat_http_url.rstrip("/") + "/",
                headers={"Authorization": "Bearer " + settings.napcat_token} if settings.napcat_token else {},
                timeout=httpx.Timeout(settings.upload_timeout, connect=10),
                trust_env=False,
                follow_redirects=False,
            )

    async def call(self, job, action, **params):
        if self.http:
            r = await self.http.post(action, json=params)
            r.raise_for_status()
            result = r.json()
            if result.get("status") != "ok" or result.get("retcode", 0) != 0:
                raise RuntimeError("OneBot API rejected request")
            return result.get("data") or {}
        platform = self.context.get_platform_inst(job.platform)
        if platform is None or platform.meta().name != "aiocqhttp":
            raise BTError("原 OneBot 平台未连接或已删除")
        result = await platform.get_client().call_action(action, **params)
        # aiocqhttp normally unwraps data; tolerate enveloped responses without assuming success.
        if isinstance(result, dict) and "retcode" in result:
            if result.get("retcode") != 0:
                raise RuntimeError("OneBot API rejected request")
            return result.get("data") or {}
        return result or {}

    async def notice(self, job, text):
        from astrbot.api.event import MessageChain

        await asyncio.wait_for(self.context.send_message(job.origin, MessageChain().message(text)), 20)

    async def upload(self, job, path, filename):
        # Check bot identity before any file send, especially when an optional HTTP endpoint is configured.
        try:
            login = await asyncio.wait_for(self.call(job, "get_login_info"), 15)
            if str(login.get("user_id", "")) != job.bot_id:
                return "failed", "NapCat 登录 QQ 与任务来源机器人不一致，未上传"
        except Exception:
            return "failed", "NapCat 连接或账号自检失败，未上传；修复后 /bt resend"
        params = {"file": path, "name": filename}
        if job.group:
            action = "upload_group_file"
            params["group_id"] = job.group
        else:
            action = "upload_private_file"
            params["user_id"] = job.user
        try:
            response = None
            # Only connect failures are provably pre-dispatch and safe for automatic retries.
            for attempt in range(3):
                try:
                    response = await asyncio.wait_for(self.call(job, action, **params), self.s.upload_timeout)
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    if attempt == 2:
                        return "failed", "无法连接 NapCat，文件保留待重传"
                    await asyncio.sleep(attempt + 1)
            receipt = str(response.get("file_id") or "") if isinstance(response, dict) else ""
            if not receipt:
                return "uncertain", "接口未返回 file_id；请在 QQ 核对文件后使用 /bt confirm 或 /bt resend"
            lookup = {"file_id": receipt}
            if job.group:
                lookup["group_id"] = job.group
            result = await asyncio.wait_for(
                self.call(job, "get_group_file_url" if job.group else "get_private_file_url", **lookup), 30
            )
            if not isinstance(result, dict) or not (result.get("url") or result.get("download_url")):
                return "uncertain", "已收到文件标识但未能确认可获取，源文件保留；请核对 QQ"
            return "sent", receipt
        except asyncio.CancelledError:
            raise
        except Exception:
            # A remote error/timeout may happen after QQ accepted the file; never auto replay.
            return "uncertain", "上传或确认超时/失败，QQ 可能已收到；请核对后确认或显式重传"

    async def close(self):
        if self.http:
            await self.http.aclose()
