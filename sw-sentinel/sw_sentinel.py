#!/usr/bin/env python3
"""
SW-Sentinel — Superwise Guardrail Proxy for Anthropic API
==========================================================
A lightweight HTTP proxy that sits between any Anthropic API client
and api.anthropic.com. Every LLM call is intercepted and run through
Superwise guardrail checks before being forwarded.

Works with any tool that uses the Anthropic API:
  - Claude Code / Paperclip
  - Python apps using the anthropic SDK
  - LangChain, LlamaIndex, or any Anthropic-compatible framework

Setup (any app):
  1. Run: python3 sw_sentinel.py
  2. Set: export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
  3. Your app now routes through SW-Sentinel automatically

Configuration:
  Edit sentinel_config.json to customize guardrails, port, and behavior.
  See sentinel_config.json for all available options.

Usage:
  python3 sw_sentinel.py
  python3 sw_sentinel.py --config /path/to/sentinel_config.json
  python3 sw_sentinel.py --port 9090
"""

import os
import sys
import json
import re
import secrets
import logging
import argparse
import threading
import socketserver
import requests
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Default config path ────────────────────────────────────────────────────────

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "sentinel_config.json")

# ── Load configuration ─────────────────────────────────────────────────────────

def _default_config_body(client_id, client_secret, api_key="", host="127.0.0.1", port=8080):
    """Return a complete config dict with sensible defaults."""
    return {
        "superwise_client_id":      client_id,
        "superwise_client_secret":  client_secret,
        "proxy_host":               host,
        "proxy_port":               port,
        "anthropic_api_base":       "https://api.anthropic.com",
        "anthropic_api_key":        api_key,
        "proxy_token":              "",
        "upstream_timeout_seconds": 120,
        "on_superwise_error":       "fail_open",
        "max_check_chars":          2000,
        "log_level":                "INFO",
        "log_file":                 "sw_sentinel.log",
        "violation_log":            "sw_sentinel_violations.log",
        "blocked_input_message":    "This request has been blocked by compliance policy. The input content violated one or more security guardrails (PII, jailbreak attempt, or toxic content detected). Please reformulate your request.",
        "blocked_output_message":   "The model response has been blocked by compliance policy. The generated content contained sensitive information that cannot be returned. Please rephrase your request.",
        "injection_patterns": [
            "ignore\\s+(all\\s+)?(previous|prior|above|earlier)\\s+(instructions?|prompts?|directives?|rules?)",
            "disregard\\s+(all\\s+)?(previous|prior|above|earlier)\\s+(instructions?|prompts?|directives?|rules?)",
            "forget\\s+(all\\s+)?(previous|prior|above|earlier)\\s+(instructions?|prompts?|directives?|rules?)",
            "(reveal|print|show|repeat|output)\\s+(your\\s+)?(system\\s+prompt|original\\s+instructions?|true\\s+instructions?)",
            "\\bdo\\s+anything\\s+now\\b",
            "override\\s+(your\\s+)?(safety|content\\s+policy|guidelines?|restrictions?|constraints?)"
        ],
        "skip_patterns": [],
        "guardrails": {
            "input": {
                "pii_detection":      {"enabled": True,  "threshold": 0.5, "categories": ["US_SSN", "CREDIT_CARD", "US_BANK_NUMBER"]},
                "jailbreak_detection":{"enabled": True},
                "toxicity_detection": {"enabled": False, "threshold": 0.5}
            },
            "output": {
                "pii_detection":      {"enabled": True,  "threshold": 0.5, "categories": ["US_SSN", "CREDIT_CARD", "US_BANK_NUMBER"]},
                "toxicity_detection": {"enabled": False, "threshold": 0.5}
            }
        }
    }

def _config_from_env():
    """Build config from environment variables (Docker / CI mode)."""
    return _default_config_body(
        client_id=os.environ["SUPERWISE_CLIENT_ID"],
        client_secret=os.environ["SUPERWISE_CLIENT_SECRET"],
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        host=os.environ.get("SENTINEL_HOST", "0.0.0.0"),
        port=int(os.environ.get("SENTINEL_PORT", "8080")),
    )

