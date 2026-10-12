"""Durable task state machine. Commands and scheduling share one admission lock.
Uploads run separately and may never be cleaned while their worker is alive.
"""

import asyncio
import fcntl
import logging
import os
import secrets
import shutil
import time
from pathlib import Path, PurePosixPath

from .config import MIB
from .models import (
    LABELS,
    TERMINAL,
    BTError,
    Job,
    Store,
    clean_text,
    human,
    normalize_magnet,
    safe_file,
    safe_rel,
    select_indices,
)

log = logging.getLogger(__name__)


class Manager:
    def __init__(self, settings, data_dir, qb, sender):
        self.s, self.qb, self.sender = settings, qb, sender
        self.data = Path(data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.instance_lock = (self.data / "instance.lock").open("a+")
        try:
            fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.instance_lock.close()
            raise BTError("同一数据目录已有插件实例运行，请先停止旧实例") from None
        self.root = Path(settings.local_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        token_file = self.data / "instance_id"
        if not token_file.exists():
            token_file.write_text(secrets.token_hex(16))
        self.token = token_file.read_text().strip()
        self.store = Store(self.data / "tasks.sqlite3")
        self.jobs = self.store.load()
        self.lock = asyncio.Lock()
        self.uploads = {}
        self.runner = None
        self.health = "尚未自检"
        self.ready = False

    def tag(self, j):
        return f"astrbt_{self.token}_{j.id}"

    def folder(self, j):
        if not j.id.startswith("T") or len(j.id) != 9 or any(c not in "0123456789ABCDEF" for c in j.id[1:]):
            raise BTError("任务目录标识无效")
        p = self.root / j.id
        if p.is_symlink() or p.resolve().parent != self.root:
            raise BTError("任务目录异常，拒绝操作")
        return p

    def owned_folder(self, j):
        p = self.folder(j)
        marker = p / ".astrbt-owner"
        if marker.is_symlink() or not marker.is_file() or marker.read_text() != self.token:
            raise BTError("任务目录所有权校验失败，拒绝删除或上传")
        return p

    def owns(self, j, info):
        expected = str(PurePosixPath(self.s.qb_root) / j.id)
        return (
            self.tag(j) in [x.strip() for x in info.get("tags", "").split(",")]
            and info.get("save_path", "").rstrip("/") == expected
        )

    async def owned_info(self, j):
        info = await self.qb.info(j.hash)
        if info is not None and not self.owns(j, info):
            raise BTError("后端任务归属或路径改变，已停止插件操作；请管理员检查")
        return info

    def save(self, j):
        self.store.save(j)

    async def notice(self, j, text):
        try:
            await self.sender.notice(j, text)
        except Exception:
            log.warning("BT notice failed for %s", j.id)

    def active(self):
        return [j for j in self.jobs.values() if j.state not in TERMINAL]

    def authorize(self, actor):
        if self.s.admin_only and not actor.admin:
            raise BTError("插件当前仅允许 AstrBot 管理员使用")
        if not actor.admin:
            if self.s.allowed_users and actor.user not in self.s.allowed_users:
                raise BTError("你不在使用白名单中")
            if actor.group and self.s.allowed_groups and actor.group not in self.s.allowed_groups:
                raise BTError("本群未启用 BT 下载")

    def resolve(self, actor, job_id=None, states=None):
        if job_id:
            j = self.jobs.get(job_id.upper())
            if not j or (not actor.admin and (j.user != actor.user or j.origin != actor.origin)):
                raise BTError("任务不存在或无权访问，请使用 /bt list")
            if states and j.state not in states:
                raise BTError(f"当前状态为 {LABELS[j.state]}，不能进行此操作")
            return j
        candidates = [
            j
            for j in self.active()
            if j.user == actor.user and j.origin == actor.origin and (not states or j.state in states)
        ]
        if len(candidates) != 1:
            ids = "、".join(j.id for j in candidates[:10])
            raise BTError("请明确指定任务号" + (f"：{ids}" if ids else "，使用 /bt list 查看"))
        return candidates[0]

    def _usage(self):
        total = 0
        for j in self.active():
            p = self.folder(j)
            if not p.exists():
                continue
            for base, dirs, files in os.walk(p, followlinks=False):
                dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
                for name in files:
                    q = Path(base) / name
                    try:
                        stat = q.lstat()
                        if not q.is_symlink():
                            total += max(stat.st_size, getattr(stat, "st_blocks", 0) * 512)
                    except FileNotFoundError:
                        pass
        return total

    async def room(self, new_reserve=0, replace=None):
        used = await asyncio.to_thread(self._usage)
        reserved = sum(j.reserve for j in self.active() if j is not replace) + new_reserve
        if max(reserved, used) > self.s.disk_budget_mib * MIB:
            raise BTError("临时空间预算不足，请等待其他任务清理")
        free = shutil.disk_usage(self.root).free
        if free - max(0, reserved - used) < self.s.min_free_mib * MIB:
            raise BTError("磁盘可用空间不足，已阻止下载")

    async def submit(self, actor, magnet):
        self.authorize(actor)
        h, clean = normalize_magnet(magnet, self.s.trusted_trackers)
        async with self.lock:
            if not self.ready:
                raise BTError("后端尚未就绪：" + self.health)
            active = self.active()
            if len(active) >= self.s.max_pending:
                raise BTError("任务队列已满")
            if (
                sum(j.user == actor.user and j.platform == actor.platform for j in active)
                >= self.s.max_user_pending
            ):
                raise BTError("你的待处理任务已达上限")
            if any(j.hash == h for j in active) or await self.qb.info(h):
                raise BTError("此资源已存在任务；第一版不共享同一资源，请稍后重试")
            reserve = self.s.overhead_mib * MIB
            await self.room(reserve)
            while True:
                jid = "T" + secrets.token_hex(4).upper()
                if jid not in self.jobs and not (self.root / jid).exists():
                    break
            j = Job(
                id=jid,
                hash=h,
                magnet=clean,
                user=actor.user,
                origin=actor.origin,
                platform=actor.platform,
                group=actor.group,
                bot_id=actor.bot_id,
                reserve=reserve,
            )
            p = self.folder(j)
            p.mkdir(mode=0o770)
            p.chmod(0o2770)  # Keep the deployment's shared group writable across containers.
            (p / ".astrbt-owner").write_text(self.token)
            self.jobs[j.id] = j
            self.save(j)
            return f"任务 {j.id} 已创建，正在等待解析。完成后将返回文件列表；/bt status {j.id} 查询。"

    def listing(self, actor):
        jobs = [j for j in self.jobs.values() if j.user == actor.user and j.origin == actor.origin]
        jobs.sort(key=lambda j: j.created, reverse=True)
        return (
            "\n".join(f"{j.id}｜{LABELS[j.state]}｜{clean_text(j.name or '待解析', 50)}" for j in jobs[:20])
            or "暂无任务"
        )

    def files_text(self, j, page=1, query=""):
        files = [(i, f) for i, f in enumerate(j.files) if query.lower() in f["name"].lower()]
        pages = max(1, (len(files) + self.s.page_size - 1) // self.s.page_size)
        if not 1 <= page <= pages:
            raise BTError(f"页码范围为 1—{pages}")
        text = [
            f"{j.id}｜{clean_text(j.name)}",
            f"共 {len(j.files)} 个文件，资源总大小 {human(sum(f['size'] for f in j.files))}",
        ]
        for i, f in files[(page - 1) * self.s.page_size : page * self.s.page_size]:
            text.append(f"{i + 1}. {clean_text(f['name'])}｜{human(f['size'])}")
        text += [
            f"第 {page}/{pages} 页；/bt files {j.id} 页码",
            f"/bt download {j.id} 1,3 或 all",
            f"所选上限 {self.s.max_task_mib} MiB，单文件上限 {self.s.max_file_mib} MiB",
        ]
        return "\n".join(text)

    def status(self, j):
        pct = min(100, j.downloaded * 100 / j.size) if j.size else 0
        text = [f"{j.id}｜{LABELS[j.state]}｜{clean_text(j.name)}"]
        if j.selected:
            text.append(
                f"所选文件：{len(j.selected)} 个；{human(j.downloaded)} / {human(j.size)}（{pct:.1f}%）"
            )
        if j.state == "downloading":
            text += [
                f"速度 {human(j.speed)}/s；节点 {j.peers}",
                f"预计剩余 {j.eta} 秒" if 0 < j.eta < 8640000 else "预计剩余：未知",
            ]
        if j.state in {"uploading", "retry", "uncertain"}:
            text.append(
                f"已确认上传 {len(j.sent)}/{len(j.selected)} 个"
                + (f"；正在处理文件 {j.current + 1}" if j.current >= 0 else "")
            )
        if j.uncertain:
            text.append("待确认文件编号：" + ",".join(str(i + 1) for i in j.uncertain))
            text.append(f"核对 QQ 后 /bt confirm {j.id} 编号；或 /bt resend {j.id}（可能重复发送）")
        if j.error:
            text.append(j.error)
        return "\n".join(text)

    async def choose(self, j, expr):
        selected = select_indices(expr, len(j.files), self.s.max_files)
        size = sum(j.files[i]["size"] for i in selected)
        if size > self.s.max_task_mib * MIB:
            raise BTError("所选大小超过限制，未开始下载，请重新选择")
        if any(j.files[i]["size"] > self.s.max_file_mib * MIB for i in selected):
            raise BTError("选中内容含超过 QQ 回传配置上限的文件，请重新选择")
        info = await self.owned_info(j)
        if not info:
            raise BTError("后端任务已不存在，请取消后重新提交")
        props = await self.qb.props(j.hash)
        piece = int(props.get("piece_size", 0))
        if piece <= 0:
            raise BTError("后端未返回有效分片大小，暂不能安全预算空间，请稍后重试")
        reserve = 2 * size + 2 * len(selected) * piece + self.s.overhead_mib * MIB
        await self.room(reserve, j)
        await self.qb.stop(j.hash)
        await self.qb.priorities(j.hash, [f["index"] for f in j.files], 0)
        await self.qb.priorities(j.hash, [j.files[i]["index"] for i in selected], 1)
        j.selected, j.size, j.reserve = selected, size, reserve
        j.transition("queued")
        self.save(j)
        return f"{j.id} 已选择 {len(selected)} 个文件，共 {human(size)}，进入下载队列。"

    async def command(self, actor, command, args):
        self.authorize(actor)
        async with self.lock:
            if command == "list":
                return self.listing(actor)
            if command == "admin":
                if not actor.admin:
                    raise BTError("此命令仅限 AstrBot 管理员")
                free = shutil.disk_usage(self.root).free
                active = self.active()
                return f"后端：{self.health}\n磁盘剩余 {human(free)}\n" + (
                    "\n".join(f"{j.id} {LABELS[j.state]} 用户 {j.user}" for j in active) or "无活动任务"
                )
            states = {
                "download": {"selecting"},
                "pause": {"downloading", "queued"},
                "resume": {"paused"},
                "resend": {"retry", "uncertain"},
                "confirm": {"uncertain"},
            }.get(command)
            job_id = args.pop(0) if args and args[0].upper().startswith("T") else None
            j = self.resolve(actor, job_id, states)
            if command == "status":
                if args:
                    raise BTError("用法：/bt status [任务号]")
                return self.status(j)
            if command == "files":
                if not j.files:
                    raise BTError("尚未取得文件列表")
                page = int(args.pop(0)) if args and args[0].isdigit() else 1
                return self.files_text(j, page, " ".join(args))
            if command == "download":
                if not args:
                    raise BTError("请指定文件编号或 all，例如 /bt download 1,3")
                return await self.choose(j, "".join(args))
            if command == "pause":
                if await self.owned_info(j):
                    await self.qb.stop(j.hash)
                j.transition("paused")
            elif command == "resume":
                await self.room(j.reserve, j)
                j.transition("queued")
            elif command == "cancel":
                if j.id in self.uploads:
                    raise BTError("文件正在上传，请等待本次上传结束后再取消，避免上传时删除源文件")
                if j.state in TERMINAL:
                    return self.status(j)
                # Uncertain uploads may still be in use remotely. Defer deleting their local source.
                j.not_before = max(j.not_before, time.time() + self.s.upload_timeout) if j.uncertain else 0
                j.finish_as = "cancelled"
                j.transition("cleaning", "正在终止任务并清理" + ("；等待潜在上传结束" if j.uncertain else ""))
                if await self.owned_info(j):
                    await self.qb.stop(j.hash)
            elif command == "resend":
                if j.not_before > time.time():
                    raise BTError("上一上传可能仍在处理，请稍后重传；等待结束前不会删除文件")
                j.uncertain = []
                j.transition("uploading")
            elif command == "confirm":
                confirmed = select_indices("".join(args), len(j.files), self.s.max_files)
                if any(i not in j.uncertain for i in confirmed):
                    raise BTError("只能确认 status 中列出的待确认文件；请逐项填写编号")
                for i in confirmed:
                    j.sent[str(i)] = "user-confirmed"
                    j.uncertain.remove(i)
                if len(j.sent) == len(j.selected):
                    j.finish_as = "done"
                    j.transition("cleaning")
                    j.not_before = max(j.not_before, time.time() + self.s.cleanup_delay)
                elif not j.uncertain:
                    j.transition("retry", "其余文件尚未发送，可使用 /bt resend")
            else:
                raise BTError("未知子命令，请使用 /bt help")
            self.save(j)
            return self.status(j)

    async def start(self):
        # Reconcile before permitting new commands. A cancelled upload is never auto-replayed.
        for j in self.active():
            if j.state == "uploading":
                if j.current >= 0 and str(j.current) not in j.sent:
                    j.uncertain = sorted(set(j.uncertain + [j.current]))
                j.current = -1
                j.transition(
                    "uncertain" if j.uncertain else "retry",
                    "上次进程在上传阶段退出，请核对 QQ 文件后确认或重传",
                )
                j.not_before = time.time() + self.s.upload_timeout
                self.save(j)
        await self.check_backend()
        if self.ready:
            await self.reconcile_downloads()
        self.runner = asyncio.create_task(self.run(), name="astrbt-scheduler")

    async def reconcile_downloads(self):
        for j in self.active():
            if j.state == "downloading":
                if await self.owned_info(j):
                    await self.qb.stop(j.hash)
                j.transition("queued", "重启后重新进入队列")
                self.save(j)

    async def check_backend(self):
        try:
            version = await self.qb.check()
            self.health, self.ready = f"qBittorrent {version} 已连接", True
        except BTError as e:
            self.health, self.ready = str(e), False

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("BT scheduler error")
            await asyncio.sleep(self.s.poll_seconds)

    async def fail(self, j, reason, cleanup=False):
        info = await self.owned_info(j)
        if info:
            await self.qb.stop(j.hash)
        j.finish_as = "expired"
        j.transition("cleaning" if cleanup else "failed", reason)
        self.save(j)
        await self.notice(j, f"{j.id}：{reason}")

    async def tick(self):
        async with self.lock:
            if not self.ready:
                await self.check_backend()
                if not self.ready:
                    return
                await self.reconcile_downloads()
            now = time.time()
            for j in list(self.jobs.values()):
                if j.state in TERMINAL:
                    if now - j.changed > self.s.history_days * 86400:
                        self.store.delete(j.id)
                        del self.jobs[j.id]
                    continue
                if j.id in self.uploads:
                    continue
                try:
                    await self.advance(j, now)
                except BTError as e:
                    j.error = str(e)
                    self.health = str(e)
                    self.save(j)
                    if now - j.last_notice > 300:
                        j.last_notice = now
                        self.save(j)
                        await self.notice(j, f"{j.id} 暂停推进：{e}")
                except Exception:
                    log.exception("BT task %s failed", j.id)
                    j.error = "内部异常，请管理员查看日志；源文件暂时保留"
                    self.save(j)
            # Backpressure also covers in-flight metadata jobs and retained failed uploads.

    async def advance(self, j, now):
        if j.state == "cleaning":
            if now < j.not_before:
                return
            info = await self.owned_info(j)
            if info:
                await self.qb.stop(j.hash)
                await self.qb.remove(j.hash)
                return  # Observe removal on a subsequent tick before removing local bytes.
            p = self.folder(j)
            if p.exists():
                self.owned_folder(j)
                await asyncio.to_thread(shutil.rmtree, p)
            j.reserve = 0
            final_note = j.error
            j.transition(j.finish_as)
            self.save(j)
            await self.notice(j, f"{j.id}｜{LABELS[j.state]}" + (f"：{final_note}" if final_note else ""))
            return
        if j.state in {"failed", "retry", "uncertain"}:
            if now - j.changed > self.s.failure_retention and now >= j.not_before:
                j.finish_as = "expired"
                j.transition("cleaning", "失败文件保留时间到期")
                self.save(j)
                await self.notice(j, f"{j.id} 保留时间到期，将清理本地文件。")
            return
        if j.state == "new":
            if now - j.created > self.s.queue_timeout:
                await self.fail(j, "等待解析超时", True)
                return
            parsing = sum(x.state in {"adding", "parsing"} for x in self.active())
            if parsing >= self.s.max_parse_concurrent:
                return
            existing = await self.qb.info(j.hash)
            if existing:
                # Never take ownership of another client/user's existing torrent.
                p = self.owned_folder(j)
                await asyncio.to_thread(shutil.rmtree, p)
                j.transition("cancelled", "后端已存在同一资源，未接管")
                j.reserve = 0
                self.save(j)
                await self.notice(j, self.status(j))
                return
            j.transition("adding")
            self.save(j)  # Write intent before remote mutation; reconcile after ambiguous failures.
            await self.qb.add(j, self.tag(j))
            j.transition("parsing")
            self.save(j)
            return
        if j.state in {"adding", "parsing"}:
            info = await self.owned_info(j)
            if now - j.changed > self.s.parse_timeout:
                await self.fail(j, "获取元数据超时，资源可能暂无可用节点", True)
                return
            if not info:
                return
            if info.get("state") in {"error", "missingFiles"}:
                await self.fail(j, "后端解析失败，请检查磁盘权限和下载器日志", True)
                return
            if info.get("state") in {"metaDL", "forcedMetaDL", "queuedDL", "checkingResumeData"}:
                return
            files = await self.qb.files(j.hash)
            if not files:
                return
            # Metadata stop is mandatory: a backend that downloaded payload before approval is rejected.
            if info.get("state") not in {"stoppedDL", "stoppedUP", "pausedDL", "pausedUP"}:
                await self.fail(j, "后端未在元数据完成后停止，已中止；请核对 qBittorrent 版本与配置", True)
                return
            if int(info.get("downloaded", 0)) > 0:
                await self.fail(j, "解析阶段检测到正文下载，已中止该任务", True)
                return
            if len(files) > self.s.max_metadata_files:
                await self.fail(j, "资源文件数量超过元数据上限", True)
                return
            try:
                names = set()
                for f in files:
                    safe_rel(f["name"])
                    if (
                        f["name"] in names
                        or int(f["size"]) < 0
                        or ".astrbt-owner" in PurePosixPath(f["name"]).parts
                    ):
                        raise BTError("资源含冲突或无效文件名/大小")
                    names.add(f["name"])
            except BTError as e:
                await self.fail(j, str(e), True)
                return
            total = sum(int(f["size"]) for f in files)
            if self.s.max_torrent_mib and total > self.s.max_torrent_mib * MIB:
                await self.fail(j, "资源总大小超过严格上限，已取消", True)
                return
            j.files = [{"index": int(f["index"]), "name": f["name"], "size": int(f["size"])} for f in files]
            j.name = clean_text(info.get("name", "未命名资源"))
            await self.qb.priorities(j.hash, [f["index"] for f in j.files], 0)
            j.transition("selecting")
            self.save(j)
            await self.notice(
                j, self.files_text(j) + f"\n{self.s.selection_timeout // 60} 分钟内未选择将取消。"
            )
            return
        if j.state == "selecting":
            info = await self.owned_info(j)
            if info and info.get("state") not in {"stoppedDL", "stoppedUP", "pausedDL", "pausedUP"}:
                await self.fail(j, "等待选择期间任务被外部启动，已取消", True)
                return
            if now - j.changed > self.s.selection_timeout:
                await self.fail(j, "等待选择超时", True)
            return
        if j.state in {"queued", "paused"}:
            if now - j.changed > self.s.queue_timeout:
                await self.fail(j, "排队或暂停超过时限", True)
                return
            if j.started and now - j.started > self.s.download_timeout:
                await self.fail(j, "任务超过下载总时限", True)
                return
            if j.state == "paused":
                return
            running = [x for x in self.active() if x.state == "downloading"]
            if (
                len(running) >= self.s.max_concurrent
                or sum(x.user == j.user and x.platform == j.platform for x in running) >= self.s.max_per_user
            ):
                return
            await self.room(j.reserve, j)
            if not await self.owned_info(j):
                await self.fail(j, "后端任务丢失", True)
                return
            # Persist before starting; a lost response must not create an untracked download.
            j.started = j.started or now
            j.last_progress = now
            j.transition("downloading")
            self.save(j)
            await self.qb.start(j.hash)
            return
        if j.state == "downloading":
            try:
                await self.room(j.reserve, j)
            except BTError as e:
                await self.fail(j, str(e), True)
                return
            info = await self.owned_info(j)
            if not info:
                await self.fail(j, "后端任务丢失", True)
                return
            if info.get("state") in {"error", "missingFiles"}:
                await self.fail(j, "下载器报告磁盘或文件错误", True)
                return
            files = {int(f["index"]): f for f in await self.qb.files(j.hash)}
            wanted = [files.get(j.files[i]["index"]) for i in j.selected]
            if any(f is None for f in wanted):
                await self.fail(j, "后端文件列表改变，已停止下载", True)
                return
            if any(
                f["name"] != j.files[i]["name"] or int(f["size"]) != j.files[i]["size"]
                for i, f in zip(j.selected, wanted)
            ):
                await self.fail(j, "后端文件名称或大小改变，已停止下载", True)
                return
            downloaded = sum(int(f["size"] * min(1.0, float(f.get("progress", 0)))) for f in wanted)
            if downloaded > j.downloaded:
                j.last_progress = now
            j.downloaded, j.speed = downloaded, int(info.get("dlspeed", 0))
            j.peers, j.eta = (
                int(info.get("num_seeds", 0)) + int(info.get("num_leechs", 0)),
                int(info.get("eta", 0)),
            )
            if all(float(f.get("progress", 0)) >= 1 for f in wanted):
                await self.qb.stop(j.hash)
                p = self.owned_folder(j)
                for i in j.selected:
                    f = j.files[i]
                    path = safe_file(p, f["name"])
                    if not path.is_file() or path.stat().st_size != f["size"]:
                        await self.fail(j, "下载文件校验失败：缺失、大小不符或目录映射不正确")
                        return
                j.transition("uploading")
                await self.notice(j, f"{j.id} 下载完成，开始回传 QQ。")
            elif now - j.started > self.s.download_timeout or now - j.last_progress > self.s.stall_timeout:
                await self.fail(j, "下载超时或长时间无进展", True)
                return
            self.save(j)
            return
        if j.state == "uploading":
            # Only one upload worker globally; do not saturate QQ or create a burst of files.
            if not self.uploads:
                worker = asyncio.create_task(self.upload(j), name=f"astrbt-upload-{j.id}")
                self.uploads[j.id] = worker
                worker.add_done_callback(lambda _, jid=j.id: self.uploads.pop(jid, None))

    async def upload(self, j):
        try:
            for i in j.selected:
                if str(i) in j.sent:
                    continue
                async with self.lock:
                    j.current = i
                    self.save(j)  # In-flight intent is durable before invoking QQ.
                f = j.files[i]
                try:
                    p = safe_file(self.owned_folder(j), f["name"])
                    if not p.is_file() or p.stat().st_size != f["size"]:
                        raise BTError("本地文件缺失或大小不符，无法上传")
                    remote = str(PurePosixPath(self.s.napcat_root) / j.id / safe_rel(f["name"]))
                    outcome, receipt = await self.sender.upload(
                        j, remote, clean_text(Path(f["name"]).name, 180)
                    )
                except BTError as e:
                    outcome, receipt = "failed", str(e)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    outcome, receipt = "uncertain", "上传调用异常，结果未知"
                async with self.lock:
                    j.current = -1
                    if outcome == "sent":
                        j.sent[str(i)] = receipt
                    elif outcome == "uncertain":
                        j.uncertain = sorted(set(j.uncertain + [i]))
                        j.not_before = time.time() + self.s.upload_timeout
                        j.transition("uncertain", receipt)
                    else:
                        j.transition("retry", receipt)
                    self.save(j)
                if outcome != "sent":
                    await self.notice(j, self.status(j))
                    return
            async with self.lock:
                j.finish_as = "done"
                j.not_before = time.time() + self.s.cleanup_delay
                j.transition("cleaning")
                self.save(j)
            await self.notice(j, f"{j.id} 全部文件已确认上传，将清理本地数据。")
        except asyncio.CancelledError:
            # Termination reconciles this durable in-flight marker at next startup.
            raise
        except Exception:
            log.exception("BT upload worker failed for %s", j.id)
            async with self.lock:
                if j.current >= 0:
                    j.uncertain = sorted(set(j.uncertain + [j.current]))
                j.current = -1
                j.transition("uncertain", "上传流程异常，请核对 QQ 后确认或重传")
                j.not_before = time.time() + self.s.upload_timeout
                self.save(j)

    async def close(self):
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        workers = list(self.uploads.values())
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        # Best effort stop owned tasks when unloading; never delete files on shutdown.
        for j in self.active():
            try:
                async with asyncio.timeout(5):
                    if await self.owned_info(j):
                        await self.qb.stop(j.hash)
                    if j.state == "downloading":
                        j.transition("queued")
                        self.save(j)
            except Exception:
                pass
        await self.qb.close()
        await self.sender.close()
        self.store.close()
        self.instance_lock.close()
