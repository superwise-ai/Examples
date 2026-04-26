#!/usr/bin/env python3
"""
SW-Sentinel — Superwise Guardrail Proxy for LLM APIs
=====================================================
A lightweight HTTP proxy that sits between any LLM API client and its
upstream provider. Every call is intercepted and run through Superwise
guardrail checks before being forwarded.

Supported providers (auto-detected from request path):
  - Anthropic  POST /v1/messages          → api.anthropic.com
  - OpenAI     POST /v1/chat/completions  → api.openai.com

Works with any tool that uses these APIs:
  - Claude Code / Paperclip (Anthropic)
  - Python apps using the anthropic or openai SDK
  - LangChain, LlamaIndex, or any compatible framework

Setup:
  1. Run: sw-sentinel
  2. Point your app at the proxy:
       Anthropic: export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
       OpenAI:    export OPENAI_BASE_URL=http://127.0.0.1:8080
  3. Your app now routes through SW-Sentinel automatically

Configuration:
  Edit sentinel_config.json to customize guardrails, port, and behavior.
"""

import os
import sys
import json
import re
import time
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

# ── Provider routing table ─────────────────────────────────────────────────────

PROVIDERS = {
    "/v1/messages": {
        "name":                "Anthropic",
        "api_base_cfg":        "anthropic_api_base",
        "api_base_default":    "https://api.anthropic.com",
        "api_key_cfg":         "anthropic_api_key",
        "api_key_env":         "ANTHROPIC_API_KEY",
        "passthrough_headers": {
            "content-type", "anthropic-version", "anthropic-beta",
            "x-api-key", "authorization"
        },
    },
    "/v1/chat/completions": {
        "name":                "OpenAI",
        "api_base_cfg":        "openai_api_base",
        "api_base_default":    "https://api.openai.com",
        "api_key_cfg":         "openai_api_key",
        "api_key_env":         "OPENAI_API_KEY",
        "passthrough_headers": {
            "content-type", "authorization",
            "openai-organization", "openai-project"
        },
    },
    "/openai/v1/chat/completions": {
        "name":                "Groq",
        "api_base_cfg":        "groq_api_base",
        "api_base_default":    "https://api.groq.com",
        "api_key_cfg":         "groq_api_key",
        "api_key_env":         "GROQ_API_KEY",
        "passthrough_headers": {
            "content-type", "authorization"
        },
    },
    "/v1beta/openai/chat/completions": {
        "name":                "Gemini",
        "api_base_cfg":        "gemini_api_base",
        "api_base_default":    "https://generativelanguage.googleapis.com",
        "api_key_cfg":         "gemini_api_key",
        "api_key_env":         "GEMINI_API_KEY",
        "passthrough_headers": {
            "content-type", "authorization"
        },
    },
}

def detect_provider(path):
    """Return provider dict for the given request path, defaulting to Anthropic."""
    return PROVIDERS.get(path.split("?")[0], PROVIDERS["/v1/messages"])

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
        "openai_api_base":          "https://api.openai.com",
        "openai_api_key":           "",
        "groq_api_base":            "https://api.groq.com",
        "groq_api_key":             "",
        "gemini_api_base":          "https://generativelanguage.googleapis.com",
        "gemini_api_key":           "",
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
            "input": [
                {"type": "pii_detection",    "threshold": 0.5, "categories": ["US_SSN", "CREDIT_CARD", "US_BANK_NUMBER"]},
                {"type": "detect_jailbreak", "threshold": 0.7}
            ],
            "output": [
                {"type": "pii_detection", "threshold": 0.5, "categories": ["US_SSN", "CREDIT_CARD", "US_BANK_NUMBER"]}
            ]
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

CONFIG                   = {}
SW_CLIENT                = None
SW_GUARDRAIL_VERSION_IDS = {}   # direction -> set of version UUIDs for run_versions()
INJECTION_RE             = []
log                      = None
_sw_lock                 = threading.Lock()

# ── Logging setup ──────────────────────────────────────────────────────────────

def setup_logging(config):
    log_level = getattr(logging, config.get("log_level", "INFO").upper(), logging.INFO)
    log_file  = config.get("log_file", "sw_sentinel.log")
    handlers  = [logging.StreamHandler(sys.stdout)]

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

