# AstrBot BT 下载助手 · v0.1.0

通过 `/bt` 在 QQ 中解析磁力链、选择文件、限制大小、查询下载进度，下载完成后回传文件并清理服务器上的任务数据。

**交付状态：源代码、安装包、自动化测试和部署示例已提供。尚未在用户的 AstrBot / NapCat / qBittorrent 实例上进行真实下载与 QQ 联调。**

## 1. 支持范围

- Linux，Python 3.11+；AstrBot 4.16+、小于 5，使用 `aiocqhttp` / OneBot v11 适配器。
- qBittorrent **5.x** 独立无界面服务，开放经过认证的 WebUI API。
- NapCat：需要 `upload_group_file`、`upload_private_file`；自动清理还要求返回 `file_id`，并可通过 `get_group_file_url` / `get_private_file_url` 获取文件地址。
- AstrBot、qBittorrent、NapCat 必须能访问**同一份下载文件**。支持不同容器内的路径映射；第一版不包含跨服务器文件搬运。
- 支持 BT v1 磁力（40 位十六进制或 32 位 Base32 BTIH），以及带 BTIH 的混合磁力。不支持纯 BT v2、HTTP 直链、`.torrent` 上传。
- 不需要大模型 API。文件内容不发送给大模型。
- 群任务返回原群；私聊任务返回原私聊。文件逐个发送，不自动解压、执行、转码、压缩或分卷。

## 2. 最短部署顺序

1. 为三个服务准备同一份专用下载目录，配置读写权限和路径映射。
2. 启动 qBittorrent 5.x，设置 WebUI 用户名和密码，启用 DHT。
3. 在 AstrBot 插件页面上传本 ZIP；如果没有本地上传入口，按下文手动安装。
4. 配置插件的后端地址、用户名、密码、三个下载目录；默认保持 `admin_only=true`。
5. 开启 `enabled` 并重载插件。发送 `/bt admin` 检查后端状态。
6. 先用自己制作、有人做种的几 KB 测试资源，完成“解析 → 选择 → 下载 → QQ 回传 → 删除”验证，再提高大小和并发限制。

> 插件默认 `enabled=false`。安装不会自行启动下载，也不会自动安装/管理下载器。

## 3. Docker 部署示例

下面的示例只新增下载后端，不替换你现有的 AstrBot 或 NapCat。

### 3.1 准备目录

默认下载器使用 UID/GID 1000。如果你的容器使用其他身份，请统一替换，**不要对现有 Obsidian、Syncthing 等资料目录递归修改权限**。

```bash
sudo install -d -m 2770 -o 1000 -g 1000 /srv/astrbt/downloads
sudo install -d -m 0750 -o 1000 -g 1000 /srv/astrbt/qb-config
```

非 root 的 AstrBot、NapCat 进程需要加入共享 GID（示例为 1000）。任务目录会继承父目录组并设置为 2770。AstrBot 和下载器需要读写权限，NapCat 只需读取。

### 3.2 启动后端

在解压后的插件目录执行：

```bash
docker compose -f deploy/compose.qbittorrent.yml up -d
docker logs astrbt-qbittorrent
```

示例采用 LinuxServer 镜像的 `latest` 标签作为首次部署入口；**确认实际版本为 5.x 并完成联调后，使用 `QBITTORRENT_IMAGE` 固定已验证的镜像标签或 digest**。插件会拒绝非 5.x 后端，不会静默兼容未来的大版本。

WebUI 仅映射到服务器的 `127.0.0.1:8080`。在自己的电脑上可通过 SSH 隧道访问：

```bash
ssh -L 18080:127.0.0.1:8080 你的用户名@服务器地址
```

浏览器访问 `http://127.0.0.1:18080`，从下载器启动日志取得初始临时密码，登录后设置新密码。保留身份验证。将 DHT 打开；建议关闭下载队列限制，以插件队列为准，避免任务一直 `queuedDL`。

插件为每个资源显式设置保存路径和“元数据获取后停止”，但仍应使用独立下载器：不要在 WebUI 手动启动、移动、重命名插件任务，不要配置完成后执行外部程序、自动移动或自动删除任务。可以查看状态。

6881 TCP/UDP 用于 BT 入站连接。是否开放应结合你的服务器网络配置；没有可用节点的磁力仍可能解析失败，插件无法保证所有磁力都能下载。

### 3.3 让现有容器共享文件

