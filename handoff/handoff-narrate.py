#!/usr/bin/env python3
"""
handoff-narrate.py — model-written Session Narrative for a handoff doc.

The deterministic generate-handoff.py is a regex skimmer: it captures WORDS
(task sentence, file list, order board) but never the ARC of a session — the
*why* behind decisions, the dead-ends and the reasoning that killed them, the
current mental model. A cold session reading a regex skeleton gets "the last
two or three parts," not "it feels like the same session."

This script closes that gap: it reads the transcript, calls `claude -p` (a
cheap model) to write a real narrative, and merges it into the handoff doc
between the Boot block and the Order Ledger. It is GATED + DEBOUNCED by the
caller (handoff-writer.sh) so it runs a handful of times a day, not on every
Stop — 29 handoffs/day must NOT become 29 model calls/day.

Usage:
  handoff-narrate.py <transcript_path> <handoff_md_path> [model]

Idempotent: writes a sidecar handoff-<id>.narrative.md keyed by a transcript
content hash; if the sidecar is already current, it re-merges from cache with
NO model call. Safe to run async/backgrounded.
"""
import json, sys, re, subprocess, hashlib
from pathlib import Path

MODEL_DEFAULT = "haiku"
NARRATIVE_BEGIN = "<!-- HANDOFF-NARRATIVE:BEGIN -->"
NARRATIVE_END = "<!-- HANDOFF-NARRATIVE:END -->"
SECTION_TITLE = "## 🧠 Session Narrative  (the why — read to feel continuous)"

# Skip wrappers that aren't real conversation turns.
SKIP_PREFIX = ("<shed-context", "<system-reminder", "<ide_", "<!--",
               "[TRIDENT", "[COMPACT-GUARD", "[handoff]", "# Handoff",
               "Stop hook feedback", "[approver]")


def load_turns(transcript_path):
    """Return [(role, text)] for genuine user/assistant text turns, in order.
    Tool calls and tool results are dropped — the narrative is about intent and
    reasoning, not the mechanical trace (which the skeleton already pointers)."""
    turns = []
    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            msg = e.get("message") if isinstance(e.get("message"), dict) else e
            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                content = []
            chunks = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    t = (b.get("text") or "").strip()
                    if not t:
                        continue
                    t = re.sub(r'<ide_[^>]*>.*?</ide_[^>]*>', '', t, flags=re.DOTALL).strip()
                    if t and not any(t.startswith(p) for p in SKIP_PREFIX):
                        chunks.append(t)
            if chunks:
                turns.append((role, "\n".join(chunks)))
    return turns


def transcript_digest(turns, cap=60000):
    """Build the conversation text the model reads. Keep the WHOLE arc when it
    fits; when it's huge, keep the head (original intent) + the tail (where we
    actually are) — the middle of a long session is the most re-derivable part.
    """
    lines = []
    for role, text in turns:
        tag = "USER" if role == "user" else "ASSISTANT"
        lines.append(f"### {tag}\n{text}")
    full = "\n\n".join(lines)
    if len(full) <= cap:
        return full, len(turns), False
    # Too big: head 40% + tail 60% (the tail carries current state).
    head_cap = int(cap * 0.4)
    tail_cap = cap - head_cap
    head = full[:head_cap]
    tail = full[-tail_cap:]
    return f"{head}\n\n…[middle of session elided — {len(turns)} turns total]…\n\n{tail}", len(turns), True


PROMPT = """You are writing the SESSION NARRATIVE section of a handoff document. \
A different Claude session, with ZERO prior context, will read ONLY this \
narrative plus a deterministic facts-skeleton (file list, git state, order \
board, the user's last message). Your job is to make that cold session feel \
like it never left — to transplant the *understanding*, not the trace.

Write the narrative so the next session can think the way this one ended up \
thinking. Capture, in this order, only what applies:

1. **What we're really doing & why** — the actual goal behind the literal \
request, in 2-4 sentences. The mission, not the ticket.
2. **The mental model** — the key facts/architecture/constraints we figured \
out that aren't obvious from the code. What does the next session need to \
"just know"?
3. **Decisions + reasoning** — choices made and *why* (including why the \
alternatives lost). This is the highest-value content; be specific.
4. **Dead-ends** — what we tried that did NOT work, and the reason, so the \
next session doesn't waste a cycle re-attempting it.
5. **Current state & the open thread** — where we genuinely are right now, and \
the very next thing on our mind (even if unstated by the user).

Rules:
- Write in plain, dense prose with short headers. NO preamble, NO "in this \
session we…", NO restating the file list or git state (the skeleton has those).
- Be concrete: name the real files, functions, decisions, numbers.
- If something is genuinely uncertain or unresolved, say so — a known unknown \
is more useful than false confidence.
- Length: as long as the session's substance warrants, but every line must \
earn its place. A short session gets a short narrative.
- Output ONLY the markdown body (headers + prose). Do not wrap it in a code \
block. Do not add a top-level title — that's added for you.

Here is the conversation transcript:

---
{TRANSCRIPT}
---

Now write the Session Narrative."""


