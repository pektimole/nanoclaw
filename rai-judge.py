#!/usr/bin/env python3
"""
rai-judge.py — RAI Value-Prop Judge
OL-338 | 2026-06-21

Final gate between KEEP (approved) candidates and the threat library.
Scores each approved signal on three axes, then decides promote/hold/discard.

Every signal that enters the threat library should have narrative weight for RAI,
not just be a valid AI-era threat. This script enforces that distinction.

Scoring axes:
  tl_strength    1-5  deepens an existing TL entry (more evidence, sharper mechanism)
  narrative_fit  1-5  maps to a deck beat (Beat 1 threat-exists, Beat 2 specific-class)
  new_angle      1-5  suggests a positioning gap RAI doesn't cover yet

Action (computed in Python, not by model):
  promote  tl_strength >= 4 AND narrative_fit >= 3
           OR new_angle >= 4 (gap candidate, promote to document)
  hold     any score >= 3 (useful context, not yet library-ready)
  discard  all scores <= 2 (real threat, zero narrative value for RAI right now)

Model tier:
  Haiku  bulk scoring (cheap, fast)
  Sonnet auto-escalation when Haiku returns new_angle >= 4

Usage:
  python3 rai-judge.py --judge           # score all approved signals missing judge_score
  python3 rai-judge.py --judge --force   # re-score all approved signals
  python3 rai-judge.py --status          # ranked table of judged signals
  python3 rai-judge.py --promote-list    # print promote candidates for review
"""

import os, sys, json, re, time, sqlite3, datetime, argparse
import urllib.request

BASE_DIR      = "/home/tim/nanoclaw"
NO5_DIR       = "/home/tim/no5-context"
DB_PATH       = f"{BASE_DIR}/data/rai-poc2.db"
LOG_PATH      = f"{BASE_DIR}/logs/rai-judge.log"
TL_JSONL_PATH = f"{NO5_DIR}/logs/rai-threat-library.jsonl"

HAIKU_MODEL   = "claude-haiku-4-5-20251001"
SONNET_MODEL  = "claude-sonnet-4-6"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"

BATCH_SIZE    = 1   # one signal per call (scores need per-signal grounding)
ESCALATE_THRESHOLD = 4   # new_angle >= this → re-run with Sonnet

# ── TL candidates not yet in library (hardcoded until next promote pass) ──────
TL_CANDIDATES = [
    {
        "id": "TL-011",
        "name": "Content authenticity doubt — audio/music media",
        "description": "Synthetic music and AI vocal tracks circulate without disclosure. "
                       "Users cannot tell if a track, voice, or band is human-created. "
                       "Completes the media-authenticity triad: TL-005 (image), TL-006 (video), TL-011 (audio).",
    },
    {
        "id": "TL-012",
        "name": "AI-laundered authority / epistemic weaponization",
        "description": "AI chat output cited as authoritative evidence in disputes. "
                       "Same question yields contradictory AI answers; both sides cite their screenshot as proof. "
                       "Expert knowledge displaced by chat screenshot. "
                       "Distinct from TL-001: the AI does not deceive the victim directly — "
                       "humans weaponize AI inconsistency against each other.",
    },
]

# ── Deck beats (RAI pitch deck v2) ────────────────────────────────────────────
DECK_BEATS = """
Beat 1 (The Hook): AI is going local, but someone still controls the router. The AI era
creates new consumer threats that no existing product covers. Target audience: GenZ/Telegram
communities who already distrust big tech. Signal that lands here: vivid first-person harm
stories that make the threat feel real and imminent.

Beat 2 (Specific Threat Classes): Concrete threat taxonomy with evidence. Each class needs
at least one sharp victim anecdote as proof. Current Beat 2 classes: TL-001 confident
deception, TL-004 gaslighting, TL-005/006 image+video authenticity, TL-002 voice
impersonation, VCCE vendor covert capability expansion, confused-deputy agent action,
context-provenance poisoning, hybrid-inference router (economic conflict). Signal that
lands here: clean mechanism + clear victim experience + quantifiable harm.

Beat 3 (RAI Positioning): RAI is the only layer on the user's side of the boundary.
No economic stake in routing decisions. Constitution Rule 4: audit-without-profit.
Signal that lands here: evidence of vendor economic conflict or consent failure.

Beat 4 (How It Works): P0 local regex + P1 Claude scan + P2 multi-agent consensus.
Zero data leaves the device. Free/Pro/Premium tier gating. Not directly a signal target.

Beat 5 (Traction): POC community experiments, POC #1-#4. Signal that lands here:
real user adoption stories, community validation moments.
"""


