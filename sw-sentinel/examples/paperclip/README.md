# SW-Sentinel + Paperclip Integration

This guide covers how to integrate SW-Sentinel with Paperclip so that all
Anthropic API traffic from Paperclip agents is automatically routed through
Superwise guardrail checks.

---

## How It Works

Paperclip communicates with the Anthropic API using the `ANTHROPIC_BASE_URL`
environment variable. By pointing this variable at SW-Sentinel instead of
`api.anthropic.com` directly, every API call Paperclip makes is intercepted
and checked before forwarding.

```
Paperclip Agent → ANTHROPIC_BASE_URL → SW-Sentinel (port 8080)
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
                                               Returned to Paperclip
```

---

## Prerequisites

- SW-Sentinel installed and configured (see [SW-Sentinel README](../../README.md))
- Paperclip installed and running
- Python 3.8+
- `pip install superwise-api requests`

---

## Setup

### Step 1 — Start SW-Sentinel

```bash
python3 sw_sentinel.py
```

Confirm you see:
```
Proxy ready. Waiting for requests...
Superwise connection: OK
```

### Step 2 — Point Paperclip at SW-Sentinel

Set the environment variable before launching Paperclip:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
```

If Paperclip runs as a separate service or under a different user, set this
variable in its startup script, systemd unit, or `.env` file.

### Step 3 — Verify traffic is flowing

Once Paperclip is active, watch the SW-Sentinel log:

```bash
tail -f sw_sentinel.log
```

You should see entries like:
```
[INFO] Checking 2000 chars [input] request_id=20260415205201946745
[INFO] Forwarding /v1/messages?beta=true → Anthropic (request_id=20260415205201946745)
```

---

## Guardrail Skill for Paperclip Agents

For deeper integration, you can give Paperclip agents an explicit guardrail
skill that instructs them to invoke SW-Sentinel checks as part of their
workflow — not just passively through the proxy, but actively as a step in
task processing.

This is useful when agents are processing structured data (e.g. support
tickets) where you want input and output checked at the application level
in addition to the proxy layer.

See [`lab-setup.md`](./lab-setup.md) for a concrete example of this pattern
using a support ticket processing workflow.

---

## Handling False Positives

Paperclip sends internal orchestration messages through the API (e.g. agent
initialization, heartbeat prompts). These can trigger false positives,
especially with jailbreak detection enabled.

Use `skip_patterns` in `sentinel_config.json` to whitelist known-safe
internal messages:

```json
"skip_patterns": [
  "Continue your Paperclip work",
  "You are agent",
  "Base directory for this skill"
]
```

See the [SW-Sentinel README](../../README.md) for full details on
`skip_patterns`.

---

## Monitoring

| Log | Purpose |
|-----|---------|
| `sw_sentinel.log` | Live activity — every request checked and forwarded |
| `sw_sentinel_violations.log` | Violations only — fires when a request is blocked |

```bash
# Watch all activity
tail -f sw_sentinel.log

# Watch for violations only
tail -f sw_sentinel_violations.log
```
