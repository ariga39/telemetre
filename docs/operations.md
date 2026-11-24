# telemetre 运维手册

本手册覆盖切片三：受限 SSH 投递、定时与单实例作业、预期来源健康检查、中心 SQLite 在线备份与恢复。采集、月报和额度采样的字段口径见 `docs/DESIGN.md`、`docs/agy-usage.md` 和 `docs/quota.md`。

所有命令只用 Python 标准库和系统 `ssh`。绑定文件、主机公钥文件和身份文件不含模型凭据；不要在本手册涉及的任何文件中写入 OAuth token、API key、私钥正文或 provider 原始输出。

## 1. 主机与目录

采集和额度采样必须在该机器实际登录 agy/Codex 的用户下运行，保留其可写 home、登录状态和 CLI 路径。不要用中心的受限服务用户运行源端作业。

源端用户（示例用户 `alpha`）：

```text
~/.gemini/antigravity-cli                          agy 1.5.16 数据根
~/.local/state/telemetre/client.sqlite3            usage 待发队列
~/.local/state/telemetre/quota.sqlite3             额度本地库
~/.config/telemetre/telemetre.env                  机器/账号/绑定 ID/SSH 目标
~/.config/telemetre/bindings.json                  客户端校验用的绑定副本
~/.config/telemetre/quota.json                     本地 agy/Codex 采样配置
~/.ssh/telemetre_usage                             绑定 alpha-usage 的专用密钥
~/.ssh/telemetre_quota_agy                         绑定 alpha-quota-agy 的专用密钥
~/.ssh/telemetre_quota_codex                       绑定 alpha-quota-codex 的专用密钥
~/.ssh/telemetre_known_hosts                       固定中心主机公钥
```

中心（只运行受限服务用户 `telemetre`）：

```text
/etc/telemetre/bindings.json         权威绑定表（root:telemetre 0640）
/etc/telemetre/center.env            中心数据库/备份/健康检查路径
/etc/telemetre/quota-center.json     中心日摘要时区和两个 provider
/var/lib/telemetre/center.sqlite3    中心唯一 SQLite 数据库
/var/backups/telemetre/              在线备份快照
```

每个客户端机器有自己的稳定 `machine` 别名和账号别名。每把 SSH 公钥在中心只绑定一个身份；usage、agy 额度和 Codex 额度使用不同密钥和不同绑定 ID。额度写入权只发给指定采样机。

## 2. 中心绑定与强制命令

`bindings.json` 示例见 `examples/bindings.example.json`。键是绑定 ID，值只包含：

```json
{
  "bindings": {
    "alpha-usage": {
      "machine": "alpha",
      "account": "personal@example.test",
      "kind": "agy_usage"
    },
    "alpha-quota-agy": {
      "machine": "alpha",
      "account": "personal@example.test",
      "kind": "quota_sample",
      "provider": "agy"
    },
    "alpha-quota-codex": {
      "machine": "alpha",
      "account": "personal@example.test",
      "kind": "quota_sample",
      "provider": "codex"
    }
  }
}
```

服务端从被认证的绑定取 `machine/account/kind/provider`；请求体里的同名字段一律不使用。`quota_sample` 必须写 `provider`，`agy_usage` 不得写。源端 `~/.config/telemetre/bindings.json` 只需放本机用到的条目，并保持与中心副本一致；真正的授权以中心文件为准。

中心为专用用户 `telemetre` 的 `authorized_keys` 安装每把公钥自己的强制命令。示例：

```text
restrict,command="/usr/bin/python3 -m telemetre.delivery receive --bindings /etc/telemetre/bindings.json --binding-id alpha-usage --center-db /var/lib/telemetre/center.sqlite3 --max-rows 1000 --max-bytes 262144" ssh-ed25519 AAAA... alpha-usage
restrict,command="/usr/bin/python3 -m telemetre.delivery receive --bindings /etc/telemetre/bindings.json --binding-id alpha-quota-agy --center-db /var/lib/telemetre/center.sqlite3 --max-bytes 262144" ssh-ed25519 AAAA... alpha-quota-agy
restrict,command="/usr/bin/python3 -m telemetre.delivery receive --bindings /etc/telemetre/bindings.json --binding-id alpha-quota-codex --center-db /var/lib/telemetre/center.sqlite3 --max-bytes 262144" ssh-ed25519 AAAA... alpha-quota-codex
```