`deploy/compose-existing-snippet.yml` 是**合并片段，不是可单独运行的 Compose**。把其中 volumes、共享 group 和 network 合并到现有 AstrBot/NapCat 服务，保留原来全部配置和网络。

三者都映射：

| 服务 | 主机目录 | 容器目录 | 权限 |
|---|---|---|---|
| AstrBot | `/srv/astrbt/downloads` | `/bt-downloads` | 读写 |
| qBittorrent | `/srv/astrbt/downloads` | `/bt-downloads` | 读写 |
| NapCat | `/srv/astrbt/downloads` | `/bt-downloads` | 只读即可 |

三个容器在 `astrbt-net` 网络中时，插件 `qb_url` 填 `http://qbittorrent:8080`。容器里的 `127.0.0.1` 只代表容器自身。

如果你使用不同的容器内路径，例如 `/data/bt`、`/downloads/bt`、`/files/bt`，对应填写 `local_root`、`qb_root`、`napcat_root`；它们必须映射到同一个主机目录。

### 3.4 安装插件

优先使用 AstrBot WebUI → 插件 → 从本地 ZIP 安装（不同版本入口名称可能不同）。ZIP 内有一个 `astrbot_plugin_magnet/` 顶层目录。

手动方式：将该目录放入 AstrBot 的插件目录，确保路径为：

```text
data/plugins/astrbot_plugin_magnet/main.py
```

不要形成两层同名目录。在 AstrBot 的 Python 环境安装 `requirements.txt`，随后重启/重载插件：

```bash
python -m pip install -r data/plugins/astrbot_plugin_magnet/requirements.txt
```

不要在插件配置里填写任何 QQ 用户能够自行控制的本地根目录。请用专用目录，不要挂载 Obsidian 仓库、个人下载目录或系统根目录。

### 3.5 插件配置

| 字段 | 示例 / 含义 |
|---|---|
| `enabled` | 配置完成后设 `true` 并重载 |
| `qb_url` | `http://qbittorrent:8080` |
| `qb_username` / `qb_password` | WebUI 登录信息 |
| `local_root` | AstrBot 内路径 `/bt-downloads` |
| `qb_root` | 下载器内路径 `/bt-downloads` |
| `napcat_root` | NapCat 内路径 `/bt-downloads` |
| `admin_only` | 默认 `true`；仅 AstrBot 配置的管理员，群管理员不自动获得此权限 |
| `allowed_users` | 关闭仅管理员模式后可配置 QQ 号列表，空为不限制用户 |
| `allowed_groups` | 群白名单；空为不限制群；与用户白名单同时满足 |
| `napcat_http_url` | 可选，例如 `http://napcat:3000`，须为已启用的 NapCat HTTP 服务 |
| `napcat_token` | HTTP 服务对应 Access Token |

默认通过 AstrBot 已有的 OneBot 连接上传。AstrBot 当前适配器源码设置了 180 秒 API 超时，插件自身更长的超时不会覆盖这个内层限制；大文件可能因此进入“待确认”。需要更长上传等待时间时，配置可选的 NapCat HTTP 服务，插件将使用自己的 `upload_timeout`。HTTP 端口号以你实际设置为准，不会自动启动服务。

配置 HTTP 时，插件会先核对该服务的登录 QQ 是否与任务来源机器人一致。若有多个机器人，每个插件实例的可选 HTTP 地址只对应一个 QQ；不匹配的任务不会发送。

默认后台消息仍通过 AstrBot 原会话发送。不要把 qBittorrent WebUI 或带 Token 的 NapCat HTTP 直接开放给陌生人。

## 4. 命令

| 命令 | 作用 |
|---|---|
| `/bt`、`/bt help` | 帮助 |
| `/bt magnet:?xt=urn:btih:…` | 创建解析任务 |
| `/bt files T12345678 2` | 查看第 2 页 |
| `/bt files T12345678 1 pdf` | 按文件名搜索，显示匹配结果第 1 页；编号仍是原文件编号 |
| `/bt download T12345678 1,3,5` | 下载这些文件 |
| `/bt download T12345678 2-6` | 下载连续范围 |
| `/bt download T12345678 all` | 下载全部（仍受数量和大小限制） |
| `/bt status T12345678` | 查询状态、速度、进度、发送结果 |
| `/bt list` | 本用户在当前会话中的最近 20 条任务 |
| `/bt pause T12345678` | 暂停下载/排队 |
| `/bt resume T12345678` | 重新排队，不重置下载总时限 |
| `/bt cancel T12345678` | 终止并清理；正在执行文件上传时暂不允许取消 |
| `/bt resend T12345678` | 重传尚未确认的文件；不会重传已确认项 |
| `/bt confirm T12345678 2` | 用户已在 QQ 核对后，确认待确认的第 2 个文件 |
| `/bt admin` | 管理员查询后端、空间及全部活动任务 |

