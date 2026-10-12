import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .config import Settings
from .manager import Manager
from .models import Actor, BTError
from .qb import QB
from .transport import QQSender

HELP = """BT 下载助手
/bt magnet:?…  解析磁力链
/bt files [任务号] [页码] [关键词]  查看/搜索文件
/bt download [任务号] 1,3 或 2-6 或 all
/bt status [任务号]  查询进度
/bt list  查看自己的任务
/bt pause [任务号]  暂停
/bt resume [任务号]  继续
/bt cancel [任务号]  取消并清理
/bt resend [任务号]  重传未确认文件（可能重复）
/bt confirm [任务号] 编号  确认 QQ 中已收到的待确认文件
/bt admin  管理员查看队列和空间
/bt help  帮助
只有一个符合条件的当前会话任务时，可省略任务号。
先解析、后选择；成功上传后清理服务器文件。"""


@register("astrbot_plugin_magnet", "Shnmg", "QQ 磁力解析、选择下载、回传与清理", "0.1.0")
class MagnetPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.settings = Settings.from_mapping(config)
        self.manager = None
        self.start_error = ""

    async def initialize(self):
        if not self.settings.enabled:
            return
        try:
            data = StarTools.get_data_dir("astrbot_plugin_magnet")
            qb = QB(self.settings)
            sender = QQSender(self.settings, self.context)
            self.manager = Manager(self.settings, data, qb, sender)
            await self.manager.start()
        except Exception:
            logger.exception("BT 插件初始化失败")
            self.start_error = "初始化失败，请管理员检查插件配置、目录权限和日志"

    def actor(self, event):
        if event.get_platform_name() != "aiocqhttp":
            raise BTError("第一版只支持 OneBot v11（NapCat）平台")
        return Actor(
            user=str(event.get_sender_id()),
            origin=event.unified_msg_origin,
            platform=event.get_platform_id(),
            group=str(event.get_group_id() or ""),
            bot_id=str(event.message_obj.self_id),
            admin=event.is_admin(),
        )

    async def execute(self, event, body):
        parts = body.strip().split()
        if not parts or parts[0].lower() in ("help", "帮助"):
            return HELP
        if not self.settings.enabled:
            return "BT 插件尚未启用。请管理员配置 qBittorrent、共享目录，然后开启 enabled 并重载插件。"
        if self.start_error or self.manager is None:
            return self.start_error or "插件正在初始化，请稍后重试"
        actor = self.actor(event)
        if parts[0].lower().startswith("magnet:?"):
            if len(parts) != 1:
                raise BTError("一次只提交一条完整磁力链：/bt magnet:?…")
            return await self.manager.submit(actor, parts[0])
        command = parts.pop(0).lower()
        if command not in {
            "files",
            "download",
            "status",
            "list",
            "pause",
            "resume",
            "cancel",
            "resend",
            "confirm",
            "admin",
        }:
            raise BTError("未知命令，请使用 /bt help")
        return await self.manager.command(actor, command, parts)

    @filter.command("bt")
    async def bt(self, event: AstrMessageEvent):
        """磁力解析与下载；详细命令见 /bt help。"""
        body = re.sub(r"^/?bt(?:\s+|$)", "", event.get_message_str().strip(), count=1)
        try:
            reply = await self.execute(event, body)
        except BTError as e:
            reply = str(e)
        except Exception:
            logger.exception("BT 命令处理异常")
            reply = "处理失败，请稍后查询 /bt status；管理员可查看日志。"
        event.stop_event()
        yield event.plain_result(reply)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def detect_magnet(self, event: AstrMessageEvent):
        if not self.settings.auto_detect or not self.settings.enabled:
            return
        text = event.get_message_str().strip()
        if not text.lower().startswith("magnet:?"):
            return
        try:
            reply = await self.execute(event, text)
        except BTError as e:
            reply = str(e)
        except Exception:
            logger.exception("BT 自动解析异常")
            reply = "自动解析失败，请管理员查看日志。"
        event.stop_event()
        yield event.plain_result(reply)

    async def terminate(self):
        if self.manager:
            await self.manager.close()
            self.manager = None
