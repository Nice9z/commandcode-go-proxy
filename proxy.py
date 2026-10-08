"""
Command Code (commandcode.ai) Go-plan reverse proxy
===================================================
The $1/mo Go plan has no official API: /provider/v1/* returns
"upgrade_required" on Go. But the cmd CLI itself talks to
POST https://api.commandcode.ai/alpha/generate on every turn, and that
endpoint is NOT plan-gated. This proxy speaks that envelope:

  OpenAI client  ->  POST /v1/chat/completions (OpenAI shape)
                 ->  translated to Command Code envelope, stream forced on
                 ->  NDJSON response parsed, re-emitted as OpenAI SSE/JSON

Browser ->  http://HOST:PORT/dashboard   live usage dashboard (SQLite-backed)

Run (local):
  set CMD_CODE_TOKEN=user_xxxx
  python command_code_proxy.py
Run (VPS):
  CMD_CODE_TOKEN=user_... CMD_CODE_KEY=pick-a-secret CMD_CODE_HOST=0.0.0.0 \
  CMD_CODE_PORT=18787 python3 command_code_proxy.py
Env:
  CMD_CODE_TOKEN     your user_... API key from commandcode.ai (server-side secret)
  CMD_CODE_KEY       gateway key clients must send as Bearer (SET ON VPS)
  CMD_CODE_HOST      listen host (default 127.0.0.1; 0.0.0.0 on VPS)
  CMD_CODE_PORT      listen port (default 18787)
  CMD_CODE_VERSION   x-command-code-version header (default 1.53.1)
  CMD_CODE_UPSTREAM  default https://api.commandcode.ai
  CMD_CODE_DB        sqlite path (default cc_proxy_usage.db next to script)
  CMD_CODE_CURL      curl binary (default auto)
  HTTPS_PROXY        egress proxy if needed

  CMD_CODE_KEY    gateway key clients must send as Bearer (SET THIS ON A VPS)
  CMD_CODE_HOST   listen host (default 127.0.0.1; use 0.0.0.0 on a VPS)
  CMD_CODE_DB     sqlite path (default cc_proxy_usage.db next to script)
  CMD_CODE_PROJECT_SLUG  x-project-slug header (default cc-proxy)
  CMD_CODE_DEFAULT_MODEL model when client sends none (default z-ai/glm-5.3-flash)
  CMD_CODE_UPSTREAM_RETRY_MAX    transparent pre-first-byte retries (default 2)
  CMD_CODE_MAX_INFLIGHT          global concurrency cap (default 4; 0=off)

Notes:
  - Upstream rejects stream:false, so the proxy always streams upstream and
    buffers for non-stream clients.
  - /alpha/generate is undocumented; protocol reverse-engineered from the CLI
    (safzanpirani/pi-commandcode-provider, nasrulhadi/proxy-commandcode,
    MAXeaglet/commandcode-proxy). ToS risk is on you; personal Go traffic only.

Privacy / data flow (what leaves your machine):
  - To the upstream: the translated chat envelope (your prompt text -
    unavoidable) plus headers. The device context is a DETERMINISTIC FAKE
    (stable fake project dir / git shape per key, burner-device style):
    realistic traffic shape without ever sending your real paths, hostname,
    device info, client IP or client headers.
  - Session id = UUID5(SHA-256(upstream key), 12h bucket + deterministic
    jitter). Never derived from message content; stable like the CLI's own
    long-lived session, unlinkable across buckets.
  - Local SQLite stores: model name, token counts, cost, latency, status,
    REDACTED error text. NO message bodies, NO keys. DB chmod 600.
  - stderr logs are credential-redacted (user_*/sk-* masked); request bodies
    are never logged; per-request temp files are unlinked after each call.
  - The upstream key lives only in the env var (or passes straight through
    from the client) and is never written to disk or logs.

Anti-ban measures (traffic shaped like the official CLI):
  - protocol version pinned to the implemented wire shape (1.53.1); do not
    blindly bump to npm latest (old dialect claiming new version is worse)
  - stable per-key session, x-project-slug header, empty-system space
    placeholder (prevents upstream injecting its ~7.5K-token default prompt)
  - in-flight concurrency cap; bounded transparent retry (2 tries, 400ms
    backoff) ONLY before the first upstream byte; 503 + Retry-After to
    clients when saturated; semantic upstream errors are never retried.
  - keep request frequency human-like; personal-use tool, not a farm.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("CMD_CODE_UPSTREAM", "https://api.commandcode.ai")
TOKEN = os.environ.get("CMD_CODE_TOKEN", "").strip()
PROXY_KEY = os.environ.get("CMD_CODE_KEY", "").strip()
# Protocol version (MAXeaglet/commandcode-proxy discipline): report the version
# whose wire SHAPE we implement, self-consistent. Do NOT blindly track npm latest -
# claiming a new version with an old dialect is more suspicious than an old version.
# Bump only after re-aligning the envelope from the CLI bundle.
VERSION = os.environ.get("CMD_CODE_VERSION", "1.53.1")
PROJECT_SLUG = os.environ.get("CMD_CODE_PROJECT_SLUG", "cc-proxy")
# model used when the client does not specify one (Go-plan default)
DEFAULT_MODEL = os.environ.get("CMD_CODE_DEFAULT_MODEL", "z-ai/glm-5.3-flash")
HOST = os.environ.get("CMD_CODE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CMD_CODE_PORT", "18787"))
DB_PATH = os.environ.get("CMD_CODE_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cc_proxy_usage.db")
CURL = os.environ.get("CMD_CODE_CURL") or (
    shutil.which("curl.exe") or shutil.which("curl") or "curl")

APP_VERSION = "0.0.5"          # keep in sync with the latest GitHub Release tag
_REPO = "Nice9z/commandcode-go-proxy"
_latest_cache = {"v": None, "ts": 0.0}


def latest_github_version():
    """Latest release tag from the GitHub API, cached 1h; None on any failure."""
    if time.time() - _latest_cache["ts"] < 3600:
        return _latest_cache["v"]
    v = None
    try:
        r = subprocess.run(
            [CURL, "-sS", "--max-time", "10",
             "https://api.github.com/repos/%s/releases/latest" % _REPO],
            capture_output=True, timeout=15)
        d = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
        tag = (d.get("tag_name") or "").lstrip("v")
        if tag:
            v = tag
    except Exception:  # noqa: BLE001 - offline / rate-limited: just skip
        v = None
    _latest_cache["v"] = v
    _latest_cache["ts"] = time.time()
    return v
# anti-ban knobs: bounded transparent retry before first byte, concurrency cap,
# stable per-key session rotating on a 12h window (+deterministic jitter)
UPSTREAM_RETRY_MAX = int(os.environ.get("CMD_CODE_UPSTREAM_RETRY_MAX", "2"))
UPSTREAM_RETRY_BASE_MS = int(os.environ.get("CMD_CODE_UPSTREAM_RETRY_BASE_MS", "400"))
MAX_INFLIGHT = int(os.environ.get("CMD_CODE_MAX_INFLIGHT", "4"))
SESSION_TTL_S = 12 * 3600

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "host",
       "content-length", "accept-encoding", "authorization"}

MODELS = [
    # default (Go-plan daily driver)
    "z-ai/glm-5.3-flash",
    # free
    "poolside/laguna-s-2.1-free", "inclusionai/ling-3.0-flash-sante:free",
    "inclusionai/ling-3.1-flash:free",
    # open-weight, Go plan and above
    "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-flash-fast", "deepseek/deepseek-v4-flash-vision-exp",
    "deepseek/deepseek-v4.1-flash",
    "moonshotai/Kimi-K3", "moonshotai/Kimi-K2.7-Code",
    "moonshotai/Kimi-K2.7-Code-Highspeed", "moonshotai/Kimi-K2.6",
    "moonshotai/Kimi-K2.5",
    "zai-org/GLM-5.3", "zai-org/GLM-5.2",
    "zai-org/GLM-5.2-Fast", "zai-org/GLM-5.1", "zai-org/GLM-5",
    "MiniMaxAI/MiniMax-M3", "MiniMaxAI/MiniMax-M2.7", "MiniMaxAI/MiniMax-M2.5",
    "xiaomi/mimo-v2.5-pro", "xiaomi/mimo-v2.5",
    "Qwen/Qwen3.8-Max-0902", "Qwen/Qwen3.8-Max", "Qwen/Qwen3.8-Flash",
    "Qwen/Qwen3.8-27B", "Qwen/Qwen3.7-Max", "Qwen/Qwen3.7-Plus",
    "Qwen/Qwen3.7-Flash", "Qwen/Qwen3.6-Max-Preview", "Qwen/Qwen3.6-Plus",
    "stepfun/Step-3.7-Flash", "stepfun/Step-3.5-Flash",
    "tencent/hy4-preview", "tencent/hy3-paid",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "thinkingmachines/inkling", "thinkingmachines/inkling-small",
    "meituan/LongCat-2.0",
    # premium on Go
    "gpt-5.6-luna", "xai/grok-4.5",
    "meta/muse-spark-1.2-contributor", "meta/muse-spark-1.3-contributor",
]


def _redact(s):
    """Mask credential-shaped strings before they reach logs or the DB."""
    s = re.sub(r"user_[A-Za-z0-9_\-]{4,}", "user_***", s)
    s = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-***", s)
    return s


def log(*a):
    sys.stderr.write("[cc-proxy] " + _redact(" ".join(str(x) for x in a)) + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- translation
def _text_of(content):
    """OpenAI content (str | list) -> plain text."""
    if isinstance(content, str):
        return content
    out = []
    for part in content or []:
        if isinstance(part, str):
            out.append(part)
        elif part.get("type") == "text":
            out.append(part.get("text", ""))
    return "\n".join(out)


def _user_parts(content):
    """OpenAI user content -> Vercel AI SDK content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    parts = []
    for part in content or []:
        t = part.get("type")
        if t == "text":
            parts.append({"type": "text", "text": part.get("text", "")})
        elif t == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                head, _, b64 = url.partition(",")
                media = head[5:].split(";")[0] or "image/png"
                parts.append({"type": "image", "image": url, "mediaType": media})
            else:
                parts.append({"type": "text", "text": "[image: %s]" % url})
    return parts or [{"type": "text", "text": ""}]