`restrict` 禁用 PTY、端口转发、agent 转发、X11 和用户 rc；无需再单独放行任何转发。客户端发来的远程命令不会被执行，实际执行的是该公钥的 `command=`。

`/etc/ssh/sshd_config` 至少收紧到：

```text
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
AllowUsers telemetre
PermitTTY no
AllowTcpForwarding no
AllowAgentForwarding no
X11Forwarding no
PermitTunnel no
PermitUserRC no
AuthenticationMethods publickey
MaxAuthTries 2
```

`telemetre` 用户需要能执行强制命令，因此 shell 使用 `/bin/sh`，但只接受上面这些受限公钥。数据库目录归 `telemetre` 所有：

```bash
install -d -o telemetre -g telemetre -m 0750 /var/lib/telemetre /var/backups/telemetre
install -d -o root -g telemetre -m 0750 /etc/telemetre
chown telemetre:telemetre /var/lib/telemetre/center.sqlite3 2>/dev/null || true
chmod 0640 /etc/telemetre/bindings.json /etc/telemetre/center.env
```

## 3. 客户端配置

在该机器实际使用 agy/Codex 的普通用户下生成三把专用密钥，一把密钥只对应一个绑定和一个 provider：

```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/telemetre_usage
ssh-keygen -t ed25519 -N '' -f ~/.ssh/telemetre_quota_agy
ssh-keygen -t ed25519 -N '' -f ~/.ssh/telemetre_quota_codex
```

把公钥交给中心管理员，分别写入 `alpha-usage`、`alpha-quota-agy`、`alpha-quota-codex` 的 `authorized_keys` 行。私钥权限必须是 `0600`；`send`/`send-quota` 会拒绝组或其他用户可读写的身份文件。

agy 1.5.16 的数据根是 `~/.gemini/antigravity-cli`，`agy -p /usage` 还会写入该用户的登录/日志目录，因此源端作业必须以该用户运行并可写自己的 home。Codex 的 `app-server` 也使用该用户的登录状态。systemd 示例因此是 **user unit**，不要套用中心的 `ProtectHome=read-only` 或 `telemetre` 服务用户。若 CLI 不在默认 PATH，编辑 user unit 的 `Environment=PATH=...`，例如把 nvm 的 node bin 或 `~/.local/bin` 放在前面。

固定中心主机公钥，必须先通过带外渠道核对指纹，再把公钥行写入 `known_hosts`。非 22 端口时主机名写成 `[centre.example.test]:2222`：

```text
centre.example.test ssh-ed25519 AAAAC3Nza...
```

客户端传 `--known-hosts`，不要依赖 `~/.ssh/config`：投递命令固定使用 `-F /dev/null`、`StrictHostKeyChecking=yes`、`UserKnownHostsFile=<指定文件>`、`GlobalKnownHostsFile=/dev/null`、`BatchMode=yes`、`IdentitiesOnly=yes`，并关闭所有转发。身份文件不要设口令；定时作业没有交互输入。

源端路径模板是 `examples/telemetre.user.env.example`（复制到 `~/.config/telemetre/telemetre.env`）；中心路径模板是 `examples/telemetre.center.env.example`（复制到 `/etc/telemetre/center.env`）。手工采集与投递：

```bash
export PYTHONPATH=/opt/telemetre

python3 -m telemetre collect \
  --client-db ~/.local/state/telemetre/client.sqlite3 \
  --machine alpha --account personal@example.test \
  --root ~/.gemini/antigravity-cli

python3 -m telemetre.delivery send \
  --client-db ~/.local/state/telemetre/client.sqlite3 \
  --bindings ~/.config/telemetre/bindings.json \
  --binding-id alpha-usage \
  --ssh-host centre.example.test --ssh-user telemetre \
  --identity ~/.ssh/telemetre_usage \
  --known-hosts ~/.ssh/telemetre_known_hosts
```

额度采样与投递，agy 和 Codex 各用自己的密钥和绑定：

