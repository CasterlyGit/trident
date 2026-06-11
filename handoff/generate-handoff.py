#!/usr/bin/env python3
"""
generate-handoff.py — Session handoff snapshot generator.
Reads transcript JSONL, extracts signal, writes workspace-scoped handoff to
~/.claude/state/handoff-<workspace-key>.md.
Completeness-first: the CASTER KITCHEN board (a hard per-reply rule) is scraped
as the COMPLETE order ledger so queued/unstarted work — which leaves no code or
command footprint — can never be silently dropped. Leanness applies only to
re-pasteable noise (tool output, logs), never to orders/intent/state.
Usage: python3 generate-handoff.py <transcript_path> <session_id> [hf_id]
"""
import json, sys, re
from pathlib import Path
from datetime import datetime, timezone
from collections import OrderedDict

def is_junk_user_text(txt):
    """Return True if this looks like tool output noise, not a real user message."""
    if len(txt) <= 10:
        return True
    # File listings
    if re.match(r'^-r[w-]|^drwx|^total \d', txt):
        return True
    # IN/OUT blocks (harness tool summaries)
    if re.search(r'\nIN\n|\nOUT\n|\nIN:\n|\nOUT:\n', txt):
        return True
    # Looks like cat -n output (line-numbered file dump)
    if re.match(r'^\s*\d+\t', txt):
        return True
    # Looks like a bash listing injected via tool result
    if re.match(r'^-rw|^drwx|^\d{4}-\d{2}-\d{2}', txt):
        return True
    # Very short follow-up confirmations that aren't the original task
    short_followups = re.compile(
        r'^(ok|okay|yes|no|yep|nope|sure|thanks|got it|done|nice|great|cool|'
        r'did you|does it|is it|can you|what about|and|also|so |hmm|uh |um )',
        re.IGNORECASE
    )
    if short_followups.match(txt) and len(txt) < 80:
        return True
    return False

def score_user_text(txt):
    """Score a user message by how likely it is to be the REAL task description."""
    score = len(txt)  # longer = more substantive
    # Slash-command templates (/loop, /goal, …) are harness-expanded prompt
    # scaffolding, not the user's intent. They're often THOUSANDS of chars, so a
    # fixed penalty can't beat the length bonus — force the score to the floor so
    # the real ask always wins the Active Task slot.
    if (re.match(r'^/[a-z][\w-]*\b', txt.strip())
            or re.match(r'^#+\s*/[a-z][\w-]*\b', txt.strip())
            or "Parse the input below into" in txt
            or re.search(r'(?m)^##?\s+Parsing\b', txt)):
        return -10_000
    # Reward concrete imperative task language
    if re.search(r'\b(fix|build|add|create|update|check|write|refactor|deploy|test|debug|'
                 r'implement|make|generate|change|move|delete|find|show|explain|help)\b', txt, re.I):
        score += 200
    # Penalise follow-up noise
    if re.match(r'^(ok|yes|no|yep|sure|thanks|got it|done|nice|great)', txt, re.I):
        score -= 500
    # Penalise SHORT meta-questions about the plumbing (handoff/session/token system)
    # but NOT substantive work on shed itself, which legitimately mentions these terms.
    # Threshold: a short message that mentions plumbing is meta-chatter; a long one is real work.
    if len(txt) < 240 and re.search(
        r'\b(handoff|hand.?off|hf_|shed-?ho-?|new session|fresh session|compact.?guard|'
        r'inject\.sh|bypass permission)\b', txt, re.I
    ):
        score -= 600
    return score

