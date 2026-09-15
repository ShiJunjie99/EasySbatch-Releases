# EasySbatch Internal Alpha

这是实验室内部测试包，不是公开发行版。当前仅把 Linux x86_64 包列为可分发候选；Windows/macOS 的 native 与真人凭据验收仍未完成。

## 支持的测试范围

- 启动 Launcher、配置实验室集群并通过 OpenSSH 登录
- 验证当前 Linux 用户的 Web/Worker 身份
- 在当前用户自己的设备上配置 DeepSeek API Key，并执行 synthetic AI Analyze
- 浏览器打开、退出、重新连接和 Launcher status/stop

请只使用合成项目，不提交 Slurm job，不测试重要生产任务。

## 首次使用

1. 向集群管理员取得本机对应的集群 profile 和 OpenSSH/known-hosts 配置。
2. 启动 EasySbatch，使用自己的 Linux 账号连接集群。
3. 在自己的 Launcher 中配置自己的 DeepSeek API Key。不要把 Key 发给开发者或同学；Key 不上传到 gpu00，也不通过 SSH。
4. 使用 synthetic project 做 Analyze，记录结果后退出并重新连接。

Linux 当前若没有 Secret Service/KWallet，API Key 只在本次 Launcher Agent 内存中存在；关闭 Agent 后需要重新配置。不得创建 .env、配置文件或明文 key 文件。

## 当前不在 Alpha 范围

M10-C Workspace、Upload、远端 Scanner、多用户 Slurm Submit、Job History migration 均未完成。Alpha 不代表生产就绪，也不保证复杂 MPI/多节点/GPU 工作流。

反馈请使用 docs/INTERNAL_ALPHA_FEEDBACK.md，不要上传 API Key、SSH 密码、私钥、session token 或敏感科研文件。
