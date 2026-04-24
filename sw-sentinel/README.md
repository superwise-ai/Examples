# SW-Sentinel
### Superwise Guardrail Proxy for Anthropic API

SW-Sentinel is a lightweight HTTP proxy that sits between any Anthropic API client and `api.anthropic.com`. Every LLM call is automatically intercepted and run through **Superwise guardrail checks** before being forwarded — without any changes to your application code.

---

## How It Works

```
Your App → ANTHROPIC_BASE_URL → SW-Sentinel (port 8080)
                                      ↓
                           Superwise Guardrail Check
                                ↓           ↓
                            BLOCKED       PASSED
                               ↓             ↓
                        Canned response   api.anthropic.com
                        returned to app      ↓
                                       Response checked
                                       (output guardrails)
                                             ↓
                                       Returned to app
```

SW-Sentinel checks:
- **Input** — text sent to the LLM is screened before reaching Anthropic
- **Output** — LLM responses are screened before returning to your app

If a check fails, a safe canned response is returned instead. Your app never sees the blocked content and the request never reaches Anthropic.

---

## Requirements

- Python 3.8+
- A Superwise account (free Starter plan works): https://app.superwise.ai
- `pip install superwise-api requests`

---

## Installation

```bash
# 1. Clone or copy the sw-sentinel folder to your machine

# 2. Install dependencies
pip install superwise-api requests

# 3. Copy the example config and add your credentials
cp sentinel_config.json.example sentinel_config.json

# 4. Edit sentinel_config.json
#    Set superwise_client_id and superwise_client_secret
#    (Generate tokens at: https://app.superwise.ai → Settings → API Tokens)
```

---

## Running SW-Sentinel

```bash
python3 sw_sentinel.py
```

You should see:
```
=======================================================
  SW-Sentinel — Superwise Guardrail Proxy
  v1.0.0
=======================================================
  Proxy:         127.0.0.1:8080
  Forwarding to: https://api.anthropic.com
  Violation log: sw_sentinel_violations.log
=======================================================
  To use with any Anthropic app:
  export ANTHROPIC_BASE_URL=http://127.0.0.1:8080

  Superwise connection: OK
=======================================================
Proxy ready. Waiting for requests...
```

---

## Connecting Your App

Set one environment variable — that's it:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
```

**Works with:**
- Claude Code / Paperclip
- Python `anthropic` SDK
- LangChain with Anthropic backend
- LlamaIndex with Anthropic backend
- Any tool that respects `ANTHROPIC_BASE_URL`

**Example — Python SDK:**
```python
import anthropic
import os

os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8080"

client = anthropic.Anthropic()  # automatically uses SW-Sentinel
message = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}]
)
```

**Example — Claude Code:**
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
claude  # all traffic now routes through SW-Sentinel
```

---

## Configuration Reference (`sentinel_config.json`)

| Key | Default | Description |
|-----|---------|-------------|
| `superwise_client_id` | — | **Required.** Your Superwise client ID |
| `superwise_client_secret` | — | **Required.** Your Superwise client secret |
| `proxy_host` | `127.0.0.1` | Host to bind proxy to. Use `0.0.0.0` for network access (not recommended) |
| `proxy_port` | `8080` | Port to listen on |
| `anthropic_api_base` | `https://api.anthropic.com` | Upstream API to forward to |
| `anthropic_api_key` | `""` | Optional. Injects API key into forwarded requests |
| `on_superwise_error` | `fail_open` | `fail_open` = allow request if SW unreachable; `fail_closed` = block |
| `max_check_chars` | `2000` | Max characters of text to send to Superwise per check |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `log_file` | `sw_sentinel.log` | Proxy activity log |
| `violation_log` | `sw_sentinel_violations.log` | Violation audit log |
| `blocked_input_message` | See config | Message returned to app when input is blocked |
| `blocked_output_message` | See config | Message returned to app when output is blocked |
| `skip_patterns` | `[]` | List of strings — if found in message, skip guardrail check. Use for internal orchestration messages. |

### Guardrail Configuration

Each guardrail can be enabled/disabled and tuned independently for `input` and `output`:

