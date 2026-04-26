# SW-Sentinel
### Superwise Guardrail Proxy for Anthropic API

SW-Sentinel is a lightweight HTTP proxy that sits between your app and `api.anthropic.com`. Every LLM call is automatically intercepted and run through **Superwise guardrail checks** before being forwarded — without any changes to your application code.

**Built for API-first teams:** SW-Sentinel is designed for applications and workflows that call Claude via the Anthropic API using an API key (`sk-ant-...`). If your team is building on Claude — internal tools, automations, agentic workflows — you're in the right place. Tools that authenticate via a Claude.ai subscription (including Claude Code on a Team or Pro plan) use OAuth rather than an API key, so their traffic runs outside SW-Sentinel's intercept layer. For full guardrail coverage across your organization, pairing SW-Sentinel with a dedicated API key deployment is the recommended approach.

---

## What Does It Do?

SW-Sentinel acts as a security checkpoint for all Anthropic API traffic:

```
Your App  →  SW-Sentinel (port 8080)
                    ↓
          ┌─────────────────────┐
          │  Guardrail Checks   │
          │  • PII detection    │
          │  • Jailbreak detect │
          │  • Toxicity detect  │
          │  • Prompt injection │
          └─────────────────────┘
               ↓           ↓
           BLOCKED       PASSED
              ↓             ↓
       Canned response  api.anthropic.com
       returned to app       ↓
                       Response checked
                       (output guardrails)
                             ↓
                       Returned to app
```

**Both directions are checked:**
- **Input** — text your app sends to the LLM is screened before it reaches Anthropic
- **Output** — the LLM's response is screened before it reaches your app

If a check fails, a safe canned message is returned. Your app never sees the blocked content.

---

## What Gets Detected?

| Check | Direction | Description |
|-------|-----------|-------------|
| **PII Detection** | Input + Output | Blocks SSNs, credit cards, bank numbers, and other personal data |
| **Jailbreak Detection** | Input | Detects attempts to bypass the LLM's safety guidelines *(optional — disabled by default, see [note below](#jailbreak-detection--important-note))* |
| **Toxicity Detection** | Input + Output | Detects harmful, abusive, or inappropriate language |
| **Prompt Injection** | Input | Catches attempts to hijack the AI's instructions (e.g. "ignore all previous instructions") |

PII and jailbreak checks are powered by the **Superwise** platform. Prompt injection uses fast built-in regex patterns that run locally with no external call.

---

## Requirements

- Python 3.10 or newer
- A Superwise account — the free Starter plan works: https://app.superwise.ai
- An Anthropic API key: https://console.anthropic.com

---

## Installation

### Option A — pip install (recommended)

```bash
pip install git+https://github.com/superwise-ai/Examples.git#subdirectory=sw-sentinel
```

This installs the `sw-sentinel` command globally. Then run the setup wizard:

```bash
sw-sentinel init
```

The wizard will ask for your Superwise credentials and Anthropic API key, then write `sentinel_config.json` for you.

### Option B — Run directly with Python

```bash
# 1. Install dependencies
pip install requests superwise-api

# 2. Copy the example config
cp sentinel_config.json.example sentinel_config.json

# 3. Edit sentinel_config.json and fill in your credentials
#    (see Configuration Reference below)
```

---

## Starting the Proxy

**If installed via pip:**
```bash
sw-sentinel
```

**If running directly:**
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

## Stopping and Restarting the Proxy

How you stop and restart depends on how the proxy is running:

**Foreground (terminal):**
```bash
# Stop
Ctrl+C

# Start again
sw-sentinel
```

**systemd service:**
```bash
sudo systemctl restart sw-sentinel

# Or to stop/start separately:
sudo systemctl stop sw-sentinel
sudo systemctl start sw-sentinel
```

**Docker:**
```bash
docker restart <container_name>
```

> A restart is required any time you publish updated guardrails in the Superwise UI — the proxy reads the current guardrail version once at startup.

---

## Connecting Your App

Set one environment variable — that's all your app needs:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
```

**Works with:**
- Claude Code
- Python `anthropic` SDK
- LangChain with Anthropic backend
- LlamaIndex with Anthropic backend
- Any tool that respects `ANTHROPIC_BASE_URL`

**Example — Python SDK:**
```python
import anthropic, os