def git_state(workdir):
    """One cheap snapshot of the repo at write-time: dirty-state + last commit.
    Runs inside the Stop/compact hook that already fires — adds no turn stage.
    Returns (dirty_count, last_commit_line, ahead_behind) or (None, None, None)."""
    import subprocess
    if not workdir or not Path(workdir).is_dir():
        return None, None, None
    def run(args):
        try:
            return subprocess.run(args, cwd=workdir, capture_output=True,
                                  text=True, timeout=3).stdout.strip()
        except Exception:
            return ""
    if not run(["git", "rev-parse", "--is-inside-work-tree"]) == "true":
        return None, None, None
    status = run(["git", "status", "--short"])
    dirty = len([l for l in status.splitlines() if l.strip()]) if status else 0
    last_commit = run(["git", "log", "-1", "--format=%h %s"])[:80] or None
    ab = run(["git", "rev-list", "--count", "--left-right", "@{upstream}...HEAD"])
    ahead_behind = None
    if ab and "\t" in ab:
        behind, ahead = ab.split("\t")[:2]
        bits = []
        if ahead and ahead != "0": bits.append(f"{ahead} ahead")
        if behind and behind != "0": bits.append(f"{behind} behind")
        ahead_behind = ", ".join(bits) if bits else "in sync with upstream"
    return dirty, last_commit, ahead_behind


def _word_overlap(a, b):
    """Count label/message words that match by exact token OR stem prefix (≥4
    chars), so 'handoffs' matches 'handoff' — strict exact-match misses variants."""
    wa = set(re.findall(r'[a-z]{4,}', (a or "").lower()))
    wb = set(re.findall(r'[a-z]{4,}', (b or "").lower()))
    n = 0
    for x in wa:
        if any(x == y or x.startswith(y) or y.startswith(x) for y in wb):
            n += 1
    return n


def parse_kitchen_board(assistant_texts):
    """The CASTER KITCHEN board is rendered in (nearly) every multi-order reply as a
    hard behavioral rule, so the MOST RECENT board in the transcript is the live,
    COMPLETE order list — including QUEUED items that left zero code/command
    footprint. Scraping it is what makes the handoff complete-by-construction:
    if an order is on the board, it cannot be dropped from the ledger.

    Returns (orders, board_seen):
      orders     = [{emoji,num,label,status,raw}] parsed from the latest board
      board_seen = True if any board appeared at all (drives the fail-loud guard).
    """
    # A text "mentions" the board (prose like "the CASTER KITCHEN is your dashboard")
    # far more often than it *is* a board. Collect every candidate that could hold
    # rows, then parse the MOST RECENT one that actually yields ≥1 row — so a later
    # prose mention can't shadow the real board.
    board_seen = any("CASTER KITCHEN" in t for t in assistant_texts)
    candidates = [t for t in assistant_texts
                  if "CASTER KITCHEN" in t or ("ORDER" in t and "│" in t)]
    if not candidates:
        return [], board_seen

    STATUS_EMOJI = "✅🔥⏳⏸❌🆕"
    EMOJI_STATUS = {"✅": "SERVED", "🔥": "IN-PROGRESS", "⏳": "QUEUED",
                    "⏸": "PAUSED", "❌": "DROPPED", "🆕": "NEW"}
    def parse_rows(text):
        rows = []
        for line in text.splitlines():
            if "ORDER" not in line:
                continue
            # A row is a box line OR an emoji-led line — not prose mentioning "order".
            if "│" not in line and not any(ch in STATUS_EMOJI for ch in line):
                continue
            m = re.search(r'ORDER\s*(\d+)\b[\s.:：–—-]*(.*)$', line)
            if not m:
                continue
            num = m.group(1)
            # Trailing box border + padding off the captured remainder.
            rest = re.sub(r'[│|]\s*$', '', m.group(2)).rstrip()
            # Emoji = first known status glyph before "ORDER"; lenient about variation
            # selectors / box chars sitting between it and the word.
            emoji = next((ch for ch in line[:m.start()] if ch in STATUS_EMOJI), "")
            # Status = trailing ALL-CAPS token; else inferred from the row emoji.
            status, label = "", rest
            sm = re.search(r'\s{2,}([A-Z][A-Z \-/]{2,})\s*$', rest)
            if sm:
                status, label = sm.group(1).strip(), rest[:sm.start()].strip()
            if not status and emoji:
                status = EMOJI_STATUS.get(emoji, "")
            label = re.sub(r'^[\s│|✅🔥⏳⏸❌🆕️•]+', '', label).strip()
            if not label:
                continue
            rows.append({"emoji": emoji, "num": num, "label": label,
                         "status": status, "raw": line.strip()})
        return rows

    for txt in reversed(candidates):       # newest first; first real board wins
        rows = parse_rows(txt)
        if rows:
            return rows, board_seen
    return [], board_seen


