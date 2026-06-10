#!/usr/bin/env python3
"""trident — hook-independent floor (DESIGN.md §3.8).

Reverse proxy 127.0.0.1:8742 → api.anthropic.com. stdlib asyncio only.

SSE-TRANSPARENT BY CONSTRUCTION: response bytes are relayed chunk-by-chunk exactly
as they arrive — never parsed, never buffered (a side tee feeds the usage ledger).

Only two powers:
  1. rewrite "model" in a /v1/messages request body when its tier exceeds the wallet
     ceiling for the requesting class (headless default — this proxy is wired for
     headless daemons only in Wave 4a);
  2. append {ts, model, usage} to proxy-ledger.jsonl (from final SSE message_delta
     or non-streaming response), best-effort.

Known SPOF mitigation: self-checks upstream on start and exits non-zero if dead
(launchd KeepAlive loops) rather than half-working; consumers (env-stamp.sh) only
set ANTHROPIC_BASE_URL after a live port health-check, so a dead proxy means
DIRECT calls, not broken ones. `trident proxy off` flips the kill flag + unloads.
"""

import asyncio
import json
import os
import re
import ssl
import sys
import time

STATE = os.environ.get("TRIDENT_STATE_DIR") or os.path.expanduser("~/.claude/state")
WALLET = os.path.join(STATE, "wallet.json")
LEDGER = os.path.join(STATE, "proxy-ledger.jsonl")
OFF_FLAG = os.path.join(STATE, "trident-proxy-off")
UPSTREAM = "api.anthropic.com"
PORT = 8742
TIER_RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3}
TIER_MODEL = {"haiku": "claude-haiku-4-5-20251001",
              "sonnet": "claude-sonnet-4-6",
              "opus": "claude-opus-4-8",
              "fable": "claude-fable-5"}


def model_tier(model_id):
    for t in TIER_RANK:
        if t in (model_id or ""):
            return t
    return None


def wallet_ceiling():
    """Headless class ceiling from wallet; stale/missing wallet → no ceiling (fail open)."""
    try:
        if time.time() - os.path.getmtime(WALLET) > 120:
            return None
        with open(WALLET) as f:
            w = json.load(f)
        if not w.get("contract_ok"):
            return None
        return model_tier(w.get("headless", {}).get("model", ""))
    except Exception:
        return None


def ledger_append(rec):
    try:
        with open(LEDGER, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


async def read_headers(reader):
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin1").split("\r\n")
    request_line = lines[0]
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return request_line, headers, head


def rebuild_request(request_line, headers, body):
    out = [request_line]
    headers["host"] = UPSTREAM
    if body is not None:
        headers["content-length"] = str(len(body))
    headers.pop("accept-encoding", None)  # keep response parseable for the ledger tee
    # One connection per request: upstream closes at response end → relay loop gets
    # EOF immediately (exact stream-end detection; keep-alive would hang the ledger).
    headers["connection"] = "close"
    for k, v in headers.items():
        out.append(f"{k}: {v}")
    blob = ("\r\n".join(out) + "\r\n\r\n").encode("latin1")
    return blob + (body or b"")


def maybe_rewrite(body):
    """Rewrite request model above the wallet ceiling. Returns (body, original, final)."""
    try:
        d = json.loads(body)
    except Exception:
        return body, None, None
    model = d.get("model")
    ceiling = wallet_ceiling()
    t = model_tier(model)
    if model and ceiling and t and TIER_RANK[t] > TIER_RANK[ceiling]:
        d["model"] = TIER_MODEL[ceiling]
        return json.dumps(d).encode(), model, d["model"]
    return body, model, model


def parse_usage(tail_bytes):
    """Best-effort usage from the response tail (final SSE message_delta or JSON body)."""
    text = tail_bytes.decode("utf-8", "replace")
    for m in reversed(re.findall(r'"usage"\s*:\s*({(?:[^{}]|{[^{}]*})*})', text)):
        try:
            return json.loads(m)
        except Exception:
            continue
    return None


async def handle(client_r, client_w):
    up_w = None
    try:
        request_line, headers, _ = await read_headers(client_r)
        clen = int(headers.get("content-length", 0) or 0)
        body = await client_r.readexactly(clen) if clen else b""

        orig = final = None
        if "/v1/messages" in request_line and body:
            body, orig, final = maybe_rewrite(body)

        ctx = ssl.create_default_context()
        up_r, up_w = await asyncio.open_connection(UPSTREAM, 443, ssl=ctx)
        up_w.write(rebuild_request(request_line, headers, body))
        await up_w.drain()

        # Relay response bytes verbatim — chunk through untouched (SSE-transparent).
        tail = b""
        try:
            while True:
                chunk = await up_r.read(65536)
                if not chunk:
                    break
                client_w.write(chunk)
                await client_w.drain()
                tail = (tail + chunk)[-32768:]  # side tee for the ledger, bounded
        finally:
            # ledger even if the client hung up mid-stream — metering is honest
            if final:
                ledger_append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               "model": final, "requested_model": orig,
                               "rewritten": bool(orig and orig != final),
                               "usage": parse_usage(tail)})
    except Exception:
        pass
    finally:
        for w in (client_w, up_w):
            try:
                if w:
                    w.close()
            except Exception:
                pass


async def main():
    if os.path.exists(OFF_FLAG):
        print("trident-proxy: off flag set — exiting", file=sys.stderr)
        sys.exit(0)  # clean exit: KeepAlive with SuccessfulExit=false won't thrash
    # Self-check upstream BEFORE binding — never half-work.
    try:
        ctx = ssl.create_default_context()
        r, w = await asyncio.wait_for(
            asyncio.open_connection(UPSTREAM, 443, ssl=ctx), timeout=10)
        w.close()
    except Exception as e:
        print(f"trident-proxy: upstream unreachable ({e}) — exiting non-zero", file=sys.stderr)
        sys.exit(1)
    server = await asyncio.start_server(handle, "127.0.0.1", PORT)
    print(f"trident-proxy: listening on 127.0.0.1:{PORT} → {UPSTREAM}", file=sys.stderr)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
