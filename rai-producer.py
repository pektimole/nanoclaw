#!/usr/bin/env python3
"""
rai-producer.py — RAI Threat-Research PRODUCE stage
OL-325 | 2026-05-30

Turns collected victim-signal candidates (from rai-poc2.py + rai-reddit-research.py)
into NOVEL pattern cards. Permanent threat-research pipeline, not POC scaffolding:
the patterns it produces feed the same threat library that live RAI scans will use.

Pipeline:
  B  cluster    — group candidate instances into pattern classes (>=2 sources to promote)
  A  novelty    — tag each pattern novel / variant / known vs existing RAI taxonomy
  C  two-axis   — manipulation_type (VS namespace) + rai_layer (canonical L-2..L3)
  D  synthesize — Sonnet writes a deck-ready pattern card per novel pattern

Usage:
  python3 rai-producer.py --cluster      # B: group pending candidates into patterns
  python3 rai-producer.py --enrich       # A+C+D: novelty + layer + card per pattern
  python3 rai-producer.py --all          # cluster + enrich in sequence
  python3 rai-producer.py --status       # show pattern table
"""

import os, sys, json, re, time, sqlite3, datetime, argparse
import urllib.request

BASE_DIR  = "/home/tim/nanoclaw"
NO5_DIR   = "/home/tim/no5-context"
DB_PATH   = f"{BASE_DIR}/data/rai-poc2.db"
LOG_PATH  = f"{BASE_DIR}/logs/rai-producer.log"
LIBRARY_JSONL = f"{NO5_DIR}/logs/rai-threat-library.jsonl"  # OL-412 dedup source

SONNET_MODEL  = "claude-sonnet-4-6"
HAIKU_MODEL   = "claude-haiku-4-5-20251001"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"

MIN_INSTANCES_TO_PROMOTE = 2   # a pattern needs >=2 independent sources to be credible

# OL-412 promote gate (criterion locked 2026-06-30 vs rai-1010-what-rai-is.md)
VALID_COVERAGE = {"scan", "verify", "gate", "orch"}
COVERAGE_DEFAULTS = {
    "VS6": "orch", "VS5": "orch", "VS0": "verify",
    "VS3": "scan", "VS1": "scan", "VS4": "scan", "VS2": "scan",
    "L0": "gate", "L-2": "scan", "L-1": "scan", "L1": "orch",
    "unmapped-consumer-harm": "scan",
}

# ── Canonical RAI taxonomy (novelty baseline) ─────────────────────────────────
# Source: rai/docs/19-rai-context.md "Canonical Threat Layer Schema".
# These describe AI-STACK-directed threats. Victim signals (human-directed
# AI-enabled deception) are mostly NOT covered here — that gap IS the POC finding.
TAXONOMY_REFERENCE = """
RAI CANONICAL THREAT LAYERS (existing taxonomy — threats to the AI stack):
- L-2 Infrastructure/supply chain: compromised tool, MCP server, upstream context file, credential exfil
- L-1 Model poisoning/drift: engineered content shifting agent behavior over time, persona replacement, role override
- L0  Prompt injection: direct instruction override, jailbreak, system-prompt leak; also unintentional PII/credential exposure
- L1  AI-provenance: non-human-generated content; downstream misinformation propagation, source pollution
- L2  Cascade risk: passed clean in isolation, triggers on cross-pipeline context
- L3  Systemic harm: coordinated multi-message pattern, compound injection across sessions

KNOWN REAL-WORLD CASES already in the deck:
- Marketplace Trap (L-2): malicious skill on a marketplace
- One-Click Takeover (L0): hidden instruction triggers action
- Confident Wrong Answer (L1): AI states false fact with high confidence
- Hidden Instruction (L0): injected directive in content

KEY: this taxonomy protects the AI STACK. It has NO clean home for HUMAN-DIRECTED
AI-enabled deception (voice clones impersonating family, AI gaslighting a person,
synthetic personas deceiving a community, fake-friend messages). Patterns of that
class are NOVEL relative to this taxonomy — map them rai_layer='unmapped-consumer-harm'.
"""

# ── Victim-signal manipulation taxonomy (VS namespace — separate axis) ─────────
VS_TAXONOMY = """
manipulation_type uses the VS namespace (the victim's EXPERIENCE, distinct axis
from rai_layer which is RAI's structural threat layer). DO NOT use L0-L5 here:
- VS0 impersonation: AI/account posing as a specific known person or entity
- VS1 behavioral-nudge: dark pattern, subtle steering, manufactured engagement
- VS2 false-urgency: manufactured scarcity, time pressure, FOMO
- VS3 gaslighting: AI denies reality, manipulates user's confidence in facts
- VS4 reality-distortion: persistent, destabilizing identity/reality erosion
- VS5 synthetic-persona: voice clone, deepfake, AI-generated human impersonation
- VS6 authenticity-doubt: victim cannot tell if shared content/news is real or AI-made
"""