```bash
python3 -m telemetre.quota sample --config ~/.config/telemetre/quota.json --if-due

python3 -m telemetre.delivery send-quota \
  --quota-db ~/.local/state/telemetre/quota.sqlite3 \
  --bindings ~/.config/telemetre/bindings.json \
  --binding-id alpha-quota-agy \
  --ssh-host centre.example.test --ssh-user telemetre \
  --identity ~/.ssh/telemetre_quota_agy \
  --known-hosts ~/.ssh/telemetre_known_hosts

python3 -m telemetre.delivery send-quota \
  --quota-db ~/.local/state/telemetre/quota.sqlite3 \
  --bindings ~/.config/telemetre/bindings.json \
  --binding-id alpha-quota-codex \
  --ssh-host centre.example.test --ssh-user telemetre \
  --identity ~/.ssh/telemetre_quota_codex \
  --known-hosts ~/.ssh/telemetre_known_hosts
```

一次额度请求只带一个样本，中心对单样本事务提交后才回 ACK。超时或 ACK 丢失时同一 `sample_id` 重传，中心按样本 ID 幂等。同一本地额度库里可以同时存在多台机器、多个 provider 或多个账号的样本；每次 `send-quota` 只同步并尝试当前绑定对应的 machine/provider/account，其他样本保持待发。

`--max-bytes` 必须不大于服务端强制命令里的值。默认 262144 字节；`send` 默认每次最多 1000 条 agy 事实，超限会自动缩小批次，单条放不下时保留队列并报错。

## 4. 定时、单实例与硬超时

`examples/systemd/` 区分源端 user unit 和中心 system unit：

源端（以实际登录 agy/Codex 的用户运行）：

- `telemetre-collect.service/.timer`：每小时采集 `~/.gemini/antigravity-cli` 并投递 usage 队列。
- `telemetre-quota.service/.timer`：每 15 分钟按各来源间隔采样；agy 和 Codex 各自使用自己的密钥/绑定投递。

中心（只运行受限用户 `telemetre`）：

- `telemetre-backup.service/.timer`：每天在线备份。
- `telemetre-health.service/.timer`：每 30 分钟检查预期来源。
- `telemetre-quota-daily.service/.timer`：每天 00:17 生成“昨天”的额度摘要，时区和两个 provider 由 `quota-center.json` 决定。

源端安装：

```bash
mkdir -p ~/.config/systemd/user ~/.config/telemetre
cp examples/systemd/telemetre-collect.service examples/systemd/telemetre-collect.timer \
   examples/systemd/telemetre-quota.service examples/systemd/telemetre-quota.timer \
   ~/.config/systemd/user/
install -m 0600 examples/telemetre.user.env.example ~/.config/telemetre/telemetre.env
install -m 0600 examples/bindings.example.json ~/.config/telemetre/bindings.json
$EDITOR ~/.config/telemetre/telemetre.env
systemctl --user daemon-reload
systemctl --user enable --now telemetre-collect.timer telemetre-quota.timer
loginctl enable-linger "$USER"
```

`telemetre-quota.service` 先跑 `quota sample --if-due`，再分别对 `alpha-quota-agy` 和 `alpha-quota-codex` 调用 `send-quota`；每条 `send-quota` 都带 `-` 前缀，因此一个 provider 采样失败或投递失败不会阻止另一个。同一额度库里混有多台机器/多个 provider/多个账号时，`send-quota` 只同步并尝试当前绑定元组（machine/provider/account）的样本，其他样本保持待发。

中心安装：

```bash
sudo cp examples/systemd/telemetre-backup.service examples/systemd/telemetre-backup.timer \
  examples/systemd/telemetre-health.service examples/systemd/telemetre-health.timer \
  examples/systemd/telemetre-quota-daily.service examples/systemd/telemetre-quota-daily.timer \
  /etc/systemd/system/
sudo install -m 0640 -o root -g telemetre examples/telemetre.center.env.example /etc/telemetre/center.env
sudo install -m 0640 -o root -g telemetre examples/quota.center.json /etc/telemetre/quota-center.json
sudoedit /etc/telemetre/center.env
sudoedit /etc/telemetre/quota-center.json
sudo systemctl daemon-reload
sudo systemctl enable --now telemetre-backup.timer telemetre-health.timer telemetre-quota-daily.timer
```