os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:8080"

client = anthropic.Anthropic()  # automatically routes through SW-Sentinel
message = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}]
)
```

**Example — Claude Code CLI:**
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
claude  # all traffic now routes through SW-Sentinel
```

**Streaming requests** (e.g. `stream=True`) are fully supported. The proxy accumulates the streamed response text and checks it before passing the stream back to your app.

**Verify everything is wired up correctly** by running this in the same terminal as your app:
```bash
sw-sentinel check
```

---

## Running with Docker

SW-Sentinel includes a `Dockerfile` for containerized deployments.

**Build the image:**
```bash
docker build -t sw-sentinel .
```

**Run in the foreground (useful for testing):**
```bash
docker run -p 8080:8080 \
  -e SUPERWISE_CLIENT_ID=your_client_id \
  -e SUPERWISE_CLIENT_SECRET=your_client_secret \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  sw-sentinel
```

**Run in the background (recommended for production):**
```bash
docker run -d --name sw-sentinel -p 8080:8080 \
  -e SUPERWISE_CLIENT_ID=your_client_id \
  -e SUPERWISE_CLIENT_SECRET=your_client_secret \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  sw-sentinel
```

The `-d` flag runs the container in detached (background) mode. `--name sw-sentinel` gives it a name so it's easy to reference in later commands.

**Run with a config file instead of environment variables:**
```bash
docker run -d --name sw-sentinel -p 8080:8080 \
  -v /path/to/sentinel_config.json:/app/sentinel_config.json \
  sw-sentinel
```

**Confirm it started correctly:**
```bash
docker logs sw-sentinel
```

You should see the startup banner:
```
=======================================================
  SW-Sentinel — Superwise Guardrail Proxy
  v1.0.0
=======================================================
  Proxy:         0.0.0.0:8080
  Forwarding to: https://api.anthropic.com
  Violation log: sw_sentinel_violations.log
=======================================================
Proxy ready. Waiting for requests...
```

**Connect your app:**

When SW-Sentinel is running in Docker, point your app at the Docker host instead of `127.0.0.1`:
```bash
# If your app is running on the same machine as Docker:
export ANTHROPIC_BASE_URL=http://localhost:8080

# If your app is on a different machine:
export ANTHROPIC_BASE_URL=http://<docker-host-ip>:8080
```

**Stop and restart the container:**
```bash
docker stop sw-sentinel
docker start sw-sentinel

# Or restart in one command:
docker restart sw-sentinel
```

### Environment Variable Reference (Docker / CI)

| Variable | Required | Description |
|----------|----------|-------------|
| `SUPERWISE_CLIENT_ID` | Yes | Your Superwise client ID |
| `SUPERWISE_CLIENT_SECRET` | Yes | Your Superwise client secret |
| `ANTHROPIC_API_KEY` | Recommended | Anthropic API key to inject into forwarded requests |
| `SENTINEL_HOST` | No | Host to bind to (default: `0.0.0.0`) |
| `SENTINEL_PORT` | No | Port to listen on (default: `8080`) |

---

## Configuration Reference (`sentinel_config.json`)

| Key | Default | Description |
|-----|---------|-------------|
| `superwise_client_id` | — | **Required.** Your Superwise client ID |
| `superwise_client_secret` | — | **Required.** Your Superwise client secret |
| `proxy_host` | `127.0.0.1` | Host to bind proxy to. Use `0.0.0.0` for Docker / network access |
| `proxy_port` | `8080` | Port to listen on |
| `anthropic_api_base` | `https://api.anthropic.com` | Upstream Anthropic API to forward to |
| `anthropic_api_key` | `""` | Optional. Injects your API key into forwarded requests |
| `proxy_token` | `""` | Optional shared secret. If set, clients must send `X-Sentinel-Token: <token>` header. Recommended when `proxy_host` is `0.0.0.0` |
| `upstream_timeout_seconds` | `120` | How long to wait for Anthropic to respond |
| `max_request_bytes` | `10MB` | Requests larger than 10MB are rejected with HTTP 413 |
| `on_superwise_error` | `fail_open` | `fail_open` = allow if Superwise unreachable; `fail_closed` = block |
| `max_check_chars` | `2000` | Max characters of text sent to Superwise per check |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `log_file` | `sw_sentinel.log` | Proxy activity log |
| `violation_log` | `sw_sentinel_violations.log` | Violation audit log |
| `blocked_input_message` | See config | Message returned to app when input is blocked |
| `blocked_output_message` | See config | Message returned to app when output is blocked |
| `injection_patterns` | 6 built-in patterns | Regex patterns for prompt injection detection |
| `skip_patterns` | `[]` | Strings — if found in the system prompt, skip guardrail checks entirely |