def run_init_wizard(config_path):
    """Interactive first-time setup wizard. Returns the saved config dict."""
    import getpass

    sep = "─" * 45
    print(f"\n  {sep}")
    print(f"  SW-Sentinel First-Time Setup")
    print(f"  {sep}")
    print(f"  No config found. Let's set things up.\n")
    print(f"  Superwise credentials  (app.superwise.ai → Settings):")

    client_id = input("    Client ID:      > ").strip()
    if not client_id:
        print("ERROR: Superwise Client ID is required.")
        sys.exit(1)

    client_secret = getpass.getpass("    Client Secret:  > ").strip()
    if not client_secret:
        print("ERROR: Superwise Client Secret is required.")
        sys.exit(1)

    print(f"\n  Anthropic API Key  (console.anthropic.com → API Keys):")
    api_key = getpass.getpass("    API Key (Enter to skip): > ").strip()

    print(f"\n  Proxy settings:")
    port_raw = input("    Port [8080]: > ").strip()
    port = int(port_raw) if port_raw.isdigit() else 8080

    config = _default_config_body(client_id, client_secret, api_key, port=port)
    proxy_token = secrets.token_urlsafe(32)
    config["proxy_token"] = proxy_token

    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)

    print(f"\n  {sep}")
    print(f"  Config saved → {config_path}")
    print(f"")
    print(f"  Proxy token (add to your app as X-Sentinel-Token header):")
    print(f"  {proxy_token}")
    print(f"  {sep}\n")
    return config

def load_config(config_path):
    """Load config from file, env vars, or first-run wizard."""

    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)
        required = ["superwise_client_id", "superwise_client_secret"]
        for field in required:
            if not config.get(field) or config[field].startswith("YOUR_"):
                print(f"ERROR: '{field}' not set in {config_path}")
                sys.exit(1)
        return config

    # No file — try environment variables (Docker / CI)
    if os.environ.get("SUPERWISE_CLIENT_ID") and os.environ.get("SUPERWISE_CLIENT_SECRET"):
        return _config_from_env()

    # No file, no env vars — run wizard if interactive, otherwise fail
    if sys.stdin.isatty():
        return run_init_wizard(config_path)

    print(f"ERROR: Config file not found: {config_path}")
    print(f"       Set SUPERWISE_CLIENT_ID and SUPERWISE_CLIENT_SECRET env vars,")
    print(f"       or run 'sw-sentinel init' to create a config file.")
    sys.exit(1)

# ── Globals (populated after config load) ─────────────────────────────────────

CONFIG                  = {}
SW_CLIENT               = None
SW_GUARDRAIL_VERSION_IDS = {}   # direction -> set of version UUIDs for run_versions()
INJECTION_RE            = []
log                     = None
_sw_lock                = threading.Lock()

# ── Logging setup ──────────────────────────────────────────────────────────────

def setup_logging(config):
    log_level   = getattr(logging, config.get("log_level", "INFO").upper(), logging.INFO)
    log_file    = config.get("log_file", "sw_sentinel.log")
    handlers    = [logging.StreamHandler(sys.stdout)]

    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers
    )
    return logging.getLogger("sw_sentinel")

# ── Superwise client ───────────────────────────────────────────────────────────

def init_sw_client():
    """Initialize the Superwise client once at startup."""
    global SW_CLIENT
    from superwise_api.superwise_client import SuperwiseClient
    SW_CLIENT = SuperwiseClient(
        client_id=CONFIG["superwise_client_id"],
        client_secret=CONFIG["superwise_client_secret"]
    )