def call_model(digest, model):
    """Run `claude -p` headless. Returns narrative text or None on any failure.
    Fails OPEN: a missing/erroring model just means no narrative this run, never
    a broken handoff."""
    prompt = PROMPT.replace("{TRANSCRIPT}", digest)
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", model],
            input=prompt, capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        print("[narrate] claude CLI not found — skipping", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print("[narrate] model timed out — skipping", file=sys.stderr)
        return None
    except Exception as ex:
        print(f"[narrate] model call failed: {ex}", file=sys.stderr)
        return None
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or len(out) < 80:
        print(f"[narrate] model returned nothing usable (rc={proc.returncode}, "
              f"len={len(out)})", file=sys.stderr)
        return None
    # Strip an accidental code-fence wrap or echoed title.
    out = re.sub(r'^```[a-z]*\n', '', out)
    out = re.sub(r'\n```\s*$', '', out)
    out = re.sub(r'^#+\s*Session Narrative.*\n', '', out, count=1, flags=re.I)
    return out.strip()


def merge_into_doc(doc_path, narrative, digest_hash):
    """Insert/replace the narrative block in the handoff md, placed right after
    the Boot block (before the Order Ledger) so the WHY sits above the WHAT."""
    block = (
        f"{NARRATIVE_BEGIN}\n{SECTION_TITLE}\n"
        f"<sub>_model-written from the full transcript · hash {digest_hash[:8]}_</sub>\n\n"
        f"{narrative}\n{NARRATIVE_END}"
    )
    text = doc_path.read_text() if doc_path.exists() else ""

    # Replace an existing block if present.
    if NARRATIVE_BEGIN in text and NARRATIVE_END in text:
        new = re.sub(
            re.escape(NARRATIVE_BEGIN) + r".*?" + re.escape(NARRATIVE_END),
            block, text, flags=re.DOTALL)
        doc_path.write_text(new)
        return

    # Otherwise insert before the Order Ledger header, else before the first
    # "---" fold, else append.
    anchor = None
    for marker in ("## 📋 Order Ledger", "## ⚠️ ORDER LEDGER", "## 📨 Last message"):
        idx = text.find(marker)
        if idx != -1:
            anchor = idx
            break
    if anchor is None:
        idx = text.find("\n---\n")
        anchor = idx + 1 if idx != -1 else len(text)
    new = text[:anchor].rstrip() + "\n\n" + block + "\n\n" + text[anchor:].lstrip()
    doc_path.write_text(new)


def main():
    if len(sys.argv) < 3:
        print("usage: handoff-narrate.py <transcript> <handoff_md> [model]", file=sys.stderr)
        sys.exit(2)
    transcript_path = Path(sys.argv[1])
    doc_path = Path(sys.argv[2])
    model = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else MODEL_DEFAULT
    if not transcript_path.exists():
        print("[narrate] transcript missing — skipping", file=sys.stderr)
        sys.exit(0)

    turns = load_turns(transcript_path)
    if len(turns) < 2:
        print("[narrate] too few turns — skipping", file=sys.stderr)
        sys.exit(0)

    digest, n_turns, elided = transcript_digest(turns)
    digest_hash = hashlib.sha256(digest.encode("utf-8", "replace")).hexdigest()

    # Debounce: sidecar caches the last narrative keyed by content hash. If the
    # conversation hasn't changed since the last narration, re-merge from cache
    # with NO model call.
    sidecar = doc_path.with_suffix(".narrative.md")
    cached_hash = None
    if sidecar.exists():
        head = sidecar.read_text()[:200]
        m = re.search(r'hash:\s*([0-9a-f]{64})', head)
        if m:
            cached_hash = m.group(1)

    if cached_hash == digest_hash:
        body = sidecar.read_text()
        body = re.sub(r'^<!--.*?-->\n', '', body, count=1, flags=re.DOTALL)
        merge_into_doc(doc_path, body.strip(), digest_hash)
        print("[narrate] reused cached narrative (no model call)", file=sys.stderr)
        sys.exit(0)

    narrative = call_model(digest, model)
    if not narrative:
        sys.exit(0)  # fail open — skeleton stands on its own

    sidecar.write_text(f"<!-- hash: {digest_hash} -->\n{narrative}\n")
    merge_into_doc(doc_path, narrative, digest_hash)
    print(f"[narrate] wrote narrative ({len(narrative)} chars, {n_turns} turns"
          f"{', elided' if elided else ''}) via {model}", file=sys.stderr)


if __name__ == "__main__":
    main()