def check_injection_patterns(text, request_id="unknown", provider_name="unknown"):
    """Fast local check for known prompt injection signatures before Superwise API call."""
    if not INJECTION_RE or not text:
        return False, ""
    for pattern in INJECTION_RE:
        if pattern.search(text):
            log_violation(
                request_id, "input",
                [{"guard": "InjectionPatternMatch", "message": f"Matched pattern: {pattern.pattern}"}],
                ["PROMPT_INJECTION"],
                text[:300],
                provider_name
            )
            return True, pattern.pattern
    return False, ""

def build_guards(direction):
    """Build guardrail list from config. Supports list format and legacy named-block format."""
    from superwise_api.models.guardrails.guardrails import (
        AllowedTopicsGuard, CompetitorCheckGuard, CorrectLanguageGuard,
        DetectJailbreakGuard, PiiDetectionGuard, RestrictedTopicsGuard,
        StringCheckGuard, ToxicityGuard
    )

    GUARD_REGISTRY = {
        "pii_detection":      PiiDetectionGuard,
        "detect_jailbreak":   DetectJailbreakGuard,
        "toxicity":           ToxicityGuard,
        "allowed_topics":     AllowedTopicsGuard,
        "restricted_topics":  RestrictedTopicsGuard,
        "competitor_check":   CompetitorCheckGuard,
        "correct_language":   CorrectLanguageGuard,
        "string_check":       StringCheckGuard,
    }

    guards    = []
    guard_cfg = CONFIG.get("guardrails", {})
    dir_cfg   = guard_cfg.get(direction, {})

    # ── New list format ────────────────────────────────────────────────────────
    if isinstance(dir_cfg, list):
        for entry in dir_cfg:
            guard_type = entry.get("type")
            if not guard_type:
                log.warning(f"Guardrail entry missing 'type' field — skipping: {entry}")
                continue
            cls = GUARD_REGISTRY.get(guard_type)
            if not cls:
                log.warning(f"Unknown guardrail type '{guard_type}' — skipping")
                continue
            params = {k: v for k, v in entry.items() if k != "type"}
            params.setdefault("name", f"Sentinel {direction.title()} {guard_type.replace('_', ' ').title()}")
            params.setdefault("tags", [direction])
            try:
                guards.append(cls(**params))
            except Exception as e:
                log.warning(f"Failed to create guard '{guard_type}': {e}")
        return guards

    # ── Legacy named-block format (backward compatibility) ────────────────────
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

def run_guardrail_check(text, direction, request_id="unknown", provider_name="unknown"):
    """
    Run Superwise guardrail checks on text.
    Returns (passed: bool, violations: list, flags: list)
    Fails open if Superwise is unreachable (configurable).
    """
    if not text or not text.strip():
        return True, [], []

    max_chars = CONFIG.get("max_check_chars", 2000)
    log.info(f"[{provider_name}] Checking {len(text)} chars [{direction}] request_id={request_id}")

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

            log_violation(request_id, direction, violations, flags, text[:300], provider_name)
            return False, violations, flags

        return True, [], []

    except Exception as e:
        fail_behavior = CONFIG.get("on_superwise_error", "fail_open")
        log.error(f"[{provider_name}] Superwise error: {e} — {fail_behavior}")
        if fail_behavior == "fail_closed":
            return False, [{"guard": "ERROR", "message": str(e)}], ["SW_ERROR"]
        return True, [], []  # fail_open — allow request through

# ── Violation logging ──────────────────────────────────────────────────────────

def log_violation(request_id, direction, violations, flags, snippet, provider_name="unknown"):
    """Write violation to local audit log."""
    timestamp     = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    violation_log = CONFIG.get("violation_log", "sw_sentinel_violations.log")

    try:
        with _sw_lock:
            with open(violation_log, "a") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"TIMESTAMP:  {timestamp}\n")
                f.write(f"REQUEST_ID: {request_id}\n")
                f.write(f"PROVIDER:   {provider_name}\n")
                f.write(f"DIRECTION:  {direction.upper()}\n")
                f.write(f"FLAGS:      {', '.join(flags)}\n")
                f.write(f"SNIPPET:    {snippet}\n")
                f.write(f"VIOLATIONS:\n")
                for v in violations:
                    f.write(f"  [{v['guard']}] {v['message']}\n")
        log.warning(f"VIOLATION [{provider_name}] [{direction.upper()}] request_id={request_id} flags={flags}")
    except Exception as e:
        log.error(f"Failed to write violation log: {e}")