def _keyhash16(auth_header):
    return hashlib.sha256((auth_header or "").encode()).hexdigest()[:16]


_FP_USERS = ["dev", "alex", "chen", "marcus", "wei", "dmitri"]
_FP_SLUGS = ["app", "api-server", "web-client", "cli-tools", "billing-worker"]
_FP_BRANCHES = ["main", "master", "develop", "feat/api-v2"]


def _device_profile(auth_header):
    """Deterministic fake project context, stable per upstream key.

    A real CLI sends a populated config envelope (project dir, git state).
    Sending empty fields forever is itself an outlier signal, and sending the
    REAL machine is a privacy leak - so we fabricate one consistent "normal
    Windows dev machine" per key: same account always sees the same device,
    nothing about the actual host ever leaves the machine."""
    kh = _keyhash16(auth_header)

    def pick(field, items):
        h = hashlib.sha256(("%s\0%s" % (kh, field)).encode()).hexdigest()
        return items[int(h[:8], 16) % len(items)]

    user = pick("user", _FP_USERS)
    slug = pick("slug", _FP_SLUGS)
    branch = pick("branch", _FP_BRANCHES)
    return {
        "workingDir": "C:\\Users\\%s\\projects\\%s" % (user, slug),
        "isGitRepo": True,
        "currentBranch": branch,
        "mainBranch": "main",
        "gitStatus": "",
        "structure": [],
        "recentCommits": [],
    }


