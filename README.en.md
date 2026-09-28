**English** | [简体中文](README.md)

# ByteDa Skill

Generate design assets from a single sentence — posters, marketing long images, social-media
graphics, infographics, PPT decks, comic strips, H5 pages, plus single images, short videos and
voice-overs / voice cloning — via the [ByteDa](https://byteda.net) MCP service.

The bundled `scripts/byteda.py` depends only on the Python standard library (3.8+) and talks to
ByteDa's MCP endpoint directly; the host does **not** need an MCP server configured.

## Install

```bash
git clone https://github.com/oujuncan/byteda-skill.git ~/.claude/skills/byteda   # Claude Code
git clone https://github.com/oujuncan/byteda-skill.git ~/.agents/skills/byteda   # Codex
```

## API Key

Create one at https://byteda.net (avatar → API Key → New API Key), then:

```bash
python3 ~/.claude/skills/byteda/scripts/byteda.py login <API_KEY>   # or `login -` to read stdin
python3 ~/.claude/skills/byteda/scripts/byteda.py doctor
```

The key is validated and stored in `~/.byteda/config.json` (mode 0600). Precedence:
`--token` > `$BYTEDA_TOKEN` > config file. Upgrading from v1: remove the `export BYTEDA_TOKEN=…`
line that v1's `set-token` wrote into your shell profile — `login` tells you the exact file and line.

## Usage

```bash
S=~/.claude/skills/byteda/scripts/byteda.py
python3 $S image --prompt "coffee shop opening poster" --ratio 3:4 --ref ./logo.png --out ./outputs
python3 $S video --prompt "the cat turns to the camera" --ref first_frame:./cat.png --duration 5
python3 $S audio --text "Welcome!" --speaker <voiceId>
python3 $S h5    --requirement "opening event long image" --scene LONG_IMAGE
python3 $S brief --prompt "a full launch kit based on this plan" --ref ./plan.pdf
python3 $S wait <taskId>
```

- Generation commands block until the task finishes and print one JSON object on stdout
  (progress on stderr). `--no-wait` submits only.
- Every submission gets an idempotency key; re-run with the same `--idempotency-key` after a
  network failure without being charged twice. A local timeout never cancels the server task —
  resume with `wait <taskId>`.
- Exit codes: `0` done · `1` failed · `2` usage · `3` still running · `4` needs input · `5` auth.
- `byteda.py tools` lists every server tool; `byteda.py call <tool> '<json>'` calls any of them.

## License

MIT
