# EasySbatch Launcher Alpha

`0.1.0-alpha.5` adds the persistent local Agent lifecycle. After one system
OpenSSH authentication, the Agent owns the SSH connection, Web tunnel, Worker
and optional AI stream independently of the startup terminal. Re-running the
Launcher opens the existing session; `status`, `open`, and `stop` are supported.
No SSH password or private key is saved.

`0.1.0-alpha.3` 源码候选将 Worker/Launcher协议升级为v2，并在同一条已认证SSH remote-command stdin/stdout上加入有界Structured AI Egress；不使用`ssh -R`、不新增服务器端口，也不增加第二次SSH认证。该版本尚未发布，必须与目标集群的 `user-worker-v2.py`同步部署；真人双用户与Windows/macOS artifact验收完成前不要替换旧版默认下载。

`v0.1.0-alpha.2` 改进 Windows 失败提示：SSH 尚未进入密码验证时会明确提示检查校园网/VPN，双击启动发生错误后窗口会保留，运行期异常不再误报为配置文件错误。

请选择与电脑系统和架构对应的文件。Launcher 使用系统 OpenSSH，通过用户配置的集群 SSH 入口建立加密通道，不保存 SSH 密码或私钥。

- Windows x86_64：`EasySbatch-Windows-x86_64.exe`
- macOS Apple Silicon：`EasySbatch-macOS-arm64.dmg` 或 `.zip`
- macOS Intel：`EasySbatch-macOS-x86_64.dmg` 或 `.zip`
- Linux x86_64：`EasySbatch-Linux-x86_64`

本 Alpha 尚未进行 Windows Authenticode 签名或 Apple Developer ID 签名、公证。请先用 `SHA256SUMS` 核对下载文件。