# ── Env ───────────────────────────────────────────────────────────────────────
def load_env(path=f"{BASE_DIR}/.env"):
    if not os.path.exists(path): return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
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
        with open(LOG_PATH, "a") as f: f.write(line + "\n")
    except Exception: pass

# ── LLM ───────────────────────────────────────────────────────────────────────
def call_llm(model, system, user, max_tokens=2000):
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
    if not raw: return None
    raw = re.sub(r"^```json\s*", "", raw.strip())
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    # Grab first balanced [...] or {...}
    for opener, closer in (("[", "]"), ("{", "}")):
        s = raw.find(opener)
        if s != -1:
            depth = 0
            for i in range(s, len(raw)):
                if raw[i] == opener: depth += 1
                elif raw[i] == closer:
                    depth -= 1
                    if depth == 0:
                        try: return json.loads(raw[s:i+1])
                        except Exception: break
    try: return json.loads(raw)
    except Exception as e:
        log(f"JSON parse fail: {e} | {raw[:160]}")
        return None

# ── DB ────────────────────────────────────────────────────────────────────────
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidates (
            id TEXT PRIMARY KEY, source TEXT, post_id TEXT, subreddit TEXT,
            title TEXT, body TEXT, url TEXT, post_score INTEGER,
            created_at REAL, fetched_at REAL, haiku_verdict TEXT,
            haiku_reasoning TEXT, haiku_pattern TEXT, haiku_l_class TEXT,
            status TEXT DEFAULT 'pending', reviewed_at REAL, promoted_at REAL, l_class TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS patterns (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            name              TEXT,
            mechanism         TEXT,
            manipulation_type TEXT,   -- VS namespace
            rai_layer         TEXT,   -- canonical L-2..L3 or 'unmapped-consumer-harm'
            novelty           TEXT,   -- novel / variant / known
            novelty_rationale TEXT,
            instance_ids      TEXT,   -- JSON array of candidate ids
            instance_count    INTEGER,
            card              TEXT,    -- synthesized deck-ready markdown card
            status            TEXT DEFAULT 'candidate',  -- candidate/approved/rejected
            created_at        REAL,
            reviewed_at       REAL,
            enriched          INTEGER DEFAULT 0,
            coverage          TEXT,
            dup_of            TEXT
        )""")
    try:
        conn.execute("ALTER TABLE patterns ADD COLUMN coverage TEXT")  # OL-412 migration for live DB
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE patterns ADD COLUMN dup_of TEXT")
    except Exception:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS threat_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT, pattern_name TEXT,
            description TEXT, l_class TEXT, source_ids TEXT, promoted_at REAL
        )""")
    conn.commit()
    return conn

# ── B: Cluster ────────────────────────────────────────────────────────────────
CLUSTER_SYSTEM = (
    "You are RAI's threat-research analyst. You receive a list of victim-signal "
    "candidates (real social posts where someone experienced AI manipulation or "
    "AI-enabled deception). Group them into distinct PATTERN CLASSES.\n\n"
    "A pattern is a recurring class of manipulation, NOT a single instance. "
    "Posts describing the same underlying mechanism belong to one pattern even if "
    "the surface details differ (e.g. 'voice clone of my mom' + 'AI call faking my "
    "boss' = one pattern: synthetic-voice impersonation of a trusted person).\n\n"
    + VS_TAXONOMY +
    "\nReturn JSON array, one object per pattern:\n"
    "[{\"name\":\"short pattern name\",\"mechanism\":\"how the manipulation works, 1-2 sentences\","
    "\"manipulation_type\":\"VS3\",\"instance_ids\":[\"id1\",\"id2\"]}]\n"
    "Every candidate id must land in exactly one pattern. Singletons are allowed "
    "(a pattern with one instance) but keep them separate, do not force-merge."
)