只有当前会话中**自己唯一一个符合条件的活动任务**时可以省略任务号。例如 `/bt download 1,3`、`/bt status`。任务号统一以 `T` 开头，不会与文件编号混淆。多个候选任务时列出编号，不猜测。

设置 AstrBot 的唤醒前缀包含 `/`。群里是否需要 @机器人还受 AstrBot 的群消息响应配置影响。自动识别裸磁力默认关闭；开启后只接收整条文本为一条磁力的形式，不从普通聊天中随意提取链接。

## 5. 限额、队列与时限

默认限制为：所选总大小 1024 MiB，单文件 500 MiB，20 个文件，全局同时下载 2 个、每用户 1 个，同时解析 2 个；全局活动任务最多 20 个，每用户最多 3 个。失败保留、等待选择、正在上传的任务也计入活动任务上限。

所有这些值都可在插件配置修改。`max_file_mib` 是你设置的策略，不是 QQ 官方承诺；请按账号、群容量和协议端实际表现调低或调高。

- `max_torrent_mib=0`：只按所选内容限额。20 GiB 的资源里选 100 MiB 文件可以通过。
- `max_torrent_mib>0`：额外限制整个资源，解析出总大小超限即取消。
- 默认磁盘预算 10 GiB，最低剩余 5 GiB。预算同时考虑所选文件、分片边界、额外预留和一次额外副本；实际占用也会查询。
- 大小超限发生在正文下载之前。所选超限时保持待选择，允许换更小的内容。
- 解析默认 180 秒；选择 10 分钟；排队/暂停 1 小时；无进展 20 分钟；下载总时限 2 小时；单次上传等待 20 分钟；失败保留 1 小时。
- 失败保留不小于上传超时；上传结果未知时还会等待一个上传超时窗口再允许重传/删除，降低协议端仍在读取源文件的风险。
- 每任务 BT 上传默认限制 256 KiB/s；下载速度默认不限制。下载结束由调度器停止做种，最长通常相差一个轮询周期，不能保证零上传。

磁盘保护依赖预留和定时检查，不是文件系统硬配额；进程停机或其他程序同时写磁盘时无法承诺绝对不超。需要硬上限可为专用下载目录配置文件系统 quota。不要把所有服务器剩余空间分配给此插件。

## 6. 上传确认、失败与删除

每个文件在发送前写入“正在上传”记录，取得非空 `file_id` 且可查询文件地址后才记为已确认上传。成功不代表用户已下载到手机，也不保证 QQ 永久保存。

- 全部上传确认后默认等待 60 秒，先移除下载器任务，再观察后端任务确实消失，最后删除插件自己的任务目录。
- 下载器删除接口使用 `deleteFiles=false`；本地递归删除必须通过任务 ID、目录范围和所有权标记校验。不会用用户输入拼出任意删除路径。
- 部分失败：保留文件，`resend` 只发送尚未成功的部分。
- 网络超时、协议端错误或没有文件 ID：记为“待确认”，不会自动重发，也不会立刻删除。
- 只有 HTTP 连接建立失败、能确定未提交上传时，才最多自动重试 2 次。其他错误由用户显式处理。
- 已在 QQ 收到文件：按 `status` 给出的编号使用 `/bt confirm 任务号 编号`。该命令表示你已经人工核对，不是跳过上传的普通步骤。
- 确认没收到：等待隔离窗口结束后 `/bt resend 任务号`。若先前其实已成功，显式重传可能产生重复文件。
- 一小时保留到期：通知并清理；待确认任务还须满足隔离窗口。可通过配置延长保留时间。
- 删除失败保留“待清理”，后台重试；不提前显示“已清理”。

**清理范围：插件任务目录内的下载文件、未选文件边界数据、任务标记等，以及后端对应任务；历史状态默认保留 7 天。不会删除已经发到 QQ 的文件，也不会扫描删除 NapCat/QQ 自己管理的缓存。**