def _session_for(auth_header):
    """Stable session id per upstream key, rotating every 12h (+jitter).
    Mirrors the official CLI (one device = one long-lived session); deriving
    from the key - never from message content - keeps conversations unlinkable
    across sessions and survives restarts."""
    keyhash = _keyhash16(auth_header)
    jitter = int(hashlib.sha256(("jitter:" + keyhash).encode()).hexdigest()[:8], 16) % 3600
    bucket = int((time.time() + jitter) // SESSION_TTL_S)
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          "cc-go-gateway:%s:%d" % (keyhash, bucket)))


def translate_body(openai_body, auth_header=""):
    """OpenAI chat body -> envelope (device context faked from the key)."""
    system_parts, msgs, tool_names = [], [], {}
    pending_tool_results = []

    def flush_tools():
        nonlocal pending_tool_results
        if pending_tool_results:
            msgs.append({"role": "tool", "content": pending_tool_results})
            pending_tool_results = []

    for m in openai_body.get("messages") or []:
        role = m.get("role")
        if role in ("system", "developer"):
            system_parts.append(_text_of(m.get("content")))
        elif role == "user":
            flush_tools()
            msgs.append({"role": "user", "content": _user_parts(m.get("content"))})
        elif role == "assistant":
            flush_tools()
            blocks = []
            txt = _text_of(m.get("content"))
            if txt:
                blocks.append({"type": "text", "text": txt})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                cid = tc.get("id") or ("call_" + uuid.uuid4().hex[:12])
                tool_names[cid] = fn.get("name", "")
                blocks.append({"type": "tool-call", "toolCallId": cid,
                               "toolName": fn.get("name", ""), "input": args})
            msgs.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            value = _text_of(m.get("content"))
            cid = m.get("tool_call_id") or ""
            pending_tool_results.append({
                "type": "tool-result", "toolCallId": cid,
                "toolName": tool_names.get(cid, ""),
                "output": {"type": "text", "value": value},
            })
    flush_tools()

    tools = []
    for t in openai_body.get("tools") or []:
        fn = t.get("function") if t.get("type") == "function" else t
        if fn and fn.get("name"):
            tools.append({"name": fn["name"],
                          "description": fn.get("description", ""),
                          "input_schema": fn.get("parameters")
                          or fn.get("input_schema") or {"type": "object"}})

    # empty system -> single space placeholder: upstream otherwise injects its
    # own ~7.5K-token default prompt (MAXeaglet/commandcode-proxy issue #17)
    params = {"model": openai_body.get("model", ""),
              "system": "\n\n".join(p for p in system_parts if p) or " ",
              "messages": msgs, "tools": tools,
              "max_tokens": openai_body.get("max_completion_tokens")
              or openai_body.get("max_tokens") or 32000,
              "stream": True}
    if openai_body.get("temperature") is not None:
        params["temperature"] = openai_body["temperature"]
    if openai_body.get("top_p") is not None:
        params["top_p"] = openai_body["top_p"]

    profile = _device_profile(auth_header)
    envelope = {
        "config": {**profile, "date": time.strftime("%Y-%m-%d"),
                   "environment": "production"},
        "memory": "", "taste": None, "skills": None,
        "permissionMode": "standard", "params": params,
    }
    return envelope


# ------------------------------------------------------------------- upstream

def _env_proxy_args():
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    return ["-x", proxy] if proxy else []


class UpstreamBusy(Exception):
    pass


INFLIGHT = {"n": 0}
_INFLIGHT_LOCK = threading.Lock()


def upstream_generate(envelope, session, auth_header):
    """POST /alpha/generate via curl subprocess (curl's TLS fingerprint passes
    the gateway's Cloudflare bot check; python-urllib gets 403 error 1010).
    Returns (proc, req_tmp_path, err_tmp_path); NDJSON body on stdout.
    Raises UpstreamBusy when the global in-flight cap is reached."""
    with _INFLIGHT_LOCK:
        if MAX_INFLIGHT > 0 and INFLIGHT["n"] >= MAX_INFLIGHT:
            raise UpstreamBusy("in-flight limit reached (%d)" % MAX_INFLIGHT)
        INFLIGHT["n"] += 1
    url = UPSTREAM + "/alpha/generate"
    fd, reqpath = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "wb") as f:
        f.write(json.dumps(envelope).encode())
    errfd, errpath = tempfile.mkstemp(suffix=".err")
    os.close(errfd)
    errfh = open(errpath, "wb")
    cmd = [CURL, "-sS", "-N", "--no-buffer", "-X", "POST", url,
           "-H", "Content-Type: application/json",
           "-H", "Authorization: " + auth_header,
           "-H", "x-cli-environment: production",
           "-H", "x-command-code-version: " + VERSION,
           "-H", "x-project-slug: " + PROJECT_SLUG,
           "-H", "x-session-id: " + session,
           "--data-binary", "@" + reqpath,
           "-o", "-",  # body -> stdout (we stream from the pipe)
           "--max-time", "600"] + _env_proxy_args()
    log("upstream ->", session[:8], "model=" + envelope["params"]["model"])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfh)
    errfh.close()
    return proc, reqpath, errpath