# ── Text extraction ────────────────────────────────────────────────────────────

def extract_input_text(body, provider_name="Anthropic"):
    """Extract the most recent user message for guardrail checking."""
    skip_patterns = CONFIG.get("skip_patterns", [])

    try:
        if skip_patterns:
            if provider_name in _OPENAI_FORMAT_PROVIDERS:
                # OpenAI-compatible: system prompt is a message with role "system"
                parts = []
                for m in body.get("messages", []):
                    if m.get("role") == "system":
                        c = m.get("content", "")
                        parts.append(c if isinstance(c, str) else
                                     " ".join(b.get("text", "") for b in c if isinstance(b, dict)))
                system_text = " ".join(parts)
            else:
                # Anthropic: system is a top-level field
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
                            # Anthropic tool results — prompt injection blind spot
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

_OPENAI_FORMAT_PROVIDERS = {"OpenAI", "Groq", "Gemini"}

def extract_output_text(body, provider_name="Anthropic"):
    """Extract assistant response text for guardrail checking."""
    try:
        if provider_name in _OPENAI_FORMAT_PROVIDERS:
            choices = body.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "") or ""
                return content[:CONFIG.get("max_check_chars", 2000)]
        else:
            texts = []
            for block in body.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            return " ".join(texts)[:CONFIG.get("max_check_chars", 2000)]
    except Exception:
        pass
    return ""

def extract_streaming_text(raw_content, provider_name="Anthropic"):
    """Extract text from SSE streaming response for guardrail checking."""
    text_parts = []
    try:
        for line in raw_content.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                data = json.loads(line[6:])
                if provider_name in _OPENAI_FORMAT_PROVIDERS:
                    choices = data.get("choices", [])
                    if choices:
                        delta_content = choices[0].get("delta", {}).get("content", "")
                        if delta_content:
                            text_parts.append(delta_content)
                else:
                    if data.get("type") == "content_block_delta":
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text_parts.append(delta.get("text", ""))
            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    return "".join(text_parts)[:CONFIG.get("max_check_chars", 2000)]