def run_cluster():
    conn = init_db()
    # Rebuild only UN-reviewed patterns each run so same-class signals arriving in
    # different crawls group together. Approved/rejected patterns persist (their
    # candidate instances are already promoted/rejected, so out of the pending pool).
    conn.execute("DELETE FROM patterns WHERE status='candidate'")
    conn.commit()
    rows = conn.execute(
        "SELECT id, haiku_pattern, haiku_reasoning, title, subreddit "
        "FROM candidates WHERE status='pending'"
    ).fetchall()
    if not rows:
        log("No pending candidates to cluster.")
        conn.close(); return
    # Build compact input
    items = []
    for cid, pat, reason, title, sub in rows:
        src = f"r/{sub}" if sub else "HN"
        items.append(f"id={cid} [{src}] pattern: {pat} | reason: {reason} | title: {(title or '')[:80]}")
    user = "Candidates to cluster:\n\n" + "\n".join(items)
    log(f"Clustering {len(rows)} candidates via Sonnet...")
    raw = call_llm(SONNET_MODEL, CLUSTER_SYSTEM, user, max_tokens=8000)
    clusters = parse_json(raw)
    if not clusters or not isinstance(clusters, list):
        log("Clustering returned no usable result.")
        conn.close(); return
    valid_ids = {r[0] for r in rows}
    created = 0
    for cl in clusters:
        ids = [i for i in cl.get("instance_ids", []) if i in valid_ids]
        if not ids: continue
        conn.execute(
            "INSERT INTO patterns(name,mechanism,manipulation_type,instance_ids,instance_count,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (cl.get("name","?"), cl.get("mechanism",""), cl.get("manipulation_type",""),
             json.dumps(ids), len(ids), time.time())
        )
        created += 1
        log(f"  Pattern: {cl.get('name','?')} ({len(ids)} instances, {cl.get('manipulation_type','?')})")
    conn.commit()
    log(f"Cluster done: {created} patterns created")
    conn.close()

# ── A+C+D: Enrich (novelty + layer + card) ────────────────────────────────────
ENRICH_SYSTEM = (
    "You are RAI's threat-research analyst. For ONE victim-signal pattern, do these:\n\n"
    "0. AI_MEDIATED: is a person deceived/manipulated because something in an AI-saturated "
    "interaction (voice, content, persona, agent action, chatbot output) is not what it appears? "
    "true/false. Old-world baseline with NO AI-specific mechanism (password/account hijack, non-AI "
    "phone scam) or a pure quality complaint ('the advice was bad') = false.\n\n"
    "0b. DUP_OF: if this pattern is a near-duplicate of an entry in EXISTING LIBRARY (in the user "
    "message), return its TL-id (e.g. 'TL-003'); else null. A DIFFERENT modality (audio/voice vs "
    "image vs video vs text vs agent-action) is a DISTINCT mechanism, not a duplicate — audio/music "
    "authenticity is NOT a dup of image authenticity.\n\n"
    "0c. COVERAGE: how RAI acts on this today — scan (P0/P1 detection signal), verify (grounded "
    "consensus), gate (deterministic ActionGate enforcement), orch (orchestrate a specialist "
    "detector). Pick exactly one. This is honesty about delivery, never a quality verdict.\n\n"
    "1. NOVELTY: compare against RAI's existing taxonomy below. Is this pattern "
    "'novel' (no clean home in the taxonomy), 'variant' (a twist on a known layer), "
    "or 'known' (already fully covered)? Give a one-sentence rationale.\n\n"
    "2. RAI_LAYER: map to the closest canonical layer (L-2/L-1/L0/L1/L2/L3), OR if it "
    "is human-directed AI-enabled deception with no clean home, use 'unmapped-consumer-harm'.\n\n"
    "3. CARD: write a deck-ready pattern card (4-6 lines markdown): bold pattern name, "
    "one-line mechanism, the victim experience, why it matters for a consumer beachhead, "
    "instance count. No hype, no em-dashes.\n\n"
    + TAXONOMY_REFERENCE +
    "\nReturn JSON: {\"ai_mediated\":true,\"dup_of\":null,\"coverage\":\"scan|verify|gate|orch\","
    "\"novelty\":\"novel|variant|known\",\"novelty_rationale\":\"...\","
    "\"rai_layer\":\"L1|unmapped-consumer-harm|...\",\"card\":\"markdown card\"}"
)

def load_library_summary():
    import os, json
    if not os.path.exists(LIBRARY_JSONL):
        return "(empty)"
    out = []
    with open(LIBRARY_JSONL) as f:
        for line in f:
            try:
                e = json.loads(line)
                out.append(f"{e.get('id')}: {e.get('name')} [{e.get('l_class')}]")
            except Exception:
                pass
    return "\n".join(out) if out else "(empty)"