def order_detail(order, user_texts, orders_json):
    """Enrich a board row with enough context to ACT. Priority:
      1. crafted detail from the structured ledger (orders-<session>.json) — wins;
      2. the most relevant CLAUSE the user actually said about THIS order (scanned
         across every message, so two orders born from one message still get
         distinct, on-topic detail — not the same generic blob);
      3. the head of the best-overlapping originating ask as a last resort."""
    label = order["label"]
    for o in orders_json or []:
        if str(o.get("num", "")) == str(order["num"]) or (
                o.get("label") and _word_overlap(o["label"], label) >= 2):
            d = (o.get("detail") or "").strip()
            if d:
                return d, "ledger"
    # Clause-level match: split every user message into sentence/clause units and
    # pick the one most about this order's label.
    best, best_score = None, 0
    for txt in user_texts:
        for clause in re.split(r'(?<=[.!?])\s+|\n+|…|…|(?<=\.\.\.)\s+', txt):
            clause = re.sub(r'\s+', ' ', clause).strip(" -•\t")
            if not (15 <= len(clause) <= 240):
                continue
            sc = _word_overlap(label, clause)
            if sc > best_score or (sc == best_score and best and len(clause) > len(best)):
                best, best_score = clause, sc
    if best and best_score >= 1:
        return best, "transcript"
    # Last resort: head of the best-overlapping whole message.
    fb, fb_score = None, 0
    for txt in user_texts:
        sc = _word_overlap(label, txt)
        if sc > fb_score and len(txt) > 20:
            fb, fb_score = txt, sc
    if fb:
        lead = re.sub(r'\s+', ' ', fb).strip()
        return (lead[:240] + ("…" if len(lead) > 240 else "")), "transcript"
    return None, None


