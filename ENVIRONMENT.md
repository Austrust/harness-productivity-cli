# 运行环境与安装

本项目以 Python 源码交付，安装后生成 `mailcal` 和 `ios-reminders` 两个命令。三个系统使用相同的命令和 JSON 协议。Skill 在 `skills/manage-calendar-reminders/`，无需 harness 专用 SDK。

## 基础要求

| 项目 | 要求 |
|---|---|
| Python | 3.11 或更高；当前本机验证版本为 3.12.2 |
| 系统 | Windows、Linux、macOS；本轮在 Windows 验证，Linux/macOS 尚未实机验证 |
| Python 环境 | 推荐独立 venv；包含 pip 和 venv 模块 |
| 网络 | 安装时访问 Python 包索引；运行时 HTTPS 访问所用的 Apple/CalDAV/Microsoft 服务 |
| 账号 | 日历使用 CalDAV 应用密码或 Microsoft 设备码登录；提醒事项使用 Apple 账号及双重认证 |
| 运行身份 | 登录和 harness 使用同一操作系统用户、profile、状态目录；不要以管理员/root 身份代替普通用户运行 |

运行依赖由 `pyproject.toml` 声明，`pip install .` 按当前操作系统安装。`pyicloud` 固定为 2.6.5，因提醒事项使用其 CloudKit 接口。无需 Go、Node、iCloud 桌面客户端或 OpenClaw。Python 依赖中存在本机扩展；若当前 Python/CPU 组合没有预编译依赖，需要对应编译工具链，建议先使用常见平台的 Python 3.12 环境。

## Windows / PowerShell

安装 Python 后，在项目目录执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\mailcal.exe doctor
.\.venv\Scripts\ios-reminders.exe doctor
```

无需激活环境，也无需修改 PowerShell 执行策略。之后直接使用上述两个命令的绝对路径，或将 `.venv\Scripts` 加入 harness 的 PATH。

日历密码和微软 token 缓存使用当前 Windows 用户的 DPAPI 加密，不需要额外安装密钥环服务。默认数据根目录为 `%LOCALAPPDATA%\HarnessProductivity`；没有该变量时为 `~\AppData\Local\HarnessProductivity`。

## Linux / macOS

先确认 `python3 --version` 至少为 3.11。在项目目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
.venv/bin/mailcal doctor
.venv/bin/ios-reminders doctor
```

如果发行版将 venv 拆为独立系统包，先安装对应 Python 版本的 venv 包。无需 `sudo pip`。后续使用 `.venv/bin/mailcal` 和 `.venv/bin/ios-reminders` 的绝对路径，或把 `.venv/bin` 加入 harness 的 PATH。

### macOS 凭证与目录

日历密码和微软 token 缓存存入当前用户的 macOS Keychain。首次登录/访问可能出现系统钥匙串授权提示，由你在本机完成。后台 agent 运行前，应使用同一 Python 环境完成登录和 `status` 检查，并确保登录钥匙串已解锁。

默认数据根目录为 `~/Library/Application Support/HarnessProductivity`。迁移 Python 解释器、操作系统用户或状态目录后，需要重新检查凭证访问，必要时重新登录。

### Linux 凭证与目录

日历密码和微软 token 缓存使用 Freedesktop Secret Service。Python 侧的 `keyring`、`SecretStorage` 会随项目安装；操作系统还需提供已启动的 Secret Service 服务，例如 GNOME Keyring，以及可访问的用户 D-Bus session。不是仅安装 Python 依赖就能获得系统密钥环服务。

桌面会话中先解锁登录密钥环，再执行 auth。CLI 不自动弹出 Linux 密钥环解锁窗口：服务不可用或密钥环被锁时，返回 `credential_store_unavailable`，不退回明文文件。

默认数据根目录为 `$XDG_DATA_HOME/HarnessProductivity`；XDG_DATA_HOME 未设置或不是绝对路径时，使用 `~/.local/share/HarnessProductivity`。用户目录权限为 0700，配置文件写入权限为 0600。

### Linux 无桌面 / SSH / systemd

日历 CLI 仍需要持久的用户 D-Bus session 和已解锁的 Secret Service 密钥环。仅通过 SSH 或 systemd 启动进程不保证这些条件存在。按 [keyring 官方无桌面环境说明](https://keyring.readthedocs.io/en/stable/#using-keyring-on-headless-linux-systems) 配置服务和解锁，再让 auth 与 agent 继承同一 session 的 `DBUS_SESSION_BUS_ADDRESS`；不要把密钥环密码写进聊天、Skill 或命令参数。

配置完成后，先在 agent 的实际运行环境执行 `mailcal --profile NAME status`。不要只在桌面终端检查成功后就认为 systemd/SSH 一定可用。macOS 后台服务同样需要可访问的用户钥匙串。

`ios-reminders` 使用 Apple 会话文件，不读取上述日历密钥环，但首次 Apple 双重认证仍需你在终端完成。会话失效时需要重新登录。会话文件具有账号访问能力，整个状态目录只供该用户访问，不要同步进 Git 或复制进 Skill。

## 接入 WorkBuddy / OpenClaw / 其他 harness

1. 将 `skills/manage-calendar-reminders` 文件夹放入目标 harness 的 Skill 目录，按该产品自身方式加载。
2. 把本项目 venv 的命令目录加入 harness 的 PATH，或配置环境变量 `HARNESS_PRODUCTIVITY_BIN` 为该目录的绝对路径。这个变量是 Skill 的发现约定，由 agent 据此拼接命令路径；CLI 自身不读取它。
3. 在同一 OS 用户下完成 README 中的本地登录、通知校准和 `status` 检查。确保 harness 子进程继承所需的 PATH、Linux D-Bus 环境和代理设置。
4. 后台执行建议传递命令绝对路径与 UTF-8 JSON 文件的绝对路径。无需先执行 activate，不依赖 harness 的当前工作目录。

目录举例：Windows 为 `D:\Tools\productivity-cli\.venv\Scripts`，Linux 为 `/home/me/tools/productivity-cli/.venv/bin`，macOS 为 `/Users/me/tools/productivity-cli/.venv/bin`。含空格的路径应作为单独参数传递。PowerShell 用 `& "绝对命令路径" 参数` 调用；POSIX shell 用 `"绝对命令路径" 参数`。

`--state-dir ABSOLUTE_ROOT` 和 `--profile NAME` 都放在子命令前。相同账户的不同 harness 应指向同一状态目录和 profile，才能共享幂等操作日志和进程锁。跨系统部署时分别登录，不复制 DPAPI/Keychain 或 Apple 会话作为通用登录方案。

## 离线自检与开发验证

`doctor` 加载 Python 依赖并检查 CloudKit 类型，不访问账号，不验证密钥环可用性、云端登录或手机同步。账号连接使用 `status` 检查；写入结果由业务命令的服务器回读验证。

```sh
# Windows 把 .venv/bin/python 换成 .\.venv\Scripts\python.exe
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

系统更新或更换 Python 后，建议重新创建 venv、安装依赖并运行两个 doctor，再执行 status。不要从其他操作系统复制整个 venv。
