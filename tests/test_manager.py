import asyncio
import time

import pytest
from astrbot_plugin_magnet.config import MIB
from astrbot_plugin_magnet.models import Actor, BTError, Store
from conftest import MAGNET, complete, parsed


async def test_full_select_download_send_cleanup(env):
    m, qb, sender, actor = env
    j = await complete(env)
    assert j.selected == [0, 2] and j.size == 150
    assert ("priority", [0, 1, 2], 0) in qb.calls
    assert ("priority", [0, 2], 1) in qb.calls
    assert j.state == "uploading" and not sender.sent
    await m.tick()
    await asyncio.gather(*list(m.uploads.values()))
    assert j.state == "cleaning" and len(sender.sent) == 2
    assert all("/nap/" in item[1] for item in sender.sent)
    assert m.folder(j).exists()
    await m.tick()  # Remove backend handle first.
    assert j.hash not in qb.rows and m.folder(j).exists()
    await m.tick()
    assert j.state == "done" and not m.folder(j).exists()
    assert len(j.sent) == 2


async def test_no_download_before_selection(env):
    m, qb, _, _ = env
    j = await parsed(env)
    await m.tick()
    assert j.state == "selecting"
    assert not any(c[0] == "start" for c in qb.calls)


async def test_oversize_selection_can_choose_smaller(env):
    m, qb, _, actor = env
    j = await parsed(
        env,
        [
            {"index": 0, "name": "huge", "size": 2000 * MIB, "progress": 0},
            {"index": 1, "name": "small", "size": 100, "progress": 0},
        ],
    )
    with pytest.raises(BTError, match="超过限制"):
        await m.command(actor, "download", [j.id, "all"])
    assert j.state == "selecting"
    await m.command(actor, "download", [j.id, "2"])
    assert j.selected == [1]


async def test_single_file_return_limit(env):
    m, _, _, actor = env
    j = await parsed(env, [{"index": 0, "name": "video", "size": 600 * MIB, "progress": 0}])
    with pytest.raises(BTError, match="回传"):
        await m.command(actor, "download", [j.id, "all"])
    assert j.state == "selecting"


async def test_strict_total_size(env):
    m, _, _, _ = env
    m.s.max_torrent_mib = 1
    j = await parsed(env, [{"index": 0, "name": "big", "size": 2 * MIB, "progress": 0}])
    assert j.state == "cleaning"


async def test_metadata_backend_must_stop(env):
    m, qb, _, actor = env
    await m.submit(actor, MAGNET)
    j = next(iter(m.jobs.values()))
    await m.tick()
    qb.rows[j.hash]["state"] = "downloading"
    qb.contents[j.hash] = [{"index": 0, "name": "a", "size": 100, "progress": 0}]
    await m.tick()
    assert j.state == "cleaning" and "未在元数据" in j.error
    assert qb.rows[j.hash]["state"] == "stoppedDL"


async def test_metadata_payload_rejected(env):
    m, qb, _, actor = env
    await m.submit(actor, MAGNET)
    j = next(iter(m.jobs.values()))
    await m.tick()
    qb.rows[j.hash].update(state="stoppedDL", downloaded=10)
    qb.contents[j.hash] = [{"index": 0, "name": "a", "size": 100, "progress": 0}]
    await m.tick()
    assert j.state == "cleaning" and "正文" in j.error


async def test_unsafe_metadata_is_cleaned(env):
    j = await parsed(env, [{"index": 0, "name": "../escape", "size": 10, "progress": 0}])
    assert j.state == "cleaning"


async def test_timeout_cleanup(env):
    m, _, _, _ = env
    j = await parsed(env)
    j.changed = time.time() - m.s.selection_timeout - 1
    await m.tick()
    assert j.state == "cleaning"
    await m.tick()
    await m.tick()
    assert j.state == "expired"


async def test_permission_and_implicit_scope(env):
    m, _, _, actor = env
    j = await parsed(env)
    intruder = Actor("999", actor.origin, actor.platform)
    with pytest.raises(BTError):
        await m.command(intruder, "cancel", [j.id])
    other_chat = Actor(actor.user, "another", actor.platform)
    with pytest.raises(BTError):
        await m.command(other_chat, "status", [j.id])
    assert j.state == "selecting"
    assert j.id in await m.command(actor, "status", [])


async def test_ambiguous_implicit_id(env):
    m, _, _, actor = env
    await parsed(env)
    await parsed(env, h="b")
    with pytest.raises(BTError, match="明确指定"):
        await m.command(actor, "download", ["1"])


async def test_duplicate_submissions_atomic(env):
    m, qb, _, actor = env
    result = await asyncio.gather(m.submit(actor, MAGNET), m.submit(actor, MAGNET), return_exceptions=True)
    assert sum(isinstance(x, BTError) for x in result) == 1
    assert len(m.jobs) == 1


async def test_queue_user_limit(env):
    m, qb, _, actor = env
    a = await parsed(env)
    b = await parsed(env, h="b")
    await m.command(actor, "download", [a.id, "1"])
    await m.command(actor, "download", [b.id, "1"])
    await m.tick()
    assert [a.state, b.state] == ["downloading", "queued"]
    await m.command(actor, "pause", [a.id])
    await m.tick()
    assert b.state == "downloading"


async def test_disk_budget_rejects_reservation(env):
    m, _, _, actor = env
    j = await parsed(env)
    m.s.disk_budget_mib = 1
    with pytest.raises(BTError, match="预算"):
        await m.command(actor, "download", [j.id, "all"])
    assert j.state == "selecting"