def init_sw_guardrails():
    """Create or retrieve persistent Superwise guardrails so checks appear in the dashboard."""
    global SW_GUARDRAIL_VERSION_IDS

    for direction in ["input", "output"]:
        guards = build_guards(direction)
        if not guards:
            continue

        name = f"SW-Sentinel {direction.title()}"
        try:
            page  = SW_CLIENT.guardrails.get(search=name, size=25)
            items = page.items if hasattr(page, "items") else (page if isinstance(page, list) else [])
            existing = [g for g in items if g.name == name]

            if existing:
                guardrail = existing[0]
                if guardrail.current_version:
                    SW_GUARDRAIL_VERSION_IDS[direction] = {guardrail.current_version.id}
                    log.info(f"  SW guardrail [{direction}]: existing (id={str(guardrail.id)[:8]}...)")
                    continue
                guardrail_id = str(guardrail.id)
            else:
                guardrail    = SW_CLIENT.guardrails.create(
                    name=name,
                    description=f"SW-Sentinel proxy {direction} guardrail"
                )
                guardrail_id = str(guardrail.id)

            version = SW_CLIENT.guardrails.create_version(
                guardrail_id=guardrail_id,
                name="v1",
                guardrules=guards
            )
            SW_GUARDRAIL_VERSION_IDS[direction] = {version.id}
            log.info(f"  SW guardrail [{direction}]: created (version={str(version.id)[:8]}...)")

        except Exception as e:
            log.warning(f"  SW guardrail [{direction}]: setup failed ({e}) — using stateless checks")

def init_injection_patterns():
    """Pre-compile injection pattern regexes from config at startup."""
    global INJECTION_RE
    INJECTION_RE = []
    for p in CONFIG.get("injection_patterns", []):
        try:
            INJECTION_RE.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            log.warning(f"Invalid injection pattern '{p}': {e}")
    if INJECTION_RE:
        log.info(f"  Injection patterns: {len(INJECTION_RE)} loaded")

def check_injection_patterns(text, request_id="unknown"):
    """Fast local check for known prompt injection signatures before Superwise API call."""
    if not INJECTION_RE or not text:
        return False, ""
    for pattern in INJECTION_RE:
        if pattern.search(text):
            log_violation(
                request_id, "input",
                [{"guard": "InjectionPatternMatch", "message": f"Matched pattern: {pattern.pattern}"}],
                ["PROMPT_INJECTION"],
                text[:300]
            )
            return True, pattern.pattern
    return False, ""

def build_guards(direction):
    """Build guardrail list from config."""
    from superwise_api.models.guardrails.guardrails import (
        ToxicityGuard, PiiDetectionGuard, DetectJailbreakGuard
    )

    guards      = []
    guard_cfg   = CONFIG.get("guardrails", {})
    dir_cfg     = guard_cfg.get(direction, {})

    if dir_cfg.get("pii_detection", {}).get("enabled", False):
        categories = set(dir_cfg["pii_detection"].get("categories", ["US_SSN", "CREDIT_CARD"]))
        threshold  = dir_cfg["pii_detection"].get("threshold", 0.5)
        guards.append(PiiDetectionGuard(
            name=f"Sentinel {direction.title()} PII",
            tags=[direction],
            threshold=threshold,
            categories=categories
        ))

    if dir_cfg.get("jailbreak_detection", {}).get("enabled", False):
        guards.append(DetectJailbreakGuard(
            name=f"Sentinel {direction.title()} Jailbreak",
            tags=[direction]
        ))

    if dir_cfg.get("toxicity_detection", {}).get("enabled", False):
        threshold = dir_cfg["toxicity_detection"].get("threshold", 0.5)
        guards.append(ToxicityGuard(
            name=f"Sentinel {direction.title()} Toxicity",
            tags=[direction],
            threshold=threshold,
            validation_method="sentence"
        ))

    return guards

# ── Guardrail check ────────────────────────────────────────────────────────────