def make_blocked_response(request_body, message, provider_name="Anthropic"):
    """Construct a provider-appropriate blocked response body."""
    ts    = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    model = request_body.get("model", "unknown")

    if provider_name in _OPENAI_FORMAT_PROVIDERS:
        return {
            "id":      f"sentinel_blocked_{ts}",
            "object":  "chat.completion",
            "created": int(time.time()),
            "model":   model,
            "choices": [{
                "index":         0,
                "message":       {"role": "assistant", "content": message},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }
    else:
        return {
            "id":            f"sentinel_blocked_{ts}",
            "type":          "message",
            "role":          "assistant",
            "model":         model if model != "unknown" else "claude-sonnet-4-6",
            "content":       [{"type": "text", "text": message}],
            "stop_reason":   "end_turn",
            "stop_sequence": None,
            "usage":         {"input_tokens": 0, "output_tokens": len(message.split())}
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

        provider      = detect_provider(self.path)
        provider_name = provider["name"]
        request_id    = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 10 * 1024 * 1024:  # 10 MB hard limit
            self._send_error(413, "Request body too large")
            return
        raw_body = self.rfile.read(content_length) if content_length else b""

        try:
            request_body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            request_body = {}

        model = request_body.get("model", "unknown")

        # ── INPUT CHECK ────────────────────────────────────────────────────────
        input_text = extract_input_text(request_body, provider_name)

        if input_text:
            blocked, _ = check_injection_patterns(input_text, request_id, provider_name)
            if blocked:
                msg = CONFIG.get("blocked_input_message",
                    "This request has been blocked by compliance policy.")
                self._send_json_response(200, make_blocked_response(request_body, msg, provider_name))
                return

            passed, violations, flags = run_guardrail_check(input_text, "input", request_id, provider_name)
            if not passed:
                msg = CONFIG.get("blocked_input_message",
                    "This request has been blocked by compliance policy.")
                self._send_json_response(200, make_blocked_response(request_body, msg, provider_name))
                return

        # ── FORWARD TO UPSTREAM ────────────────────────────────────────────────
        api_base   = CONFIG.get(provider["api_base_cfg"], provider["api_base_default"])
        target_url = f"{api_base}{self.path}"

        log.info(f"[{provider_name}] {self.path} → {api_base}  model={model}  ({len(input_text)} chars checked)")

        try:
            resp = requests.post(
                target_url,
                headers=self._build_forward_headers(provider),
                data=raw_body,
                timeout=CONFIG.get("upstream_timeout_seconds", 120),
                stream=False
            )
        except requests.RequestException as e:
            log.error(f"[{provider_name}] Upstream request failed: {e}")
            self._send_error(502, f"Upstream error: {e}")
            return

        # ── OUTPUT CHECK ───────────────────────────────────────────────────────
        is_streaming = request_body.get("stream", False)

        if is_streaming:
            output_text = extract_streaming_text(resp.content, provider_name)
        else:
            try:
                response_body = resp.json()
            except Exception:
                response_body = {}
            output_text = extract_output_text(response_body, provider_name)

        if output_text:
            passed, violations, flags = run_guardrail_check(output_text, "output", request_id, provider_name)
            if not passed:
                msg = CONFIG.get("blocked_output_message",
                    "The model response has been blocked by compliance policy.")
                self._send_json_response(200, make_blocked_response(request_body, msg, provider_name))
                return

        # ── PASS THROUGH CLEAN RESPONSE ────────────────────────────────────────
        self._send_raw_response(resp.status_code, resp.headers, resp.content)

    def do_GET(self):
        if not self._check_proxy_token():
            self._reject_unauthorized()
            return
        provider   = detect_provider(self.path)
        api_base   = CONFIG.get(provider["api_base_cfg"], provider["api_base_default"])
        target_url = f"{api_base}{self.path}"
        try:
            resp = requests.get(target_url, headers=self._build_forward_headers(provider), timeout=30)
            self._send_raw_response(resp.status_code, resp.headers, resp.content)
        except Exception as e:
            self._send_error(502, str(e))

    def _build_forward_headers(self, provider):
        headers = {}
        for key, val in self.headers.items():
            if key.lower() in provider["passthrough_headers"]:
                headers[key] = val
        # Inject API key from config/env if client didn't send one
        api_key = CONFIG.get(provider["api_key_cfg"]) or os.environ.get(provider["api_key_env"], "")
        if api_key:
            if provider["name"] == "OpenAI":
                if "authorization" not in {k.lower() for k in headers}:
                    headers["Authorization"] = f"Bearer {api_key}"
            else:
                if "x-api-key" not in {k.lower() for k in headers}:
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

# ── Health check ───────────────────────────────────────────────────────────────

def run_check(config_path):
    """Self-diagnostic: verify env vars, proxy port, and reachability for each provider."""
    sep        = "─" * 45
    ok         = "✓"
    fail       = "✗"
    warn       = "!"
    all_passed = True

    print(f"\n  SW-Sentinel Health Check")
    print(f"  {sep}")

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
    check_host = "127.0.0.1" if host == "0.0.0.0" else host
    expected   = f"http://{host}:{port}"

    # 1. Check env vars for each provider
    provider_env_vars = [
        ("ANTHROPIC_BASE_URL", "Anthropic"),
        ("OPENAI_BASE_URL",    "OpenAI   "),
        ("GROQ_BASE_URL",      "Groq     "),
        ("GEMINI_BASE_URL",    "Gemini   "),
    ]
    any_url_set = False
    for env_var, label in provider_env_vars:
        url = os.environ.get(env_var, "")
        if url == expected:
            print(f"  {env_var:<22} ... {url}  {ok}")
            any_url_set = True
        elif url:
            print(f"  {env_var:<22} ... {url}  {warn}  (expected {expected})")
            all_passed = False
            any_url_set = True
        else:
            print(f"  {env_var:<22} ... (not set)")

    if not any_url_set:
        print(f"")
        print(f"  No provider env vars set. Set at least one to route traffic through SW-Sentinel:")
        print(f"    export ANTHROPIC_BASE_URL={expected}")
        print(f"    export OPENAI_BASE_URL={expected}")
        print(f"    export GROQ_BASE_URL={expected}")
        print(f"    export GEMINI_BASE_URL={expected}")

    # 2. Check proxy port is listening
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        listening = s.connect_ex((check_host, port)) == 0
    if listening:
        print(f"  Proxy listening    ... {host}:{port}  {ok}")
    else:
        print(f"  Proxy listening    ... {host}:{port}  {fail}")
        print(f"    → Start the proxy: sw-sentinel")
        all_passed = False

    # 3. Check reachability for each provider endpoint
    if listening:
        probe_requests = [
            ("Anthropic", "/v1/messages",
             {"model": "claude-haiku-4-5-20251001", "max_tokens": 1,
              "messages": [{"role": "user", "content": "ping"}]}),
            ("OpenAI",    "/v1/chat/completions",
             {"model": "gpt-4o-mini", "max_tokens": 1,
              "messages": [{"role": "user", "content": "ping"}]}),
            ("Groq",      "/openai/v1/chat/completions",
             {"model": "llama-3.1-8b-instant", "max_tokens": 1,
              "messages": [{"role": "user", "content": "ping"}]}),
            ("Gemini",    "/v1beta/openai/chat/completions",
             {"model": "gemini-1.5-flash", "max_tokens": 1,
              "messages": [{"role": "user", "content": "ping"}]}),
        ]
        headers_base = {"Content-Type": "application/json"}
        if token:
            headers_base["X-Sentinel-Token"] = token

        for pname, path, payload in probe_requests:
            try:
                t0   = time.monotonic()
                resp = requests.post(
                    f"http://{check_host}:{port}{path}",
                    headers=headers_base,
                    json=payload,
                    timeout=5
                )
                ms = int((time.monotonic() - t0) * 1000)
                # Check if the proxy itself rejected the request (proxy token required)
                is_proxy_401 = (resp.status_code == 401 and
                                b"X-Sentinel-Token" in resp.content)
                if is_proxy_401:
                    print(f"  {pname:<10} {path} ... got 401  {fail}")
                    print(f"    → proxy_token is set — include X-Sentinel-Token in your app")
                    all_passed = False
                else:
                    # Any response (even upstream errors) means the proxy is alive
                    print(f"  {pname:<10} {path} ... OK ({ms}ms)  {ok}")
            except Exception as e:
                print(f"  {pname:<10} {path} ... FAILED ({e})  {fail}")
                all_passed = False

    print(f"  {sep}")
    if all_passed:
        print(f"  All checks passed. Your app is routing through SW-Sentinel.\n")
    else:
        print(f"  One or more checks failed. See suggestions above.\n")


def main():
    global CONFIG, log

    parser = argparse.ArgumentParser(description="SW-Sentinel — Superwise Guardrail Proxy")
    parser.add_argument("command", nargs="?", choices=["init", "check"],
                        help="init: run setup wizard | check: verify proxy is reachable from this terminal")
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

    anthropic_base = CONFIG.get("anthropic_api_base", "https://api.anthropic.com")
    openai_base    = CONFIG.get("openai_api_base",    "https://api.openai.com")
    groq_base      = CONFIG.get("groq_api_base",      "https://api.groq.com")
    gemini_base    = CONFIG.get("gemini_api_base",    "https://generativelanguage.googleapis.com")

    # Initialize violation log
    violation_log = CONFIG.get("violation_log", "sw_sentinel_violations.log")
    with open(violation_log, "a") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"SW-Sentinel Started\n")
        f.write(f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n")
        f.write(f"Listening: {host}:{port}\n")

    log.info(f"{'='*55}")
    log.info(f"  SW-Sentinel — Superwise Guardrail Proxy")
    log.info(f"  v1.1.0")
    log.info(f"{'='*55}")
    log.info(f"  Proxy:    {host}:{port}")
    log.info(f"  Providers:")
    log.info(f"    Anthropic  /v1/messages                     → {anthropic_base}")
    log.info(f"    OpenAI     /v1/chat/completions             → {openai_base}")
    log.info(f"    Groq       /openai/v1/chat/completions      → {groq_base}")
    log.info(f"    Gemini     /v1beta/openai/chat/completions  → {gemini_base}")
    log.info(f"  Violation log: {violation_log}")
    log.info(f"  On SW error:   {CONFIG.get('on_superwise_error', 'fail_open')}")
    log.info(f"{'='*55}")
    log.info(f"")
    log.info(f"  To use with Anthropic: export ANTHROPIC_BASE_URL=http://{host}:{port}")
    log.info(f"  To use with OpenAI:    export OPENAI_BASE_URL=http://{host}:{port}")
    log.info(f"  To use with Groq:      export GROQ_BASE_URL=http://{host}:{port}")
    log.info(f"  To use with Gemini:    export GEMINI_BASE_URL=http://{host}:{port}")
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
