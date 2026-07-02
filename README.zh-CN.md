[English](README.md) | **简体中文**

# ByteDa Skill 百搭技能包

一句话生成专业设计物料 —— 海报、营销长图、社媒图文、科普图解、PPT、条漫、邀请函、日签、招聘海报、公众号封面、可交互 H5、图片画布等，AI 从文案到排版一站式完成。产物分 3 个顶层类型（`H5` / `APPLICATION` / `IMAGE`），H5 下再用 `scene` 细分 11 种设计品类。

这是 [百搭 ByteDa](https://byteda.net) MCP 服务的 Agent Skill。它自带一个仅依赖 Python 标准库的 CLI 脚本，直接调用 ByteDa 的 MCP JSON-RPC 端点，**无需**在宿主客户端里配置 MCP server 即可使用。

## 目录结构

```
byteda/
├── SKILL.md                 # 技能说明（Agent 读取的主文件）
├── README.md                # 英文说明
├── README.zh-CN.md          # 本文件
├── LICENSE                  # MIT
├── .gitignore
├── agents/
│   └── byteda.yaml          # Agent 定义
└── scripts/
    └── byteda_cli.py        # 自包含 CLI（stdlib-only）
```

## 前置条件

- Python 3.8+
- 一个 ByteDa API Key（`mcp_` 开头）。获取步骤见下方「获取 API Key」。

## 获取 API Key

登录 https://byteda.net 后，按以下步骤创建并复制 API Key：

1. **点击头像** — 页面左下角的用户头像。
2. **点击「API Key」** — 在弹出的菜单中选择「API Key」（账户设置 / 我的订单 / 我的积分 / **API Key** / 退出登录）。
3. **点击「+ 新建 API Key」** — API Key 页面右上角。
4. **选择空间** — 弹窗里选择 Key 所属空间（默认是「你的手机号的空间」）。API Key 绑定该空间，生成的作品、消耗的积分都归属此空间。
5. **输入 Key 名称** — 用于区分多个 Key，例如 `cherry-studio`、`claude-code`。
6. **点击「确定」** — 完成创建。
7. **点击复制** — 在列表中找到新建的 Key（`mcp_…` 开头），点击 API Key 列的复制按钮，把完整 Key 粘贴到下方配置中。

> ⚠️ Key 一旦创建即明文展示在列表中，请妥善保管，勿提交到代码仓库或暴露在客户端代码里。平台若检测到 Key 公开泄露，可能自动更换或失效该 Key。需要作废时，在列表「操作」列点「删除」即可。

## 配置令牌

任选一种方式（优先级从高到低）：

1. 命令行一次性传入：`--token mcp_xxx`
2. 环境变量：`export BYTEDA_TOKEN=mcp_xxx`（脚本默认读取）
3. 配置文件 `~/.byteda/config.json`：

   ```json
   {
     "token": "mcp_xxx",
     "base_url": "https://api.byteda.net/byte-da/mcp"
   }
   ```

`base_url` 可选，默认生产地址 `https://api.byteda.net/byte-da/mcp`；如需对接自建或测试环境，用 `--base-url` 或环境变量 `BYTEDA_BASE_URL` 覆盖。

## 快速使用

```bash
# 列出顶层 appType（3 个）与 H5 场景（11 个）
python scripts/byteda_cli.py app-types
python scripts/byteda_cli.py scenes

# 生成一张招聘海报（H5 + scene）
python scripts/byteda_cli.py generate \
  --prompt "招聘前端工程师，公司 XX科技，薪资 15-25K，地点北京" \
  --app-type H5 --scene RECRUITMENT

# 上传素材后基于素材生成
python scripts/byteda_cli.py upload --file ./logo.png        # -> fileId
python scripts/byteda_cli.py generate --prompt "基于品牌色生成产品长图" --file-id <fileId>

# 查询设计风格并指定风格生成
python scripts/byteda_cli.py styles --name "商务"            # -> style id
python scripts/byteda_cli.py generate --prompt "年度总结 PPT" --app-type H5 --scene PPT --style-id <id>

# 迭代修改（复用上次返回的 appId）
python scripts/byteda_cli.py generate --app-id 12345 --prompt "标题放大，换暖色调"

# 生成并把产物下载到本地（HTML + 图片素材一起本地化，离线可直接打开；目录自动创建）
python scripts/byteda_cli.py generate --prompt "夏季促销长图，全场五折" --out ./outputs/sale.html
# 得到 outputs/sale.html 与 outputs/sale_assets/（图片素材）；加 --no-assets 则只存 HTML
```

## 作为 Agent Skill 安装

将 `byteda/` 目录放到你的 Agent 技能目录下，例如：

- Claude Code：`~/.claude/skills/byteda/` 或项目内 `.claude/skills/byteda/`

Agent 会读取 `SKILL.md` 的 frontmatter（`name` / `description`）来决定何时触发，并按其中的 workflow 调用 `scripts/byteda_cli.py`。

## 作为 MCP server 直接对接（可选）

如果你的客户端原生支持 MCP，也可以不走脚本、直接配置 server：

```json
{
  "mcpServers": {
    "byteda": {
      "type": "streamable-http",
      "url": "https://api.byteda.net/byte-da/mcp",
      "headers": { "Authorization": "Bearer mcp_xxx" }
    }
  }
}
```

## 工具一览

| CLI 子命令 | MCP 工具 | 说明 |
|------------|----------|------|
| `generate` | `generate_app` | 生成 / 迭代设计产物，返回 appId、url、cost |
| `upload` | `upload_file_base64` | 上传本地文件作为参考素材 |
| `styles` | `query_styles` | 分页查询设计风格 |
| `style` | `get_style` | 单个风格完整详情 |
| `files` | `get_files` | 查询已上传文件详情 |

## 注意事项

- `generate` 默认走 SSE 流式，实时把进度（`[10%]…[95%]`）打到 stderr，通常耗时 1-3 分钟；加 `--no-stream` 可回退到单次同步请求。请配置足够长的超时（脚本默认 300s，`--timeout 0` 不限）。
- 每次 `generate` 消耗积分，具体在返回的 `cost.consume_points`（赠送积分优先消耗）；迭代修改比首次生成便宜。
- `--token` 会出现在进程列表/history，优先用 `BYTEDA_TOKEN` 环境变量或 `~/.byteda/config.json`（建议 `chmod 600`）。
- `upload` 默认限制 8MB（`--max-mb` 可调），base64 整串走 JSON-RPC body；大文件请改用平台 multipart 上传。
- 令牌默认 30 天过期，过期后在平台重新创建。

## License

MIT