`quota-center.json` 列出 agy 和 Codex 两个 account；`telemetre-quota-daily` 不带 `--date`，因此 `quota daily` 在配置时区里选择昨天，并把摘要写入中心库的 `quota_daily`。需要补生成或查看某一天时显式给日期：

```bash
python3 -m telemetre.quota daily \
  --config /etc/telemetre/quota-center.json \
  --db /var/lib/telemetre/center.sqlite3 \
  --date 2026-11-05 --json
```

每个耗时作业都包一层：

```text
python3 -m telemetre.delivery run --lock <lock> --timeout <秒> -- <实际命令...>
```

`run` 做两件事：`flock` 保证同一锁只有一个实例；到时向整个进程组发 TERM/KILL，返回 124。已有一个实例时后启动者输出 `skipped: already running` 并以 0 退出，不排队、不重复采集。systemd 的 `TimeoutStartSec` 是外层保险，内层 `--timeout` 应更短。

cron 等价示例见 `examples/cron/telemetre.crontab`。cron 没有 systemd 的 `TimeoutStartSec`，因此必须使用 `delivery run --timeout`。

## 5. 健康检查与监控

中心检查预期来源，而不是只列出“已上报”的来源：

```bash
python3 -m telemetre.delivery check \
  --center-db /var/lib/telemetre/center.sqlite3 \
  --expect-usage alpha:personal@example.test \
  --expect-quota alpha:agy:personal@example.test \
  --expect-quota alpha:codex:personal@example.test \
  --max-age-seconds 7200 \
  --format json
```

退出码：

- `0`：所有预期来源健康。
- `3`：有来源从未上报、扫描报告源缺失、最近一次额度尝试失败，或样本超过新鲜度阈值。
- `2`：参数、数据库或绑定配置错误。

`--expect-usage` 和 `--expect-quota` 可重复。没有列出预期来源时，只检查中心已存在的 usage 来源；生产监控应显式列出每台机器/账号，否则“从未上报”不会被发现。JSON 中每项带 `kind`、`machine`、`account`/`provider`、`reason`、`last_success` 等字段。

## 6. 在线备份

中心每次备份都用 SQLite 在线备份接口，不改动运行中的数据库：

```bash
python3 -m telemetre.delivery backup \
  --center-db /var/lib/telemetre/center.sqlite3 \
  --dest /var/backups/telemetre
```

命令先写临时文件，执行 `PRAGMA integrity_check` 和 `PRAGMA foreign_key_check`，再原子改名为独立快照；失败不会留下半成品。输出包含 `path`、`tables`、`row_counts` 和 `integrity`。可随时独立复核：

```bash
python3 -m telemetre.delivery verify-backup --backup /var/backups/telemetre/telemetre-center-....sqlite3
```

快照包含 usage 和 quota 两类表（若中心已写入）。备份目录的保留策略由外部负责，例如用文件系统快照或定期归档；本项目不自动删除旧快照。

## 7. 恢复

恢复只允许写入一个**不存在的新路径**，不会替换运行中的数据库，也不会移动其 `-wal`/`-shm`。先用原子 no-clobber 链接安装，两个并发恢复只有一个能成功；已存在的目标文件保持不变。

```bash
# 1. 停掉所有会写中心的定时器/服务
sudo systemctl stop telemetre-collect.timer telemetre-quota.timer \
  telemetre-backup.timer telemetre-health.timer
sudo systemctl stop telemetre-collect.service telemetre-quota.service

# 2. 恢复到一个全新路径（不能已存在）
python3 -m telemetre.delivery restore \
  --backup /var/backups/telemetre/telemetre-center-....sqlite3 \
  --center-db /var/lib/telemetre/center-restored.sqlite3

# 3. 用恢复库出月报和额度历史，核对后再切换
python3 -m telemetre report --center-db /var/lib/telemetre/center-restored.sqlite3 \
  --month 2026-11 --format json
python3 -m telemetre.quota report --db /var/lib/telemetre/center-restored.sqlite3 --json
```