def ndjson_lines(proc):
    """Unbounded NDJSON reader over the curl stdout pipe."""
    buf = b""
    while True:
        chunk = proc.stdout.read(65536)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, _, buf = buf.partition(b"\n")
            line = line.strip()
            if line:
                yield line
    if buf.strip():
        yield buf.strip()


class UpstreamError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _status_from_error(err):
    s = json.dumps(err).lower()
    if "unauthorized" in s or ("invalid" in s and "token" in s) or "authorization" in s:
        return 401
    if "upgrade_required" in s or "api access" in s or "plan" in s:
        return 403
    if "usage" in s or "credit" in s or "quota" in s or "limit" in s:
        return 402
    if "bad_request" in s or "validation" in s or "expected" in s:
        return 400
    return 502


def run_generate(openai_body, auth_header):
    """Yield ('text'|'reasoning', s) | ('tool_start', {..}) | ('done', {..})"""
    envelope = translate_body(openai_body, auth_header)
    usage, finish, cost = None, None, None
    toolcalls, tindex = {}, -1
    session = _session_for(auth_header)
    proc = reqpath = errpath = None
    # bounded transparent retry: upstream flaps (CF hiccup, reset before any
    # byte) are absorbed here so the client never resends a huge context.
    # NEVER retries once output has been yielded (semantics already committed),
    # and never retries upstream *semantic* errors (401/402/403/429/400).
    for attempt in range(UPSTREAM_RETRY_MAX + 1):
        try:
            proc, reqpath, errpath = upstream_generate(
                envelope, _session_for(auth_header), auth_header)
            break
        except UpstreamBusy:
            raise
        except OSError:
            if attempt >= UPSTREAM_RETRY_MAX:
                raise
            time.sleep(UPSTREAM_RETRY_BASE_MS / 1000.0 * (attempt + 1))
    try:
        for line in ndjson_lines(proc):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "error" or ("error" in ev and not t):
                err = ev.get("error") or ev
                raise UpstreamError(_status_from_error(err),
                                    json.dumps(err)[:500])
            elif t == "text-delta":
                yield ("text", ev.get("text", ""))
            elif t == "reasoning-delta":
                yield ("reasoning", ev.get("text", ""))
            elif t in ("tool-input-start", "tool-call"):
                cid = ev.get("id") or ev.get("toolCallId")
                if cid and cid not in toolcalls:
                    tindex += 1
                    toolcalls[cid] = {"index": tindex, "id": cid,
                                      "name": ev.get("toolName", ""), "args": ""}
                    yield ("tool_start", {"index": tindex, "id": cid,
                                          "name": ev.get("toolName", "")})
                if t == "tool-call" and ev.get("input") is not None:
                    tc = toolcalls.get(cid)
                    if tc:
                        tc["args"] = json.dumps(ev["input"])
            elif t == "tool-input-delta":
                tc = toolcalls.get(ev.get("id"))
                if tc:
                    tc["args"] += ev.get("delta", "")
            elif t in ("finish-step", "finish"):
                if ev.get("usage"):
                    usage = ev["usage"]
                if ev.get("totalUsage"):
                    usage = ev["totalUsage"]
                if ev.get("finishReason"):
                    finish = ev["finishReason"]
                gw = (ev.get("providerMetadata") or {}).get("gateway") or {}
                if gw.get("cost"):
                    try:
                        cost = float(gw["cost"])
                    except (TypeError, ValueError):
                        cost = None
        # EOF without finish event
        if finish is None and usage is None and not toolcalls:
            proc.wait(timeout=10)
            stderr_txt = ""
            try:
                with open(errpath, "r", encoding="utf-8", errors="replace") as f:
                    stderr_txt = f.read()[:300]
            except OSError:
                pass
            raise UpstreamError(502,
                                "upstream ended without finish event" +
                                ("; curl: " + stderr_txt if stderr_txt else ""))
    finally:
        try:
            if proc is not None and proc.poll() is None:
                proc.kill()
        except OSError:
            pass
        for pth in (reqpath, errpath):
            if pth:
                try:
                    os.unlink(pth)
                except OSError:
                    pass
        with _INFLIGHT_LOCK:
            INFLIGHT["n"] = max(0, INFLIGHT["n"] - 1)
    yield ("done", {"usage": usage, "finish": finish or "stop",
                    "toolcalls": toolcalls, "cost": cost, "session": session})


def openai_usage(u):
    if not u:
        return None
    inp = u.get("inputTokens", 0)
    cached = u.get("cachedInputTokens", 0)
    out = u.get("outputTokens", 0)
    d = {"prompt_tokens": inp, "completion_tokens": out,
         "total_tokens": u.get("totalTokens", inp + out)}
    if cached:
        d["prompt_tokens_details"] = {"cached_tokens": cached}
    return d


