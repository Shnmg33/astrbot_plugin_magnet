import asyncio
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

from .models import BTError


class QB:
    def __init__(self, settings):
        self.s = settings
        url = settings.qb_url.rstrip("/")
        if urlsplit(url).scheme not in ("http", "https"):
            raise BTError("qBittorrent URL 必须使用 HTTP(S)")
        self.http = httpx.AsyncClient(
            base_url=url + "/api/v2/",
            timeout=20,
            follow_redirects=False,
            headers={"Referer": url + "/"},
            trust_env=False,
        )
        self.auth_lock = asyncio.Lock()
        self.version = ""

    async def request(self, method, endpoint, **kwargs):
        try:
            r = await self.http.request(method, endpoint, **kwargs)
            if r.status_code == 403:
                async with self.auth_lock:
                    auth = await self.http.post(
                        "auth/login", data={"username": self.s.qb_username, "password": self.s.qb_password}
                    )
                    if auth.status_code != 200 or auth.text.strip() != "Ok.":
                        raise BTError("qBittorrent 登录失败，请检查账号密码及 WebUI 设置")
                r = await self.http.request(method, endpoint, **kwargs)
            r.raise_for_status()
            return r
        except httpx.HTTPError:
            raise BTError("qBittorrent 请求失败，请检查连接与后端状态") from None

    async def check(self):
        self.version = (await self.request("GET", "app/version")).text.strip().lstrip("v")
        api = (await self.request("GET", "app/webapiVersion")).text.strip()
        try:
            version = tuple(int(x) for x in api.split("."))
            major = int(self.version.split(".")[0])
        except ValueError:
            raise BTError("无法识别 qBittorrent 版本") from None
        if major != 5 or version < (2, 8, 15):
            raise BTError("第一版要求 qBittorrent 5.x（支持获取元数据后停止）")
        return self.version

    async def info(self, hash_value):
        rows = (await self.request("GET", "torrents/info", params={"hashes": hash_value})).json()
        return next((x for x in rows if x["hash"].lower() == hash_value), None)

    async def files(self, h):
        return (await self.request("GET", "torrents/files", params={"hash": h})).json()

    async def props(self, h):
        return (await self.request("GET", "torrents/properties", params={"hash": h})).json()

    async def add(self, job, tag):
        r = await self.request(
            "POST",
            "torrents/add",
            data={
                "urls": job.magnet,
                "savepath": str(PurePosixPath(self.s.qb_root) / job.id),
                "tags": tag,
                "autoTMM": "false",
                "useDownloadPath": "false",
                "stopped": "false",
                "stopCondition": "MetadataReceived",
                "contentLayout": "Original",
                "dlLimit": str(self.s.download_kib * 1024),
                "upLimit": str(self.s.upload_kib * 1024),
            },
        )
        if r.text.strip() != "Ok.":
            raise BTError("qBittorrent 未接受磁力任务")

    async def stop(self, h):
        await self.request("POST", "torrents/stop", data={"hashes": h})

    async def start(self, h):
        await self.request("POST", "torrents/start", data={"hashes": h})

    async def priorities(self, h, ids, priority):
        if ids:
            await self.request(
                "POST",
                "torrents/filePrio",
                data={"hash": h, "id": "|".join(map(str, ids)), "priority": str(priority)},
            )

    async def remove(self, h):
        # Local cleanup owns deletion. Never ask qBittorrent to recursively delete arbitrary paths.
        await self.request("POST", "torrents/delete", data={"hashes": h, "deleteFiles": "false"})

    async def close(self):
        await self.http.aclose()
