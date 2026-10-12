import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_magnet.config import Settings
from astrbot_plugin_magnet.manager import Manager
from astrbot_plugin_magnet.models import Actor

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


class FakeQB:
    def __init__(self, settings):
        self.s = settings
        self.rows = {}
        self.contents = {}
        self.calls = []

    async def check(self):
        return "5.1.4"

    async def info(self, h):
        return self.rows.get(h)

    async def files(self, h):
        return self.contents.get(h, [])

    async def props(self, h):
        return {"piece_size": 16384}

    async def add(self, j, tag):
        self.calls.append(("add", j.hash))
        self.rows[j.hash] = {
            "hash": j.hash,
            "save_path": self.s.qb_root + "/" + j.id,
            "tags": tag,
            "state": "metaDL",
            "name": "测试资源",
            "downloaded": 0,
        }

    async def stop(self, h):
        self.calls.append(("stop", h))
        if h in self.rows:
            self.rows[h]["state"] = "stoppedDL"

    async def start(self, h):
        self.calls.append(("start", h))
        self.rows[h]["state"] = "downloading"

    async def priorities(self, h, ids, priority):
        self.calls.append(("priority", list(ids), priority))
        for f in self.contents.get(h, []):
            if f["index"] in ids:
                f["priority"] = priority

    async def remove(self, h):
        self.calls.append(("remove", h))
        self.rows.pop(h, None)

    async def close(self):
        pass


class FakeSender:
    def __init__(self):
        self.notices = []
        self.sent = []
        self.result = ("sent", "file-123")
        self.wait = None

    async def notice(self, j, text):
        self.notices.append(text)

    async def upload(self, j, path, filename):
        self.sent.append((j.id, path, filename))
        if self.wait:
            await self.wait.wait()
        return self.result

    async def close(self):
        pass


@pytest.fixture
async def env(tmp_path):
    s = Settings(
        local_root=str(tmp_path / "downloads"),
        qb_root="/qb",
        napcat_root="/nap",
        min_free_mib=0,
        overhead_mib=1,
        cleanup_delay=0,
        admin_only=False,
    )
    qb = FakeQB(s)
    sender = FakeSender()
    m = Manager(s, tmp_path / "state", qb, sender)
    m.ready = True
    actor = Actor("101", "platform:group:202", "platform", "202", "303")
    yield m, qb, sender, actor
    await m.close()


async def parsed(env, files=None, h="a"):
    m, qb, _, actor = env
    await m.submit(actor, "magnet:?xt=urn:btih:" + h * 40)
    j = next(j for j in m.jobs.values() if j.hash == h * 40)
    await m.tick()
    qb.contents[j.hash] = files or [
        {"index": 0, "name": "folder/讲义.pdf", "size": 100, "progress": 0},
        {"index": 1, "name": "folder/video.mp4", "size": 200, "progress": 0},
        {"index": 2, "name": "folder/练习.zip", "size": 50, "progress": 0},
    ]
    qb.rows[j.hash]["state"] = "stoppedDL"
    await m.tick()
    return j


async def complete(env, selected="1,3"):
    m, qb, _, actor = env
    j = await parsed(env)
    await m.command(actor, "download", [j.id, selected])
    await m.tick()
    for i in j.selected:
        f = qb.contents[j.hash][i]
        f["progress"] = 1
        p = m.folder(j) / f["name"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * f["size"])
    await m.tick()
    return j
