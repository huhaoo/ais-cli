# ais — AI CLI Switcher

统一管理并一键切换 **OpenAI Codex CLI** 和 **Claude Code** 的 Provider 配置。

- 纯文本配置（可用 vim / git / rsync 直接管理），无数据库
- 无常驻进程，不做 API 转发 / model routing / 用量统计
- 永不触碰官方登录凭据（`~/.codex/auth.json`、`~/.claude/.credentials.json`）
- 所有写入：先备份 → 校验格式（TOML/JSON）→ 临时文件 → 原子 rename

## 安装

需要 Python ≥ 3.10（3.11+ 用标准库 `tomllib`，3.10 自动回退到 `tomli`）。

```bash
git clone https://github.com/huhaoo/ais-cli.git
cd ais-cli
pipx install .          # 推荐
# 本地开发：editable 安装，ais 直接跟踪仓库源码
pipx install --editable .
# 或传统 venv
python3 -m venv .venv && .venv/bin/pip install -e .
```

## 快速上手

```bash
# 1. 先用官方方式登录一次（ais 不会动这些凭据）
codex login

# 2. 手头正在用某个第三方 provider？把它存成 profile
ais codex save current-work
ais claude save current-work

# 3. 以后一键切换
ais codex use current-work
ais codex official          # 回到官方登录（等价于 clear）
ais codex run current-work  # 切换并直接启动 codex

# 4. 保存当前正在用的 claude 配置
ais claude save current-work
ais claude use current-work
ais claude clear
ais claude official
```

## 命令一览

| 命令 | 作用 |
|---|---|
| `ais codex\|claude list` | 列出 profile（当前项带标记） |
| `ais codex\|claude current` | 当前生效的 profile；手动改过会显示 `(modified)`，文件没了显示 `(missing)`，未被 ais 管理显示 `unmanaged` |
| `ais codex\|claude use <p>` | 切换到 profile（`use official` = 回官方） |
| `ais codex\|claude save <p> [--force]` | 把当前 live 配置存成 profile；已存在默认拒绝覆盖；permissions / 目录信任等本地键不入 profile |
| `ais codex\|claude delete <p> [--force]` | 删除 profile（正在使用时需 `--force`） |
| `ais codex\|claude show <p>` | 打印 profile 内容 |
| `ais codex\|claude clear` | 删除 live 配置（先备份），回到官方登录 |
| `ais codex\|claude official` | 同 `clear`：App 回到默认配置 + 官方登录凭据 |
| `ais codex\|claude run <p>` | `use <p>` 后直接启动 `codex` / `claude`，退出不自动切回 |
| `ais codex\|claude edit <p>` | 用 `$EDITOR` 编辑 profile 主配置 |
| `ais status` | 两个 App 的 current / 官方登录检测 / profile 数 / 署名隐藏开关 |
| `ais attribution [on\|off]` | 隐藏 git commit 里的 AI 署名（默认开启；省略参数 = 查看当前状态） |
| `ais doctor` / `ais validate` | 全面体检 / 语法校验（坏配置退出码 1） |
| `ais backup` | 手动备份当前 live 配置 |

## Shell 补全（Tab）

```bash
ais --install-completion    # 自动识别 bash/zsh/fish，写入 shell 配置
exec bash                   # 或重开终端（zsh 需已启用 compinit）
```

之后：

- `ais <TAB>` → 列出 codex / claude / status / doctor / validate / backup
- `ais codex <TAB>` → 列出子命令
- `ais codex use <TAB>` → **动态列出你现有的 profile 名 + `official`**（实时读取 `~/.config/ais/`，前缀自动过滤）

默认按一下 Tab 补全公共前缀、按两下列出候选；想让 Tab 在候选间循环选择，可在 `~/.inputrc` 加 `TAB: menu-complete`（这是 readline 全局设置，影响所有命令）。

## 文件布局

```
~/.config/ais/
├── state.json                     # 每个 App 的 current + 最后写入内容的 sha256
├── settings.json                  # ais 自身设置（隐藏 AI 署名开关等；机器本地，不参与同步）
├── backups/                       # 每次修改 live 配置前的时间戳备份（保留最近 50 份）
├── codex/
│   └── <profile>/
│       ├── config.toml            # 完整 Codex 配置，字段不限，未来新字段照用
│       ├── models.json            # 可选：原样保存、原样使用，不重写不丢字段
│       └── env.json               # 可选：run 时注入的环境变量（{"NAME": "value"}）
└── claude/
    └── <profile>/
        ├── settings.json          # 完整 settings.json，未知字段原样保留
        └── env.json               # 可选
```

`state.json` 不盲信：`current` 会对比 live 配置的内容 hash，手动改过立刻能看出来。

## Codex models.json 的可移植性

`save` 时如果 `config.toml` 里的 `model_catalog_json` 指向一个存在的本地文件：

