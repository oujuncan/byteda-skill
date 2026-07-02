**English** | [简体中文](README.zh-CN.md)

# ByteDa Skill

Generate professional design assets from a single sentence — posters, marketing long
images, social-media graphics, infographics, PPT decks, comic strips, invitations,
daily-sign cards, recruitment posters, WeChat covers, interactive H5, image canvases, and
more. The AI handles everything from copywriting to layout in one shot. Output is organized
into 3 top-level types (`H5` / `APPLICATION` / `IMAGE`); under `H5` an additional `scene`
splits into 11 concrete design categories.

This is an Agent Skill for the [ByteDa](https://byteda.net) MCP service. It ships a
self-contained CLI script that depends only on the Python standard library and calls
ByteDa's MCP JSON-RPC endpoint directly — **no** MCP server needs to be configured in the
host client.

## Layout

```
byteda/
├── SKILL.md                 # Skill spec (the main file the agent reads)
├── README.md                # This file (English)
├── README.zh-CN.md          # Chinese
├── LICENSE                  # MIT
├── .gitignore
├── agents/
│   └── byteda.yaml          # Agent definition
└── scripts/
    └── byteda_cli.py        # Self-contained CLI (stdlib-only)
```

## Prerequisites

- Python 3.8+
- A ByteDa API Key (starts with `mcp_`). See "Getting an API Key" below.

## Getting an API Key

After logging in at https://byteda.net, create and copy an API Key as follows:

1. **Click your avatar** — the user avatar in the bottom-left corner.
2. **Click "API Key"** — in the popup menu (Account Settings / My Orders / My Credits /
   **API Key** / Log Out).
3. **Click "+ New API Key"** — top-right of the API Key page.
4. **Choose a workspace** — in the dialog, pick which workspace the Key belongs to (defaults
   to "the workspace of your phone number"). The API Key is bound to that workspace; the
   works it generates and the credits it consumes all belong to that workspace.
5. **Enter a Key name** — to distinguish multiple Keys, e.g. `cherry-studio`, `claude-code`.
6. **Click "Confirm"** — to finish creating it.
7. **Click copy** — find the newly created Key (starts with `mcp_…`) in the list, click the
   copy button in the API Key column, and paste the full Key into the config below.

> ⚠️ Once created, the Key is shown in plaintext in the list. Keep it safe — do not commit it
> to a repository or expose it in client-side code. If the platform detects that a Key has
> been publicly leaked, it may automatically rotate or invalidate it. To revoke a Key, click
> "Delete" in the "Actions" column of the list.

## Configuring the token

Pick any one method (highest priority first):

1. Pass it once on the command line: `--token mcp_xxx`
2. Environment variable: `export BYTEDA_TOKEN=mcp_xxx` (the script reads this by default)
3. Config file `~/.byteda/config.json`:

   ```json
   {
     "token": "mcp_xxx",
     "base_url": "https://api.byteda.net/byte-da/mcp"
   }
   ```

`base_url` is optional and defaults to the production endpoint
`https://api.byteda.net/byte-da/mcp`; to target a self-hosted or test environment, override
it with `--base-url` or the `BYTEDA_BASE_URL` environment variable.

## Quick start

```bash
# List the top-level appTypes (3) and H5 scenes (11)
python scripts/byteda_cli.py app-types
python scripts/byteda_cli.py scenes

# Generate a recruitment poster (H5 + scene)
python scripts/byteda_cli.py generate \
  --prompt "Recruiting a front-end engineer, company XX Tech, salary 15-25K, location Beijing" \
  --app-type H5 --scene RECRUITMENT

# Upload an asset, then generate based on it
python scripts/byteda_cli.py upload --file ./logo.png        # -> fileId
python scripts/byteda_cli.py generate --prompt "Product long image using the brand colors" --file-id <fileId>

# Query design styles and generate with a chosen style
python scripts/byteda_cli.py styles --name "商务"            # -> style id
python scripts/byteda_cli.py generate --prompt "Year-end summary PPT" --app-type H5 --scene PPT --style-id <id>

# Iterate (reuse the appId returned last time)
python scripts/byteda_cli.py generate --app-id 12345 --prompt "Enlarge the title, switch to warm tones"

# Generate and download the result locally (HTML + image assets bundled together,
# openable offline; the directory is created automatically)
python scripts/byteda_cli.py generate --prompt "Summer sale long image, everything 50% off" --out ./outputs/sale.html
# Produces outputs/sale.html and outputs/sale_assets/ (image assets); add --no-assets to save HTML only
```

## Installing as an Agent Skill

Place the `byteda/` directory under your agent's skills directory, e.g.:

- Claude Code: `~/.claude/skills/byteda/` or, per-project, `.claude/skills/byteda/`

The agent reads the `SKILL.md` frontmatter (`name` / `description`) to decide when to
trigger, and follows the workflow in it to invoke `scripts/byteda_cli.py`.

## Connecting as an MCP server directly (optional)

If your client natively supports MCP, you can skip the script and configure the server
directly:

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

## Tool overview

| CLI subcommand | MCP tool | Description |
|----------------|----------|-------------|
| `generate` | `generate_app` | Generate / iterate a design asset; returns appId, url, cost |
| `upload` | `upload_file_base64` | Upload a local file as a reference asset |
| `styles` | `query_styles` | Paginated query of design styles |
| `style` | `get_style` | Full detail of a single style |
| `files` | `get_files` | Query details of uploaded files |

## Notes

- `generate` uses SSE streaming by default, printing progress (`[10%]…[95%]`) to stderr in
  real time; it typically takes 1-3 minutes. Add `--no-stream` to fall back to a single
  synchronous request. Configure a long enough timeout (the script defaults to 300s;
  `--timeout 0` disables the limit).
- Each `generate` consumes credits; the exact amount is in the returned
  `cost.consume_points` (gift credits are spent first). Iterative edits are cheaper than the
  first generation.
- `--token` shows up in the process list / shell history — prefer the `BYTEDA_TOKEN`
  environment variable or `~/.byteda/config.json` (recommend `chmod 600`).
- `upload` is capped at 8MB by default (`--max-mb` to adjust); the entire base64 string
  rides inside the JSON-RPC body, so use the platform's multipart upload for large files.
- Tokens expire after 30 days by default; recreate them on the platform once expired.

## License

MIT