def run_guardrail_check(text, direction, request_id="unknown"):
    """
    Run Superwise guardrail checks on text.
    Returns (passed: bool, violations: list, flags: list)
    Fails open if Superwise is unreachable (configurable).
    """
    if not text or not text.strip():
        return True, [], []

    max_chars = CONFIG.get("max_check_chars", 2000)
    log.info(f"Checking {len(text)} chars [{direction}] request_id={request_id}")

    try:
        if direction in SW_GUARDRAIL_VERSION_IDS:
            results = SW_CLIENT.guardrails.run_versions(
                tag=direction,
                ids=SW_GUARDRAIL_VERSION_IDS[direction],
                query=text[:max_chars]
            )
        else:
            guards = build_guards(direction)
            if not guards:
                return True, [], []
            results = SW_CLIENT.guardrails.run_guardrules(
                tag=direction,
                guardrules=guards,
                query=text[:max_chars]
            )

        violations = [
            {"guard": r.name, "message": r.message}
            for r in results if not r.valid
        ]

        if violations:
            flags = []
            for v in violations:
                if "Jailbreak" in v["guard"]:
                    flags.append("JAILBREAK")
                elif "PII" in v["guard"]:
                    flags.append("PII_DETECTED")
                elif "Toxicity" in v["guard"]:
                    flags.append("TOXIC")

            log_violation(request_id, direction, violations, flags, text[:300])
            return False, violations, flags

        return True, [], []

    except Exception as e:
        fail_behavior = CONFIG.get("on_superwise_error", "fail_open")
        log.error(f"Superwise error: {e} — {fail_behavior}")
        if fail_behavior == "fail_closed":
            return False, [{"guard": "ERROR", "message": str(e)}], ["SW_ERROR"]
        return True, [], []  # fail_open — allow request through

# ── Violation logging ──────────────────────────────────────────────────────────

def log_violation(request_id, direction, violations, flags, snippet):
    """Write violation to local audit log."""
    timestamp    = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    violation_log = CONFIG.get("violation_log", "sw_sentinel_violations.log")

    try:
        with _sw_lock:
            with open(violation_log, "a") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"TIMESTAMP:  {timestamp}\n")
                f.write(f"REQUEST_ID: {request_id}\n")
                f.write(f"DIRECTION:  {direction.upper()}\n")
                f.write(f"FLAGS:      {', '.join(flags)}\n")
                f.write(f"SNIPPET:    {snippet}\n")
                f.write(f"VIOLATIONS:\n")
                for v in violations:
                    f.write(f"  [{v['guard']}] {v['message']}\n")
        log.warning(f"BLOCKED [{direction.upper()}] request_id={request_id} flags={flags}")
    except Exception as e:
        log.error(f"Failed to write violation log: {e}")

# ── Text extraction ────────────────────────────────────────────────────────────

def extract_input_text(body):
    """Extract the most recent user message for guardrail checking."""
    skip_patterns = CONFIG.get("skip_patterns", [])

    try:
        # skip_patterns apply only to the system prompt — never to user content
        if skip_patterns:
            system = body.get("system", "")
            if isinstance(system, list):
                system_text = " ".join(b.get("text", "") for b in system if isinstance(b, dict))
            else:
                system_text = system or ""
            if any(p in system_text for p in skip_patterns):
                return ""

        messages = body.get("messages", [])
        for msg in reversed(messages):
            if msg.get("role") in ("user", "human"):
                content = msg.get("content", "")

                if isinstance(content, str):
                    content = content.strip()
                    if content:
                        return content[:CONFIG.get("max_check_chars", 2000)]

                elif isinstance(content, list):
                    texts = []
                    for block in content:
                        if not isinstance(block, dict):
                            if isinstance(block, str):
                                texts.append(block)
                            continue
                        if block.get("type") == "text":
                            val = block.get("text", "")
                            if isinstance(val, str):
                                texts.append(val.strip())
                        elif block.get("type") == "tool_result":
                            # Extract text from tool results — prompt injection blind spot
                            tr = block.get("content", "")
                            if isinstance(tr, str):
                                texts.append(tr.strip())
                            elif isinstance(tr, list):
                                for tb in tr:
                                    if isinstance(tb, dict) and tb.get("type") == "text":
                                        texts.append(tb.get("text", "").strip())
                    result = " ".join(texts).strip()
                    if result:
                        return result[:CONFIG.get("max_check_chars", 2000)]
    except Exception:
        pass
    return ""

def extract_output_text(body):
    """Extract assistant response text for guardrail checking."""
    texts = []
    try:
        content = body.get("content", [])
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
    except Exception:
        pass
    return " ".join(texts)[:CONFIG.get("max_check_chars", 2000)]