def run_enrich(dry_run=False):
    conn = init_db()
    library = load_library_summary()
    where = "status='candidate'" if dry_run else "enriched=0 AND status='candidate'"
    pats = conn.execute(
        "SELECT id, name, mechanism, manipulation_type, instance_ids, instance_count "
        f"FROM patterns WHERE {where}"
    ).fetchall()
    if not pats:
        log("No patterns to enrich.")
        conn.close(); return
    log(f"Enriching {len(pats)} patterns...")
    for pid, name, mech, vtype, ids_json, count in pats:
        ids = json.loads(ids_json or "[]")
        # Pull instance excerpts for grounding
        excerpts = []
        for cid in ids[:5]:
            row = conn.execute("SELECT subreddit, title, body, url FROM candidates WHERE id=?", (cid,)).fetchone()
            if row:
                sub, title, body, url = row
                src = f"r/{sub}" if sub else "HN"
                excerpts.append(f"[{src}] {(title or '')[:80]}: {(body or '')[:200]}")
        user = (
            f"EXISTING LIBRARY (reject as dup if this pattern near-duplicates any):\n{library}\n\n"
            f"PATTERN: {name}\nMECHANISM: {mech}\nmanipulation_type: {vtype}\n"
            f"INSTANCES ({count}):\n" + "\n".join(excerpts)
        )
        raw = call_llm(SONNET_MODEL, ENRICH_SYSTEM, user, max_tokens=1200)
        result = parse_json(raw)
        if not result:
            log(f"  Pattern {pid} ({name}): enrich failed, skipping")
            continue
        # OL-412 gate
        ai_med = result.get("ai_mediated")
        dup_of = result.get("dup_of")
        if isinstance(dup_of, str) and dup_of.strip().lower() in ("", "null", "none"):
            dup_of = None
        coverage = result.get("coverage") or COVERAGE_DEFAULTS.get(vtype) \
                   or COVERAGE_DEFAULTS.get(result.get("rai_layer") or "") or "scan"
        if coverage not in VALID_COVERAGE:
            coverage = "scan"
        # OL-412: auto-reject ONLY non-AI baseline; dup_of is ADVISORY (surfaced in promote CLI)
        if ai_med is False:
            new_status, gate_reason = "rejected", "non-AI-mediated baseline"
        else:
            new_status, gate_reason = "candidate", None
        rationale = (result.get("novelty_rationale") or "")
        if gate_reason:
            rationale = (rationale + f" [AUTO-GATE: {gate_reason}]").strip()
        nov = result.get("novelty","?")
        if new_status == "rejected":
            tag = f"REJECT({gate_reason})"
        elif dup_of:
            tag = f"keep/{coverage} [DUP?->{dup_of}]"
        else:
            tag = f"keep/{coverage}"
        log(f"  Pattern {pid} ({name}): {nov} | {result.get('rai_layer','?')} | {tag}")
        if dry_run:
            time.sleep(0.3)
            continue
        conn.execute(
            "UPDATE patterns SET novelty=?, novelty_rationale=?, rai_layer=?, card=?, coverage=?, dup_of=?, status=?, enriched=1 WHERE id=?",
            (result.get("novelty"), rationale, result.get("rai_layer"),
             result.get("card"), coverage, dup_of, new_status, pid)
        )
        conn.commit()
        time.sleep(0.5)
    log("Enrich done.")
    conn.close()

# ── Status ────────────────────────────────────────────────────────────────────
def run_status():
    conn = init_db()
    rows = conn.execute(
        "SELECT id, name, manipulation_type, rai_layer, novelty, instance_count, status, enriched "
        "FROM patterns ORDER BY novelty DESC, instance_count DESC"
    ).fetchall()
    print(f"\nRAI Producer — Pattern Table ({len(rows)} patterns)")
    print(f"{'ID':>3}  {'NOV':8} {'TYPE':5} {'LAYER':22} {'#':>2}  {'STATUS':10} NAME")
    novel_promotable = 0
    for pid, name, vtype, layer, nov, count, status, enr in rows:
        nov = nov or ("..." if not enr else "?")
        layer = (layer or "?")[:22]
        promotable = (nov in ("novel","variant")) and (count or 0) >= MIN_INSTANCES_TO_PROMOTE and status == "approved"
        if nov == "novel" and (count or 0) >= MIN_INSTANCES_TO_PROMOTE:
            novel_promotable += 1
        print(f"{pid:>3}  {nov:8} {(vtype or '?'):5} {layer:22} {count or 0:>2}  {status:10} {(name or '')[:40]}")
    lib = conn.execute("SELECT COUNT(*) FROM threat_library").fetchone()[0]
    print(f"\nNovel patterns (>= {MIN_INSTANCES_TO_PROMOTE} instances): {novel_promotable}")
    print(f"Threat library (promoted): {lib}/10")
    conn.close()

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="RAI Producer — novel pattern synthesis")
    ap.add_argument("--cluster", action="store_true", help="B: group candidates into patterns")
    ap.add_argument("--enrich",  action="store_true", help="A+C+D: novelty + layer + card")
    ap.add_argument("--all",     action="store_true", help="cluster + enrich")
    ap.add_argument("--status",  action="store_true", help="show pattern table")
    ap.add_argument("--dry-run", action="store_true", help="enrich gate preview, no DB writes")
    args = ap.parse_args()
    if args.all:
        run_cluster(); run_enrich()
    elif args.cluster:
        run_cluster()
    elif args.enrich:
        run_enrich(dry_run=args.dry_run)
    elif args.status:
        run_status()
    else:
        ap.print_help()