---

## Guardrail Configuration

### How guardrails are created and used

On first startup, SW-Sentinel automatically creates two persistent guardrail objects in your Superwise tenant:

- **SW-Sentinel Input** — checks text sent to the LLM
- **SW-Sentinel Output** — checks responses returned from the LLM

These are built from the `guardrails` block in `sentinel_config.json` and registered in your Superwise account via the SDK. Once created, they appear under **Guardrails** in the Superwise UI and all check results are logged to your dashboard.

On every subsequent startup, the proxy looks up these two guardrails by name, retrieves the current version, and uses that for all checks. **The `guardrails` block in `sentinel_config.json` is only read once — at initial creation.** After that, your Superwise tenant is the source of truth.

> To modify guardrails after initial setup, log into your tenant at https://app.superwise.ai → Guardrails, edit `SW-Sentinel Input` or `SW-Sentinel Output`, publish the new version, and restart the proxy.

### Initial configuration (`sentinel_config.json`)

The `guardrails` block controls what gets created on first run. It uses a list format — add any guard types you want, with their parameters:

```json
"guardrails": {
  "input": [
    {"type": "pii_detection", "threshold": 0.5, "categories": ["US_SSN", "CREDIT_CARD", "US_BANK_NUMBER"]},
    {"type": "detect_jailbreak", "threshold": 0.7},
    {"type": "restricted_topics", "topics": ["violence", "self-harm"]}
  ],
  "output": [
    {"type": "pii_detection", "threshold": 0.5, "categories": ["US_SSN", "CREDIT_CARD", "US_BANK_NUMBER"]}
  ]
}
```

### Available guard types

| Type | Parameters | Description |
|------|-----------|-------------|
| `pii_detection` | `threshold`, `categories` | Blocks personally identifiable information |
| `detect_jailbreak` | `threshold` | Detects attempts to bypass LLM safety guidelines |
| `toxicity` | `threshold`, `validation_method` | Detects harmful or abusive language |
| `restricted_topics` | `topics` (list) | Blocks messages about specific topics |
| `allowed_topics` | `topics` (list) | Only allows messages about specified topics |
| `competitor_check` | `competitor_names` (list) | Blocks mentions of named competitors |
| `correct_language` | `language_codes`, `filter_mode` | Enforces language (e.g. English only) |
| `string_check` | `regex_pattern` (list) | Blocks custom regex patterns |

**`threshold`** — confidence score (0.0–1.0) above which a detection triggers a block. Lower = stricter, Higher = more permissive.

**PII categories** — common options supported by Superwise:

| Category | Example |
|----------|---------|
| `US_SSN` | 123-45-6789 |
| `CREDIT_CARD` | 4111 1111 1111 1111 |
| `US_BANK_NUMBER` | 123456789 |
| `EMAIL_ADDRESS` | user@example.com |
| `PHONE_NUMBER` | (555) 867-5309 |
| `IP_ADDRESS` | 192.168.1.1 |
| `MEDICAL_LICENSE` | MD12345 |

For a full list of supported PII categories, see the Superwise documentation at https://app.superwise.ai.

---

## Prompt Injection Detection

SW-Sentinel includes built-in regex-based detection for common prompt injection attacks — attempts to override the AI's instructions. This check runs locally with no external call.

**Default patterns detect phrases like:**
- "Ignore all previous instructions"
- "Forget your prior directives"
- "Reveal your system prompt"
- "Do Anything Now" (DAN)
- "Override your safety guidelines"

You can add your own patterns in `sentinel_config.json`:

```json
"injection_patterns": [
  "ignore\\s+(all\\s+)?(previous|prior)\\s+instructions?",
  "your\\s+new\\s+instructions\\s+are"
]
```

