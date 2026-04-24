# SW-Sentinel + Paperclip — Lab Setup Reference

This document describes the specific deployment of SW-Sentinel in the
Superwise OpenClaw lab environment. It serves as both an internal reference
and a real-world example of SW-Sentinel running in a multi-user agentic
environment.

---

## Environment Overview

| Component | Details |
|-----------|---------|
| Host | `openclaw-lab` VM |
| Proxy user | `clawrunner` |
| Proxy script | `/home/clawrunner/sw_proxy.py` |
| Paperclip user | `clawrunner` |
| Paperclip path | `/home/clawrunner/paperclip/` |
| OpenClaw gateway | Running as `labadmin`, port 8080 |
| Upstream | `https://api.anthropic.com` |

---

## Process Layout

SW-Sentinel (`sw_proxy.py`) and Paperclip both run under the `clawrunner`
user. OpenClaw runs the gateway process under `labadmin`. All Paperclip
API traffic routes through SW-Sentinel before reaching Anthropic.

```
Paperclip (clawrunner)
    └─ ANTHROPIC_BASE_URL=http://127.0.0.1:8080
          └─ sw_proxy.py (clawrunner, port 8080)
                └─ Superwise guardrail check
                      └─ api.anthropic.com
```

---

## Logs

```bash
# Live proxy activity (requires sudo from labadmin)
sudo tail -f /home/clawrunner/sw_proxy.log

# Violations audit log
sudo tail -f /home/clawrunner/sw_proxy_violations.log
```

Sample log output showing Paperclip heartbeat traffic passing through:
```
[INFO] Checking 101 chars [input] request_id=... sample='You are agent 3e5ae7e2... Continue your Paperclip work.'
[INFO] Forwarding /v1/messages?beta=true → Anthropic (request_id=...)
```

---

## Guardrail Skill — SKILL_SW_GUARDRAILS.md

In addition to the proxy layer, Paperclip agents in this environment are
given an explicit guardrail skill (`SKILL_SW_GUARDRAILS.md`) that instructs
them to invoke a standalone guardrail checker script as part of their task
workflow.

### Skill location
```
/home/clawrunner/SKILL_SW_GUARDRAILS.md
```

### Guardrail checker script
```
/home/clawrunner/sw_guardrail_check.py
```

### How it works

The skill instructs agents to run guardrail checks at the application level
on ticket content — separate from and in addition to the proxy layer:

**Input check — before processing a ticket:**
```bash
python3 /home/clawrunner/sw_guardrail_check.py \
  --ticket-id "TKT-XXXX" \
  --direction input \
  --text "TICKET BODY TEXT HERE"
```

**Output check — before writing a response:**
```bash
python3 /home/clawrunner/sw_guardrail_check.py \
  --ticket-id "TKT-XXXX" \
  --direction output \
  --text "DRAFTED RESPONSE HERE"
```

Exit code `0` = PASSED, exit code `1` = BLOCKED.

### Why both layers?

| Layer | What it catches |
|-------|----------------|
| SW-Sentinel proxy | All API traffic — passive, no agent awareness required |
| SKILL_SW_GUARDRAILS | Structured task content — agent actively checks ticket data before and after processing |

The proxy is the safety net. The skill is defense-in-depth for structured
workflows where the agent is processing sensitive data (e.g. customer
support tickets containing PII).

---

## Violation Logging

All violations are written to:
```
/home/clawrunner/sw_proxy_violations.log
```

This log is the audit trail for compliance review.

---

## Notes

- SW-Sentinel binds to `127.0.0.1` by default — accessible only from localhost
- The proxy runs persistently as a background process under `clawrunner`
- Paperclip's internal orchestration messages (agent init, heartbeats) pass
  through clean — they are whitelisted via `skip_patterns` in the config
