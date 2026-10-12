# 本地验证记录

版本：v0.1.0  
日期：2026-10-11  
平台：Linux / Python 3.12.14

## 结果

- `python -m pytest -q astrbot_plugin_magnet/tests`：**67 passed**。
- `python -m ruff check astrbot_plugin_magnet`：**All checks passed**。
- `python -m compileall -q astrbot_plugin_magnet`：通过。
- 配置 schema、插件 metadata、ZIP 目录和 Python 文件语法在打包时再次校验。

测试依赖：httpx 0.28.1，pytest 9.1.1，pytest-asyncio 1.4.0，ruff 0.17.0。

## 覆盖范围

| 范围 | 验证内容 |
|---|---|
| 输入 | BTIH 十六进制/Base32，非法磁力、URL 参数剥离、选择范围与数量 |
| 文件安全 | 路径越界、符号链接、所有权标记、后端任务归属、文件名称改变 |
| 下载状态 | 元数据后停止、审批前禁止正文下载、大小限制、小文件选择、队列、并发、超时 |
| 权限 | 用户及会话隔离、省略任务号歧义、重复提交的原子处理 |
| 回传 | 群/私聊动作参数、机器人账号匹配、file_id 和 URL 确认、超时与未知结果 |
| 持久化 | SQLite 上传前意图记录、添加响应丢失恢复、上传中重启、单实例文件锁 |
| 清理 | 后端移除后再删本地、未知上传保留、部分失败只重传未成功项、标记异常拒删 |
| AstrBot 入口 | 使用 API 替身验证导入、命令前缀、完整参数转交、禁用时不连接后端 |

## 尚未完成的真实环境验证

本次没有连接用户的 QQ、服务器或实际下载器。测试中的下载器和 HTTP 请求均为模拟接口；AstrBot 入口使用 API 替身，**不等同于真实 AstrBot 运行测试**。

以下需要部署后完成：真实 BT 节点与 DHT 下载、真实 qBittorrent 5.x 元数据停止时序、NapCat 不同版本的 file_id/文件地址确认行为、大文件传输、QQ 群容量限制、三容器实际目录权限。README 第 9 节提供验收步骤。

未实际启动 Docker；Compose 文件经过 YAML 结构检查，尚未在用户服务器验证镜像与网络。
