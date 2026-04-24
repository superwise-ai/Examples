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
import logging
import argparse
import threading
import requests
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Default config path ────────────────────────────────────────────────────────

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "sentinel_config.json")

# ── Load configuration ─────────────────────────────────────────────────────────

def load_config(config_path):
    """Load configuration from JSON file."""
    if not os.path.exists(config_path):
        print(f"ERROR: Config file not found: {config_path}")
        print(f"       Copy sentinel_config.json.example to sentinel_config.json and fill in your credentials.")
        sys.exit(1)

    with open(config_path) as f:
        config = json.load(f)

    # Validate required fields
    required = ["superwise_client_id", "superwise_client_secret"]
    for field in required:
        if not config.get(field) or config[field].startswith("YOUR_"):
            print(f"ERROR: '{field}' not set in {config_path}")
            print(f"       Edit sentinel_config.json and add your Superwise credentials.")
            sys.exit(1)

    return config

# ── Globals (populated after config load) ─────────────────────────────────────

CONFIG     = {}
log        = None
_sw_lock   = threading.Lock()

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

def get_sw_client():
    """Create a new Superwise client. Called per-request to avoid thread-safety issues."""
    os.environ["SUPERWISE_CLIENT_ID"]     = CONFIG["superwise_client_id"]
    os.environ["SUPERWISE_CLIENT_SECRET"] = CONFIG["superwise_client_secret"]
    from superwise_api.superwise_client import SuperwiseClient
    return SuperwiseClient()

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

    guards = build_guards(direction)
    if not guards:
        return True, [], []

    max_chars = CONFIG.get("max_check_chars", 2000)
    log.info(f"Checking {len(text)} chars [{direction}] request_id={request_id}")

    try:
        sw      = get_sw_client()
        tag     = direction
        results = sw.guardrails.run_guardrules(tag=tag, guardrules=guards, query=text[:max_chars])

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
        messages = body.get("messages", [])
        for msg in reversed(messages):
            if msg.get("role") in ("user", "human"):
                content = msg.get("content", "")

                if isinstance(content, str):
                    # Strip XML-like internal tags
                    content = re.sub(r'<[^>]+>.*?</[^>]+>', '', content, flags=re.DOTALL)
                    content = content.strip()

                    # Skip internal orchestration messages
                    if any(p in content for p in skip_patterns):
                        return ""
                    if content:
                        return content[:CONFIG.get("max_check_chars", 2000)]

                elif isinstance(content, list):
                    texts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            val = block.get("text", "")
                            if isinstance(val, str):
                                val = re.sub(r'<[^>]+>.*?</[^>]+>', '', val, flags=re.DOTALL)
                                texts.append(val.strip())
                        elif isinstance(block, str):
                            texts.append(block)
                    result = " ".join(texts).strip()
                    if any(p in result for p in skip_patterns):
                        return ""
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

# ── HTTP proxy handler ─────────────────────────────────────────────────────────

class SentinelProxyHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Suppress default HTTP server logging

    def do_POST(self):
        request_id     = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body       = self.rfile.read(content_length) if content_length else b""

        try:
            request_body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            request_body = {}

        # ── INPUT CHECK ────────────────────────────────────────────────────────
        input_text = extract_input_text(request_body)

        if input_text:
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

def main():
    global CONFIG, log

    parser = argparse.ArgumentParser(description="SW-Sentinel — Superwise Guardrail Proxy for Anthropic API")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to sentinel_config.json")
    parser.add_argument("--port",   type=int, help="Override proxy port from config")
    args = parser.parse_args()

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

    # Test Superwise connection
    try:
        get_sw_client()
        log.info(f"  Superwise connection: OK")
    except Exception as e:
        log.warning(f"  Superwise connection: FAILED ({e})")

    log.info(f"{'='*55}")
    log.info(f"Proxy ready. Waiting for requests...")

    server = HTTPServer((host, port), SentinelProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("SW-Sentinel stopped.")
        server.shutdown()

if __name__ == "__main__":
    main()
