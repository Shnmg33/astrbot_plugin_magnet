from dataclasses import dataclass, field, fields
from pathlib import Path, PurePosixPath

MIB = 1024**2
GIB = 1024**3


@dataclass
class Settings:
    enabled: bool = False
    qb_url: str = "http://qbittorrent:8080"
    qb_username: str = "admin"
    qb_password: str = ""
    local_root: str = "/bt-downloads"
    qb_root: str = "/bt-downloads"
    napcat_root: str = "/bt-downloads"
    napcat_http_url: str = ""
    napcat_token: str = ""
    admin_only: bool = True
    allowed_users: list = field(default_factory=list)
    allowed_groups: list = field(default_factory=list)
    auto_detect: bool = False
    trusted_trackers: list = field(default_factory=list)
    max_task_mib: int = 1024
    max_file_mib: int = 500
    max_torrent_mib: int = 0
    max_files: int = 20
    max_metadata_files: int = 10000
    max_concurrent: int = 2
    max_per_user: int = 1
    max_pending: int = 20
    max_user_pending: int = 3
    max_parse_concurrent: int = 2
    disk_budget_mib: int = 10240
    min_free_mib: int = 5120
    overhead_mib: int = 128
    download_kib: int = 0
    upload_kib: int = 256
    parse_timeout: int = 180
    selection_timeout: int = 600
    queue_timeout: int = 3600
    stall_timeout: int = 1200
    download_timeout: int = 7200
    upload_timeout: int = 1200
    failure_retention: int = 3600
    cleanup_delay: int = 60
    poll_seconds: int = 5
    page_size: int = 10
    history_days: int = 7

    @classmethod
    def from_mapping(cls, values):
        s = cls(
            **{
                f.name: values.get(f.name, f.default_factory() if callable(f.default_factory) else f.default)
                for f in fields(cls)
            }
        )
        for f in fields(cls):
            v = getattr(s, f.name)
            if f.type is int and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
                raise ValueError(f"{f.name} 必须是非负整数")
        for key in (
            "max_task_mib",
            "max_file_mib",
            "max_files",
            "max_metadata_files",
            "max_concurrent",
            "max_per_user",
            "max_pending",
            "max_user_pending",
            "max_parse_concurrent",
            "disk_budget_mib",
            "parse_timeout",
            "selection_timeout",
            "queue_timeout",
            "stall_timeout",
            "download_timeout",
            "upload_timeout",
            "failure_retention",
            "poll_seconds",
            "page_size",
            "history_days",
        ):
            if getattr(s, key) == 0:
                raise ValueError(f"{key} 必须大于 0")
        if s.failure_retention < s.upload_timeout:
            raise ValueError("失败保留时间必须不小于上传超时")
        if not Path(s.local_root).is_absolute() or Path(s.local_root).resolve() == Path("/"):
            raise ValueError("local_root 必须是专用绝对目录，不能是根目录")
        for key in ("qb_root", "napcat_root"):
            p = PurePosixPath(getattr(s, key))
            if not p.is_absolute() or str(p) == "/" or ".." in p.parts:
                raise ValueError(f"{key} 必须是 Linux 专用绝对目录")
        s.allowed_users = [str(x) for x in s.allowed_users]
        s.allowed_groups = [str(x) for x in s.allowed_groups]
        return s