def generate(transcript_path, session_id, hf_id=None):
    events = []
    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try: events.append(json.loads(line))
            except: continue

    modified_files = OrderedDict()
    bash_significant = []
    assistant_texts = []        # all assistant texts
    user_texts = []             # all real user texts
    last_errors = []
    accomplishments = []        # what was done/shipped
    cwd_counts = {}             # working dirs seen (free structured signal)
    branch_counts = {}          # git branches seen
    touched_symbols = []        # def/class/function names from edits — what changed
    verify_cmd = None           # inferred test/run command — how to verify
    ruled_out = []              # approaches the session tried and abandoned
    decisions = []              # choices made + (sometimes) why
    last_edit_file = None       # most recent file edited — resume pointer anchor
    raw_last_user = None        # genuine last user turn (light filter; keep short asks)
    user_texts_light = []       # all real user turns, light filter — detail-matching
                                # pool (is_junk drops "also/and…" asks that ARE orders)
    SKIP = ("<shed-context", "<system-reminder", "<ide_opened_file", "<ide_selection",
            "<ide_", "<!--", "🔴", "⚠️", "📋", "🛑", "[handoff]", "# Handoff",
            "[approver]", "Stop hook feedback", "[TRIDENT", "[COMPACT-GUARD")

    SYM_RE = re.compile(r'(?:^|\n)\s*(?:export\s+)?(?:async\s+)?'
                        r'(?:def|class|func|function|fn)\s+([A-Za-z_]\w*)')

    for e in events:
        # Free structured signal the harness records on every event.
        cwd = e.get("cwd")
        if cwd:
            cwd_counts[cwd] = cwd_counts.get(cwd, 0) + 1
        br = e.get("gitBranch")
        if br and br != "HEAD":
            branch_counts[br] = branch_counts.get(br, 0) + 1

        msg = e.get("message") if isinstance(e.get("message"), dict) else e
        role = msg.get("role", "")
        content = msg.get("content", [])
        if not isinstance(content, list): content = []

        if role == "assistant":
            for b in content:
                if not isinstance(b, dict): continue
                if b.get("type") == "text":
                    txt = b.get("text", "").strip()
                    if txt and not any(txt.startswith(p) for p in SKIP):
                        assistant_texts.append(txt)
                        # Extract accomplishment sentences (skip pipe-table lines)
                        done_hits = re.findall(
                            r'(?:^|(?<=\n))([^|\n][^.\n]{14,119}(?:done|fixed|shipped|added|deployed|'
                            r'updated|written|created|removed|works now|working)[^.\n]{0,60}\.)',
                            txt, re.IGNORECASE
                        )
                        accomplishments.extend(done_hits[:2])
                        # Ruled-out / dead-path sentences — the single most valuable
                        # thing to carry forward: stops the next agent re-trying it.
                        # Require the dead-path verb to LEAD the sentence (an actual
                        # report — "Tried X, reverted") so design/explanatory prose
                        # ("the highest-value gap is...") doesn't false-positive.
                        ro_hits = re.findall(
                            r'(?:^|(?<=\n))\s*([A-Z][^|\n]{0,12}?\b(?:tried|attempted|'
                            r'reverted|rolled back|abandoned|backed out|gave up)\b'
                            r'[^.\n]{6,110}\.)',
                            txt
                        )
                        ruled_out.extend(h.strip() for h in ro_hits[:2])
                        # Decision sentences — choices made WITH rationale. Require an
                        # explicit choice verb leading + a reason marker, else it's noise.
                        dec_hits = re.findall(
                            r'(?:^|(?<=\n))\s*([A-Z][^|\n]{0,14}?\b(?:chose|decided|'
                            r'opted for|going with|settled on|switched to)\b'
                            r'[^.\n]{6,130}\.)',
                            txt
                        )
                        decisions.extend(h.strip() for h in dec_hits[:2])
                elif b.get("type") == "tool_use":
                    name = b.get("name", "")
                    inp = b.get("input") or {}
                    if name in ("Write", "Edit", "MultiEdit"):
                        fp = inp.get("file_path", "")
                        if fp:
                            modified_files[fp] = name.lower()
                            last_edit_file = fp
                        # Pull symbol names out of what was written/changed so the
                        # next agent knows WHAT changed, not just which file.
                        blob = " ".join(str(inp.get(k, "")) for k in
                                        ("new_string", "content", "old_string"))
                        GENERIC_SYM = {"run", "main", "init", "test", "setup",
                                       "wrapper", "inner", "helper", "cb", "fn", "f"}
                        for sym in SYM_RE.findall(blob):
                            if (len(sym) > 2 and sym.lower() not in GENERIC_SYM
                                    and sym not in touched_symbols):
                                touched_symbols.append(sym)
                    elif name == "Bash":
                        cmd = (inp.get("command") or "").strip()
                        if cmd and any(k in cmd for k in (
                            "git commit", "git push", "npm install", "pip install",
                            "pytest", "brew install", "uv "
                        )):
                            bash_significant.append(cmd[:120])
                        # First test/run invocation seen = how to verify the work.
                        if verify_cmd is None and re.search(
                            r'\b(pytest|npm (?:run )?test|npm test|jest|vitest|go test|'
                            r'cargo test|bash .*test|make test|python3? -m pytest)\b', cmd):
                            # keep just the test command line, not a multi-line script
                            for ln in cmd.splitlines():
                                if re.search(r'\b(pytest|test)\b', ln):
                                    verify_cmd = ln.strip()[:120]
                                    break

        if role == "user":
            for b in content:
                if not isinstance(b, dict): continue
                if b.get("type") == "text":
                    txt = b.get("text", "").strip()
                    if not txt or any(txt.startswith(p) for p in SKIP):
                        continue
                    # Strip any embedded IDE harness blocks before scoring
                    txt = re.sub(r'<ide_[^>]*>.*?</ide_[^>]*>', '', txt, flags=re.DOTALL).strip()
                    if not txt or any(txt.startswith(p) for p in SKIP):
                        continue
                    # Lighter filter for the LAST-message capture: keep short human
                    # confirmations ("yes, ship it") that is_junk would drop — the
                    # unprocessed tail must survive — while still rejecting tool noise.
                    if (len(txt) > 2 and not re.match(r'^\s*\d+\t|^-r[w-]|^drwx|^total \d', txt)
                            and '\nIN\n' not in txt and '\nOUT\n' not in txt):
                        raw_last_user = txt
                        user_texts_light.append(txt)
                    if not is_junk_user_text(txt):
                        user_texts.append(txt)
                elif b.get("type") == "tool_result":
                    rc = b.get("content") or []
                    if isinstance(rc, str): rc = [{"type": "text", "text": rc}]
                    for rb in rc:
                        if not isinstance(rb, dict): continue
                        t = rb.get("text", "")
                        # Real errors only — must look like an actual runtime/tool failure
                        has_traceback = "Traceback" in t
                        has_exit = bool(re.search(r'\bExit code [1-9]\b|\bexited with\b|\bfailed with\b', t))
                        has_error_line = bool(re.search(r'^(Error|TypeError|ValueError|ImportError|ModuleNotFoundError|SyntaxError|FileNotFoundError|PermissionError|KeyError|AttributeError|RuntimeError|OSError)[\s:]', t, re.MULTILINE))
                        if not (has_traceback or has_exit or has_error_line):
                            continue
                        # A bare non-zero exit whose body is just a file/dir listing
                        # (grep/ls/find returning "no match") is not a real failure.
                        if has_exit and not has_traceback and not has_error_line:
                            body = re.sub(r'^Exit code \d+\s*', '', t).strip()
                            listing_lines = sum(
                                1 for ln in body.splitlines()[:6]
                                if re.match(r'^(-[rwxsdtST-]{9}|drwx|total \d|\S+\s+\d+\s)', ln))
                            if listing_lines >= 2:
                                continue
                        if len(t) < 20: continue
                        if re.match(r'^\d+\t', t): continue          # Read tool line-numbered output
                        if "# Handoff" in t[:100]: continue           # handoff doc contents
                        if t.startswith("[handoff]"): continue
                        if re.match(r'^-r[w-]|^drwx', t): continue   # file listings
                        last_errors.append(t[:300])

    # ── Pick the best "Active Task" from user messages ──────────────────────
    # Use the highest-scored message; exclude the very last one if it's a
    # follow-up like "check if this worked"
    task_candidates = user_texts[:]
    if task_candidates:
        best_task = max(task_candidates, key=score_user_text)
    else:
        best_task = None

    # ── Next steps from recent assistant messages ────────────────────────────
    todo_hits = []
    for txt in assistant_texts[-10:]:
        hits = re.findall(
            r'(?:^|\n)[-*\d.]\s*(?:TODO|Next|Still need|Remaining|Then|After that|also|and then)'
            r'[:\s]+([^\n.]{10,80})',
            txt, re.IGNORECASE
        )
        todo_hits.extend(hits)
    seen = set()
    deduped_todos = []
    for t in todo_hits:
        k = t.strip().lower()[:40]
        if k not in seen:
            seen.add(k)
            deduped_todos.append(t.strip())

    # ── Derive shared facts ──────────────────────────────────────────────────
    workdir = max(cwd_counts, key=cwd_counts.get) if cwd_counts else None
    branch = max(branch_counts, key=branch_counts.get) if branch_counts else None
    dirty, last_commit, ahead_behind = git_state(workdir)

    def dedupe(seq, n, klen=44):
        seen_, out = set(), []
        for x in seq:
            k = x.strip().lower()[:klen]
            if k and k not in seen_:
                seen_.add(k); out.append(x.strip())
            if len(out) >= n: break
        return out

    # Compress Active Task to its densest lead sentence (biggest token sink).
    lead = None
    if best_task:
        task = best_task.strip()
        m = re.match(r'(.{40,280}?[.!?])(\s|$)', task)
        lead = (m.group(1) if m else task[:280]).strip()

    # "Where we left off" — prefer a completed-state message over pending-action.
    # Widened: keep up to 2 substantive recent assistant turns (the arc, not one
    # truncated sentence) at a larger cap, so a deep session isn't flattened to
    # 480 chars. The model narrative carries the *why*; this carries the *what*.
    left_off = None
    if assistant_texts:
        def score_state(t):
            s = 50 if len(t) > 80 else 0
            if re.search(r'\b(done|fixed|shipped|applied|updated|written|created|deployed|'
                         r'works|working|complete|all set)\b', t, re.I): s += 300
            if re.search(r'\b(files to update|will update|then i.ll|about to|next i.ll|'
                         r'going to|need to|let me)\b', t, re.I): s -= 400
            return s
        def clip(t, n):
            t = t[:n].strip()
            if t and t[-1] not in ".!?\n":
                cut = max(t.rfind(". "), t.rfind(".\n"))
                if cut > int(n * 0.33): t = t[:cut+1]
            return t
        substantive = [t for t in assistant_texts if len(t) > 80]
        pool = substantive[-6:] if substantive else assistant_texts[-3:]
        if pool:
            ranked = sorted(pool, key=score_state, reverse=True)[:2]
            # Preserve chronological order among the kept turns.
            ranked = [t for t in pool if t in ranked]
            left_off = "\n\n".join(clip(t, 700) for t in ranked).strip()
        else:
            left_off = clip(assistant_texts[-1], 700)

    # Recent Exchange — the last few real user↔assistant turns, kept (nearly)
    # verbatim. This is the cheapest thing that makes a resume feel continuous:
    # the next session sees the actual end of the conversation, not a summary of
    # it. Skip tool-noise turns; cap each turn so it can't blow up the doc.
    recent_exchange = []
    convo = []
    for e in events:
        msg = e.get("message") if isinstance(e.get("message"), dict) else e
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            content = []
        txts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                tt = (b.get("text") or "").strip()
                tt = re.sub(r'<ide_[^>]*>.*?</ide_[^>]*>', '', tt, flags=re.DOTALL).strip()
                if tt and not any(tt.startswith(p) for p in SKIP):
                    txts.append(tt)
        if not txts:
            continue
        joined = "\n".join(txts)
        if role == "user" and is_junk_user_text(joined):
            continue
        convo.append((role, joined))
    for role, txt in convo[-6:]:
        who = "🧑 You" if role == "user" else "🤖 Claude"
        body = re.sub(r'\s+\n', '\n', txt).strip()
        if len(body) > 700:
            body = body[:700].rstrip() + " …"
        recent_exchange.append((who, body))

    # Resume pointer: the one concrete thing to open/run first.
    resume = None
    if last_edit_file:
        anchor = f"`{last_edit_file}`"
        if touched_symbols:
            anchor += f" (near `{touched_symbols[-1]}`)"
        resume = f"Open {anchor}"
    elif verify_cmd:
        resume = f"Run `{verify_cmd}` to see current state"

    ro = dedupe(ruled_out, 4)
    dec = dedupe(decisions, 4)

    # ── Order ledger: the COMPLETE, drop-proof list of tracked asks ───────────
    # The footprint scrape above is blind to QUEUED work (no edit, no command).
    # The CASTER KITCHEN board — a hard per-reply rule — is the live, complete
    # mirror of every order, so its latest render IS the ledger. Inline ALL of it
    # verbatim and enrich each row. Completeness is structural: on the board ⇒ in
    # the handoff.
    board_orders, board_seen = parse_kitchen_board(assistant_texts)
    if board_seen and not board_orders:
        print("[handoff] WARNING: kitchen boards present but order rows unparsed — "
              "ledger may be incomplete", file=sys.stderr)

    orders_json = []
    state_dir = Path.home() / ".claude/state"
    oj = state_dir / f"orders-{session_id}.json"
    # Resolve short-id ledgers too: a helper may key by the 8-hex prefix while the
    # generator gets the full UUID (or vice-versa). Match on the shared prefix.
    if not oj.exists() and len(session_id) >= 8:
        cands = sorted(state_dir.glob(f"orders-{session_id[:8]}*.json"))
        if cands:
            oj = cands[0]
    try:
        if oj.exists():
            data = json.loads(oj.read_text())
            orders_json = data if isinstance(data, list) else (data.get("orders") or [])
    except Exception:
        orders_json = []

    for o in board_orders:
        o["detail"], o["detail_src"] = order_detail(o, user_texts_light, orders_json)

    # The unprocessed tail: genuine last user turn, kept (nearly) verbatim.
    last_msg = raw_last_user or best_task or (user_texts[-1] if user_texts else None)

    # ── Build document ───────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # YAML-style frontmatter: stamped for the autocontinue daemon so it knows
    # which project folder to cd into without any path guessing.
    project_name = Path(workdir).name if workdir else ""
    frontmatter = (
        f"---\ncwd: {workdir or ''}\nproject: {project_name}\ntimestamp: {ts}\n---\n"
    )
    # Clickable title: markdown link opens the file; bare backtick ID double-clicks
    # to copy into a new session.
    if hf_id:
        link = f"file://{Path.home()}/.claude/state/handoff-{hf_id}.md"
        title = f"# Handoff — [{hf_id}]({link})  ·  `{hf_id}`"
    else:
        title = f"# Handoff — {ts}"
    parts = [frontmatter, title, f"_session: {session_id[:16]}_", ""]

    # ════ TIER 0 — BOOT BLOCK (always read; everything needed for first action) ════
    parts.append("## ⚡ Boot  (read this first — enough to act)")
    if lead:
        parts.append(f"**Task:** {lead}")
    if resume:
        parts.append(f"**Resume:** {resume}")
    # one-line repo state
    state_bits = []
    if workdir: state_bits.append(f"`{workdir}`")
    if branch:  state_bits.append(f"branch `{branch}`")
    if dirty is not None:
        state_bits.append("**uncommitted edits present**" if dirty else "clean tree")
    if ahead_behind: state_bits.append(ahead_behind)
    if state_bits:
        parts.append(f"**Repo:** {' · '.join(state_bits)}")
    if last_commit:
        parts.append(f"**Last commit:** `{last_commit}`")
    if verify_cmd:
        parts.append(f"**Verify:** `{verify_cmd}`")
    if ro:
        parts.append("**Don't re-try (already ruled out):**")
        for r in ro:
            parts.append(f"  - {r}")
    parts.append("")

    # ── Order Ledger (mandatory, complete) + the unprocessed tail: the two
    #    sections that must NEVER be lean. Kept ABOVE the fold. ──
    if board_orders:
        parts.append("## 📋 Order Ledger — COMPLETE  (every tracked order; nothing dropped)")
        for o in board_orders:
            em = o["emoji"] or "•"
            st = f"  _{o['status']}_" if o["status"] else ""
            parts.append(f"- {em} **ORDER {o['num']}** — {o['label']}{st}")
            if o.get("detail"):
                parts.append(f"    - {o['detail']}")
            else:
                parts.append("    - ⚠️ no detail captured — recover from the original "
                             "ask in the transcript")
        parts.append("")
    elif board_seen:
        parts.append("## ⚠️ ORDER LEDGER — PARSE FAILED")
        parts.append("CASTER KITCHEN boards were shown this session but their rows could "
                     "not be parsed. **Do not trust this as complete** — recover the open "
                     "orders from the transcript before resuming.")
        parts.append("")

    if last_msg:
        lm = last_msg.strip()
        if len(lm) > 1200:
            lm = lm[:1200] + " …[truncated — see transcript]"
        parts.append("## 📨 Last message (not yet processed)")
        for ln in (lm.splitlines() or [lm]):
            parts.append(f"> {ln}")
        parts.append("")

    parts.append("> The Boot + Order Ledger + Last message above are the marching "
                 "orders. Everything below the line is reference context (state, "
                 "decisions, files) and may already be done.")
    parts.append("")
    parts.append("---")  # fold: reference context below
    parts.append("")

    # ════ TIER 1 — BODY (read only if the boot block points you here) ════
    if left_off:
        parts += ["## Where We Left Off", left_off, ""]

    # Recent Exchange — verbatim tail of the conversation. Makes a resume feel
    # continuous: the next session reads the actual end of the dialogue.
    if recent_exchange:
        parts.append("## Recent Exchange  (verbatim tail — what was just said)")
        for who, body in recent_exchange:
            parts.append(f"**{who}:**")
            for ln in body.splitlines():
                parts.append(f"> {ln}")
            parts.append("")

    if dec:
        parts.append("## Decisions  (don't silently undo)")
        for d in dec:
            parts.append(f"- {d}")
        parts.append("")

    if touched_symbols:
        parts.append("## Symbols Touched")
        parts.append(", ".join(f"`{s}`" for s in touched_symbols[:10]))
        parts.append("")

    if modified_files:
        parts.append("## Files Modified")
        for fp, action in list(modified_files.items())[:12]:
            parts.append(f"- `{fp}`  ({action})")
        parts.append("")

    if accomplishments:
        seen_acc, acc_lines = set(), []
        for a in accomplishments[:5]:
            k = a.strip().lower()[:40]
            if k not in seen_acc:
                seen_acc.add(k); acc_lines.append(f"- {a.strip()}")
        if acc_lines:
            parts.append("## Accomplished This Session")
            parts += acc_lines + [""]

    if deduped_todos:
        parts.append("## Mentioned Mid-Session (may already be done)")
        for t in deduped_todos[:5]:
            parts.append(f"- {t}")
        parts.append("")

    # ════ TIER 2 — POINTERS (never inline bulky bodies; fetch on demand) ════
    pointer_lines = []
    if bash_significant:
        pointer_lines.append(f"- Significant cmds run: {len(bash_significant)} "
                             f"(latest: `{bash_significant[-1][:70]}`)")
    if last_errors:
        # Pointer, not the 300-char body: 90% of resumes never need it.
        first_line = last_errors[-1].splitlines()[0][:90] if last_errors[-1] else ""
        pointer_lines.append(f"- Last error (re-run to reproduce if relevant): {first_line}")
    if pointer_lines:
        parts.append("## Pointers  (fetch only if needed)")
        parts += pointer_lines + [""]

    has_content = bool(
        board_orders or board_seen or orders_json or last_msg or
        modified_files or bash_significant or
        (assistant_texts and any(len(t) > 50 for t in assistant_texts))
    )
    return "\n".join(parts) if has_content else ""