同一资源（BTIH）在后端已有任务时拒绝接管；第一版不共享下载，以免不同用户选择、删除互相影响。

## 7. 重启、停机和数据目录

任务数据库及实例 ID 位于 AstrBot 插件数据目录中的 `astrbot_plugin_magnet`，典型路径为 `data/plugin_data/astrbot_plugin_magnet/`。必须持久化整个 AstrBot data 目录。

- 正常卸载/重载：尽量停止本插件任务，保留文件；下次加载重新入队。
- 突然崩溃：后端可能继续运行；再次启动会核对归属，停止并重新调度下载任务。
- 上传过程中重启：已确认项目保留，正在发送的项目标记为未知，不能自动重新发送。
- 同一数据目录仅允许一个运行实例；使用文件锁防止重载/多进程同时管理。
- 不要在有任务时修改下载根目录、实例数据目录或清空数据库。旧任务和标记可能无法对应，插件会拒绝冒险删除。丢失数据库后的遗留目录需人工核实，插件不会盲目回收。

## 8. 故障排查

| 现象 | 检查 |
|---|---|
| `/bt` 没反应 | 插件是否加载、唤醒前缀是否含 `/`、是否需 @、是否启用此会话插件 |
| 提示未启用 | 配置 `enabled=true` 后重载 |
| 登录失败或连接失败 | qBittorrent 密码、容器网络、WebUI 端口和版本；`localhost` 是否指错容器 |
| 一直获取元数据 | DHT、节点是否在线、网络是否允许 BT、后端是否排队；超时可调 |
| 提示空间不足 | 默认最低剩余 5 GiB，并有空间预留；不要仅看选中文件大小 |
| 下载完成但本地文件缺失 | 三个路径是否映射同一目录，权限是否一致，是否被后端移动/重命名 |
| QQ 收到文件但待确认 | API 返回缺少 file_id、查询失败或 OneBot 内层超时；核对后 `confirm` |
| 大文件经常超时 | 优先设置可选 NapCat HTTP 服务，或调小单文件上限 |
| 群上传失败 | 群文件空间、机器人权限、协议端状态；测试私聊与群聊分别验证 |
| 无法私聊回传 | QQ 临时会话或好友关系可能限制上传，先用机器人好友私聊测试 |
| 待清理不消失 | 目录权限、磁盘只读、文件所有权标记、后端是否可达；查看 AstrBot 日志 |

配置修改后请重载插件；默认不热更新运行中的限制。不要在日志或群聊贴出密码和 Token。

## 9. 验证与开发

本地执行：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
```

自动化测试使用隔离的临时文件、模拟下载器和 HTTP mock。覆盖磁力标准化、选择范围、路径保护、大小限制、权限/会话隔离、并发队列、真实 SQLite 持久化、重复任务、元数据阶段禁止正文下载、取消、上传部分失败、结果未知、重启恢复、文件确认和清理顺序等。具体测试数量和版本见 `TEST_REPORT.md`。

部署后仍需执行以下实际验收：

1. 群聊与好友私聊各上传一个几 KB 文件，确认可下载，且本地任务目录随后删除。
2. 多文件种子只选部分文件，确认发送清单准确；未选择文件可能有分片边界缓存，但不应回传。
3. 设置很小的大小上限，确认解析后拒绝正文下载。
4. 让 NapCat 暂时离线，确认数据保留；恢复后重传成功。
5. 下载中重启插件，确认任务重新排队；上传中重启，确认没有自动重复发送。
6. 取消/超时后确认后端任务和插件目录删除，其他下载器任务不受影响。

## 10. 参考接口

开发核对日期：2026-10-11。未复制第三方实现代码，参考公开 API 和源码确认行为。

- [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot 消息发送](https://docs.astrbot.app/dev/star/guides/send-message.html)
- [qBittorrent 5.x WebUI API](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-5.0%29)
- [qBittorrent 添加任务与 stopCondition 实现](https://github.com/qbittorrent/qBittorrent/blob/master/src/webui/api/torrentscontroller.cpp)
- [NapCat 文件上传接口](https://napneko.github.io/onebot/api)
- [NapCat 群文件上传实现](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/go-cqhttp/UploadGroupFile.ts)
- [NapCat 私聊文件上传实现](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/go-cqhttp/UploadPrivateFile.ts)
- [LinuxServer qBittorrent 镜像文档](https://docs.linuxserver.io/images/docker-qbittorrent/)
