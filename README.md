# 跨平台日历与 iOS 提醒事项 CLI

[![Tests](https://github.com/Austrust/harness-productivity-cli/actions/workflows/test.yml/badge.svg)](https://github.com/Austrust/harness-productivity-cli/actions/workflows/test.yml)

两个命令：`mailcal` 管理邮箱日历；`ios-reminders` 管理 iPhone 原生“提醒事项”的 iCloud 数据。一个可移植 Skill：`skills/manage-calendar-reminders/`，使用标准 SKILL.md，不依赖任何 harness 的插件 SDK。

提醒事项依赖固定版本 `pyicloud 2.6.5` 的 CloudKit 接口；日历通过标准 CalDAV 或 Microsoft Graph 读写。Graph 支持 Outlook.com 和 Microsoft 365 商业/学校账户，受租户授权策略限制。

| 组件 | 用途 |
|---|---|
| `mailcal` | 创建、查询、修改、删除邮箱日历事项，写入后独立回读验证 |
| `ios-reminders` | 创建、查询、完成、删除 iCloud 原生提醒事项，写入后独立回读验证 |
| [manage-calendar-reminders](skills/manage-calendar-reminders/SKILL.md) | WorkBuddy、OpenClaw 等 harness 共用的 Skill，负责将自然语言请求转为 CLI 调用 |

## 环境与安装

先克隆仓库并进入目录：

```sh
git clone https://github.com/Austrust/harness-productivity-cli.git
cd harness-productivity-cli
```

使用 Python 3.11+ 和独立虚拟环境，执行 `python -m pip install .` 安装两个命令。Windows、Linux、macOS 的完整步骤、系统凭证要求和 harness 配置见 [ENVIRONMENT.md](ENVIRONMENT.md)。项目以源码交付，不提供预编译可执行文件。

安装后，可在配置好 PATH 的终端执行 `mailcal doctor` 与 `ios-reminders doctor`。也可以直接使用虚拟环境中的命令绝对路径，无需激活环境。下面示例假定两个命令已在 PATH 中。

## 先完成本机登录

以下 auth 命令由你在本地终端执行；不要把密码、验证码或 token 发到聊天中。除 auth 外所有业务命令不会等待密码或验证码输入。

### iCloud 日历

在 Apple 账户页面生成应用专用密码，然后执行：

```powershell
mailcal --profile icloud auth --provider caldav --username you@example.com --server https://caldav.icloud.com --timezone Asia/Shanghai
mailcal --profile icloud calendars
```

密码在隐藏提示中输入，存入系统凭证存储：Windows DPAPI、macOS Keychain 或 Linux Secret Service。其他支持 Basic／应用密码认证的 CalDAV 服务可以替换 server 和 username。仅有 IMAP/SMTP 的邮箱不能用于日历；本版本不实现 Google Calendar 的 OAuth 登录。

中国大陆 iCloud 日历也可显式使用 `--server https://caldav.icloud.com.cn`。从 0.2.1 起，Apple HTTPS 域名 `icloud.com` 与 `icloud.com.cn` 之间的地址发现和跳转均受支持；其他 CalDAV 服务仍只信任配置的服务器地址和端口。

如果 0.2.0 报 `Refused CalDAV URL outside the configured trusted hosts`，在仓库目录执行 `git pull --ff-only`，再用原虚拟环境的 Python 执行 `-m pip install --upgrade .`，确认 `mailcal --version` 为 0.2.1 或更高后重试原登录命令。新的拦截错误包含 `configured_origin`、`blocked_origin` 和 `caldav_stage`，只报告域名/端口和请求阶段，不输出用户路径、查询参数或密码。若仍失败，用这三个字段排查实际跳转目标；该错误本身不表示密码错误。

### Outlook / Microsoft 365

1. 在 Microsoft Entra 注册一个你自己的应用。个人 Outlook.com 账户需要应用受众包含个人 Microsoft 账户；组织账户按租户策略配置。
2. 在身份验证设置启用 **Allow public client flows**。添加 Microsoft Graph **委托权限** `Calendars.ReadWrite`。这是公共客户端，不配置 client secret。
3. 使用应用的 Application (client) ID 执行下面命令，按终端显示的网址和设备码登录。组织可能要求管理员同意，或者禁用设备码登录；CLI 会报错，不会绕过策略。

```powershell
mailcal --profile outlook auth --provider graph --client-id YOUR-APPLICATION-CLIENT-ID --tenant common --timezone Asia/Shanghai
mailcal --profile outlook calendars
```

本版本接入 `graph.microsoft.com` 公有云，不接入中国世纪互联或其他主权云端点。Graph 一个日程最多支持一个提醒提前量。

### iOS 提醒事项

iPhone 开启 iCloud“提醒事项”同步；目标列表必须属于 iCloud，而不是其他邮箱账户。

```powershell
ios-reminders auth --apple-id you@example.com --timezone Asia/Shanghai
ios-reminders status
ios-reminders lists
```

这里输入 Apple 账户密码，并按提示输入双重认证验证码，不能使用日历的应用专用密码。密码不保存；会话保存在当前操作系统用户的受限目录。中国大陆 iCloud 账户可在 auth 时增加 `--china-mainland`。Apple 条款、网页访问/安全设置或接口变化可能要求你先在 iCloud 网页完成操作；CLI 不会自动接受条款或修改安全设置。

### 如果需要到点提醒：通知校准

CloudKit 是非公开接口。本实现不猜测 Date 通知字段，而是从你手机创建的一条实际样本读取字段结构：

1. 在 iPhone“提醒事项”的目标 iCloud 列表中新建一条测试提醒，设置明确日期、时间和到点提醒，使用单次事项。
2. 运行 `ios-reminders list --list "实际列表名"`，找到它的完整 `Reminder/...` ID。
3. 执行 `ios-reminders calibrate-alert --source-id "Reminder/完整ID"`。

该命令只读取样本，不改写它。支持 Type=Date、单一 TIMESTAMP 字段匹配样本到期时间的结构；字段不符合已支持范围就拒绝校准，不能保证所有 Apple 账户的通知结构都可用。模板不保存源事项标题、正文、日期或 ID。校准后，创建时的 `notify: true` 会原子写入 Reminder、Alarm、AlarmTrigger 并回读关联和时间。没有校准时，带通知的请求在写入前失败，不会降级为无通知任务。

## 创建并验证

先查询实际日历和列表 ID，修改 `examples/` 中的 JSON。下面命令会真实写入你配置的账户，只在登录后主动执行：

```powershell
mailcal --profile icloud create --input "examples/calendar-create.json"
ios-reminders create --input "examples/reminder-create.json"
```

`request_id` 是一次业务操作的稳定编号。重试必须使用原编号和相同内容；另一个编号表示另一次创建。日历用稳定 UID／Graph transactionId＋隐藏扩展属性，待办用稳定 CloudKit recordName，同时使用 SQLite 操作日志和进程锁，避免多 harness 并发或超时后重复创建。修改已用编号的输入会返回 `request_conflict`。

新增命令不根据本地对象判断成功，会另行请求服务器回读业务字段。无论初次写入响应是否丢失，只在回读匹配时返回 `ok: true` 与 `verification.verified: true`。已保留的请求重放只读验证，不再次提交创建。创建后仍无法确认时保留请求与资源定位信息，以便查证；不要通过换编号盲目重建。

服务器记录校验不等于 iPhone 实机通知验收。`device_notification` 始终为 `not_observed`；手机通知权限、专注模式、网络和同步状态需要你验证。

## 命令与返回约定

`--profile` 和 `--state-dir` 放在子命令之前。默认时区 Asia/Shanghai，所有业务时间输出包含偏移；日期对象明确返回 `all_day`。全天日历 end 为排他日期。JSON 输入推荐用绝对文件路径，避免 Shell 插值。

```text
mailcal: auth status logout capabilities doctor now calendars list get create update delete verify
ios-reminders: auth status logout capabilities doctor now lists list get create complete delete verify calibrate-alert
```

`--help` 显示各命令参数，详细用法也在 Skill 的两个 references 文件中。所有常规命令向 stdout 输出一个 JSON 对象；help/version 是普通文本，交互登录提示在 stderr。

| 退出码 | 含义 |
|---|---|
| 0 | 操作成功；写入命令已完成服务器回读验证 |
| 1 | 输入、配置、权限、冲突或服务端错误 |
| 2 | 需要本机登录或通知校准 |
| 3 | 读到了记录，但字段或通知记录验证不一致 |
| 4 | 写入结果不确定，需要按原请求编号查证 |
| 130 | 用户中断；已提交的写入需要查证 |

`logout` 只清理本机登录态，不全局撤销 Apple/微软授权，不删除操作日志。各账户建议使用不同 profile；登录与 agent 使用同一操作系统用户。各系统的状态路径、凭证存储和 Linux 后台进程的 D-Bus 要求见 [环境说明](ENVIRONMENT.md)。迁移系统或用户后重新 auth。

## 支持边界

- 日历支持单次事项创建、查询、修改、删除及 DISPLAY 提醒；不添加参会人或主动发送邮件，拒绝循环系列修改/删除。
- 原生提醒事项支持创建、查询、完成、删除及经校准的时间通知；不修改已有提醒时间、重复规则、地点提醒或共享权限。
- 两个服务不共享事务。一条微信指令包含两项时，Skill 分别报告两项结果，不把部分成功说成全部成功。
- Google OAuth、Graph 主权云、所有厂商 CalDAV 方言及所有 Apple 私有通知字段不在本次实测覆盖范围。支持的是 Basic-auth CalDAV，而不是任意邮箱地址。
- 测试使用真实序列化/解析层和模拟 HTTP/CloudKit 服务，未使用个人账户。登录、实际云端读写、iPhone 同步与弹通知需要按下文验收。

## 真实账户验收

1. 本地 auth，执行 status，重启终端再次 status，确认无需再次输入密码。
2. 创建日历和待办测试项，检查返回的服务器验证字段，再在 iPhone 查看。
3. 重放相同 JSON，确认返回 `replayed: true` 且手机只有一项。
4. 创建北京时间 08:00 的定时待办和日期型全天待办，确认 all_day 与时间分别正确。
5. 通知校准后创建几分钟后的 notify=true 待办，确认通知记录验证成功，并在 iPhone 实际等到通知。
6. 将 Skill 导入 WorkBuddy／其他 harness，从微信发起一句同时创建日历和待办的指令，检查两项的独立结果。电脑保持运行。

## 开发与验证

开发环境执行 `python -m pip install -e ".[dev]"` 后运行 `python -m pytest -q`，三个系统用法相同。安装与自检命令见 [环境说明](ENVIRONMENT.md)。GitHub Actions 使用 Windows、Linux、macOS 与 Python 3.11/3.12 的六组环境执行离线测试和 CLI 自检，不接入个人账号。

## 依据

- [Apple：第三方应用访问 iCloud](https://support.apple.com/en-us/121539)
- [Microsoft：公共客户端设备码登录](https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens)
- [Microsoft Graph：事件扩展属性](https://learn.microsoft.com/en-us/graph/api/singlevaluelegacyextendedproperty-post-singlevalueextendedproperties?view=graph-rest-1.0)
- [pyicloud 2.6.5](https://pypi.org/project/pyicloud/2.6.5/)：CloudKit 会话、记录类型、CRDT 编解码。私有接口可能变化；本版本固定依赖并通过回读避免把接口异常报成成功。