def extract_streaming_text(raw_content):
    """Extract text from Anthropic SSE streaming response for guardrail checking."""
    text_parts = []
    try:
        for line in raw_content.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                data = json.loads(line[6:])
                if data.get("type") == "content_block_delta":
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text_parts.append(delta.get("text", ""))
            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    return "".join(text_parts)[:CONFIG.get("max_check_chars", 2000)]

def make_blocked_response(request_body, message):
    """Construct a fake Anthropic API response for blocked requests."""
    return {
        "id":           f"sentinel_blocked_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "type":         "message",
        "role":         "assistant",
        "model":        request_body.get("model", "claude-sonnet-4-6"),
        "content":      [{"type": "text", "text": message}],
        "stop_reason":  "end_turn",
        "stop_sequence": None,
        "usage":        {"input_tokens": 0, "output_tokens": len(message.split())}
    }

# ── Threaded HTTP server ───────────────────────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True  # threads exit when main process exits

# ── HTTP proxy handler ─────────────────────────────────────────────────────────

class SentinelProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Suppress default HTTP server logging

    def _check_proxy_token(self):
        """Return True if proxy_token auth passes (or is not configured)."""
        required = CONFIG.get("proxy_token", "")
        if not required:
            return True
        return secrets.compare_digest(
            self.headers.get("X-Sentinel-Token", ""), required
        )

    def _reject_unauthorized(self):
        """Log and return 401 for requests with missing or invalid token."""
        client_ip = self.client_address[0]
        log.warning(f"UNAUTHORIZED request from {client_ip} — invalid X-Sentinel-Token [{self.command} {self.path}]")
        violation_log = CONFIG.get("violation_log", "sw_sentinel_violations.log")
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with _sw_lock:
                with open(violation_log, "a") as f:
                    f.write(f"\n{'='*60}\n")
                    f.write(f"TIMESTAMP:  {timestamp}\n")
                    f.write(f"EVENT:      UNAUTHORIZED_REQUEST\n")
                    f.write(f"CLIENT_IP:  {client_ip}\n")
                    f.write(f"REQUEST:    {self.command} {self.path}\n")
        except Exception as e:
            log.error(f"Failed to write violation log: {e}")
        self._send_error(401, "Unauthorized: missing or invalid X-Sentinel-Token")

    def do_POST(self):
        if not self._check_proxy_token():
            self._reject_unauthorized()
            return

        request_id     = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 10 * 1024 * 1024:  # 10 MB hard limit
            self._send_error(413, "Request body too large")
            return
        raw_body       = self.rfile.read(content_length) if content_length else b""

        try:
            request_body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            request_body = {}

        # ── INPUT CHECK ────────────────────────────────────────────────────────
        input_text = extract_input_text(request_body)

        if input_text:
            blocked, _ = check_injection_patterns(input_text, request_id)
            if blocked:
                msg = CONFIG.get("blocked_input_message",
                    "This request has been blocked by compliance policy. "
                    "The input content violated one or more security guardrails.")
                self._send_json_response(200, make_blocked_response(request_body, msg))
                return

            passed, violations, flags = run_guardrail_check(input_text, "input", request_id)
            if not passed:
                msg = CONFIG.get("blocked_input_message",
                    "This request has been blocked by compliance policy. "
                    "The input content violated one or more security guardrails.")
                self._send_json_response(200, make_blocked_response(request_body, msg))
                return

        # ── FORWARD TO ANTHROPIC ───────────────────────────────────────────────
        anthropic_base = CONFIG.get("anthropic_api_base", "https://api.anthropic.com")
        target_url     = f"{anthropic_base}{self.path}"
        fwd_headers    = self._build_forward_headers()

        log.info(f"Forwarding {self.path} → Anthropic (request_id={request_id})")

        try:
            resp = requests.post(
                target_url,
                headers=fwd_headers,
                data=raw_body,
                timeout=CONFIG.get("upstream_timeout_seconds", 120),
                stream=False
            )
        except requests.RequestException as e:
            log.error(f"Upstream request failed: {e}")
            self._send_error(502, f"Upstream error: {e}")
            return

        # ── OUTPUT CHECK ───────────────────────────────────────────────────────
        is_streaming = request_body.get("stream", False)

        if is_streaming:
            output_text = extract_streaming_text(resp.content)
        else:
            try:
                response_body = resp.json()
            except Exception:
                response_body = {}
            output_text = extract_output_text(response_body)

        if output_text:
            passed, violations, flags = run_guardrail_check(output_text, "output", request_id)
            if not passed:
                msg = CONFIG.get("blocked_output_message",
                    "The model response has been blocked by compliance policy. "
                    "The generated content contained sensitive information that cannot be returned.")
                self._send_json_response(200, make_blocked_response(request_body, msg))
                return

        # ── PASS THROUGH CLEAN RESPONSE ────────────────────────────────────────
        self._send_raw_response(resp.status_code, resp.headers, resp.content)

    def do_GET(self):
        if not self._check_proxy_token():
            self._reject_unauthorized()
            return
        anthropic_base = CONFIG.get("anthropic_api_base", "https://api.anthropic.com")
        target_url     = f"{anthropic_base}{self.path}"
        try:
            resp = requests.get(target_url, headers=self._build_forward_headers(), timeout=30)
            self._send_raw_response(resp.status_code, resp.headers, resp.content)
        except Exception as e:
            self._send_error(502, str(e))

    def _build_forward_headers(self):
        headers     = {}
        passthrough = {"content-type", "anthropic-version", "anthropic-beta", "x-api-key", "authorization"}
        for key, val in self.headers.items():
            if key.lower() in passthrough:
                headers[key] = val
        # Inject API key if configured and not already present
        api_key = CONFIG.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key and "x-api-key" not in {k.lower() for k in headers}:
            headers["x-api-key"] = api_key
        return headers

    def _send_json_response(self, status_code, body_dict):
        body_bytes = json.dumps(body_dict).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_raw_response(self, status_code, headers, body_bytes):
        self.send_response(status_code)
        passthrough = {"content-type", "x-request-id"}
        for key, val in headers.items():
            if key.lower() in passthrough:
                self.send_header(key, val)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_error(self, status_code, message):
        body       = json.dumps({"error": {"type": "proxy_error", "message": message}})
        body_bytes = body.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

