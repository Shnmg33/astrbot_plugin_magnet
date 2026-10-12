import base64
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlencode, urlsplit


class BTError(Exception):
    """Expected user-facing failure; never include credentials or raw remote errors."""


TERMINAL = {"done", "cancelled", "expired"}
LABELS = {
    "new": "等待解析",
    "adding": "添加任务中",
    "parsing": "获取元数据",
    "selecting": "等待选择",
    "queued": "排队中",
    "downloading": "下载中",
    "paused": "已暂停",
    "uploading": "上传中",
    "retry": "待重传",
    "uncertain": "上传待确认",
    "failed": "失败待清理",
    "cleaning": "待清理",
    "done": "已发送并清理",
    "cancelled": "已取消并清理",
    "expired": "已过期并清理",
}


@dataclass
class Actor:
    user: str
    origin: str
    platform: str
    group: str = ""
    bot_id: str = ""
    admin: bool = False


@dataclass
class Job:
    id: str
    hash: str
    magnet: str
    user: str
    origin: str
    platform: str
    group: str
    bot_id: str
    state: str = "new"
    name: str = ""
    files: list = field(default_factory=list)
    selected: list = field(default_factory=list)
    sent: dict = field(default_factory=dict)
    current: int = -1
    uncertain: list = field(default_factory=list)
    created: float = field(default_factory=time.time)
    changed: float = field(default_factory=time.time)
    started: float = 0
    last_progress: float = field(default_factory=time.time)
    downloaded: int = 0
    speed: int = 0
    peers: int = 0
    eta: int = 0
    size: int = 0
    reserve: int = 0
    error: str = ""
    finish_as: str = "cancelled"
    not_before: float = 0
    last_notice: float = 0

    def transition(self, state, error=""):
        self.state, self.error, self.changed = state, error, time.time()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.commit()

    def load(self):
        return {row[0]: Job(**json.loads(row[1])) for row in self.db.execute("SELECT id,body FROM jobs")}

    def save(self, job):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?)", (job.id, json.dumps(asdict(job))))

    def delete(self, job_id):
        with self.db:
            self.db.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def close(self):
        self.db.close()


def normalize_magnet(text, trackers=()):
    if len(text) > 16384:
        raise BTError("磁力链过长")
    u = urlsplit(text.strip())
    if u.scheme.lower() != "magnet":
        raise BTError("仅支持 magnet:? 磁力链")
    try:
        args = parse_qs(u.query, max_num_fields=100)
    except ValueError:
        raise BTError("磁力链参数过多") from None
    hashes = []
    for xt in args.get("xt", []):
        if not xt.lower().startswith("urn:btih:"):
            continue
        h = xt[9:]
        if re.fullmatch(r"[A-Fa-f0-9]{40}", h):
            hashes.append(h.lower())
        elif re.fullmatch(r"[A-Za-z2-7]{32}", h):
            hashes.append(base64.b32decode(h.upper()).hex())
        else:
            raise BTError("BTIH 哈希格式不正确")
    if len(set(hashes)) != 1:
        raise BTError("需要一个有效的 BTIH 哈希；第一版支持 BT v1/含 BTIH 的混合磁力，不支持纯 v2")
    h = hashes[0]
    # No user-controlled URLs (xs/as/ws/tr) are passed to the downloader.
    clean = [("xt", "urn:btih:" + h)]
    clean += [("tr", str(t)) for t in trackers]
    return h, "magnet:?" + urlencode(clean)


def clean_text(text, length=120):
    return re.sub(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]", " ", str(text)).replace("[CQ:", "[CQ：")[
        :length
    ]


def safe_rel(name):
    p = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or p.is_absolute()
        or any(x in ("..", ".", "") for x in name.split("/"))
    ):
        raise BTError("资源含不安全的文件路径，已拒绝")
    if any(":" in part for part in p.parts):
        raise BTError("资源含不支持的文件路径")
    return p


def safe_file(root: Path, name):
    rel = safe_rel(name)
    root = root.resolve()
    p = root.joinpath(*rel.parts)
    # Reject symlinks in any component, not only a resolved escape.
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise BTError("拒绝读取符号链接")
    if not p.resolve().is_relative_to(root):
        raise BTError("文件超出任务目录")
    return p


def select_indices(expr, count, limit):
    expr = expr.strip().lower().replace("，", ",")
    if expr in ("all", "全部"):
        if count > limit:
            raise BTError(f"最多选择 {limit} 个文件，请缩小范围")
        return list(range(count))
    selected = set()
    if len(expr) > 2000:
        raise BTError("选择表达式过长")
    for item in expr.split(","):
        m = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", item)
        if not m:
            raise BTError("文件编号格式：1,3,5 或 2-6 或 all")
        start, end = int(m[1]), int(m[2] or m[1])
        if not 1 <= start <= end <= count:
            raise BTError(f"文件编号应在 1—{count} 范围内")
        if end - start + 1 > limit:
            raise BTError(f"最多选择 {limit} 个文件")
        selected.update(range(start - 1, end))
        if len(selected) > limit:
            raise BTError(f"最多选择 {limit} 个文件")
    if not selected:
        raise BTError("请至少选择一个文件")
    return sorted(selected)


def human(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024