```json
"guardrails": {
  "input": {
    "pii_detection": {
      "enabled": true,
      "threshold": 0.5,
      "categories": ["US_SSN", "CREDIT_CARD", "US_BANK_NUMBER"]
    },
    "jailbreak_detection": {
      "enabled": false
    },
    "toxicity_detection": {
      "enabled": false,
      "threshold": 0.5
    }
  },
  "output": {
    "pii_detection": {
      "enabled": true,
      "threshold": 0.5,
      "categories": ["US_SSN", "CREDIT_CARD", "US_BANK_NUMBER"]
    }
  }
}
```

**PII categories** (from Microsoft Presidio):
Common options: `US_SSN`, `CREDIT_CARD`, `US_BANK_NUMBER`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `IP_ADDRESS`, `MEDICAL_LICENSE`
Full list: https://microsoft.github.io/presidio/supported_entities/

---

## Jailbreak Detection — Important Note

Jailbreak detection is **disabled by default** because it can produce false positives on internal orchestration messages from frameworks like Paperclip, LangChain, or LlamaIndex (e.g. "You are an AI assistant. Continue your work.").

If you enable it, use `skip_patterns` to whitelist known-safe internal messages:

```json
"jailbreak_detection": { "enabled": true },
"skip_patterns": ["You are an AI assistant", "Continue your work"]
```

---

## Violation Logs

All blocked requests are written to `sw_sentinel_violations.log`:

```
============================================================
TIMESTAMP:  2026-04-15T19:47:44Z
REQUEST_ID: 20260415194742919835
DIRECTION:  INPUT
FLAGS:      PII_DETECTED
SNIPPET:    Hi, my SSN is 123-45-6789 and I need help with...
VIOLATIONS:
  [Sentinel Input PII] Message contains restricted personal information
```

---

## Running as a Service (Linux systemd)

Create `/etc/systemd/system/sw-sentinel.service`:

```ini
[Unit]
Description=SW-Sentinel Guardrail Proxy
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/sw-sentinel
ExecStart=/usr/bin/python3 /path/to/sw-sentinel/sw_sentinel.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable sw-sentinel
sudo systemctl start sw-sentinel
```

Point your app at the proxy permanently by adding to your service's environment:

```ini
[Service]
Environment="ANTHROPIC_BASE_URL=http://127.0.0.1:8080"
```

---

## Security Considerations

- SW-Sentinel binds to `127.0.0.1` by default — it is only accessible from the local machine
- Do **not** set `proxy_host` to `0.0.0.0` unless you have firewall rules in place
- Your Superwise credentials are stored in `sentinel_config.json` — protect this file (`chmod 600 sentinel_config.json`)
- The violation log may contain snippets of sensitive content — protect accordingly
- `fail_open` is the default for availability; consider `fail_closed` for high-security environments

---

## Fail-Open vs. Fail-Closed

| Setting | Behavior | When to use |
|---------|----------|-------------|
| `fail_open` | If Superwise is unreachable, allow the request through | Dev/lab environments, availability-critical apps |
| `fail_closed` | If Superwise is unreachable, block the request | High-security environments, compliance-critical apps |

---

## Troubleshooting

**"Config file not found"**
→ Copy `sentinel_config.json.example` to `sentinel_config.json`

**"superwise_client_id not set"**
→ Edit `sentinel_config.json` and add your credentials from https://app.superwise.ai

**500 errors from Superwise**
→ Check `max_check_chars` — reduce if sending large payloads
→ Check your Superwise plan limits
→ Try `python3 -c "from superwise_api.superwise_client import SuperwiseClient; SuperwiseClient()"` to test connectivity

**False positive blocks on internal messages**
→ Add the triggering text to `skip_patterns` in config
→ Or disable `jailbreak_detection` (most common cause of false positives)

**App not routing through proxy**
→ Verify `ANTHROPIC_BASE_URL=http://127.0.0.1:8080` is set in the same shell/process as your app
→ Check `ss -tlnp | grep 8080` to confirm proxy is listening

---

## Credits

Built on top of the Superwise SDK and Microsoft Presidio PII detection.
Developed as part of the Superwise + Paperclip integration lab.

Superwise: https://superwise.ai
Presidio entity reference: https://microsoft.github.io/presidio/supported_entities/
