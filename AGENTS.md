# AGENTS.md

## 项目定位

个人开发的 `jms` CLI + 库（`jms` = JumpServer 缩写；项目名 `jms-cli`，规避 JumpServer 官方名称/商标），用于访问 [JumpServer v4](https://docs.jumpserver.org/zh/v4/) 堡垒机资产：远程执行命令、交互式终端、SFTP 文件传输、rsync 增量同步。这是**客户端自动化工具，不是 JumpServer 服务器本身**。

SSH 风格目标语法：`<asset>[@server]`，省略 `@server` 时用默认服务器。

## 当前状态

- 代码已基本落地（auth/http/assets/backend/transfer/verify/ssh_pipe/cli），按本文件与 DEVELOP.md 继续迭代。
- 项目背景与原型参考（本机路径、迁移清单、禁止项）见 memory，不在此文件、不进 git。

## 技术栈

- Python >= 3.10，`uv` 管理依赖（pyproject 内置清华镜像 index）。
- 依赖：click、requests、websocket-client、paramiko>=4、cryptography、pyotp、rich、PyYAML、platformdirs。
- 配置存储：**YAML**（PyYAML，只用 `yaml.safe_load`/`safe_dump`，勿用 `yaml.load`）+ **platformdirs** 解析路径；`config.py` 按下方 schema 重写（不再用 JSON）。
- src-layout，包名 `jms`（pyproject `[project] name = "jms-cli"`），console script `jms = jms.cli:main`；`ssh-pipe` 是标准 click 命令（`-l`/UNPROCESSED 参数透传 rsync 远端命令），解析在 cli 层、桥逻辑是库函数 `ssh_pipe.run_bridge()`。
- 测试 pytest；lint flake8（`max-line-length=99`，ignore E203/W503）。
- 架构与开发规则见 **DEVELOP.md**（README 是英文上线面）。

## 命令一览

```bash
uv sync
uv run jms config add <alias>          # 交互式，会先验证凭据
uv run jms ls [@server] [-q keyword]
uv run jms exec <asset>[@server] <cmd...>    # -b ssh|ws|auto(默认)
uv run jms login <asset>[@server]            # 交互 PTY，Ctrl+] 退出
uv run jms sftp <src> <dst>                  # 方向自动检测，-j N 并发
uv run jms ssh-pipe ...                      # rsync/scp 的 -e 桥
uv run jms -l DEBUG|ERROR ...                # 全局日志级别（默认 INFO）
uv run pytest tests/ -v
uv run flake8 src/ tests/
uv build
```

## JumpServer 协议要点（逆向所得，最易踩坑）

1. **双重认证，缺一不可**（`auth.py`）：`POST /api/v1/authentication/auth/` 拿 Bearer token（REST 用）+ `POST /core/auth/login/`（先 GET 拿 csrf）拿 `jms_sessionid` cookie（KoKo WebSocket 用）。仅 API 登录会被 KoKo 拒绝。
2. **MFA**：判定同时查 `data.code == "mfa_required"` 和 `data.error == "mfa_required"`（不同 JumpServer 版本字段不同）；有 `otp_secret` 用 pyotp 自动算，否则 `click.prompt` 交互输入。challenge 走 `/api/v1/authentication/mfa/challenge/`。
3. **连接 token**：REST 创建连接令牌（`protocol="ssh"`, `connect_method="web_cli"`），KoKo SSH 端口 **2222**，SSH 用户 `JMS-{token_id}`、密码 `token_value`。SFTP 与 SSH 终端共用这套机制，token 认证绕过 MFA。
4. **WebSocket 终端**：`/koko/ws/token/` 在某些版本 404，必须用 `/koko/ws/terminal/`（带 session cookie）。终端输出是**二进制帧**（opcode 2），命令输入是文本帧 JSON `{"type":"TERMINAL_DATA","data":"cmd\r"}`。账号选择用 **alias**（`@USER`）不是显示名。
5. **WS 执行标记**：命令 marker 出现两次（一次回显一次 echo），等 count >= 2；每次 execute 后清空残余 WS 数据。WS 协议 scheme 随 HTTP 升降（https→wss）。
6. **keepalive**：KoKo 有四层超时。WS 用**应用层 PING 文本帧每 30s**（Nginx WS 反代是透明 TCP 隧道，WS opcode 0x9 无效，不要用）；SSH 用 `transport.set_keepalive(30)`。
7. **并行 SFTP**（`transfer.py`）：共享一个 `paramiko.Transport`（token 只消耗一个），每 worker 线程 `SFTPClient.from_transport()` 拿独立 channel；大文件（>256MB）分块，先预分配目标文件再按 offset seek 写 `r+b`。`IOOpener` 抽象统一 本地/远程/中继 三种 I/O。跨服务器中继 = 内存流式，不经本地磁盘。
8. **校验**（`verify.py`）：传输后远程 md5 与本地 md5 比对（RemoteHasher 走 SSH exec）。
9. **HTTP 重试**：requests.Session 挂 urllib3 Retry(total=3, backoff 0.5, status_forcelist 502/503/504, 所有方法)。WS/SSH 握手失败自动重建 token 重试一次。

## 凭据与配置

- 只存 `config.yaml`（**无 .env**）：路径由 `platformdirs.user_config_dir("jms")` 解析——macOS `~/Library/Application Support/jms/`，Linux `~/.config/jms/`（即 XDG 规范）。不自动发现 `./config.yaml`；各命令另有隐藏的 `--config <path>` 选项可显式指定。
- 密码/OTP secret 沿用 AES-256-GCM，PBKDF2 从 `host+username` 派生密钥（`crypto.py`，密文带 `enc:v1:` 前缀）；保存后 `chmod 0600`。
- 多服务器，首个添加的自动成为 default；`config add` 会先连服务器验证凭据。

### config.yaml schema（v1.0）

```yaml
version: 1.0                # schema 版本，向后兼容用
default: prod               # 默认服务器 alias；首个添加的自动成为 default
servers:
  prod:                     # alias → 单服务器配置映射
    host: jump.example.com  # IP/域名，或完整 http(s):// URL
    username: alice         # 登录账号
    password: enc:v1:...    # AES-GCM 密文；明文也接受（is_encrypted() 判定）
    otp_secret: enc:v1:...  # 可选；TOTP secret，缺省空串、MFA 时交互输入
```

约束：`servers` 必须为非空映射，`host`/`username`/`password` 必填（缺失报 `ConfigError`），`otp_secret` 可选；`base_url` 由 `host` 派生（带协议则原样，否则补 `https://`）。YAML 是 JSON 超集，字段结构与原型一致，仅文件扩展名与 load/save 函数改为 YAML 实现。

## 风格

- 所有函数/变量类型注解；公开方法 Google 风格 docstring；配置对象用 `frozen=True` dataclass。
- 测试不要碰交互式终端（需真实 PTY）；传输测试用 mock/临时文件。

## Git 工作流

- 一个 feature/修复一个 commit，不批量混改；commit 信息带模块名（如 `terminal: add heartbeat keepalive`）。
- 发布用 `vX.Y.Z` annotated tag；`scripts/pre-push` hook 在 push tag 时自动 `uv build` 并校验版本与 `pyproject.toml` 一致，不一致 abort push。
- hook 安装：`cp scripts/pre-commit .git/hooks/pre-commit && chmod +x ...`（pre-push 同理）。