Patterns are regular expressions (case-insensitive). Use `\\s+` to match spaces, `?` to make words optional, etc.

---

## Superwise Dashboard

Guardrail results are logged to your **Superwise dashboard** at https://app.superwise.ai. You can see:

- Which guardrails triggered
- When and how often violations occurred
- Historical trends over time

The proxy creates persistent guardrail objects in your Superwise tenant on startup (`Sentinel Input` and `Sentinel Output`). These appear under **Guardrails** in the Superwise UI.

---

## Adding or Changing Compliance Checks

SW-Sentinel creates two guardrail objects in your Superwise tenant on first startup: **SW-Sentinel Input** and **SW-Sentinel Output**. These are the only guardrails the proxy ever uses — it identifies them by name and ignores everything else in your tenant.

**The Superwise UI is the right place to customize checks.** After the proxy has run once and created those two guardrails, log into your tenant at https://app.superwise.ai → Guardrails and edit them directly. Add new rules, adjust thresholds, or enable additional detection types. Save/publish the new version in the UI, then restart the proxy — it will pick up the updated version automatically on next startup.

> **Note:** Creating a brand-new guardrail in the Superwise UI will not be used by SW-Sentinel. Changes must be made to the existing `SW-Sentinel Input` and `SW-Sentinel Output` guardrails.

> **Note:** The `guardrails` block in `sentinel_config.json` only applies the first time those guardrail objects are created. Once they exist in Superwise, the UI is the source of truth and the config file settings are ignored.

**To add or change compliance checks:**
1. Log into your tenant at https://app.superwise.ai → Guardrails
2. Open `SW-Sentinel Input` or `SW-Sentinel Output`
3. Add or modify the checks you want
4. Save and publish the new version
5. Restart the proxy (`sw-sentinel`) to activate the changes

---

## Jailbreak Detection — Important Note

Jailbreak detection is **disabled by default** in `sentinel_config.json.example` because it can produce false positives on internal orchestration messages from frameworks like LangChain or LlamaIndex (e.g. "You are an AI assistant. Continue your work.").

If you enable it, use `skip_patterns` to whitelist known-safe messages:

```json
"jailbreak_detection": { "enabled": true },
"skip_patterns": ["You are an AI assistant", "Continue your work"]
```

`skip_patterns` matches against the **system prompt** of each request. If any pattern is found, the entire request skips the guardrail check.

---

## Proxy Token Authentication

When `proxy_token` is set in your config, every client request must include it as a header:

```bash
X-Sentinel-Token: your-token-here
```

Requests without the correct token are rejected with HTTP 401 before any guardrail check runs.

**The `sw-sentinel init` wizard generates a strong random token automatically** and displays it at the end of setup. Copy it and add it to your app's headers.

**Example — curl:**
```bash
curl -X POST http://localhost:8080/v1/messages \
  -H "X-Sentinel-Token: your-token-here" \
  -H "Content-Type: application/json" \
  ...
```

**Example — Python SDK:**
```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://localhost:8080",
    default_headers={"X-Sentinel-Token": "your-token-here"}
)
```

**Example — Docker with token:**
```bash
docker run -d --name sw-sentinel -p 8080:8080 \
  -e SUPERWISE_CLIENT_ID=your_id \
  -e SUPERWISE_CLIENT_SECRET=your_secret \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e PROXY_TOKEN=your-token-here \
  sw-sentinel
```

> `proxy_token` is optional when running on `127.0.0.1` (local only). It is strongly recommended when `proxy_host` is `0.0.0.0`.

---

## Violation Logs

All blocked requests and security events are written to `sw_sentinel_violations.log`.

**Guardrail violation:**
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

**Unauthorized access attempt (wrong or missing token):**
```
============================================================
TIMESTAMP:  2026-04-15T19:52:11Z
EVENT:      UNAUTHORIZED_REQUEST
CLIENT_IP:  192.168.1.42
REQUEST:    POST /v1/messages
```

The log captures:
- Timestamp and unique request ID for guardrail violations
- Client IP, method, and path for unauthorized access attempts
- Which guardrail triggered and why for blocked content
- A snippet of the text that triggered the violation

