# M10-B7A Internal Alpha Preflight & Real-user Acceptance 交付说明

更新时间：2026-09-11

## A. Goal

本阶段只收口当前 Launcher artifact、per-user local AI、会话隔离、生命周期和内部测试包；不实现 M10-C，不公开 GitHub，不改变 M10-B7 transport 架构。

## B. Starting State

M10-B3/B4A 核心 Launcher/Worker 与当前 Linux packaging 已实现；M10-B5/B5B/B5C 保持 PARTIAL 历史研究状态；M10-B7 为 Alpha 候选；M10-B8 为 PUBLIC_RELEASE_BLOCKED。旧 relay、remote-forward 和 structured SSH egress 未启动，也不是本次 Alpha 前置条件。

## C. Alpha Scope

允许：Launcher、SSH identity、Web session、AI 设置/Analyze、UI、logout/reconnect。禁止宣称 M10-C Workspace、Upload、multi-user Slurm Submit 或 Job History 已就绪；本次未提交 Slurm job。

## D. shijunjie Real Test

当前环境没有 shijunjie 的真人设备、SSH 密码/私钥或 DeepSeek Key，不能代测或伪造结果：

| 项目 | 结果 |
|---|---|
| packaged Launcher | NOT TESTED |
| SSH identity | NOT TESTED |
| own DeepSeek Key | NOT TESTED |
| AI Analyze | NOT TESTED |

## E. suhaoran Real Test

当前同样没有 suhaoran 的真人设备、SSH credential 或 DeepSeek Key：Launcher、SSH identity、own key、AI Analyze 均为 NOT TESTED。自动化 alice/bob fixtures 不能替代真人验收。

## F. Two-user AI Isolation

代码/自动化验证了 exact WebSession → WorkerSession → LauncherSession 绑定、同一用户多 session 不随机路由、A/B request 不交叉和 identity mismatch fail-closed；真实两台设备同时 DeepSeek 未测试。因此 simultaneous Web/AI 为 NOT TESTED，不是 PASS。

## G. Delete-key Isolation

Credential manager 的 delete、missing key、无跨用户 fallback 已由自动化覆盖；A 删除后 B 继续真实 Analyze 尚未执行，真人结论为 NOT TESTED。

## H. Bad-key Test

固定错误分类和安全 UI 文案由 provider 自动化覆盖：AI_PROVIDER_AUTH_FAILED，不回显原始 provider error；不会影响 SSH/Worker。真人无效 key 测试尚未执行。

## I. Linux Credential Behavior

当前 Linux 桌面检测到 Secret Service 时报告 Secret Service；无 secure backend 时只允许显式 --session-only，状态为 Session only，关闭 Agent 后内存 key 消失。绝不 fallback 到明文文件。CLI 已明确显示“本次运行使用（仅存于当前 Launcher Agent 内存）”。自动化 credential、redaction、missing-backend 和 persistence regression 通过。

## J. Windows Status

源码/当前 PyInstaller spec 包含 Windows Credential Manager backend discovery、fixed DeepSeek provider、TLS client 和 AI CLI；本环境没有 native Windows runner，也没有当前源码的 Windows EXE 或真人 Credential Manager Save/Restart/Read/Delete 证据。因此记录为 BUILD/REAL TEST NOT TESTED，不把旧 artifact 当作 PASS；Internal Alpha 为 NOT READY。

## K. macOS Status

macOS Keychain adapter 和 native workflow 保留，但当前没有 macOS native build 或真机测试证据：Artifact、Keychain、Real Alpha 均 NOT TESTED / NOT READY。

## L. Launcher Lifecycle Reality

当前 CLI Controller 通过 start_new_session（POSIX）或 detached process flags（Windows）启动独立 Agent；Agent 的 stdin/stdout/stderr 为 DEVNULL，拥有 exact SSH child、Web forward、Worker 和 loopback handoff。连接完成后 CLI 明确打印“EasySbatch 已在后台运行。可以关闭此终端。”因此设计/源码结论是：Linux READY 后 Terminal 不必保持；认证期间需要原终端提供 SSH 交互。Agent status/stop 和 Web logout 负责后续生命周期。Linux terminal-close 真人矩阵尚未执行；Windows lifecycle 也未真人测试，故不能把跨平台生命周期写成 PASS。

## M. Legacy Route Guard

保留并回归验证：非当前服务进程用户访问 legacy project/jobs routes 返回阻断，不会借用 shijunjie/process identity。这个 guard 通过，但它意味着 suhaoran 完整旧分析/提交流在本阶段不能被宣称已完成；本阶段不绕过 guard、不开始 M10-C。

## N. Internal Alpha Artifact

已准备 Linux 候选目录 dist/internal-alpha/：Linux x86_64 binary、内部说明、实验室 profile（仅 host/port/非 secret 连接元数据）和 SHA256SUMS。不包含 API Key、SSH credential、private Catalog、SQLite、runs 或 logs。Windows/macOS artifact 未加入。

## O. Secret Scan

当前 tree、Linux artifact 和内部包扫描未发现真实 API key、密码、私钥、bootstrap/session token。扫描只报告类别，不记录 secret 正文。实验室 host/port 仅存在于 ignored internal profile，不进入 public generic defaults。

## P. pytest

本次变更后全量回归：

    2064 passed, 3 warnings

warning 为既有 Starlette/httpx 与 AnyIO dependency deprecation；无真实集群、DeepSeek 或 Slurm 网络调用。

## Q. Known Limitations

- 真人 shijunjie/suhaoran key、SSH、Analyze 尚未完成，不能用 fake test 替代。
- 两台设备 simultaneous AI、A logout/B continuation、A delete key/B continuation 尚未真人验证。
- Linux 当前可能是 session-only；需关闭 Agent 后重新配置。
- 当前 Linux binary 在本机完成 version/self-test/packaging smoke；跨发行版兼容性仍以实际系统为准。
- Windows native build/credential/lifecycle 未测试；macOS 未测试。
- M10-C 和 multi-user Slurm Submit 不在本 Alpha。

## R. Internal Alpha Decision

自动化实现、Linux artifact 和安全边界具备受控测试条件，但真实用户验收矩阵不完整，结论为 **INTERNAL_ALPHA_PARTIAL**，不是 INTERNAL_ALPHA_READY。在完成两位用户自己的 key/SSH/Analyze、A/B simultaneous AI、delete-key isolation 和 Linux terminal-close 真人记录前，不应宣传为“已验收”。
