#!/usr/bin/env python3
"""百搭 ByteDa CLI —— 调用 ByteDa MCP 服务生成设计物料（仅依赖 Python 标准库）。

设计要点：
  * 一条命令 = 一个意图。提交任务后默认阻塞轮询到结束，模型不需要自己循环调 get_task_status。
  * 本地文件 / URL / fileId 统一走 --ref，本地文件自动完成「建会话 → 直传 → 确认」三步上传。
  * 每次提交自动生成幂等键并回显：超时或断线后带同一个 --idempotency-key 重跑，
    服务端只会拿回原任务，不会重复扣积分。
  * stdout 只输出一个 JSON 结果；进度与提示写 stderr。
  * 协议：MCP 2026-07-28 无状态请求（每次 POST 自带版本，无 initialize / session）。

退出码：
  0 完成   1 失败 / 服务端报错   2 参数错误   3 仍在运行（可用 wait 续等）
  4 需要补充信息（NEEDS_INPUT）   5 令牌缺失或无效
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import mimetypes
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

__version__ = "2.0.1"

DEFAULT_BASE_URL = "https://api.byteda.net/byte-da/mcp"
PROTOCOL_VERSION = "2026-07-28"
META_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
API_KEY_PAGE = "https://byteda.net/api-key"
# 每条命令的总时间预算（上传 + 排队 + 等待）。必须低于宿主的命令超时——Claude Code 的 Bash
# 最长 10 分钟，被宿主杀掉时 stdout 什么都没有，模型拿不到 taskId 只能重新提交、重复扣费
DEFAULT_TIMEOUT = 540
UNLIMITED_BUSY_WAIT = 1800
BUSY_RETRY_INTERVAL = 15
# 服务端标记 retryable 的「暂时忙」：提交前就被拒，没建任务也没扣费，可用同一幂等键重提
BUSY_ERROR_CODES = {"QUEUE_LIMIT_EXCEEDED", "APP_BUSY", "RATE_LIMITED"}
# 网关层的瞬时故障；请求可能已经到达后端，只能当「结果未知」处理
TRANSIENT_HTTP_CODES = {502, 503, 504}
MAX_REF_DOWNLOAD_BYTES = 50 * 1024 * 1024

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_RUNNING, EXIT_NEEDS_INPUT, EXIT_AUTH = 0, 1, 2, 3, 4, 5

# 各命令接受的 references.role，与服务端 inputSchema 的 enum 保持一致（提交前本地校验，避免白传文件）
COMMAND_ROLES = {
    "image": {"reference"},
    "video": {"reference", "first_frame", "last_frame", "sequence", "character", "avatar",
              "audio", "driving_audio", "reference_voice"},
    "audio": {"reference", "reference_voice"},
    "h5": {"reference", "first_frame", "last_frame", "source", "first_clip", "avatar",
           "audio", "driving_audio", "reference_voice"},
    "brief": {"reference", "first_frame", "last_frame", "source", "first_clip", "avatar",
              "audio", "driving_audio", "reference_voice"},
}
# --ref 用「role:目标」前缀时据此识别，避免把 https: / C: 误当 role
KNOWN_ROLES = set().union(*COMMAND_ROLES.values())

# mimetypes 在各平台上缺的扩展名补齐；服务端会校验 MIME 与扩展名同类
EXTRA_MIME = {
    ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
    ".m4v": "video/x-m4v", ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".opus": "audio/opus", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".md": "text/markdown", ".csv": "text/csv", ".txt": "text/plain",
    ".htm": "text/html", ".html": "text/html", ".pdf": "application/pdf",
    ".doc": "application/msword", ".ppt": "application/vnd.ms-powerpoint",
    ".xls": "application/vnd.ms-excel",
    # 精简容器 / 老版本 Python 的 mimetypes 表里没有 OOXML，回落成 octet-stream 会被服务端拒
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

FILE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

# get_task_status 的 nextAction → 给调用方（通常是模型）的明确指令
NEXT_ACTION_HINTS = {
    "retry": "可以重试：去掉 --idempotency-key（换新键）重新提交，或传 --node-id 在原节点上重跑。",
    "use_previous_artifact": "画布仍保留旧产物，不要重试。",
    "give_up": "不可重试，把错误原因告诉用户。",
    "provide_input": "向用户问清 questions 后，把答案并进 prompt，带同一 --app-id 重新调用 brief。",
}


class CliError(Exception):
    """带退出码的终止错误，message 直接给用户 / 模型看。"""

    def __init__(self, message: str, code: int = EXIT_FAILED, payload: dict | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


# ==================== 配置与令牌 ====================

def config_dir() -> Path:
    return Path(os.environ.get("BYTEDA_CONFIG_DIR") or Path.home() / ".byteda")


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(data: dict) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先以 0600 创建再写入，避免令牌出现在一个短暂可读的文件里
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return path


def resolve_token(args) -> tuple:
    """返回 (令牌, 来源)。优先级：--token > $BYTEDA_TOKEN > 配置文件。"""
    for token, source in ((args.token, "--token"),
                          (os.environ.get("BYTEDA_TOKEN"), "环境变量 BYTEDA_TOKEN"),
                          (load_config().get("token"), str(config_path()))):
        if token and token.strip():
            return token.strip(), source
    raise CliError(
        "未配置 ByteDa API Key。请到 %s 新建 API Key，然后执行：python3 byteda.py login <API_KEY>"
        % API_KEY_PAGE, EXIT_AUTH)


def shell_profile_exports() -> list:
    """找出 v1 set-token 写进 shell profile 的 export BYTEDA_TOKEN 行（文件:行号）。"""
    hits = []
    for name in (".zshrc", ".zprofile", ".bashrc", ".bash_profile", ".profile"):
        path = Path.home() / name
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        hits += ["%s:%d" % (path, i) for i, line in enumerate(lines, 1)
                 if re.match(r"\s*export\s+BYTEDA_TOKEN=", line)]
    return hits


def env_token_warning(token: str | None = None) -> str | None:
    """环境变量里的令牌会盖住配置文件；与期望令牌不一致时给出清理指引。"""
    env_token = (os.environ.get("BYTEDA_TOKEN") or "").strip()
    if not env_token or env_token == token:
        return None
    where = shell_profile_exports()
    return ("环境变量 BYTEDA_TOKEN 优先级高于配置文件，当前会覆盖刚保存的 API Key。"
            + ("它来自旧版 set-token 写入的 %s，请删除该行后重开终端。" % "、".join(where) if where
               else "请 unset BYTEDA_TOKEN 或从 shell 配置中删除它。"))


def resolve_base_url(args) -> str:
    return (args.base_url or os.environ.get("BYTEDA_BASE_URL")
            or load_config().get("base_url") or DEFAULT_BASE_URL).rstrip("/")


def mask(token: str) -> str:
    return token[:6] + "…" + token[-4:] if len(token) > 12 else "***"


# ==================== 输出 ====================

def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def emit(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ==================== MCP 传输 ====================

class McpClient:
    """MCP 2026-07-28 无状态客户端：每个请求自带协议版本，不做 initialize / session。"""

    def __init__(self, base_url: str, token: str, timeout: float = 60, token_source: str = ""):
        self.base_url = base_url
        self.token = token
        self.token_source = token_source
        self.timeout = timeout
        self._seq = 0

    def rpc(self, method: str, params: dict | None = None, timeout: float | None = None):
        self._seq += 1
        params = dict(params or {})
        params["_meta"] = {META_VERSION_KEY: PROTOCOL_VERSION}
        body = json.dumps({"jsonrpc": "2.0", "id": self._seq, "method": method, "params": params},
                          ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self.token,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
            "User-Agent": "byteda-skill/" + __version__,
        }
        name = params.get("name")
        if method == "tools/call" and isinstance(name, str) and name.isascii():
            headers["Mcp-Name"] = name
        request = urllib.request.Request(self.base_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                content_type = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")[:300]
            if err.code in TRANSIENT_HTTP_CODES:
                raise NetworkError("网关暂时不可用（HTTP %d）" % err.code) from err
            if err.code in (401, 403):
                message = "API Key 无效、已禁用或已过期（HTTP %d，令牌 %s 来自 %s）。请到 %s 重新创建后执行 login。" % (
                    err.code, mask(self.token), self.token_source or "--token", API_KEY_PAGE)
                warning = env_token_warning(load_config().get("token")) if self.token_source.startswith("环境变量") else None
                raise CliError(message + (warning or ""), EXIT_AUTH)
            raise CliError("服务端返回 HTTP %d：%s" % (err.code, detail))
        except (OSError, http.client.HTTPException) as err:
            # OSError 覆盖 URLError / socket.timeout（3.8~3.9 不是 TimeoutError）/ ssl.SSLError / 连接重置
            raise NetworkError("网络请求失败：%s" % (getattr(err, "reason", None) or err or type(err).__name__)) from err

        message = self._parse(raw, content_type)
        if "error" in message:
            error = message["error"] or {}
            raise CliError("MCP 协议错误 %s：%s" % (error.get("code"), error.get("message")))
        return message.get("result") or {}

    @staticmethod
    def _parse(raw: str, content_type: str) -> dict:
        # 服务端目前总是回 JSON；按规范客户端也必须能读 SSE 形式的响应
        if "text/event-stream" in content_type:
            last = None
            for line in raw.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk:
                        try:
                            parsed = json.loads(chunk)
                        except ValueError:
                            continue
                        if "result" in parsed or "error" in parsed:
                            last = parsed
            if last is None:
                raise CliError("SSE 响应里没有找到 JSON-RPC 结果")
            return last
        try:
            return json.loads(raw)
        except ValueError:
            raise CliError("服务端返回了非 JSON 内容：%s" % raw[:200])

    def call(self, tool: str, arguments: dict, timeout: float | None = None):
        """调工具并返回结构化结果；工具执行错误（isError）转成 CliError。"""
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments}, timeout)
        structured = result.get("structuredContent")
        if structured is None:
            structured = _content_json(result)
        if result.get("isError"):
            info = structured if isinstance(structured, dict) else {"message": structured}
            code = info.get("errorCode") or "TOOL_ERROR"
            exit_code = EXIT_AUTH if code in ("UNAUTHORIZED", "TOKEN_INVALID") else EXIT_FAILED
            raise CliError("%s（%s）" % (info.get("message") or "工具调用失败", code), exit_code,
                           {"errorCode": code, "retryable": info.get("retryable")})
        # list_models / get_files 的顶层数组被服务端包成 {"items": [...]}
        if isinstance(structured, dict) and set(structured) == {"items"}:
            return structured["items"]
        return structured


class NetworkError(CliError):
    """可重试的网络故障（轮询时自动重试，提交时提示用同一幂等键重跑）。"""


def _content_json(result: dict):
    for item in result.get("content") or []:
        if item.get("type") == "text":
            try:
                return json.loads(item.get("text", ""))
            except ValueError:
                return item.get("text")
    return None


def call_when_free(client: McpClient, tool: str, arguments: dict, deadline: float | None):
    """调工具；遇到服务端「暂时忙」（并发满 / 画布正忙 / 频控）就等一会儿重试，直到预算用完。"""
    stop = deadline or time.time() + UNLIMITED_BUSY_WAIT
    while True:
        try:
            return client.call(tool, arguments)
        except CliError as err:
            if isinstance(err, NetworkError) or err.payload.get("errorCode") not in BUSY_ERROR_CODES \
                    or time.time() + BUSY_RETRY_INTERVAL >= stop:
                raise
            log("服务端暂时忙（%s），%ds 后重试……" % (err.payload.get("errorCode"), BUSY_RETRY_INTERVAL))
            time.sleep(BUSY_RETRY_INTERVAL)


def strip_empty(data: dict) -> dict:
    """去掉 None / 空串 / 空列表，服务端不接受空值占位。"""
    return {k: v for k, v in data.items() if v is not None and v != "" and v != [] and v != {}}


# ==================== 上传 ====================

def guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    return EXTRA_MIME.get(ext) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def upload_file(client: McpClient, path: Path, app_id: str | None = None, deadline: float | None = None) -> dict:
    if not path.is_file():
        raise CliError("文件不存在：%s（role 前缀写法为 role:路径，可用 role：%s）"
                       % (path, "、".join(sorted(KNOWN_ROLES))), EXIT_USAGE)
    data = path.read_bytes()
    session = call_when_free(client, "create_upload_session", strip_empty({
        "fileName": path.name,
        "sizeBytes": len(data),
        "mimeType": guess_mime(path),
        "appId": app_id,
    }), deadline)
    method = (session.get("method") or "PUT").upper()
    url = session["uploadUrl"]
    if method == "POST":
        body, content_type = _multipart(session.get("fields") or {}, path.name, guess_mime(path), data)
        request = urllib.request.Request(url, data=body, headers={"Content-Type": content_type}, method="POST")
    else:
        request = urllib.request.Request(url, data=data, headers=dict(session.get("headers") or {}), method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=max(120, len(data) / 200_000)) as resp:
            resp.read()
    except urllib.error.HTTPError as err:
        raise CliError("直传对象存储失败 HTTP %d：%s" % (err.code, err.read().decode("utf-8", "replace")[:300]))
    except (OSError, http.client.HTTPException) as err:
        raise CliError("直传对象存储失败：%s" % (getattr(err, "reason", None) or err))
    done = client.call("complete_upload", {
        "uploadId": session["uploadId"],
        "sha256": hashlib.sha256(data).hexdigest(),
    })
    log("已上传 %s → fileId=%s" % (path.name, done.get("fileId")))
    return done


def _multipart(fields: dict, file_name: str, mime: str, data: bytes):
    boundary = "----byteda" + uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, key, value)).encode("utf-8"))
    # 规范要求文件字段放在最后
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                  "Content-Type: %s\r\n\r\n" % (boundary, file_name, mime)).encode("utf-8"))
    parts.append(data)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def known_suffix(name: str) -> bool:
    ext = Path(name).suffix.lower()
    return bool(ext) and (ext in EXTRA_MIME or mimetypes.guess_type("x" + ext)[0] is not None)


def download_to_temp(url: str, directory: Path) -> Path:
    """把公网素材下载到临时目录，供需要 fileId 的场景上传。无后缀的签名链接按 Content-Type 补扩展名。"""
    name = Path(urllib.parse.urlparse(url).path).name or "reference"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "byteda-skill"}),
                                    timeout=120) as resp:
            length = int(resp.headers.get("Content-Length") or 0)
            if length > MAX_REF_DOWNLOAD_BYTES:
                raise CliError("参考素材超过 50 MiB：%s" % url, EXIT_USAGE)
            data = resp.read(MAX_REF_DOWNLOAD_BYTES + 1)
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    except (OSError, http.client.HTTPException) as err:
        raise CliError("下载参考素材失败：%s（%s）" % (url, getattr(err, "reason", None) or err))
    if len(data) > MAX_REF_DOWNLOAD_BYTES:
        raise CliError("参考素材超过 50 MiB：%s" % url, EXIT_USAGE)
    if not known_suffix(name):
        ext = {v: k for k, v in EXTRA_MIME.items()}.get(content_type) or mimetypes.guess_extension(content_type)
        if not ext:
            raise CliError("无法判断素材类型（URL 无后缀且 Content-Type=%s），请下载到本地并带上扩展名：%s"
                           % (content_type or "空", url), EXIT_USAGE)
        name = Path(name).stem + ext
    target = directory / name
    target.write_bytes(data)
    return target


def parse_ref(spec: str):
    """「[role:]目标」→ (role, 目标)。目标可以是本地路径、http(s) URL 或 32 位 fileId。"""
    head, sep, rest = spec.partition(":")
    if sep and head in KNOWN_ROLES:
        return head, rest
    return None, spec


def resolve_refs(client: McpClient, specs, command: str, app_id: str | None, deadline: float | None) -> list:
    """把 --ref 列表转成服务端 references。

    只有 brief 能直接收 URL，且 URL 带可识别后缀时才透传（服务端按后缀推断类型，推断不出直接报错）；
    其余情况一律下载后上传换 fileId，由服务端按真实内容判定类型。
    """
    parsed = [parse_ref(spec) for spec in specs or []]
    allowed = COMMAND_ROLES[command]
    for role, _ in parsed:
        if role and role not in allowed:
            raise CliError("%s 不接受 role=%s，可用：%s" % (command, role, "、".join(sorted(allowed))), EXIT_USAGE)
    refs = []
    scratch = Path(tempfile.mkdtemp(prefix="byteda-"))
    try:
        for role, target in parsed:
            entry = {}
            if target.startswith(("http://", "https://")):
                if command == "brief" and known_suffix(urllib.parse.urlparse(target).path):
                    entry["url"] = target
                else:
                    entry["fileId"] = upload_file(client, download_to_temp(target, scratch), app_id, deadline)["fileId"]
            elif FILE_ID_PATTERN.match(target) and not Path(target).exists():
                entry["fileId"] = target
            else:
                entry["fileId"] = upload_file(client, Path(target).expanduser(), app_id, deadline)["fileId"]
            if role:
                entry["role"] = role
            refs.append(entry)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return refs


# ==================== 提交与轮询 ====================

def ensure_canvas(client: McpClient, args, fallback_name: str) -> tuple:
    if args.app_id:
        return str(args.app_id), None
    name = args.name or (fallback_name.strip().replace("\n", " ")[:24] or "ByteDa 画布")
    canvas = client.call("create_canvas", {"name": name})
    log("已新建画布 appId=%s  %s" % (canvas.get("appId"), canvas.get("canvasUrl") or ""))
    return str(canvas["appId"]), canvas.get("canvasUrl")


def submit(client: McpClient, tool: str, arguments: dict, args) -> dict:
    key = args.idempotency_key or "bd-" + uuid.uuid4().hex
    arguments["idempotencyKey"] = key
    app_id = arguments.get("appId")
    try:
        submitted = call_when_free(client, tool, strip_empty(arguments), None if args.no_wait else args.deadline)
    except NetworkError as err:
        # 结果未知：必须带回同一幂等键和同一画布重跑，否则会重复扣费、或在新空画布上拿回旧任务
        rerun = "--idempotency-key %s" % key + (" --app-id %s" % app_id if app_id else "")
        raise CliError("%s。任务可能已经提交成功，请原样重跑本命令并追加：%s（不会重复扣费）" % (err, rerun),
                       EXIT_FAILED, strip_empty({"idempotencyKey": key, "appId": app_id, "rerunWith": rerun}))
    submitted["idempotencyKey"] = key
    if submitted.get("duplicated"):
        log("幂等键命中，拿回之前提交的同一个任务 taskId=%s（不会重复扣费）" % submitted.get("taskId"))
    else:
        points = submitted.get("estimatedPoints")
        log("已提交 %s taskId=%s%s" % (tool, submitted.get("taskId"),
                                       "（预估 %s 积分）" % points if points is not None else ""))
    return submitted


def wait_task(client: McpClient, task_id: str, deadline: float | None, interval_hint: int | None = None) -> dict:
    start = time.time()
    interval = max(2, min(interval_hint or 5, 15))
    failures = 0
    last_state, last_logged = None, 0.0
    while True:
        try:
            status = client.call("get_task_status", {"taskId": task_id})
            failures = 0
        except NetworkError as err:
            # 轮询本身是只读幂等的，网络抖动直接重试
            failures += 1
            if failures > 5:
                raise CliError("%s（连续 5 次轮询失败，任务可能仍在运行，稍后执行 wait %s）"
                               % (err, task_id), EXIT_RUNNING, {"taskId": task_id, "status": "RUNNING"})
            log("轮询失败，%ds 后重试：%s" % (interval, err))
            time.sleep(interval)
            continue
        state = status.get("status")
        if state != "RUNNING":
            return status
        elapsed = time.time() - start
        node_state = status.get("nodeStatus") or ""
        # 状态变化立即打印，否则每 30 秒报一次心跳，避免刷屏
        if node_state != last_state or elapsed - last_logged >= 30:
            log("[%3ds] 生成中 %s" % (elapsed, node_state))
            last_state, last_logged = node_state, elapsed
        if deadline and time.time() + interval >= deadline:
            status["hint"] = ("本地等待超时，服务端任务仍在运行且不会因此中断。继续等待：byteda.py wait %s" % task_id)
            return status
        interval = max(2, min(status.get("pollIntervalSeconds") or interval, 15))
        time.sleep(interval)


def summarize(status: dict, extra: dict | None = None) -> dict:
    """把 get_task_status 收敛成模型好读的结果；--raw 时不走这里。"""
    result = status.get("result") or {}
    out = {
        "status": status.get("status"),
        "taskId": status.get("taskId"),
        "appId": status.get("appId") or result.get("appId"),
        "nodeId": status.get("nodeId"),
        "kind": status.get("kind"),
    }
    for key in ("url", "canvasUrl", "width", "height", "durationSec", "htmlLength",
                "artifacts", "assistant", "assistantTruncated", "note"):
        if result.get(key) not in (None, "", []):
            out[key] = result[key]
    if status.get("cost"):
        # design_brief 为兼容旧对接文档带了一串 *_consume_points 分项，只保留非零项
        out["cost"] = {k: v for k, v in status["cost"].items()
                       if k in ("points", "estimated") or v not in (0, 0.0, None, "")}
    if status.get("status") == "FAILED":
        out.update(strip_empty({"errorCode": status.get("errorCode"), "error": status.get("error"),
                                "retryable": status.get("retryable"), "nextAction": status.get("nextAction")}))
    if status.get("questions"):
        out["questions"] = status["questions"]
        out["nextAction"] = status.get("nextAction") or "provide_input"
    action = out.get("nextAction")
    if action in NEXT_ACTION_HINTS:
        out["hint"] = NEXT_ACTION_HINTS[action]
    if status.get("hint"):
        out["hint"] = status["hint"]
    # 服务端返回的字段优先，本地补充信息只填空缺
    for k, v in (extra or {}).items():
        if v not in (None, "") and k not in out:
            out[k] = v
    return strip_empty(out)


def finish(client: McpClient, submitted: dict, args, extra: dict | None = None) -> int:
    extra = dict(extra or {})
    extra["idempotencyKey"] = submitted.get("idempotencyKey")
    if args.no_wait:
        emit(strip_empty({**submitted, **extra,
                          "hint": "任务已提交，用 byteda.py wait %s 获取结果" % submitted.get("taskId")}))
        return EXIT_OK
    status = wait_task(client, submitted["taskId"], args.deadline, submitted.get("pollIntervalSeconds"))
    return report(client, status, args, extra)


def report(client: McpClient, status: dict, args, extra: dict | None = None) -> int:
    if getattr(args, "raw", False):
        emit(status)
    else:
        out = summarize(status, extra)
        if out.get("appId") and not out.get("canvasUrl"):
            # brief 结果与「带 --app-id 迭代」时都没有画布地址，补查一次；失败不影响主结果
            try:
                out["canvasUrl"] = client.call("get_canvas", {"appId": out["appId"]}).get("canvasUrl")
            except CliError:
                pass
        if status.get("status") == "DONE" and getattr(args, "out", None):
            out["downloaded"] = download_outputs(out, Path(args.out))
        emit(out)
    return {"DONE": EXIT_OK, "RUNNING": EXIT_RUNNING, "NEEDS_INPUT": EXIT_NEEDS_INPUT}.get(
        status.get("status"), EXIT_FAILED)


def download_outputs(out: dict, directory: Path) -> list:
    directory.mkdir(parents=True, exist_ok=True)
    urls = [a.get("url") for a in out.get("artifacts") or [] if a.get("url")] or [out.get("url")]
    saved = []
    for index, url in enumerate(u for u in urls if u):
        name = Path(urllib.parse.urlparse(url).path).name or "artifact-%d" % index
        target = directory / name
        try:
            with urllib.request.urlopen(url, timeout=300) as resp:
                target.write_bytes(resp.read())
            saved.append(str(target))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            log("下载失败 %s：%s" % (url, getattr(err, "reason", err)))
    return saved


# ==================== 子命令 ====================

def cmd_login(args, client_factory) -> int:
    token = args.api_key
    if token in (None, "-"):
        token = sys.stdin.readline().strip()
    if not token:
        raise CliError("API Key 为空", EXIT_USAGE)
    base_url = resolve_base_url(args)
    points = McpClient(base_url, token).call("get_account_points", {})
    config = load_config()
    config["token"] = token
    if args.base_url:
        config["base_url"] = args.base_url.rstrip("/")
    path = save_config(config)
    emit(strip_empty({"ok": True, "config": str(path), "token": mask(token), "baseUrl": base_url,
                      "availablePoints": points.get("availablePoints"),
                      "warning": env_token_warning(token)}))
    return EXIT_OK


def cmd_logout(args, client_factory) -> int:
    config = load_config()
    config.pop("token", None)
    save_config(config)
    emit({"ok": True, "config": str(config_path())})
    return EXIT_OK


def cmd_doctor(args, client_factory) -> int:
    client = client_factory()
    discover = client.rpc("server/discover")
    points = client.call("get_account_points", {})
    emit({"version": __version__, "baseUrl": client.base_url, "token": mask(client.token),
          "tokenSource": client.token_source, "protocolVersions": discover.get("supportedVersions"),
          "availablePoints": points.get("availablePoints"), "spaceId": points.get("spaceId")})
    return EXIT_OK


def cmd_image(args, client_factory) -> int:
    client = client_factory()
    app_id, canvas_url = ensure_canvas(client, args, args.prompt)
    refs = resolve_refs(client, args.ref, "image", app_id, args.deadline)
    submitted = submit(client, "create_image_task", {
        "appId": app_id, "nodeId": args.node_id, "prompt": args.prompt,
        "aspectRatio": args.ratio, "resolution": args.resolution,
        "width": args.width, "height": args.height, "references": refs,
        "styleId": args.style_id, "model": args.model,
        "transparentBackground": True if args.transparent else None,
    }, args)
    return finish(client, submitted, args, {"canvasUrl": canvas_url})


def cmd_video(args, client_factory) -> int:
    client = client_factory()
    app_id, canvas_url = ensure_canvas(client, args, args.prompt)
    refs = resolve_refs(client, args.ref, args.command, app_id, args.deadline)
    submitted = submit(client, "create_video_task", {
        "appId": app_id, "nodeId": args.node_id, "prompt": args.prompt,
        "durationSeconds": args.duration, "aspectRatio": args.ratio, "resolution": args.resolution,
        "audio": args.audio, "seed": args.seed, "cameraFixed": True if args.camera_fixed else None,
        "references": refs, "model": args.model,
    }, args)
    return finish(client, submitted, args, {"canvasUrl": canvas_url})


def cmd_audio(args, client_factory) -> int:
    client = client_factory()
    app_id, canvas_url = ensure_canvas(client, args, args.text)
    refs = resolve_refs(client, args.ref, args.command, app_id, args.deadline)
    submitted = submit(client, "create_audio_task", {
        "appId": app_id, "nodeId": args.node_id, "prompt": args.text, "speaker": args.speaker,
        "language": args.language, "references": refs, "model": args.model,
    }, args)
    return finish(client, submitted, args, {"canvasUrl": canvas_url})


def cmd_h5(args, client_factory) -> int:
    client = client_factory()
    app_id, canvas_url = ensure_canvas(client, args, args.requirement)
    refs = resolve_refs(client, args.ref, args.command, app_id, args.deadline)
    submitted = submit(client, "create_h5_task", {
        "appId": app_id, "requirement": args.requirement, "scene": args.scene,
        "width": args.width, "height": args.height, "references": refs,
    }, args)
    return finish(client, submitted, args, {"canvasUrl": canvas_url})


def cmd_brief(args, client_factory) -> int:
    client = client_factory()
    refs = resolve_refs(client, args.ref, "brief", args.app_id, args.deadline)
    submitted = submit(client, "design_brief", {
        "prompt": args.prompt, "appId": args.app_id, "conversationId": args.conversation_id,
        "appName": args.name, "styleIds": args.style_id, "references": refs,
        "llmModel": args.llm_model, "imageModel": args.image_model,
        "videoModel": args.video_model, "audioModel": args.audio_model,
        "allowClarification": True if args.allow_clarification else None,
    }, args)
    return finish(client, submitted, args)


def cmd_wait(args, client_factory) -> int:
    client = client_factory()
    return report(client, wait_task(client, args.task_id, args.deadline), args)


def cmd_status(args, client_factory) -> int:
    client = client_factory()
    return report(client, client.call("get_task_status", {"taskId": args.task_id}), args)


def cmd_upload(args, client_factory) -> int:
    client = client_factory()
    emit([upload_file(client, Path(p).expanduser(), args.app_id) for p in args.files])
    return EXIT_OK


def cmd_canvas(args, client_factory) -> int:
    client = client_factory()
    emit(client.call("get_canvas", strip_empty({"appId": args.app_id,
                                                "includeInternalNodes": True if args.all else None})))
    return EXIT_OK


def cmd_text(args, client_factory) -> int:
    client = client_factory()
    app_id, _ = ensure_canvas(client, args, args.text)
    emit(client.call("create_text_node", strip_empty({
        "appId": app_id, "text": args.text, "name": args.node_name,
        "idempotencyKey": args.idempotency_key})))
    return EXIT_OK


def cmd_points(args, client_factory) -> int:
    client = client_factory()
    emit(client.call("get_account_points", strip_empty({
        "imageModel": args.image_model, "videoModel": args.video_model, "audioModel": args.audio_model,
        "videoDurationSeconds": args.video_duration, "videoResolution": args.video_resolution})))
    return EXIT_OK


def cmd_models(args, client_factory) -> int:
    client = client_factory()
    emit(client.call("list_models", strip_empty({"type": args.type})))
    return EXIT_OK


def cmd_styles(args, client_factory) -> int:
    client = client_factory()
    if args.id is not None:
        emit(client.call("get_style", {"styleId": args.id}))
    else:
        emit(client.call("query_styles", strip_empty({"name": args.search, "pageNo": args.page,
                                                       "pageSize": args.page_size})))
    return EXIT_OK


def cmd_tools(args, client_factory) -> int:
    client = client_factory()
    tools = client.rpc("tools/list").get("tools") or []
    if args.tool:
        match = [t for t in tools if t.get("name") == args.tool]
        if not match:
            raise CliError("没有名为 %s 的工具" % args.tool, EXIT_USAGE)
        emit(match[0])
    else:
        emit([{"name": t.get("name"), "required": (t.get("inputSchema") or {}).get("required", []),
               "description": (t.get("description") or "")[:120]} for t in tools])
    return EXIT_OK


def cmd_call(args, client_factory) -> int:
    client = client_factory()
    raw = args.arguments or "{}"
    if raw == "-":
        raw = sys.stdin.read()
    elif raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8")
    try:
        arguments = json.loads(raw)
    except ValueError as err:
        raise CliError("arguments 不是合法 JSON：%s" % err, EXIT_USAGE)
    emit(client.call(args.tool, arguments, timeout=args.timeout or None))
    return EXIT_OK


# ==================== 参数解析 ====================

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--token", help="API Key（默认读 $BYTEDA_TOKEN 或 ~/.byteda/config.json）")
    common.add_argument("--base-url", help="MCP 端点（默认 %s，或 $BYTEDA_BASE_URL）" % DEFAULT_BASE_URL)

    task = argparse.ArgumentParser(add_help=False)
    task.add_argument("--app-id", help="已有画布 appId；不传则自动新建画布")
    task.add_argument("--name", help="新建画布 / 作品的名称")
    task.add_argument("--ref", action="append", metavar="[ROLE:]PATH|URL|FILEID",
                      help="参考素材，可重复；本地文件自动上传。ROLE 如 first_frame、reference_voice")
    task.add_argument("--idempotency-key", help="幂等键；超时重跑时传入上次回显的值，不会重复扣费")
    task.add_argument("--no-wait", action="store_true", help="只提交不等待，返回 taskId")
    task.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                      help="本条命令总耗时预算秒数，含上传与排队（默认 %d，须小于宿主命令超时；0 表示不限）"
                           % DEFAULT_TIMEOUT)
    task.add_argument("--out", help="完成后把产物下载到该目录")
    task.add_argument("--raw", action="store_true", help="输出服务端原始任务状态")

    # 通用参数只挂在子命令上：同时挂主解析器时，子解析器的默认值会覆盖写在子命令前的值
    parser = argparse.ArgumentParser(prog="byteda.py", description="百搭 ByteDa 设计物料生成 CLI")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    p = sub.add_parser("login", parents=[common], help="保存 API Key 到 ~/.byteda/config.json（校验有效后写入）")
    p.add_argument("api_key", nargs="?", help="API Key；省略或传 - 则从 stdin 读取")
    p.set_defaults(func=cmd_login)
    p = sub.add_parser("logout", parents=[common], help="删除本地保存的 API Key")
    p.set_defaults(func=cmd_logout)
    p = sub.add_parser("doctor", parents=[common], help="检查令牌、端点与积分")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("brief", parents=[common, task], help="目标级需求：交给服务端 Agent 产出一整套物料")
    p.add_argument("--prompt", required=True)
    p.add_argument("--conversation-id")
    p.add_argument("--style-id", type=int, action="append", help="风格 ID，可重复")
    p.add_argument("--llm-model")
    p.add_argument("--image-model")
    p.add_argument("--video-model")
    p.add_argument("--audio-model")
    p.add_argument("--allow-clarification", action="store_true", help="信息不足时允许反问（NEEDS_INPUT）")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("image", parents=[common, task], help="生成 / 重绘一张图片")
    p.add_argument("--prompt", required=True)
    p.add_argument("--node-id", help="在已有 IMAGE 节点上重新生成")
    p.add_argument("--ratio", help="画面比例，如 1:1 / 3:4 / 16:9")
    p.add_argument("--resolution", help="分辨率档位，如 1K / 2K")
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.add_argument("--style-id", type=int)
    p.add_argument("--model")
    p.add_argument("--transparent", action="store_true", help="输出透明背景 PNG")
    p.set_defaults(func=cmd_image)

    p = sub.add_parser("video", parents=[common, task], help="生成一段视频")
    p.add_argument("--prompt", required=True)
    p.add_argument("--node-id")
    p.add_argument("--duration", type=int, help="时长（秒）")
    p.add_argument("--ratio", help="宽高比，如 16:9 / 9:16")
    p.add_argument("--resolution", help="如 720P / 1080P")
    audio = p.add_mutually_exclusive_group()
    audio.add_argument("--audio", dest="audio", action="store_const", const=True, help="生成有声视频")
    audio.add_argument("--no-audio", dest="audio", action="store_const", const=False, help="生成无声视频")
    p.add_argument("--seed", type=int)
    p.add_argument("--camera-fixed", action="store_true")
    p.add_argument("--model")
    p.set_defaults(func=cmd_video)

    p = sub.add_parser("audio", parents=[common, task], help="生成配音 / 音色克隆")
    p.add_argument("--text", required=True, help="配音文本（≤3000 字）")
    p.add_argument("--node-id")
    p.add_argument("--speaker", help="预置音色 ID，来自 models --type AUDIO")
    p.add_argument("--language")
    p.add_argument("--model")
    p.set_defaults(func=cmd_audio)

    p = sub.add_parser("h5", parents=[common, task], help="生成一个 H5 页面（长图 / PPT / 社媒图文等）")
    p.add_argument("--requirement", required=True)
    p.add_argument("--scene", help="LONG_IMAGE / SOCIAL_MEDIA_IMAGE_TEXT / PPT / INFOGRAPHIC / COMIC_STRIP / VERTICAL_POSTER")
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.set_defaults(func=cmd_h5)

    p = sub.add_parser("wait", parents=[common], help="等待已提交的任务结束")
    p.add_argument("task_id")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--out")
    p.add_argument("--raw", action="store_true")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("status", parents=[common], help="查询一次任务状态（不等待）")
    p.add_argument("task_id")
    p.add_argument("--raw", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("upload", parents=[common], help="上传本地文件，返回 fileId")
    p.add_argument("files", nargs="+")
    p.add_argument("--app-id")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("canvas", parents=[common], help="查看画布节点与产物")
    p.add_argument("app_id")
    p.add_argument("--all", action="store_true", help="包含文字等内部节点")
    p.set_defaults(func=cmd_canvas)

    p = sub.add_parser("text", parents=[common], help="在画布上写一个文字节点（不耗积分）")
    p.add_argument("--text", required=True)
    p.add_argument("--app-id")
    p.add_argument("--name", help="新建画布时的画布名")
    p.add_argument("--node-name")
    p.add_argument("--idempotency-key")
    p.set_defaults(func=cmd_text)

    p = sub.add_parser("points", parents=[common], help="查询积分与参考估价")
    p.add_argument("--image-model")
    p.add_argument("--video-model")
    p.add_argument("--audio-model")
    p.add_argument("--video-duration", type=int)
    p.add_argument("--video-resolution")
    p.set_defaults(func=cmd_points)

    p = sub.add_parser("models", parents=[common], help="查询可用模型与能力（音色、分辨率、时长）")
    p.add_argument("--type", choices=["LLM", "IMAGE", "VIDEO", "AUDIO"])
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("styles", parents=[common], help="搜索设计风格，或 --id 查看详情")
    p.add_argument("--search")
    p.add_argument("--id", type=int)
    p.add_argument("--page", type=int)
    p.add_argument("--page-size", type=int)
    p.set_defaults(func=cmd_styles)

    p = sub.add_parser("tools", parents=[common], help="列出服务端工具；给出工具名则打印完整 schema")
    p.add_argument("tool", nargs="?")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("call", parents=[common], help="直接调用任意工具（兜底）")
    p.add_argument("tool")
    p.add_argument("arguments", nargs="?", help="JSON 字符串、@文件 或 -（stdin）")
    p.add_argument("--timeout", type=float, default=0)
    p.set_defaults(func=cmd_call)
    return parser


def main(argv=None) -> int:
    # Windows 控制台默认编码不是 UTF-8，中文 JSON 会乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    # call 的 --timeout 是单次请求超时；其余命令的是整条命令的截止时间
    timeout = getattr(args, "timeout", None) if args.command != "call" else None
    args.deadline = time.time() + timeout if timeout else None

    # role 写错要在建画布、上传之前就拦下，否则会留下空画布和白传的文件
    if args.command in COMMAND_ROLES:
        allowed = COMMAND_ROLES[args.command]
        for role, _ in (parse_ref(spec) for spec in getattr(args, "ref", None) or []):
            if role and role not in allowed:
                emit({"error": "%s 不接受 role=%s，可用：%s" % (args.command, role, "、".join(sorted(allowed)))})
                return EXIT_USAGE

    def client_factory() -> McpClient:
        token, source = resolve_token(args)
        return McpClient(resolve_base_url(args), token, token_source=source)

    try:
        return args.func(args, client_factory)
    except CliError as err:
        emit(strip_empty({"error": str(err), **err.payload}))
        return err.code
    except KeyboardInterrupt:
        log("已中断。已提交的任务仍在服务端运行，可用 wait <taskId> 继续获取。")
        return EXIT_RUNNING


if __name__ == "__main__":
    sys.exit(main())
