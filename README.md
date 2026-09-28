[English](README.en.md) | **简体中文**

# ByteDa Skill

一句话生成设计物料：海报、营销长图、社媒图文、科普图、PPT、条漫、H5 页面，以及单张图片、短视频、配音 / 音色克隆。

这是 [百搭 ByteDa](https://byteda.net) 的 Agent Skill，可装进 Claude Code、Codex、Cursor 等支持 Skill 的 AI 编程工具。
自带的 `scripts/byteda.py` 只依赖 Python 标准库，直接调用百搭 MCP 服务，宿主**不需要**配置 MCP。

## 目录

```
byteda/
├── SKILL.md            # 给 Agent 读的技能说明（路由、流程、失败处理）
├── scripts/byteda.py   # 零依赖 CLI（Python 3.8+）
├── agents/byteda.yaml  # Agent 运行时入口定义
├── README.md / README.en.md
└── LICENSE
```

## 安装

把整个目录放进宿主的 skills 目录：

```bash
# Claude Code（用户级）
git clone https://github.com/oujuncan/byteda-skill.git ~/.claude/skills/byteda
# Codex
git clone https://github.com/oujuncan/byteda-skill.git ~/.agents/skills/byteda
```

升级：进入目录后 `git pull`。

## 获取并配置 API Key

1. 登录 [byteda.net](https://byteda.net)，点左下角 **头像 → API Key → 新建 API Key**，选择 Key 归属的空间（产物和积分消耗都记在这个空间）。
2. 复制 Key，执行：

```bash
python3 ~/.claude/skills/byteda/scripts/byteda.py login <API_KEY>
# 不想让 Key 进 shell 历史，就从 stdin 输入：
python3 ~/.claude/skills/byteda/scripts/byteda.py login -
```

`login` 会先校验 Key，再写入 `~/.byteda/config.json`（权限 0600）。读取优先级：`--token` > 环境变量 `BYTEDA_TOKEN` > 配置文件。
执行 `byteda.py doctor` 可查看当前生效的 Key 来源和剩余积分。

> **从 v1 升级**：v1 的 `set-token` 会把 Key 以 `export BYTEDA_TOKEN=…` 的形式写进 `~/.zshrc` / `~/.bashrc`，
> 它的优先级高于配置文件。`login` 检测到后会提示具体的文件和行号，请手动删除那一行，然后重开终端。

## 用法

一般不用手动敲命令，直接对 Agent 说"帮我做一张咖啡店开业海报"即可。手动调用示例：

```bash
S=~/.claude/skills/byteda/scripts/byteda.py
python3 $S image --prompt "咖啡店开业海报，暖棕色调" --ratio 3:4 --ref ./logo.png --out ./outputs
python3 $S video --prompt "小猫转头看向镜头" --ref first_frame:./cat.png --duration 5
python3 $S audio --text "欢迎光临" --speaker zh_female_vv_uranus_bigtts
python3 $S h5    --requirement "开业活动长图：品牌故事、招牌饮品、优惠" --scene LONG_IMAGE
python3 $S brief --prompt "按这份方案做一套开业物料" --ref ./plan.pdf
python3 $S wait <taskId>
```

完整命令列表见 `byteda.py --help`；服务端全部工具可以用 `byteda.py tools` 查看，用 `byteda.py call <工具> '<json>'` 直接调用。

### 行为约定

- 生成类命令默认阻塞到任务结束，stdout 输出一个 JSON 结果，stderr 输出进度。`--no-wait` 只提交不等待。
- 每次提交都会自动生成幂等键并回显。遇到网络中断时，带上同一个 `--idempotency-key` 重跑，不会重复扣费。
- 每条命令有总耗时预算（默认 540 秒，包括上传、排队和等待；`--timeout 0` 表示不限），预算要比宿主的命令超时短。
  用完预算时服务端任务不会中断，退出码为 3，用 `wait <taskId>` 可以继续等。
- 空间并发满、画布正忙、请求太频繁这几种情况，服务端还没建任务、也没扣费，脚本会在预算内自动重试。
- 网关出现 502/503/504 或网络中断时，报错里会给出 `rerunWith`：原样重跑命令并追加这段参数，就能拿回原任务，不会重复扣费。
- 退出码：`0` 完成 · `1` 失败 · `2` 参数错误 · `3` 仍在运行 · `4` 需要补充信息 · `5` Key 缺失或无效。

## 不想用脚本？直接配置 MCP

支持远程 MCP 的客户端（Claude Code、Cursor、Codex、Cherry Studio 等）也可以直接接入：

```bash
claude mcp add --transport http byteda https://api.byteda.net/byte-da/mcp \
  --header "Authorization: Bearer <API_KEY>"
```

> 注意：Claude Code 新版会严格校验 `tools/list`，服务端修复（`resultType` 字段）上线前可能显示 `tools fetch failed`，这种情况请先用脚本方式。

区别：直连 MCP 时，上传要自己完成三步直传，任务要靠模型自己轮询；用本技能的脚本，这两件事都一条命令搞定。

## 从 v1 迁移

| v1 | v2 |
|---|---|
| `byteda_cli.py generate --app-type H5 --scene X` | `byteda.py h5 --scene X`，或 `byteda.py brief` |
| `generate --app-type IMAGE` | `byteda.py image` |
| `generate --app-type APPLICATION` | 已下线，改用 `brief` |
| `upload --file`（base64，≤8MB） | `upload` 或 `--ref`（预签名直传：图片 15MB / 视频 50MB / 音频 20MB / 文档 50MB） |
| `set-token`（写入 shell profile） | `login`（写入 `~/.byteda/config.json`，0600） |
| SSE 长连接等待 | 提交后轮询，支持 `wait` 续等和幂等重提 |

## 许可

MIT