# ── Env ───────────────────────────────────────────────────────────────────────
def load_env(path=f"{BASE_DIR}/.env"):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

load_env()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg):
    ts   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── LLM ───────────────────────────────────────────────────────────────────────
def call_llm(model, system, user, max_tokens=800):
    if not ANTHROPIC_KEY:
        log("ANTHROPIC_API_KEY missing")
        return None
    body = json.dumps({
        "model": model, "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}]
    }).encode()
    req = urllib.request.Request(ANTHROPIC_API, data=body, headers={
        "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read().decode("utf-8", errors="replace"))
        return resp["content"][0]["text"].strip()
    except Exception as e:
        log(f"LLM error: {e}")
        return None


def parse_json(raw):
    if not raw:
        return None
    raw = re.sub(r"^```json\s*", "", raw.strip())
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    for opener, closer in (("[", "]"), ("{", "}")):
        s = raw.find(opener)
        if s != -1:
            depth = 0
            for i in range(s, len(raw)):
                if raw[i] == opener:
                    depth += 1
                elif raw[i] == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(raw[s:i+1])
                        except Exception:
                            break
    try:
        return json.loads(raw)
    except Exception as e:
        log(f"JSON parse fail: {e} | {raw[:160]}")
        return None


# ── DB migration ──────────────────────────────────────────────────────────────
def migrate_db(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(candidates)").fetchall()}
    if "judge_score" not in cols:
        conn.execute("ALTER TABLE candidates ADD COLUMN judge_score TEXT")
        log("Migration: added judge_score column to candidates")
    if "judge_action" not in cols:
        conn.execute("ALTER TABLE candidates ADD COLUMN judge_action TEXT")
        log("Migration: added judge_action column to candidates")
    conn.commit()


# ── Load TL context ───────────────────────────────────────────────────────────
def load_tl_context():
    entries = []
    if os.path.exists(TL_JSONL_PATH):
        with open(TL_JSONL_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
    entries.extend(TL_CANDIDATES)
    lines = []
    for e in entries:
        eid   = e.get("id", "?")
        name  = e.get("name", "?")
        desc  = e.get("description", "")
        beat  = e.get("deck_beat", "")
        lines.append(f"{eid}: {name} [{beat}]\n  {desc[:200]}")
    return "\n\n".join(lines)


# ── Compute action from scores ────────────────────────────────────────────────
def compute_action(tl_strength, narrative_fit, new_angle):
    if new_angle >= ESCALATE_THRESHOLD:
        return "promote"
    if tl_strength >= 4 and narrative_fit >= 3:
        return "promote"
    if tl_strength >= 3 or narrative_fit >= 3:
        return "hold"
    return "discard"


# ── Judge system prompt ───────────────────────────────────────────────────────
def build_judge_system(tl_context):
    return f"""You are the RAI value-prop judge. Your job is NOT to decide whether a signal
describes a real AI-era threat (that was decided in triage). Your job is to decide whether
this signal has narrative weight for RAI's threat library and pitch deck.

Score this signal on three axes. Return JSON only, no prose.

SCORING AXES:
- tl_strength (1-5): Does this signal deepen an existing TL entry?
  1=noise/duplicate, 2=weak instance, 3=good supporting instance,
  4=strong distinctive instance, 5=definitive/definitive mechanism example

- narrative_fit (1-5): Does this signal map to a RAI deck beat?
  1=irrelevant, 2=tangential, 3=supports a beat, 4=strengthens a beat,
  5=perfect illustration for the pitch

- new_angle (1-5): Does this signal reveal a positioning gap?
  1=fully covered by existing TL, 2=slight twist, 3=partial gap,
  4=clear gap RAI could own, 5=new TL candidate

OUTPUT FORMAT (JSON only):
{{
  "tl_strength": <1-5>,
  "narrative_fit": <1-5>,
  "new_angle": <1-5>,
  "primary_tl": "<TL-00X or null>",
  "deck_beat": "<Beat 1|Beat 2|Beat 3|null>",
  "note": "<one sentence: what makes this signal valuable or not>"
}}

CURRENT THREAT LIBRARY (TL-001..TL-012 candidates):
{tl_context}

RAI DECK BEATS:
{DECK_BEATS}

IMPORTANT:
- Be strict. Most signals score 2-3 on tl_strength. Reserve 4-5 for signals that
  add something the existing TL entry's mechanism description doesn't already cover.
- A signal can have high tl_strength (strong evidence) but low new_angle (fully covered).
- new_angle >= 4 means you're identifying something the library should add, not just
  more instances of something already in it.
- No em-dashes in your note."""


# ── Run judge ─────────────────────────────────────────────────────────────────
def run_judge(force=False):
    conn = sqlite3.connect(DB_PATH)
    migrate_db(conn)

    tl_context  = load_tl_context()
    system      = build_judge_system(tl_context)

    where = "status='approved'" if force else "status='approved' AND judge_score IS NULL"
    rows  = conn.execute(
        f"SELECT id, source, title, body, url, haiku_pattern, haiku_reasoning "
        f"FROM candidates WHERE {where} ORDER BY post_score DESC"
    ).fetchall()

    if not rows:
        log("No approved candidates to judge.")
        conn.close()
        return

    log(f"Judging {len(rows)} approved signals (force={force})...")
    promoted = held = discarded = escalated = errors = 0

    for cid, source, title, body, url, pattern, reasoning in rows:
        src_label  = source or "?"
        title_str  = (title or "")[:100]
        body_str   = (body  or "")[:400]

        user = (
            f"SOURCE: {src_label}\n"
            f"TITLE: {title_str}\n"
            f"BODY: {body_str}\n"
            f"HAIKU PATTERN: {pattern or 'n/a'}\n"
            f"HAIKU REASONING: {reasoning or 'n/a'}\n"
            f"URL: {url or 'n/a'}"
        )

        raw    = call_llm(HAIKU_MODEL, system, user, max_tokens=400)
        result = parse_json(raw)

        if not result:
            log(f"  {cid[:12]} ({title_str[:40]}): parse fail, skipping")
            errors += 1
            time.sleep(0.3)
            continue

        tl_s = int(result.get("tl_strength", 1))
        nf   = int(result.get("narrative_fit", 1))
        na   = int(result.get("new_angle", 1))

        # Escalate to Sonnet if Haiku sees a potential new angle
        if na >= ESCALATE_THRESHOLD:
            log(f"  {cid[:12]}: Haiku new_angle={na} >= {ESCALATE_THRESHOLD}, escalating to Sonnet...")
            raw2    = call_llm(SONNET_MODEL, system, user, max_tokens=600)
            result2 = parse_json(raw2)
            if result2:
                result = result2
                tl_s   = int(result.get("tl_strength", tl_s))
                nf     = int(result.get("narrative_fit", nf))
                na     = int(result.get("new_angle", na))
                escalated += 1

        action = compute_action(tl_s, nf, na)
        score_json = json.dumps({
            "tl_strength":   tl_s,
            "narrative_fit": nf,
            "new_angle":     na,
            "primary_tl":    result.get("primary_tl"),
            "deck_beat":     result.get("deck_beat"),
            "note":          result.get("note", ""),
            "model":         SONNET_MODEL if na >= ESCALATE_THRESHOLD else HAIKU_MODEL,
        })

        conn.execute(
            "UPDATE candidates SET judge_score=?, judge_action=? WHERE id=?",
            (score_json, action, cid)
        )
        conn.commit()

        if action == "promote":
            promoted += 1
        elif action == "hold":
            held += 1
        else:
            discarded += 1

        note = result.get("note", "")[:60]
        log(f"  {cid[:12]} tl={tl_s} nf={nf} na={na} → {action:8} | {note}")
        time.sleep(0.2)

    conn.close()
    log(f"Judge done: promote={promoted} hold={held} discard={discarded} escalated={escalated} errors={errors}")


# ── Status table ──────────────────────────────────────────────────────────────
def run_status():
    conn = sqlite3.connect(DB_PATH)
    migrate_db(conn)

    rows = conn.execute(
        "SELECT id, source, title, judge_score, judge_action "
        "FROM candidates WHERE status='approved' AND judge_score IS NOT NULL "
        "ORDER BY judge_action ASC, title ASC"
    ).fetchall()

    if not rows:
        print("No judged signals yet. Run --judge first.")
        conn.close()
        return

    promote_rows = [r for r in rows if r[4] == "promote"]
    hold_rows    = [r for r in rows if r[4] == "hold"]
    discard_rows = [r for r in rows if r[4] == "discard"]

    pending_judge = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE status='approved' AND judge_score IS NULL"
    ).fetchone()[0]

    print(f"\nRAI Judge — Signal Table ({len(rows)} judged, {pending_judge} pending)")
    print(f"PROMOTE: {len(promote_rows)}  HOLD: {len(hold_rows)}  DISCARD: {len(discard_rows)}\n")

    for label, group in [("PROMOTE", promote_rows), ("HOLD", hold_rows), ("DISCARD", discard_rows)]:
        if not group:
            continue
        print(f"── {label} ({len(group)}) ──────────────────────────────────")
        for cid, src, title, score_json, action in group:
            score = json.loads(score_json) if score_json else {}
            tl_s  = score.get("tl_strength", "?")
            nf    = score.get("narrative_fit", "?")
            na    = score.get("new_angle", "?")
            tl    = score.get("primary_tl") or "-"
            beat  = score.get("deck_beat") or "-"
            note  = score.get("note", "")[:60]
            print(f"  {cid[:10]} [{src:6}] tl={tl_s} nf={nf} na={na} {tl:7} {beat:8}  {(title or '')[:45]}")
            if note:
                print(f"             {note}")
        print()

    conn.close()


# ── Promote list (for review before promote_patterns.py pass) ─────────────────
def run_promote_list():
    conn = sqlite3.connect(DB_PATH)
    migrate_db(conn)

    rows = conn.execute(
        "SELECT id, source, url, title, judge_score "
        "FROM candidates WHERE status='approved' AND judge_action='promote' "
        "ORDER BY title ASC"
    ).fetchall()

    if not rows:
        print("No promote candidates.")
        conn.close()
        return

    print(f"\nRAI Judge — Promote Candidates ({len(rows)} signals)\n")
    for cid, src, url, title, score_json in rows:
        score = json.loads(score_json) if score_json else {}
        tl    = score.get("primary_tl") or "new"
        beat  = score.get("deck_beat") or "?"
        na    = score.get("new_angle", "?")
        note  = score.get("note", "")
        print(f"[{tl}] {(title or '')[:70]}")
        print(f"  tl={score.get('tl_strength')} nf={score.get('narrative_fit')} na={na} {beat}")
        print(f"  {note}")
        print(f"  {url or 'n/a'}")
        print()

    conn.close()


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="RAI Value-Prop Judge")
    ap.add_argument("--judge",        action="store_true", help="score approved signals missing judge_score")
    ap.add_argument("--force",        action="store_true", help="re-score all approved signals")
    ap.add_argument("--status",       action="store_true", help="show judged signal table")
    ap.add_argument("--promote-list", action="store_true", help="print promote candidates for review")
    args = ap.parse_args()

    if args.judge or args.force:
        run_judge(force=args.force)
    elif args.status:
        run_status()
    elif args.promote_list:
        run_promote_list()
    else:
        ap.print_help()