def auto_detect_transcript():
    """Find most recent transcript when env vars are empty (VSCode extension mode)."""
    projects_dir = Path.home() / ".claude/projects"
    candidates = []
    for p in projects_dir.rglob("*.jsonl"):
        try: candidates.append((p.stat().st_mtime, p))
        except: pass
    if not candidates:
        return None, None
    _, path = max(candidates)
    return str(path), path.stem


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] and Path(sys.argv[1]).exists():
        transcript_path, session_id = sys.argv[1], sys.argv[2]
    else:
        transcript_path, session_id = auto_detect_transcript()
        if not transcript_path:
            print("generate-handoff: no transcript found", file=sys.stderr)
            sys.exit(0)
        print(f"[handoff] auto-detected transcript: {transcript_path}", file=sys.stderr)

    # Use caller-supplied workspace key (CWD-derived, set by handoff-writer.sh)
    # so two VSCode windows in different dirs get separate handoff files. This key
    # IS the handoff ID — pass it into generate() for the clickable title link.
    workspace_key = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else Path(transcript_path).parent.name
    doc = generate(transcript_path, session_id, workspace_key)
    if not doc:
        print("[handoff] no signal found — skipping write", file=sys.stderr)
        sys.exit(0)

    out = Path.home() / f".claude/state/handoff-{workspace_key}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc)
    print(f"[handoff] written to {out}", file=sys.stderr)