# ------------------------------------------------------------------- recorder
class Recorder:
    _lock = threading.Lock()

    def _conn(self):
        c = sqlite3.connect(DB_PATH, timeout=10)
        c.execute("""CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, model TEXT, status INTEGER, stream INTEGER,
            input_tokens INTEGER, output_tokens INTEGER,
            cached_tokens INTEGER, reasoning_tokens INTEGER,
            cost_usd REAL, latency_ms INTEGER,
            finish TEXT, error TEXT, client TEXT, session TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_usage_ts ON usage(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_usage_model ON usage(model)")
        return c

    def record(self, **kw):
        cols = ("ts model status stream input_tokens output_tokens "
                "cached_tokens reasoning_tokens cost_usd latency_ms finish "
                "error client session").split()
        vals = [kw.get(c) for c in cols]
        vals[cols.index("model")] = _redact(str(vals[cols.index("model")] or ""))
        vals[cols.index("error")] = _redact(vals[cols.index("error")]) if vals[cols.index("error")] else None
        try:
            with self._lock:
                c = self._conn()
                c.execute("INSERT INTO usage (%s) VALUES (%s)"
                          % (",".join(cols), ",".join("?" * len(cols))), vals)
                c.commit()
                c.close()
        except sqlite3.Error as e:
            # loud: a silent DB failure = empty dashboard with a "working" proxy
            log("!! RECORDER WRITE FAILED:", _redact(str(e)), "| db:", DB_PATH)

    @staticmethod
    def _since(rng):
        lt = time.localtime()
        if rng == "today":
            return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                0, 0, 0, 0, 0, -1))
        if rng == "24h":
            return time.time() - 86400
        if rng == "7d":
            return time.time() - 7 * 86400
        if rng == "month":
            return time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
        if rng == "lastmonth":
            y, m = (lt.tm_year - 1, 12) if lt.tm_mon == 1 else (lt.tm_year, lt.tm_mon - 1)
            return time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1))
        return 0

    def stats(self, rng="all"):
        since = self._since(rng)
        out = {"range": rng, "requests": 0, "success": 0, "errors": 0,
               "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
               "reasoning_tokens": 0, "cost_usd": 0.0, "avg_latency_ms": 0,
               "models": []}
        try:
            with self._lock:
                c = self._conn()
                r = c.execute(
                    "SELECT COUNT(*), SUM(status<400), SUM(status>=400), "
                    "SUM(COALESCE(input_tokens,0)), "
                    "SUM(COALESCE(output_tokens,0)), "
                    "SUM(COALESCE(cached_tokens,0)), "
                    "SUM(COALESCE(reasoning_tokens,0)), "
                    "SUM(COALESCE(cost_usd,0)), AVG(latency_ms) "
                    "FROM usage WHERE ts>=?", (since,)).fetchone()
                rows = c.execute(
                    "SELECT model, COUNT(*), SUM(status<400), SUM(status>=400),"
                    " SUM(COALESCE(input_tokens,0)),"
                    " SUM(COALESCE(output_tokens,0)),"
                    " SUM(COALESCE(cached_tokens,0)),"
                    " SUM(COALESCE(reasoning_tokens,0)),"
                    " SUM(COALESCE(cost_usd,0))"
                    " FROM usage WHERE ts>=? GROUP BY model"
                    " ORDER BY COUNT(*) DESC", (since,)).fetchall()
                c.close()
            if r and r[0]:
                out.update({"requests": r[0], "success": r[1] or 0,
                            "errors": r[2] or 0, "input_tokens": r[3] or 0,
                            "output_tokens": r[4] or 0,
                            "cached_tokens": r[5] or 0,
                            "reasoning_tokens": r[6] or 0,
                            "cost_usd": round(r[7] or 0.0, 6),
                            "avg_latency_ms": int(r[8] or 0)})
            out["models"] = [
                {"model": m or "?", "requests": n, "success": s or 0,
                 "errors": e or 0, "input": i or 0, "output": o or 0,
                 "cached": ca or 0, "reasoning": re_ or 0,
                 "cost_usd": round(co or 0.0, 6)}
                for (m, n, s, e, i, o, ca, re_, co) in rows]
        except sqlite3.Error as e:
            out["error"] = str(e)
        return out

    def recent(self, limit=50):
        try:
            with self._lock:
                c = self._conn()
                rows = c.execute(
                    "SELECT ts, model, status, stream, input_tokens, "
                    "output_tokens, cached_tokens, reasoning_tokens, "
                    "cost_usd, latency_ms, finish, error, client "
                    "FROM usage ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                c.close()
            return [{"ts": r[0], "model": r[1], "status": r[2],
                     "stream": bool(r[3]), "input": r[4] or 0,
                     "output": r[5] or 0, "cached": r[6] or 0,
                     "reasoning": r[7] or 0,
                     "cost_usd": round(r[8], 6) if r[8] is not None else None,
                     "latency_ms": r[9], "finish": r[10],
                     "error": r[11], "client": r[12]} for r in rows]
        except sqlite3.Error as e:
            return [{"error": str(e)}]


REC = Recorder()


# --------------------------------------------------------------------- server
DASHBOARD_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Command Code 网关看板</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--fg:#e6edf3;--muted:#8b949e;--line:#21262d;
--accent:#58a6ff;--ok:#3fb950;--bad:#f85149}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,
"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;padding:20px}
h1{font-size:18px;font-weight:600;display:flex;align-items:center;gap:10px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--ok);display:inline-block}
.dot.err{background:var(--bad)}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:14px 0}
button{background:var(--card);color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:5px 12px;cursor:pointer;font-size:13px}
button.on{border-color:var(--accent);color:var(--accent)}
label{color:var(--muted);font-size:13px;display:flex;gap:5px;align-items:center}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
gap:10px;margin:14px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px 14px}
.kpi .v{font-size:20px;font-weight:600;margin-top:2px}
.kpi .v.ok{color:var(--ok)}.kpi .v.ac{color:var(--accent)}
.kpi .l{color:var(--muted);font-size:12px}
h2{font-size:14px;color:var(--muted);margin:18px 0 8px;font-weight:600}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:500;background:#1a2029;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
.st{font-weight:600}.st.ok{color:var(--ok)}.st.bad{color:var(--bad)}
.errtx{color:var(--bad);font-size:12px;max-width:320px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.muted{color:var(--muted)}
.foot{margin-top:16px;color:var(--muted);font-size:12px}
code{background:#1a2029;border-radius:4px;padding:1px 6px;font-size:12px}
.gh{color:var(--muted);display:inline-flex;margin-left:auto}
.gh:hover{color:var(--fg)}
</style></head><body>
<h1><span class="dot" id="dot"></span>Command Code 网关看板
<span class="muted" style="font-size:12px" id="sub"></span>
<a class="gh" href="https://github.com/Nice9z/commandcode-go-proxy"
   target="_blank" rel="noopener" title="GitHub 仓库">
<svg viewBox="0 0 16 16" width="22" height="22" fill="currentColor"
     aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47
     7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-
     .09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.
     72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95
     0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18
     1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08
     2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54
     1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16
     8c0-4.42-3.58-8-8-8Z"/></svg></a></h1>
<div class="bar">
  <button data-r="today">今天</button>
  <button data-r="24h">24小时</button>
  <button data-r="7d">7天</button>
  <button data-r="month">本月</button>
  <button data-r="lastmonth">上月</button>
  <button data-r="all" class="on">全部</button>
  <label><input type="checkbox" id="auto" checked> 自动刷新(5s)</label>
  <span class="muted" id="upd"></span>
</div>
<div class="kpis" id="kpis"></div>
<h2>按模型</h2>
<table id="models"><thead><tr>
<th>模型</th><th class="num">请求</th><th class="num">成功</th>
<th class="num">失败</th><th class="num">输入</th><th class="num">输出</th>
<th class="num">缓存读</th><th class="num">推理</th><th class="num">成本USD</th>
</tr></thead><tbody></tbody></table>
<h2>最近请求</h2>
<table id="recent"><thead><tr>
<th>时间</th><th>模型</th><th>状态</th><th class="num">输入</th>
<th class="num">输出</th><th class="num">缓存</th><th class="num">推理</th>
<th class="num">成本USD</th><th class="num">延迟</th><th>错误</th>
</tr></thead><tbody></tbody></table>
<div class="foot">
端点：<code>POST /v1/chat/completions</code>（OpenAI 兼容）
· <code>GET /v1/models</code> · 本页 <code>GET /dashboard</code>
· 成本为上游 <code>gateway.cost</code> 真实美元价，Go 计划 credit 扣除另有倍率
· <span id="ver">版本读取中…</span>
</div>
<script>
let RANGE='all';
const $=s=>document.querySelector(s);
function authHeaders(){const k=localStorage.getItem('cc_key');
  return k?{'Authorization':'Bearer '+k}:{}}
function fmtN(n){if(n==null)return'-';if(n>=1e6)return(n/1e6).toFixed(1)+'M';
  if(n>=1e4)return(n/1e3).toFixed(1)+'k';return n.toLocaleString()}
function fmtC(c){if(c==null)return'-';
  if(c===0)return'0';
  return'$'+parseFloat(c.toFixed(6)).toString()}
function kpi(l,v,cls){return '<div class="kpi"><div class="l">'+l+'</div>'+
  '<div class="v '+(cls||'')+'">'+v+'</div></div>'}
function esc(s){return (s||'').replace(/"/g,'&quot;').replace(/</g,'&lt;')}
async function load(){
  try{
    const [s,r]=await Promise.all([
      fetch('/api/stats?range='+RANGE,{headers:authHeaders()}).then(x=>x.json()),
      fetch('/api/recent?limit=50',{headers:authHeaders()}).then(x=>x.json())]);
    $('#dot').className='dot';
    $('#sub').textContent='已连接';
    const sr=(s.success||0)+(s.errors||0);
    const rate=sr?Math.round(100*s.success/sr):100;
    $('#kpis').innerHTML=
      kpi('请求数',fmtN(s.requests))+
      kpi('成功率',rate+'%','ok')+
      kpi('输入 tokens',fmtN(s.input_tokens))+
      kpi('输出 tokens',fmtN(s.output_tokens),'ac')+
      kpi('缓存读 tokens',fmtN(s.cached_tokens))+
      kpi('推理 tokens',fmtN(s.reasoning_tokens))+
      kpi('上游成本',fmtC(s.cost_usd),'ok')+
      kpi('平均延迟',(s.avg_latency_ms||0)+' ms');
    $('#models tbody').innerHTML=(s.models||[]).map(m=>
      '<tr><td>'+esc(m.model)+'</td><td class="num">'+m.requests+'</td>'+
      '<td class="num" style="color:var(--ok)">'+m.success+'</td>'+
      '<td class="num" style="color:'+(m.errors?'var(--bad)':'inherit')+'">'+m.errors+'</td>'+
      '<td class="num">'+fmtN(m.input)+'</td><td class="num">'+fmtN(m.output)+'</td>'+
      '<td class="num">'+fmtN(m.cached)+'</td><td class="num">'+fmtN(m.reasoning)+'</td>'+
      '<td class="num">'+fmtC(m.cost_usd)+'</td></tr>').join('')
      ||'<tr><td colspan="9" class="muted">暂无数据</td></tr>';
    $('#recent tbody').innerHTML=(r.rows||[]).map(x=>{
      const d=new Date(x.ts*1000);
      const st=x.status<400?'<span class="st ok">✓ '+x.status+'</span>'
        :'<span class="st bad">✗ '+x.status+'</span>';
      return '<tr><td class="muted">'+d.toLocaleString('zh-CN',{hour12:false})+'</td>'+
      '<td>'+esc(x.model)+'</td><td>'+st+'</td>'+
      '<td class="num">'+fmtN(x.input)+'</td><td class="num">'+fmtN(x.output)+'</td>'+
      '<td class="num">'+fmtN(x.cached)+'</td><td class="num">'+fmtN(x.reasoning)+'</td>'+
      '<td class="num">'+fmtC(x.cost_usd)+'</td>'+
      '<td class="num muted">'+(x.latency_ms==null?'-':x.latency_ms+'ms')+'</td>'+
      '<td class="errtx" title="'+esc(x.error)+'">'+esc(x.error||'')+'</td>'+
      '</tr>'}).join('')
      ||'<tr><td colspan="10" class="muted">暂无请求</td></tr>';
    $('#upd').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
    if(!window._verLoaded){
      window._verLoaded=true;
      fetch('/healthz',{headers:authHeaders()}).then(x=>x.json()).then(h=>{
        const el=document.getElementById('ver');
        el.textContent='当前版本 v'+h.app_version;
        if(h.latest_version && h.latest_version!==h.app_version){
          el.innerHTML='当前版本 v'+h.app_version+' - <b style="color:var(--warn,#d29922)">有新版本 v'+h.latest_version+
            '，更新方法见 README</b>';
        }
      }).catch(()=>{});
    }
  }catch(e){
    $('#dot').className='dot err';$('#sub').textContent='连接失败: '+e;
  }
}
document.querySelectorAll('button[data-r]').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('button[data-r]').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');RANGE=b.dataset.r;load();});
setInterval(()=>{if($('#auto').checked)load()},5000);
load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(fmt % args)

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _key_ok(self):
        """Gateway key check (for VPS exposure). A client-supplied user_
        token always passes through as upstream auth instead."""
        if not PROXY_KEY:
            return True
        a = self.headers.get("Authorization", "")
        if a == "Bearer " + PROXY_KEY:
            return True
        if a.lower().startswith("bearer user_"):
            return True
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return q.get("key", [""])[0] == PROXY_KEY

    def _auth(self):
        if TOKEN:
            return "Bearer " + TOKEN
        a = self.headers.get("Authorization", "")
        return a if a.lower().startswith("bearer user_") else ""

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if p == "/dashboard":
            body = DASHBOARD_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if p in ("/api/stats", "/api/recent", "/v1/models", "/models",
                 "/healthz", "/v1/healthz"):
            # healthz from the machine itself is always allowed (monitoring)
            if p in ("/healthz", "/v1/healthz") and self.client_address[0] in ("127.0.0.1", "::1"):
                pass
            elif not self._key_ok():
                self._json(401, {"error": {"message":
                    "invalid gateway key (set CMD_CODE_KEY server-side; "
                    "send it as Bearer or ?key=)"}})
                return
            if p == "/api/stats":
                rng = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query).get(
                    "range", ["all"])[0]
                self._json(200, REC.stats(
                    rng if rng in ("today", "24h", "7d", "month",
                                   "lastmonth", "all") else "all"))
            elif p == "/api/recent":
                try:
                    lim = int(urllib.parse.parse_qs(
                        urllib.parse.urlparse(self.path).query).get(
                        "limit", ["50"])[0])
                except ValueError:
                    lim = 50
                self._json(200, {"rows": REC.recent(max(1, min(lim, 500)))})
            elif p in ("/v1/models", "/models"):
                now = int(time.time())
                self._json(200, {"object": "list", "data": [
                    {"id": m, "object": "model", "created": now,
                     "owned_by": "command-code"} for m in MODELS]})
            else:
                db_ok = True
                try:
                    _c = REC._conn()
                    _c.execute("SELECT 1 FROM usage LIMIT 1")
                    _c.close()
                except sqlite3.Error:
                    db_ok = False
                self._json(200, {"ok": True, "upstream": UPSTREAM,
                                 "version_header": VERSION,
                                 "app_version": APP_VERSION,
                                 "latest_version": latest_github_version(),
                                 "token_set": bool(TOKEN),
                                 "gateway_key_required": bool(PROXY_KEY),
                                 "db": DB_PATH, "db_writable": db_ok})
        else:
            self._json(404, {"error": {"message": "not found: " + p}})

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path.rstrip("/")
        if p not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": {"message": "not found: " + p}})
            return
        if not self._key_ok():
            self._json(401, {"error": {"message":
                "invalid gateway key. Find yours on the server: cat /etc/cc-go-proxy.env (line CMD_CODE_KEY=...), then send header: Authorization: Bearer <that key>"}})
            return
        started = time.monotonic()
        client = self.client_address[0]

        def _record(status, finish=None, usage=None, cost=None,
                    error=None, session=None, rmodel="unknown"):
            u = usage or {}
            REC.record(ts=time.time(), model=rmodel, status=status,
                       stream=0,
                       input_tokens=u.get("inputTokens"),
                       output_tokens=u.get("outputTokens"),
                       cached_tokens=u.get("cachedInputTokens"),
                       reasoning_tokens=u.get("reasoningTokens"),
                       cost_usd=cost,
                       latency_ms=int((time.monotonic() - started) * 1000),
                       finish=finish, error=(error or "")[:300] or None,
                       client=client, session=session)

        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            _record(400, error="invalid JSON body")
            self._json(400, {"error": {"message": "invalid JSON body"}})
            return

        auth = self._auth()
        if not auth:
            _record(401, error="no API key configured")
            self._json(401, {"error": {"message":
                "no API key: set CMD_CODE_TOKEN env or send "
                "Authorization: Bearer user_..."}})
            return

        model = body.get("model") or DEFAULT_MODEL
        body["model"] = model
        want_stream = bool(body.get("stream"))
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        log("req", model, "stream=%s" % want_stream)

        def _record(status, finish=None, usage=None, cost=None,
                    error=None, session=None, rmodel=None):
            u = usage or {}
            REC.record(ts=time.time(), model=rmodel or model, status=status,
                       stream=1 if want_stream else 0,
                       input_tokens=u.get("inputTokens"),
                       output_tokens=u.get("outputTokens"),
                       cached_tokens=u.get("cachedInputTokens"),
                       reasoning_tokens=u.get("reasoningTokens"),
                       cost_usd=cost,
                       latency_ms=int((time.monotonic() - started) * 1000),
                       finish=finish, error=(error or "")[:300] or None,
                       client=client, session=session)

        try:
            gen = run_generate(body, auth)
            if want_stream:
                self._stream_response(gen, cid, created, model, _record)
            else:
                self._buffered_response(gen, cid, created, model, _record)
        except UpstreamBusy as e:
            _record(503, error=str(e))
            self.send_response(503)
            self.send_header("Retry-After", "2")
            body = json.dumps({"error": {"message": str(e),
                "type": "busy", "code": 503}}).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        except UpstreamError as e:
            _record(e.status if e.status >= 400 else 502, error=e.message)
            self._json(e.status if e.status >= 400 else 502,
                       {"error": {"message": e.message, "type": "upstream_error",
                                  "code": e.status}})
        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected")
        except Exception as e:  # noqa: BLE001
            log("internal error:", repr(e))
            _record(500, error=repr(e))
            try:
                self._json(500, {"error": {"message": repr(e)}})
            except Exception:  # noqa: BLE001
                pass

    def _start_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _chunk(self, data: bytes):
        self.wfile.write(("%X\r\n" % len(data)).encode() + data + b"\r\n")
        self.wfile.flush()

    def _end_stream(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _sse(self, payload: dict):
        self._chunk(b"data: " + json.dumps(payload).encode() + b"\n\n")

    def _stream_response(self, gen, cid, created, model, rec):
        self._start_stream()
        base = {"id": cid, "object": "chat.completion.chunk",
                "created": created, "model": model}
        self._sse({**base, "choices": [{"index": 0,
                   "delta": {"role": "assistant", "content": ""},
                   "finish_reason": None}]})
        final = None
        for kind, item in gen:
            if kind == "text":
                self._sse({**base, "choices": [{"index": 0,
                           "delta": {"content": item}, "finish_reason": None}]})
            elif kind == "reasoning":
                self._sse({**base, "choices": [{"index": 0,
                           "delta": {"reasoning_content": item},
                           "finish_reason": None}]})
            elif kind == "tool_start":
                self._sse({**base, "choices": [{"index": 0, "delta": {
                    "tool_calls": [{"index": item["index"], "id": item["id"],
                                    "type": "function",
                                    "function": {"name": item["name"],
                                                 "arguments": ""}}]},
                    "finish_reason": None}]})
            elif kind == "done":
                final = item
                for tc in item["toolcalls"].values():
                    if tc["args"]:
                        self._sse({**base, "choices": [{"index": 0, "delta": {
                            "tool_calls": [{"index": tc["index"],
                                            "function": {"arguments": tc["args"]}}]},
                            "finish_reason": None}]})
        fr = (final or {}).get("finish", "stop")
        last = {**base, "choices": [{"index": 0, "delta": {},
                                     "finish_reason": "tool_calls"
                                     if fr == "tool-calls" else fr}]}
        u = openai_usage((final or {}).get("usage"))
        if u:
            last["usage"] = u
        self._sse(last)
        self._chunk(b"data: [DONE]\n\n")
        self._end_stream()
        rec(200, finish=fr, usage=(final or {}).get("usage"),
            cost=(final or {}).get("cost"),
            session=(final or {}).get("session"))
        log("done stream", model, fr)

    def _buffered_response(self, gen, cid, created, model, rec):
        text, reasoning, final = [], [], None
        for kind, item in gen:
            if kind == "text":
                text.append(item)
            elif kind == "reasoning":
                reasoning.append(item)
            elif kind == "done":
                final = item
        final = final or {"usage": None, "finish": "stop", "toolcalls": {},
                          "cost": None, "session": None}
        fr = final["finish"]
        msg = {"role": "assistant", "content": "".join(text)}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        tool_calls = [{"id": tc["id"], "type": "function",
                       "function": {"name": tc["name"], "arguments": tc["args"]}}
                      for tc in final["toolcalls"].values()]
        if tool_calls:
            msg["tool_calls"] = tool_calls
        out = {"id": cid, "object": "chat.completion", "created": created,
               "model": model,
               "choices": [{"index": 0, "message": msg,
                            "finish_reason": "tool_calls"
                            if fr == "tool-calls" else fr}],
               "usage": openai_usage(final["usage"])
               or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
        self._json(200, out)
        rec(200, finish=fr, usage=final.get("usage"), cost=final.get("cost"),
            session=final.get("session"))
        log("done buffered", model, fr)


if __name__ == "__main__":
    try:  # keep the usage DB private where the OS honors file modes
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass
    try:
        _c = REC._conn()
        _c.execute("INSERT INTO usage (ts, model, status) VALUES (?, 'startup-probe', 0)",
                   (time.time(),))
        _c.commit()
        _c.execute("DELETE FROM usage WHERE model='startup-probe'")
        _c.commit()
        _c.close()
        log("db write probe OK:", DB_PATH)
    except sqlite3.Error as e:
        log("!! DB NOT WRITABLE:", DB_PATH, "|", e)
        log("!! usage dashboard will stay EMPTY. Fix permissions or set CMD_CODE_DB.")
        sys.exit(1)
    if HOST == "0.0.0.0" and not PROXY_KEY:
        log("WARNING: binding 0.0.0.0 without CMD_CODE_KEY - the gateway is "
            "OPEN. Set CMD_CODE_KEY before exposing to the internet.")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log("listening on http://%s:%d  (dashboard: /dashboard)  ->  %s/alpha/generate"
        % (HOST, PORT, UPSTREAM))
    log("api key %s | gateway key %s | version header %s | db %s | curl %s"
        % ("set" if TOKEN else "NOT SET",
           "required" if PROXY_KEY else "open",
           VERSION, DB_PATH, CURL))
    srv.serve_forever()