async def test_uncertain_upload_never_auto_replayed_or_deleted(env):
    m, _, sender, actor = env
    j = await complete(env, "1")
    sender.result = ("uncertain", "请求超时")
    await m.tick()
    await asyncio.gather(*list(m.uploads.values()))
    await m.tick()
    assert j.state == "uncertain" and m.folder(j).exists()
    assert len(sender.sent) == 1
    assert j.uncertain == [0]
    with pytest.raises(BTError, match="上一上传"):
        await m.command(actor, "resend", [j.id])
    await m.command(actor, "confirm", [j.id, "1"])
    await m.tick()
    assert j.state == "cleaning" and m.folder(j).exists()  # quarantine still applies
    j.not_before = 0
    await m.tick()
    await m.tick()
    assert j.state == "done"


async def test_cancel_during_upload_rejected(env):
    m, _, sender, actor = env
    j = await complete(env, "1")
    sender.wait = asyncio.Event()
    await m.tick()
    await asyncio.sleep(0)
    with pytest.raises(BTError, match="正在上传"):
        await m.command(actor, "cancel", [j.id])
    assert m.folder(j).exists()
    sender.wait.set()
    await asyncio.gather(*list(m.uploads.values()))


async def test_cleanup_refuses_foreign_backend(env):
    m, qb, _, actor = env
    j = await parsed(env)
    await m.command(actor, "cancel", [j.id])
    qb.rows[j.hash]["tags"] = "somebody-else"
    await m.tick()
    assert j.hash in qb.rows and m.folder(j).exists()
    assert "归属" in j.error


async def test_cleanup_refuses_marker_change(env):
    m, qb, _, actor = env
    j = await parsed(env)
    await m.command(actor, "cancel", [j.id])
    await m.tick()
    (m.folder(j) / ".astrbt-owner").write_text("other")
    await m.tick()
    assert m.folder(j).exists() and j.state == "cleaning"
    assert "所有权" in j.error


async def test_persistent_inflight_intent(env):
    m, _, sender, _ = env
    j = await complete(env, "1")
    sender.wait = asyncio.Event()
    await m.tick()
    await asyncio.sleep(0)
    other = Store(m.data / "tasks.sqlite3")
    saved = other.load()[j.id]
    other.close()
    assert saved.state == "uploading" and saved.current == 0 and not saved.sent
    sender.wait.set()
    await asyncio.gather(*list(m.uploads.values()))


async def test_filename_changed_after_selection_stops(env):
    m, qb, _, actor = env
    j = await parsed(env)
    await m.command(actor, "download", [j.id, "1"])
    await m.tick()
    qb.contents[j.hash][0]["name"] = "newname"
    await m.tick()
    assert j.state == "cleaning"


async def test_new_job_does_not_take_external_task(env):
    m, qb, _, actor = env
    await m.submit(actor, MAGNET)
    j = next(iter(m.jobs.values()))
    qb.rows[j.hash] = {"hash": j.hash, "save_path": "/other", "tags": "external"}
    await m.tick()
    assert j.state == "cancelled" and j.hash in qb.rows
    assert not m.folder(j).exists()


async def test_same_process_duplicate_instance_refused(env):
    from astrbot_plugin_magnet.manager import Manager

    m, qb, sender, _ = env
    with pytest.raises(BTError, match="已有插件实例"):
        Manager(m.s, m.data, qb, sender)


async def test_restart_inflight_does_not_repeat(env):
    from astrbot_plugin_magnet.manager import Manager

    m, qb, sender, actor = env
    j = await complete(env, "1")
    j.current = 0
    m.save(j)
    m.store.close()
    m.instance_lock.close()
    # Release original fixture resources and use the reopened manager for finalization.
    reopened = Manager(m.s, m.data, qb, sender)
    m.store = reopened.store
    m.instance_lock = reopened.instance_lock
    try:
        await reopened.start()
        saved = reopened.jobs[j.id]
        assert saved.state == "uncertain" and saved.uncertain == [0]
        assert saved.not_before > time.time()
        assert not sender.sent
    finally:
        reopened.runner.cancel()
        await asyncio.gather(reopened.runner, return_exceptions=True)


async def test_metadata_add_response_lost_reconciles(env):
    m, qb, _, actor = env
    original = qb.add

    async def lost(job, tag):
        await original(job, tag)
        raise BTError("response lost")

    qb.add = lost
    await m.submit(actor, MAGNET)
    j = next(iter(m.jobs.values()))
    await m.tick()
    assert j.state == "adding"
    qb.contents[j.hash] = [{"index": 0, "name": "file", "size": 5, "progress": 0}]
    qb.rows[j.hash]["state"] = "stoppedDL"
    await m.tick()
    assert j.state == "selecting"
    assert len([x for x in qb.calls if x[0] == "add"]) == 1


async def test_partial_send_retry_only_unsent(env):
    m, _, sender, actor = env
    j = await complete(env, "1,3")
    original = sender.upload
    n = 0

    async def partial(job, path, name):
        nonlocal n
        n += 1
        if n == 2:
            sender.result = ("failed", "未连接")
        return await original(job, path, name)

    sender.upload = partial
    await m.tick()
    await asyncio.gather(*list(m.uploads.values()))
    assert j.state == "retry" and set(j.sent) == {"0"}
    sender.upload = original
    sender.result = ("sent", "receipt2")
    await m.command(actor, "resend", [j.id])
    await m.tick()
    await asyncio.gather(*list(m.uploads.values()))
    assert j.state == "cleaning" and len(sender.sent) == 3
    assert sender.sent[-1][1].endswith("练习.zip")


async def test_download_timeout_stops_and_cleans(env):
    m, qb, _, actor = env
    j = await parsed(env)
    await m.command(actor, "download", [j.id, "1"])
    await m.tick()
    j.last_progress = time.time() - m.s.stall_timeout - 1
    await m.tick()
    assert j.state == "cleaning" and qb.rows[j.hash]["state"] == "stoppedDL"