# ── Main ───────────────────────────────────────────────────────────────────────

def run_check(config_path):
    """Self-diagnostic: verify ANTHROPIC_BASE_URL, proxy port, and reachability."""
    sep   = "─" * 45
    ok    = "✓"
    fail  = "✗"
    warn  = "!"
    all_passed = True

    print(f"\n  SW-Sentinel Health Check")
    print(f"  {sep}")

    # Load config to get host/port
    try:
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = json.load(f)
        elif os.environ.get("SUPERWISE_CLIENT_ID"):
            cfg = _config_from_env()
        else:
            cfg = {}
    except Exception:
        cfg = {}

    host  = cfg.get("proxy_host", "127.0.0.1")
    port  = cfg.get("proxy_port", 8080)
    token = cfg.get("proxy_token", "")

    # 1. Check ANTHROPIC_BASE_URL
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    expected = f"http://{host}:{port}"
    if base_url == expected:
        print(f"  ANTHROPIC_BASE_URL ... {base_url}  {ok}")
    elif base_url:
        print(f"  ANTHROPIC_BASE_URL ... {base_url}  {warn}  (expected {expected})")
        all_passed = False
    else:
        print(f"  ANTHROPIC_BASE_URL ... (not set)  {fail}")
        print(f"    → Run: export ANTHROPIC_BASE_URL={expected}")
        all_passed = False

    # 2. Check proxy port is listening
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        listening = s.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) == 0
    if listening:
        print(f"  Proxy listening    ... {host}:{port}  {ok}")
    else:
        print(f"  Proxy listening    ... {host}:{port}  {fail}")
        print(f"    → Start the proxy: sw-sentinel")
        all_passed = False

    # 3. Check proxy reachability with a lightweight request
    if listening:
        try:
            import time
            headers = {"Content-Type": "application/json"}
            if token:
                headers["X-Sentinel-Token"] = token
            t0   = time.monotonic()
            resp = requests.post(
                f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}/v1/messages",
                headers=headers,
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1,
                      "messages": [{"role": "user", "content": "ping"}]},
                timeout=5
            )
            ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code == 401:
                print(f"  Proxy reachable    ... got 401  {fail}")
                print(f"    → proxy_token is set — include X-Sentinel-Token header in your app")
                all_passed = False
            else:
                print(f"  Proxy reachable    ... OK (responded in {ms}ms)  {ok}")
        except Exception as e:
            print(f"  Proxy reachable    ... FAILED ({e})  {fail}")
            all_passed = False

    print(f"  {sep}")
    if all_passed:
        print(f"  All checks passed. Your app is routing through SW-Sentinel.\n")
    else:
        print(f"  One or more checks failed. See suggestions above.\n")