Unauthorized attempts also produce a `[WARNING]` line in `sw_sentinel.log` so they're visible in the main activity log.

---

## Fail-Open vs. Fail-Closed

This controls what happens if Superwise is unreachable:

| Setting | Behavior | When to use |
|---------|----------|-------------|
| `fail_open` | Allow the request through | Dev/lab, availability-critical apps |
| `fail_closed` | Block the request | High-security, compliance-critical apps |

Note: prompt injection detection is always enforced regardless of this setting, since it runs locally.

---

## Running as a Service (Linux systemd)

To run SW-Sentinel automatically on boot, create a systemd service file:

**`/etc/systemd/system/sw-sentinel.service`:**
```ini
[Unit]
Description=SW-Sentinel Guardrail Proxy
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/sw-sentinel
ExecStart=sw-sentinel --config /path/to/sentinel_config.json
Restart=on-failure
RestartSec=5
Environment="ANTHROPIC_BASE_URL=http://127.0.0.1:8080"

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable sw-sentinel
sudo systemctl start sw-sentinel
```

---

## Security Considerations

- SW-Sentinel binds to `127.0.0.1` by default — only accessible from the local machine
- If you set `proxy_host` to `0.0.0.0` (e.g. Docker), set a `proxy_token` in your config — clients must then send `X-Sentinel-Token: <token>` with every request, preventing unauthorized use of your Anthropic API key
- The `sw-sentinel init` wizard automatically generates a strong random token and displays it after setup
- `sentinel_config.json` is written with owner-only permissions (`600`) by the wizard — do not loosen these
- The violation log may contain snippets of sensitive content — protect it accordingly
- `fail_open` is the default for availability; switch to `fail_closed` for strict compliance environments

---

## Troubleshooting

**"Config file not found"**
→ Run `sw-sentinel init` to create one, or copy `sentinel_config.json.example` to `sentinel_config.json`

**HTTP 401 Unauthorized errors**
→ Your proxy has `proxy_token` set — clients must include `X-Sentinel-Token: <token>` in every request
→ Check `sentinel_config.json` for the token value, or run `sw-sentinel init` to generate a new one

**"superwise_client_id not set"**
→ Edit `sentinel_config.json` and add your credentials from https://app.superwise.ai → Settings → API Tokens

**Requests are blocked that shouldn't be**
→ Check `sw_sentinel_violations.log` to see which guardrail triggered
→ If it's jailbreak detection on internal messages, add those messages to `skip_patterns`
→ Raise the `threshold` value for the relevant guardrail (e.g. `0.7` instead of `0.5`)

**500 errors or Superwise connection issues**
→ Reduce `max_check_chars` if sending very large payloads
→ Check your Superwise plan limits at https://app.superwise.ai
→ Test connectivity: `python3 -c "from superwise_api.superwise_client import SuperwiseClient; SuperwiseClient()"`

**"Port 8080 is already in use"**
→ Another SW-Sentinel instance is already running. The error message will show the PID to kill:
```
[ERROR] Port 8080 is already in use — another SW-Sentinel instance may be running.
[ERROR]   To stop it, run:  kill 47525
```
→ Run the suggested `kill <pid>` command, then start the proxy again

**App traffic is not going through the proxy**
→ Run the built-in health check from the same terminal where you run your app:
```bash
sw-sentinel check
```
This checks whether `ANTHROPIC_BASE_URL` is set correctly, the proxy port is listening, and the proxy responds — with fix hints for each failure:
```
  SW-Sentinel Health Check
  ─────────────────────────────────────────────
  ANTHROPIC_BASE_URL ... (not set)  ✗
    → Run: export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
  Proxy listening    ... 127.0.0.1:8080  ✓
  Proxy reachable    ... OK (responded in 12ms)  ✓
  ─────────────────────────────────────────────
  One or more checks failed. See suggestions above.
```

**Nothing appears in Superwise dashboard**
→ Confirm the proxy started without errors (look for `Superwise connection: OK` in startup output)
→ Send a test request and check `sw_sentinel.log` for `Checking` lines confirming guardrail calls

---

## Credits

Built on the Superwise platform for AI observability and guardrails.

Superwise: https://superwise.ai