1. 该文件被**原样复制**为 profile 内的 `models.json`（不解析、不重写、未知字段不丢）；
2. profile 内的 `config.toml` 改写为 `model_catalog_json = "models.json"`（可移植占位）；
3. `use` 时再把它改写为该 profile 的绝对路径，其余内容逐字保留（注释也在）。

`model_catalog_json` 指向不存在的文件时保留原路径并给出警告。

## env.json

环境变量无法在 `ais use` 退出后留在你的 shell 里，因此 profile 的 `env.json`
只在 `ais codex run <p>` / `ais claude run <p>` 启动子进程时注入：

```json
{"OPENAI_API_KEY": "sk-...", "HTTPS_PROXY": "http://127.0.0.1:7890"}
```

## 隐藏 AI 署名（默认开启）

两个 CLI 替你写 git commit / PR 时会附加 AI 署名：

- **Codex**：commit 尾部的 `Co-authored-by: Codex <noreply@openai.com>`，PR 描述里的
  `Generated with [Codex](https://openai.com/codex/).`
- **Claude Code**：commit 尾部的 `Co-Authored-By: Claude <noreply@anthropic.com>` 与
  "Generated with Claude Code" 落款

`use` / `run` 写入 live 配置时，ais 默认顺带关闭它们（键名取自各 CLI 官方配置，
已在 Codex 0.155 / Claude Code 2.1 验证）：

| App | 注入内容 |
|---|---|
| codex | `[features] commit_attribution_enabled = false`（逐字保留其余内容与注释） |
| claude | `includeCoAuthoredBy = false`（旧键，老版本也认）+ `attribution.commit = ""`（2.1+ 新键，空字符串 = 整条落款含尾注一起隐藏） |

```bash
ais attribution        # 查看当前状态（on / off）
ais attribution off    # 关闭：use 原样写入 profile，不做任何改写
ais attribution on     # 重新开启（默认）
```

- 开关存于 `~/.config/ais/settings.json`，机器本地，**不参与 sync**。
- `save` 存 profile 前会把注入的键剥离，profile 始终是你自己的内容；你自己手写的
  同名键（如 `commit_attribution_enabled = true`、自定义 `attribution`）原样保留，
  仅在 `use` 且开关为 on 时被覆盖为隐藏值。
- `clear` / `official` 同样生效：删除 provider 覆盖后，开关为 on 时会写入一份
  **仅含隐藏键**的最小 live 配置（不含其他任何内容），官方登录下的 commit 也不带
  署名；开关为 off 时彻底删除 live 配置，App 完全回到自身默认。
- 注入在内存中完成，写盘前同样经过 TOML / JSON 校验，失败即拒绝切换。

## 本地设置不入 profile

有些键描述的是**这台机器**而不是 provider：Claude Code 的 `permissions`
（allow / deny / ask 规则、`defaultMode`、`additionalDirectories` 允许目录）与
Codex 的 `[projects]`（目录信任）。它们随日常使用不断累积、只在本地有意义，
ais 对它们只做保留、从不搬运：

- `save`：这些键从 profile 中剥离（存盘时有提示）
- `use` / `run`：profile 里的同名键一律丢弃，**沿用当前 live 配置里已有的**
  （此前没有 live 配置则置空）；切换输出会说明保留了哪些
- `clear` / `official`：provider 覆盖照常移除，本地键原样留在 live 配置里；
  只有在隐藏署名关闭、且没有任何本地键时，live 配置才会被彻底删除
- live 配置只剩隐藏键 / 本地键（official 状态）时，`save` 直接拒绝——没有
  provider 内容可存，也不会因此覆盖已有 profile

## 安全模型

- 修改 live 配置（`~/.codex/config.toml`、`~/.claude/settings.json`）前必先备份到
  `~/.config/ais/backups/`；内容先经 `tomllib` / `json` 校验，非法即拒绝，live 不动。
  「隐藏 AI 署名」的注入也发生在校验之前的内存里，产出非法内容同样拒绝写盘。
- `clear` / `official` 的语义固定为"回到官方登录"：删除 ais 管理的 provider 覆盖
  （删除前必有备份），官方登录凭据接管；不存在 baseline，也没有"恢复接管前配置"
  的行为。「隐藏 AI 署名」开启时，删除后会写入一份仅含隐藏键与本机本地键
  （permissions / 目录信任）的最小 live 配置；关闭时若还有本地键也仅保留它们，
  否则彻底删除。无论哪种写法都不含任何 provider 设置。
- `clear` / `official` 不会删除或修改任何官方登录文件。
- API key 按设计明文保存在 profile 里——所以 `~/.config/ais` 别放进公开仓库；
  想版本管理可以 `git init` 后用私有 remote。

## 环境变量

- `AIS_HOME`：把整个"home"重定向到别处（live 配置与 `~/.config/ais` 都随之移动）。
  主要给测试用，正常使用不需要。

## Seafile 同步（可选）

