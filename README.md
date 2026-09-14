# ais — AI CLI Switcher

统一管理并一键切换 **OpenAI Codex CLI** 和 **Claude Code** 的 Provider 配置。

- 纯文本配置（可用 vim / git / rsync 直接管理），无数据库
- 无常驻进程，不做 API 转发 / model routing / 用量统计
- 永不触碰官方登录凭据（`~/.codex/auth.json`、`~/.claude/.credentials.json`）
- 所有写入：先备份 → 校验格式（TOML/JSON）→ 临时文件 → 原子 rename

## 安装

需要 Python ≥ 3.10（3.11+ 用标准库 `tomllib`，3.10 自动回退到 `tomli`）。

```bash
git clone https://github.com/USERNAME/ais-cli.git   # USERNAME 替换为实际用户名
cd ais-cli
pipx install .          # 推荐；或 uv tool install .
# 本地开发安装
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
| `ais codex\|claude save <p> [--force]` | 把当前 live 配置存成 profile；已存在默认拒绝覆盖 |
| `ais codex\|claude delete <p> [--force]` | 删除 profile（正在使用时需 `--force`） |
| `ais codex\|claude show <p>` | 打印 profile 内容 |
| `ais codex\|claude clear [--hard]` | 恢复 ais 接管前的 baseline；`--hard` 才删除 live 配置 |
| `ais codex\|claude official` | 同 `clear`（恢复 baseline，官方登录继续有效） |
| `ais codex\|claude run <p>` | `use <p>` 后直接启动 `codex` / `claude`，退出不自动切回 |
| `ais codex\|claude edit <p>` | 用 `$EDITOR` 编辑 profile 主配置 |
| `ais codex\|claude reset-baseline` | 以当前 live 配置重新定义 baseline |
| `ais status` | 两个 App 的 current / 官方登录检测 / profile 数 / baseline 状态 |
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
├── baseline/
│   └── codex/
│       ├── config.toml            # ais 第一次写操作之前的原始 live 配置
│       └── existed.json           # 记录原文件当时是否存在
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

## 安全模型

- 修改 live 配置（`~/.codex/config.toml`、`~/.claude/settings.json`）前必先备份到
  `~/.config/ais/backups/`；内容先经 `tomllib` / `json` 校验，非法即拒绝，live 不动。
- baseline 只在第一次写操作前捕获一次，不会覆盖；`reset-baseline` 显式重建。
- `clear` / `official` / `--hard` 都不会删除或修改任何官方登录文件。
- API key 按设计明文保存在 profile 里——所以 `~/.config/ais` 别放进公开仓库；
  想版本管理可以 `git init` 后用私有 remote。

## 环境变量

- `AIS_HOME`：把整个"home"重定向到别处（live 配置与 `~/.config/ais` 都随之移动）。
  主要给测试用，正常使用不需要。

## 开发

```bash
python3 -m pytest tests/ -q     # 25 个用例，无需真实 codex/claude
```

实现共三个模块：`ais/core.py`（路径 / 原子写 / 备份 / state / baseline）、
`ais/apps.py`（两个 App 的适配器与 Codex catalog 改写）、`ais/cli.py`（Typer 命令，
codex/claude 子命令由同一工厂生成，杜绝两份逻辑漂移）。
