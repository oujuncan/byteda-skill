#!/usr/bin/env python3
"""Self-contained CLI for the 百搭 ByteDa MCP service.

Talks to the ByteDa MCP JSON-RPC endpoint over HTTP using only the Python
standard library. Mirrors the "bundled script" pattern: the skill invokes this
script directly, so no MCP server has to be wired into the host client.

Subcommands map 1:1 to the ByteDa MCP tools:

  generate   -> generate_app          (create / iterate a design asset)
  upload     -> upload_file_base64     (upload a local file as reference)
  styles     -> query_styles           (page through design styles)
  style      -> get_style              (single style detail)
  files      -> get_files              (look up uploaded file detail)

Auth resolution order (first hit wins):
  1. --token <value>
  2. --token-env <ENV_NAME>            (default env: BYTEDA_TOKEN)
  3. BYTEDA_TOKEN environment variable
  4. ~/.byteda/config.json  -> {"token": "...", "base_url": "..."}

Base URL resolution order:
  1. --base-url
  2. BYTEDA_BASE_URL environment variable
  3. config.json "base_url"
  4. https://api.byteda.net/byte-da/mcp   (production default)

`generate` streams over SSE by default (Accept: text/event-stream): the server
pushes notifications/progress events (printed to stderr) and a 15s heartbeat,
which keeps long jobs alive through proxies. Use --no-stream to fall back to a
single synchronous JSON-RPC request.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import mimetypes
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# 视为可本地化的图片素材后缀（CDN 的 css/js、www.example.com 占位等不会被匹配）
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp", ".ico", ".avif")
_URL_RE = re.compile(r"https?://[^\s\"'()<>]+", re.IGNORECASE)

DEFAULT_BASE_URL = "https://api.byteda.net/byte-da/mcp"
DEFAULT_TOKEN_ENV = "BYTEDA_TOKEN"
CONFIG_PATH = Path.home() / ".byteda" / "config.json"
DEFAULT_TIMEOUT = 1200
# base64 上传上限（原始字节，MB）。base64 后体积约 +33%，整串走 JSON-RPC body，
# 服务端 base64 路径无大小校验，大文件请改用平台 multipart 上传。
DEFAULT_UPLOAD_MAX_MB = 8

# appType 只有 3 个顶层类型；具体设计品类由 H5 的 scene 决定（见 SCENES）。
APP_TYPES = {
    "H5": "静态视觉页：长图/海报/社媒图文/PPT/公众号封面等（默认，配合 --scene）",
    "APPLICATION": "可交互应用 / 网页 / H5 小游戏（生成后自动发布上线）",
    "IMAGE": "图片画布 / 纯图片生成与编辑",
}

# H5 场景标识：仅 appType=H5（或不传 appType）时有效，决定尺寸/版式/提示词。
SCENES = {
    "LONG_IMAGE": "内容长图：公众号推文、产品介绍、科普、信息流长图",
    "VERTICAL_POSTER": "竖版海报：活动、产品、单屏竖版视觉",
    "SOCIAL_MEDIA_IMAGE_TEXT": "社媒图文：小红书/朋友圈/微博图文卡片",
    "INFOGRAPHIC": "一图科普：知识科普、流程说明、数据图解",
    "DAILY_SIGN": "日签：每日一句、早晚安、节气、打卡分享",
    "COMIC_STRIP": "条漫：多格漫画、剧情分镜、连续叙事",
    "INVITATION": "邀请函：会议、活动、课程、婚礼、发布会",
    "WECHAT_OFFICIAL": "公众号封面：微信文章头图、封面图",
    "PROMOTIONAL_ACTIVITY": "促销活动：电商促销、优惠券、限时活动、卖点宣传",
    "RECRUITMENT": "招聘：岗位招聘、校招社招、人才招募",
    "PPT": "演示文稿：单页或多页幻灯片",
}


class BytedaError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# config / auth
# ---------------------------------------------------------------------------

def _load_config_file() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise BytedaError(f"无法解析配置文件 {CONFIG_PATH}: {exc}") from exc


def resolve_token(args: argparse.Namespace, config: dict) -> str:
    if args.token:
        return args.token.strip()
    env_name = args.token_env or DEFAULT_TOKEN_ENV
    env_value = os.environ.get(env_name)
    if env_value:
        return env_value.strip()
    cfg_token = config.get("token")
    if cfg_token:
        return str(cfg_token).strip()
    raise BytedaError(
        "未配置 API Key，无法使用 ByteDa。请先到 https://byteda.net"
        "（头像 → API Key → 新建 API Key）创建一个 API Key，然后运行 "
        f"`byteda_cli.py set-token <API_KEY>` 写入环境变量 {DEFAULT_TOKEN_ENV}"
        f"（也可手动 export {DEFAULT_TOKEN_ENV}=... 或写入 {CONFIG_PATH}）。"
    )


def _detect_shell_profile() -> Path:
    """Pick the shell rc file to persist the env var into, based on $SHELL.

    Subsequent shells (including this agent's Bash tool calls, which init from
    the user's profile) will then pick up the exported API Key automatically.
    """
    home = Path.home()
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        return home / ".zshrc"
    if "bash" in shell:
        return home / ".bashrc"
    return home / ".profile"


def write_env_token(token: str, env_name: str) -> Path:
    """Write/overwrite `export <env_name>="<token>"` in the shell profile.

    Any existing export line for the same var is dropped first (覆盖), so the
    file never accumulates stale keys. Returns the profile path written to.
    """
    profile = _detect_shell_profile()
    marker = f"export {env_name}="
    kept: list[str] = []
    if profile.is_file():
        for line in profile.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(marker):
                continue  # 覆盖：丢弃旧的 export 行
            kept.append(line)
    else:
        profile.parent.mkdir(parents=True, exist_ok=True)
    kept.append(f'export {env_name}="{token}"')
    profile.write_text("\n".join(kept).rstrip("\n") + "\n", encoding="utf-8")
    return profile


def resolve_base_url(args: argparse.Namespace, config: dict) -> str:
    base = (
        args.base_url
        or os.environ.get("BYTEDA_BASE_URL")
        or config.get("base_url")
        or DEFAULT_BASE_URL
    )
    base = base.strip()
    if not base.startswith(("http://", "https://")):
        raise BytedaError(f"base_url 必须是 http(s) 地址: {base}")
    return base.rstrip("/")


# ---------------------------------------------------------------------------
# JSON-RPC transport
# ---------------------------------------------------------------------------

def _build_request(base_url: str, token: str, accept: str, payload: dict) -> urllib.request.Request:
    return urllib.request.Request(
        base_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": accept,
            "Authorization": f"Bearer {token}",
        },
    )


def _check_rpc_error(parsed: dict) -> dict:
    if isinstance(parsed, dict) and parsed.get("error"):
        err = parsed["error"]
        raise BytedaError(f"MCP 错误 {err.get('code')}: {err.get('message')}")
    return parsed


def _rpc(base_url: str, token: str, method: str, params: dict | None, timeout: int) -> dict:
    payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method}
    if params is not None:
        payload["params"] = params
    request = _build_request(base_url, token, "application/json", payload)
    try:
        with urllib.request.urlopen(request, timeout=timeout or None) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise BytedaError(f"HTTP {exc.code} {exc.reason}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BytedaError(f"请求失败: {exc.reason}") from exc

    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise BytedaError(f"响应不是合法 JSON: {body[:500]}") from exc
    return _check_rpc_error(parsed)


def _iter_sse_messages(resp):
    """Yield decoded JSON objects from an SSE stream's `data:` payloads."""
    data_lines: list[str] = []
    for raw in resp:
        line = raw.decode("utf-8").rstrip("\n").rstrip("\r")
        if line.startswith(":"):  # comment / heartbeat
            continue
        if line == "":  # event boundary
            if data_lines:
                blob = "\n".join(data_lines)
                data_lines = []
                try:
                    yield json.loads(blob)
                except ValueError:
                    continue
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:  # trailing event without final blank line
        try:
            yield json.loads("\n".join(data_lines))
        except ValueError:
            pass


def _rpc_sse_tool(base_url, token, name, arguments, timeout, on_progress) -> dict:
    """Call a tool over SSE; stream progress notifications, return final response."""
    rpc_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments,
            "_meta": {"progressToken": rpc_id},
        },
    }
    request = _build_request(base_url, token, "text/event-stream", payload)
    try:
        resp = urllib.request.urlopen(request, timeout=timeout or None)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise BytedaError(f"HTTP {exc.code} {exc.reason}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BytedaError(f"请求失败: {exc.reason}") from exc

    final: dict | None = None
    try:
        for msg in _iter_sse_messages(resp):
            method = msg.get("method")
            if method in ("notifications/progress", "notifications/message"):
                if on_progress:
                    on_progress(msg)
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                final = msg  # final JSON-RPC response, keep reading until stream ends
    except (socket.timeout, TimeoutError) as exc:
        # 客户端断开：--timeout 秒内未收到新数据，socket 读超时，连接由本地主动关闭。
        raise BytedaError(
            f"[客户端断开] 读取超时：{timeout}s 内未收到新数据，连接由客户端主动断开。"
            "服务端任务可能仍在继续生成，可加大 --timeout（0 表示不限）或改用 --no-stream。"
        ) from exc
    except (http.client.IncompleteRead, ConnectionError) as exc:
        # 服务端断开（异常）：连接被重置或响应未读完，服务端/上游中途掐断。
        raise BytedaError(
            f"[服务端断开] SSE 连接被中途中断（{type(exc).__name__}）。"
            "服务端任务可能仍在继续生成，请稍后重试或改用 --no-stream。"
        ) from exc
    finally:
        resp.close()

    if final is None:
        # 服务端断开（优雅）：流已正常 EOF，但结束前没发最终结果（上游/服务端提前关流）。
        raise BytedaError(
            "[服务端断开] SSE 流已正常结束（EOF），但未收到最终结果。"
            "服务端任务可能已完成或仍在继续，请稍后重试或改用 --no-stream。"
        )
    return _check_rpc_error(final)


def _unwrap_tool_result(parsed: dict) -> object:
    inner = parsed.get("result", {})
    if isinstance(inner, dict) and inner.get("isError"):
        text = inner.get("content", [{}])[0].get("text", "工具调用失败")
        raise BytedaError(text)
    if isinstance(inner, dict) and "structuredContent" in inner:
        return inner["structuredContent"]
    return inner


def _call_tool(base_url, token, name, arguments, timeout, raw=False) -> object:
    parsed = _rpc(base_url, token, "tools/call", {"name": name, "arguments": arguments}, timeout)
    return parsed if raw else _unwrap_tool_result(parsed)


def _fetch(url: str, timeout: int) -> tuple[bytes, str]:
    """Download a URL, returning (bytes, content_type)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout or None) as resp:
            return resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        raise BytedaError(f"下载失败 HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise BytedaError(f"下载失败: {exc.reason}") from exc


def _is_image_url(url: str) -> bool:
    path = url.split("?", 1)[0].split("#", 1)[0]
    return path.lower().endswith(IMAGE_EXTS)


def _localize_html_assets(html: str, out_path: Path, timeout: int) -> tuple[str, int]:
    """Download image assets referenced in the HTML into <stem>_assets/ and
    rewrite their URLs to local relative paths. Returns (new_html, count)."""
    urls = [u for u in dict.fromkeys(_URL_RE.findall(html)) if _is_image_url(u)]
    if not urls:
        return html, 0

    assets_dirname = out_path.stem + "_assets"
    assets_dir = out_path.parent / assets_dirname
    assets_dir.mkdir(parents=True, exist_ok=True)

    used: set[str] = set()
    saved = 0
    for url in urls:
        base = Path(url.split("?", 1)[0].split("#", 1)[0]).name or "asset"
        name = base
        i = 1
        while name in used:  # 不同 URL 同名时去重
            name = f"{Path(base).stem}_{i}{Path(base).suffix}"
            i += 1
        used.add(name)
        try:
            data, _ = _fetch(url, timeout)
        except BytedaError as exc:
            print(f"  ! 跳过素材 {url}: {exc}", file=sys.stderr)
            continue
        (assets_dir / name).write_bytes(data)
        html = html.replace(url, f"{assets_dirname}/{name}")
        saved += 1
    return html, saved


def _download_asset(url: str, out_path: Path, timeout: int, localize: bool) -> int:
    """Download the generated asset to out_path. If it's an HTML page and
    localize is on, also pull image assets locally. Returns asset count."""
    out_path = out_path.expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data, ctype = _fetch(url, timeout)
    is_html = "html" in ctype.lower() or out_path.suffix.lower() in (".html", ".htm") \
        or _is_html_url(url)
    if is_html and localize:
        html, count = _localize_html_assets(data.decode("utf-8", "replace"), out_path, timeout)
        out_path.write_text(html, encoding="utf-8")
        return count
    out_path.write_bytes(data)
    return 0


def _is_html_url(url: str) -> bool:
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return path.endswith((".html", ".htm"))


def _clean_args(mapping: dict) -> dict:
    """Drop empty values so we never send "", 0, [] to generate_app."""
    cleaned = {}
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, tuple, dict)) and len(value) == 0:
            continue
        cleaned[key] = value
    return cleaned


# ---------------------------------------------------------------------------
# subcommand handlers
# ---------------------------------------------------------------------------

def _print_progress(msg: dict) -> None:
    params = msg.get("params", {})
    if msg.get("method") == "notifications/progress":
        pct = params.get("progress", "?")
        text = params.get("message", "")
        print(f"  [{pct}%] {text}".rstrip(), file=sys.stderr)
    else:  # notifications/message
        data = params.get("data", "")
        print(f"  · {data}", file=sys.stderr)


def cmd_generate(args, base_url, token) -> object:
    app_type = args.app_type.strip().upper() if args.app_type else None
    if app_type and app_type not in APP_TYPES:
        raise BytedaError(
            f"未知 appType: {app_type}。可选: {', '.join(APP_TYPES)}"
        )
    scene = args.scene.strip().upper() if args.scene else None
    if scene:
        if scene not in SCENES:
            raise BytedaError(f"未知 scene: {scene}。可选: {', '.join(SCENES)}")
        if app_type and app_type != "H5":
            raise BytedaError(
                f"scene 仅在 appType=H5（或不传 appType）时有效，当前 appType={app_type}"
            )
    arguments = _clean_args(
        {
            "prompt": args.prompt,
            "appType": app_type,
            "scene": scene,
            "appName": args.app_name,
            "description": args.description,
            "appId": args.app_id,
            "fileIds": args.file_id or None,
            "styleIds": args.style_id or None,
        }
    )
    if "prompt" not in arguments:
        raise BytedaError("generate 需要 --prompt")
    if args.no_stream:
        parsed = _rpc(base_url, token, "tools/call",
                      {"name": "generate_app", "arguments": arguments}, args.timeout)
    else:
        parsed = _rpc_sse_tool(base_url, token, "generate_app", arguments, args.timeout, _print_progress)

    structured = _unwrap_tool_result(parsed)
    if args.out:
        url = structured.get("url") if isinstance(structured, dict) else None
        if not url:
            raise BytedaError("结果中没有 url，无法下载（--out）")
        count = _download_asset(url, Path(args.out), args.timeout, localize=not args.no_assets)
        if count:
            print(f"已保存到 {args.out}（含 {count} 个图片素材 → {Path(args.out).stem}_assets/）",
                  file=sys.stderr)
        else:
            print(f"已保存到 {args.out}", file=sys.stderr)
    return parsed if args.raw else structured


def cmd_upload(args, base_url, token) -> object:
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise BytedaError(f"文件不存在: {path}")
    raw = path.read_bytes()
    max_bytes = args.max_mb * 1024 * 1024
    if len(raw) > max_bytes:
        raise BytedaError(
            f"文件 {len(raw) / 1024 / 1024:.1f}MB 超过 base64 上传上限 {args.max_mb}MB。"
            "大文件请用平台 multipart 上传（https://byteda.net），或用 --max-mb 调高（不建议）。"
        )
    content = base64.b64encode(raw).decode("ascii")
    mime, _ = mimetypes.guess_type(path.name)
    data_url = f"data:{mime or 'application/octet-stream'};base64,{content}"
    arguments = _clean_args(
        {
            "fileName": args.name or path.name,
            "contentBase64": data_url,
            "parseContent": True if args.parse else None,
        }
    )
    return _call_tool(base_url, token, "upload_file_base64", arguments, args.timeout, raw=args.raw)


def cmd_styles(args, base_url, token) -> object:
    arguments = _clean_args(
        {"name": args.name, "pageNo": args.page_no, "pageSize": args.page_size}
    )
    return _call_tool(base_url, token, "query_styles", arguments, args.timeout, raw=args.raw)


def cmd_style(args, base_url, token) -> object:
    return _call_tool(base_url, token, "get_style", {"styleId": args.style_id}, args.timeout, raw=args.raw)


def cmd_files(args, base_url, token) -> object:
    return _call_tool(base_url, token, "get_files", {"fileIds": args.file_id}, args.timeout, raw=args.raw)


def cmd_tools(args, base_url, token) -> object:
    parsed = _rpc(base_url, token, "tools/list", None, args.timeout)
    return parsed if args.raw else parsed.get("result", {})


# ---------------------------------------------------------------------------
# arg parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="byteda",
        description="百搭 ByteDa MCP CLI — 一句话生成专业设计物料。",
    )
    parser.add_argument("--base-url", help=f"MCP 端点，默认 {DEFAULT_BASE_URL}")
    parser.add_argument("--token", help="MCP 访问令牌（mcp_ 开头），一次性使用")
    parser.add_argument("--token-env", help=f"从环境变量读取令牌，默认 {DEFAULT_TOKEN_ENV}")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="超时秒数，0 表示不限")
    parser.add_argument("--raw", action="store_true", help="原样打印完整 JSON-RPC 响应（不拆 envelope）")

    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="生成 / 迭代设计产物 (generate_app)")
    g.add_argument("--prompt", required=True, help="生成需求描述")
    g.add_argument("--app-type", help="产物类型 APPLICATION/H5/IMAGE，省略默认 H5")
    g.add_argument("--scene", help="H5 场景（仅 appType=H5 或不传时有效），如 LONG_IMAGE/RECRUITMENT/PPT")
    g.add_argument("--app-name", help="作品名称")
    g.add_argument("--description", help="作品描述")
    g.add_argument("--app-id", type=int, help="迭代修改时传上次返回的 appId")
    g.add_argument("--file-id", action="append", help="参考素材 fileId，可重复")
    g.add_argument("--style-id", action="append", type=int, help="设计风格 id，可重复")
    g.add_argument("--no-stream", action="store_true", help="禁用 SSE，走单次同步请求（无进度）")
    g.add_argument("--out", help="把生成产物（HTML/图片）下载到该路径；HTML 会连同图片素材一起本地化")
    g.add_argument("--no-assets", action="store_true",
                   help="配合 --out：只存 HTML 原文，不下载图片素材（图片仍指向远程）")
    g.set_defaults(func=cmd_generate)

    u = sub.add_parser("upload", help="上传本地文件作为参考素材 (upload_file_base64)")
    u.add_argument("--file", required=True, help="本地文件路径")
    u.add_argument("--name", help="覆盖文件名（含扩展名）")
    u.add_argument("--parse", action="store_true", help="解析文档正文（doc/pdf/ppt）")
    u.add_argument("--max-mb", type=float, default=DEFAULT_UPLOAD_MAX_MB,
                   help=f"base64 上传大小上限 MB，默认 {DEFAULT_UPLOAD_MAX_MB}")
    u.set_defaults(func=cmd_upload)

    s = sub.add_parser("styles", help="分页查询设计风格 (query_styles)")
    s.add_argument("--name", help="风格名称模糊搜索")
    s.add_argument("--page-no", type=int, help="页码，默认 1")
    s.add_argument("--page-size", type=int, help="每页条数，默认 100")
    s.set_defaults(func=cmd_styles)

    d = sub.add_parser("style", help="单个风格详情 (get_style)")
    d.add_argument("--style-id", required=True, type=int, help="风格 id")
    d.set_defaults(func=cmd_style)

    f = sub.add_parser("files", help="查询已上传文件详情 (get_files)")
    f.add_argument("--file-id", required=True, action="append", help="fileId，可重复")
    f.set_defaults(func=cmd_files)

    sub.add_parser("tools", help="列出服务端工具 (tools/list)").set_defaults(func=cmd_tools)

    sub.add_parser("app-types", help="列出 appType 枚举（APPLICATION/H5/IMAGE）").set_defaults(func=None)

    sub.add_parser("scenes", help="列出 H5 scene 枚举").set_defaults(func=None)

    st = sub.add_parser("set-token", help="把 API Key 写入/覆盖 shell 环境变量（持久化）")
    st.add_argument("key", nargs="?", help="API Key（mcp_ 开头）；省略则取全局 --token")
    st.set_defaults(func=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "app-types":
        for code, label in APP_TYPES.items():
            print(f"{code:<24} {label}")
        return 0

    if args.command == "scenes":
        for code, label in SCENES.items():
            print(f"{code:<24} {label}")
        return 0

    if args.command == "set-token":
        key = (args.key or args.token or "").strip()
        if not key:
            print("错误: set-token 需要 API Key，用法: byteda_cli.py set-token <API_KEY>",
                  file=sys.stderr)
            return 1
        env_name = args.token_env or DEFAULT_TOKEN_ENV
        try:
            profile = write_env_token(key, env_name)
        except OSError as exc:
            print(f"错误: 写入环境变量失败: {exc}", file=sys.stderr)
            return 1
        print(f"已将 API Key 写入/覆盖环境变量 {env_name}（{profile}）。", file=sys.stderr)
        print(f"当前 shell 立即生效请执行: source {profile}", file=sys.stderr)
        return 0

    try:
        config = _load_config_file()
        token = resolve_token(args, config)
        base_url = resolve_base_url(args, config)
        if args.command == "generate":
            print("正在生成中，通常需要 1-3 分钟，请稍候…", file=sys.stderr)
        started = time.monotonic()
        result = args.func(args, base_url, token)
        elapsed = time.monotonic() - started
    except BytedaError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "generate" and not args.raw:
        print(f"(耗时 {elapsed:.0f}s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