把 provider profiles 打包成**带密码的 AES zip**，通过 Seafile Web API 上传/下载。
适合多台机器共用一套 profile。

```bash
ais sync setup          # 首次：输入 Seafile URL、API token、密码
ais sync push           # 压缩本地 profiles 并上传
ais sync pull           # 下载、解密并替换本地 profiles（会先确认）
ais sync status         # 查看同步配置；--remote 加看远端文件信息
ais sync passwd         # 改密码（自动用新密码重新加密存储的 token）
```

要点：

- **首次输入**：URL、token、密码。之后每次只需输密码（脚本自动化可用
  `AIS_SYNC_PASSWORD` 环境变量）。
- **token 加密存储**：`~/.config/ais/sync.json` 里存的是用密码对称加密后的
  token（AES），明文 token 不落盘；`sync.json` 本身永远不参与同步。
- **只同步 profile**：压缩包只包含 `codex/` 和 `claude/` 下的 profile 目录。
  `state.json`、`backups/`、`sync.json` 都是机器本地文件，不上传。
- **加密格式**：标准 AES zip（WZ_AES）。即使不用 ais，也能用 `7z x -p<密码>`
  手工解开压缩包恢复文件。
- **同步语义**：last-writer-wins。`pull` 会**整体替换**本地两个 profile 目录
  （不是合并），执行前会显示将写入的 profile 并要求确认（`--yes` 跳过）。
  下载的内容解密后逐文件校验（TOML/JSON），任何文件非法则整体放弃，本地不动；
  压缩包内的路径穿越（`../`）成员会被拒绝。
- 修改 URL / token：重跑 `ais sync setup`；改密码：`ais sync passwd`。

## 用量统计（本地日志 + 公开单价估算）

ais 不经过 API 流量，用量从两个 CLI 写在本地的会话日志中还原：

- Claude Code：`~/.claude/projects/*/*.jsonl`（按 message id 去重，跨文件恢复的会话不会重复计数）
- Codex：`~/.codex/sessions/**/rollout-*.jsonl`（优先用逐请求的 `token_usage_record`，
  模型经 `turn_context` 匹配；老版本只有累计 `token_count`，取每个会话最后一次总量）

```bash
ais usage                 # 两个 App 全部历史，按模型列出 token 与估算成本（默认 CNY）
ais usage codex           # 只看 codex（或 claude）
ais usage --days 7        # 最近 7 天
ais usage -c usd          # 以美元输出（--currency cny|usd）
ais usage --json          # 机器可读输出
```

单价来自官方公开牌价，内置在包内 `ais/prices.json` 并标注获取日期与来源，覆盖：

- **OpenAI**（gpt-6-astra、gpt-5.6 系列、gpt-5.3-codex 及历史模型）与 **Anthropic**
  （Fable 5.1 / Opus 5 / Sonnet 5 / Haiku 4.5 及历史模型）——USD 牌价，标准档、短上下文
- **DeepSeek**（deepseek-flash / v4-pro，按**非高峰价**；高峰时段为其 2 倍）
- **智谱 GLM**（glm-5.3 / 5.2 / 5.3-flash，**国内人民币刊例**：¥8/¥2/¥28 等）
- **Kimi / Moonshot**（kimi-k3 / k2.7-code / k2.6 / k2.5 / k2；K2.6 用国内 ¥ 刊例，其余用国际站 USD 价）

每条价格带原生货币标记：显示 CNY 时人民币牌价直接使用、美元牌价才按内置汇率（6.71）换算；
`--currency usd` 时反向。缓存读缺失默认 0.1× 输入价；缓存写加价 1.25× 是 Anthropic 特有
（其条目已显式标注），其余家缓存写按普通输入价。两家 CLI 缓存语义不同，已分别处理：
OpenAI 的 `input_tokens` 是含缓存的总量（统计时已扣除，避免重复计价），Anthropic 的本身不含缓存。

- 想改价或补自定义模型（如第三方中转的实际折扣价）：把 `ais/prices.json` 复制到
  `~/.config/ais/prices.json` 编辑，同名键覆盖、新键追加
- 模型名匹配：先精确，再按 `-`/`.` 边界最长前缀（`claude-sonnet-5-20260101` 会命中 `claude-sonnet-5`）
- 查不到单价的模型（如自定义 provider 的模型）显示 `—`，token 照常统计
- 注意：这是**按官方牌价的估算**；第三方 provider 实际计费可能不同（例如按月订阅）
- 统计只读日志，不联网、不上传任何数据

## 开发

```bash
python3 -m pytest tests/ -q     # 80 个用例，无需真实 codex/claude
```

实现共三个模块：`ais/core.py`（路径 / 原子写 / 备份 / state）、
`ais/apps.py`（两个 App 的适配器与 Codex catalog 改写）、`ais/cli.py`（Typer 命令，
codex/claude 子命令由同一工厂生成，杜绝两份逻辑漂移）。