def main():
    global CONFIG, log

    parser = argparse.ArgumentParser(description="SW-Sentinel — Superwise Guardrail Proxy for Anthropic API")
    parser.add_argument("command", nargs="?", choices=["init", "check"], help="init: run setup wizard | check: verify proxy is reachable from this terminal")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to sentinel_config.json")
    parser.add_argument("--port",   type=int, help="Override proxy port from config")
    args = parser.parse_args()

    if args.command == "init":
        run_init_wizard(args.config)
        print("  Run 'sw-sentinel' to start the proxy.")
        return

    if args.command == "check":
        run_check(args.config)
        return

    CONFIG = load_config(args.config)
    log    = setup_logging(CONFIG)

    if args.port:
        CONFIG["proxy_port"] = args.port

    host = CONFIG.get("proxy_host", "127.0.0.1")
    port = CONFIG.get("proxy_port", 8080)

    # Initialize violation log
    violation_log = CONFIG.get("violation_log", "sw_sentinel_violations.log")
    with open(violation_log, "a") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"SW-Sentinel Started\n")
        f.write(f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n")
        f.write(f"Listening: {host}:{port}\n")

    log.info(f"{'='*55}")
    log.info(f"  SW-Sentinel — Superwise Guardrail Proxy")
    log.info(f"  v1.0.0")
    log.info(f"{'='*55}")
    log.info(f"  Proxy:         {host}:{port}")
    log.info(f"  Forwarding to: {CONFIG.get('anthropic_api_base', 'https://api.anthropic.com')}")
    log.info(f"  Violation log: {violation_log}")
    log.info(f"  On SW error:   {CONFIG.get('on_superwise_error', 'fail_open')}")
    log.info(f"{'='*55}")
    log.info(f"")
    log.info(f"  To use with any Anthropic app:")
    log.info(f"  export ANTHROPIC_BASE_URL=http://{host}:{port}")
    log.info(f"")

    # Initialize Superwise client
    try:
        init_sw_client()
        log.info(f"  Superwise connection: OK")
    except Exception as e:
        log.warning(f"  Superwise connection: FAILED ({e})")

    # Create or retrieve persistent guardrails in Superwise dashboard
    init_sw_guardrails()

    # Pre-compile injection patterns
    init_injection_patterns()

    log.info(f"{'='*55}")
    log.info(f"Proxy ready. Waiting for requests...")

    try:
        server = ThreadedHTTPServer((host, port), SentinelProxyHandler)
    except OSError as e:
        if e.errno == 98:  # Address already in use
            import subprocess
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True
            )
            pids = result.stdout.strip()
            log.error(f"Port {port} is already in use — another SW-Sentinel instance may be running.")
            if pids:
                log.error(f"  To stop it, run:  kill {pids}")
            else:
                log.error(f"  Run 'ss -tlnp | grep {port}' to find the process using the port.")
            sys.exit(1)
        raise

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("SW-Sentinel stopped.")
        server.shutdown()

if __name__ == "__main__":
    main()