核对通过后，把 `/etc/telemetre/center.env` 中的 `CENTER_DB` 改为新路径（服务端强制命令里的 `--center-db` 也要同步改），重载并启动定时器。旧库原样保留，供事后检查或手工归档；需要清理时由操作者显式处理。恢复目标已存在时命令直接报错，不会覆盖。

恢复只回到最近一次成功快照。快照之后的中心写入会丢失；客户端的待发队列和 agy 源日志仍在，可重新采集并重放，重放按事实键和样本 ID 幂等。

## 8. 重试、确认与恢复语义

- 客户端收到合法 ACK 才删除 `pending`（usage）或标记 outbox 已送达（quota）。超时、主机公钥失败、非零退出、非 JSON ACK、身份不匹配、输出超限都会保留待发项。
- 中心 usage 接收在事务提交后才输出 ACK；额度接收对单个样本调用 `ingest_observation`，单样本事务提交后才回 ACK。ACK 丢失后重传得到的 `duplicates` 只表示重复，不会增加计数。
- 请求体有行数和字节上限；服务端先读 `max_bytes + 1`，超限整批拒绝。usage 的坏行/坏批次不会写入部分事件。
- 主机密钥校验失败时不要关闭 `StrictHostKeyChecking`；通过带外核对新指纹后更新固定 `known_hosts`。
- 客户端离线时继续采集到本地 SQLite；中心恢复后按顺序重放。源日志被清理后，未暂存的事实无法补回。

## 9. 安全边界

- 身份只来自 SSH 公钥对应的绑定；修改请求 JSON 不能冒充其他机器、账号、provider 或数据类型。
- 正文、提示、工具参数、环境变量、OAuth/API 凭据不进入协议。receiver 对 quota 样本重新验证：拒绝不一致的展示百分比、嵌套/异常形状的 `reset.raw`、未知 anomaly 类别和不支持的字段；错误详情归并为固定安全类别，原样 provider 文本不落库。
- 额度请求只接受一个观察；失败观察不带窗口。坏批次在任何写入前拒绝。
- 客户端固定主机公钥、禁用交互认证和转发；服务端每把公钥独立强制命令、`restrict`、专用用户和受限 sshd。
- 轮换密钥：新公钥先加绑定和 `authorized_keys`，验证后删除旧行；删除密钥即撤销该绑定的写入权。绑定表不含秘密，但仍应限制写权限。

## 10. 故障排查

| 现象 | 检查 |
| --- | --- |
| `Host key verification failed` | 带外核对中心指纹，更新 `--known-hosts`；不要关闭严格校验。 |
| `acknowledgement ... does not match` | 该公钥的强制命令是否用了正确 `--binding-id`；绑定表是否对应同一 machine/account/provider。 |
| `ssh delivery timed out` | 中心接收或网络过慢；待发队列仍在。检查服务端锁等待和备份/维护窗口，再重试。 |
| `batch is ... limit` / `request body exceeds` | 客户端和服务端的 `--max-bytes` 不一致，或单条事实过大；调低 `--max-rows`，保持两端上限一致。 |
| `target database already exists` | 恢复必须选新路径；不要删除运行库来“腾位置”，先停服务并保留旧库。 |
| `check` 退出 3 | 看 JSON 的 `reason`：`no successful scan reported`、`expected source missing`、`latest attempt failed` 或 `stale sample`。 |
| `run ... skipped: already running` | 上一次定时作业未结束；这是单实例保护。检查锁文件和对应服务状态。 |

## 11. 验收

本切片的行为测试全部使用合成数据和本地假 SSH 可执行文件，不读取真实凭据、不访问真实主机。端到端测试覆盖两台客户端、受限投递、ACK 丢失/超时/主机密钥失败后的重试、并发提交、备份恢复和预期来源健康检查：

```bash
PYTHONPATH=/opt/telemetre python3 -m unittest discover -s tests -v
```

真实主机验收时使用隔离的机器/账号别名和专用密钥，按第 2、3 节配置，先运行 `delivery check` 确认来源，再跑一次 `collect` → `send` → `report`。不要用真实账号做自动化写入测试。
