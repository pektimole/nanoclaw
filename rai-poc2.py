#!/usr/bin/env python3
"""
rai-poc2.py — RAI POC #2: Victim Signal Crawler
OL-325 | 2026-05-29

Usage:
  python3 rai-poc2.py --crawl           # Fetch + classify + export-json + process-reviews
  python3 rai-poc2.py --review          # Interactive CLI review
  python3 rai-poc2.py --export          # Dump threat library as markdown
  python3 rai-poc2.py --export-json     # Write signals.json to no5-context + git push
  python3 rai-poc2.py --process-reviews # Apply review queue from Cowork artifact
  python3 rai-poc2.py --status          # Show counts
"""

import os, sys, json, re, time, sqlite3, hashlib, datetime, argparse
import urllib.request, urllib.parse

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR      = "/home/tim/nanoclaw"
NO5_DIR       = "/home/tim/no5-context"
DB_PATH       = f"{BASE_DIR}/data/rai-poc2.db"
LOG_PATH      = f"{BASE_DIR}/logs/rai-poc2.log"
EXPORT_PATH   = f"{BASE_DIR}/data/rai-poc2-threat-library.md"
SIGNALS_JSON  = f"{NO5_DIR}/logs/rai-poc2-signals.json"   # read by Cowork artifact
REVIEWS_JSON  = f"{NO5_DIR}/logs/rai-poc2-reviews.json"   # written by Cowork artifact

HAIKU_MODEL   = "claude-haiku-4-5-20251001"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"

SUBREDDITS = ["ChatGPT", "AIAssistants", "Scams", "artificial", "Fraud", "mildlyinfuriating"]

# Multiple queries — grouped by signal class for coverage
HN_QUERIES = [
    # Original: AI manipulation (direct)
    "ChatGPT manipulation",
    "AI manipulated me",
    "chatgpt lied",
    "AI felt wrong",
    "manipulated by AI",
    "chatgpt gaslighting",
    "AI scam victim",
    "AI deceived",
    # Content / link authenticity doubt
    "AI generated article fake",
    "how do I know if this is real",
    "deepfake article news",
    "AI content trust",
    "can I trust this source AI",
    # Impersonation: calls, messages, synthetic personas
    "voice cloning scam",
    "AI voice fake call",
    "deepfake phone call",
    "AI impersonation message",
    "fake message from friend AI",
    "scam voice AI family",
    "received call not real person",
    # Agent overreach / cover-up (2026-07-06, OL-412 gap fix)
    "AI agent did something without asking",
    "didn't authorize AI action",
    "AI covered up mistake",
    "agent took action I didn't approve",
    "chatgpt did this without permission",
    # Undisclosed bot / institutional mimicry (2026-07-06, OL-412 gap fix)
    "didn't tell me it was a bot",
    "AI customer service pretending human",
    "undisclosed chatbot company",
    "automated response pretending to be a person",
    "AI gatekeeping support request",
]
HN_WINDOW_DAYS = 30

PREFILTER_NEWS = re.compile(
    r'\b(study shows|researchers find|according to|published in|percent of|'
    r'scientists say|new research|survey found|data shows|report finds|'
    r'according to a|a new study|in a paper)\b', re.I
)
PREFILTER_CODE = re.compile(r'```')
MIN_BODY_LEN = 50

CANDIDATE_COLS = [
    "id", "source", "post_id", "subreddit", "title", "body", "url",
    "post_score", "created_at", "fetched_at", "haiku_verdict",
    "haiku_reasoning", "haiku_pattern", "haiku_l_class", "status",
    "reviewed_at", "promoted_at", "l_class"
]

HAIKU_SYSTEM = (
    "You analyze social media posts for AI manipulation and AI-enabled deception victim signals.\n\n"
    "Classify whether this post describes a person's DIRECT PERSONAL EXPERIENCE in any of these categories:\n\n"
    "1. AI INTERACTION MANIPULATION: felt manipulated/deceived/nudged by an AI chatbot or tool\n"
    "   - Post-hoc unease ('that felt off', 'something seemed wrong')\n"
    "   - Realizing AI was gaslighting, nudging, or creating false impressions\n\n"
    "2. CONTENT/LINK AUTHENTICITY DOUBT: received or shared content and questioned whether it was real\n"
    "   - 'Is this article/post/image real?'\n"
    "   - Distrust of shared links, AI-generated text passed as human, synthetic media\n"
    "   - Wanting to verify news or social posts before trusting\n\n"
    "3. IMPERSONATION / SYNTHETIC PERSONA: received a call, message, or contact that claimed to be "
    "someone known but felt wrong or turned out to be AI/fake\n"
    "   - Voice cloning calls pretending to be family/friends\n"
    "   - Messages from hacked/cloned accounts of known people\n"
    "   - 'That didn't sound like them' — post-hoc realization\n\n"
    "NOT: academic analysis, news articles, third-person reporting, code questions, "
    "general AI quality complaints with no personal experience described.\n\n"
    "Respond with JSON only, no other text:\n"
    "{\"verdict\":\"victim_signal\",\"pattern\":\"one-line pattern description\","
    "\"l_class\":\"L1\",\"reasoning\":\"under 20 words\"}\n"
    "OR\n"
    "{\"verdict\":\"not_signal\",\"pattern\":null,\"l_class\":null,"
    "\"reasoning\":\"under 20 words\"}\n\n"
    "L-class guide:\n"
    "L0 = social engineering / impersonation of known entity\n"
    "L1 = behavioral nudge / dark pattern / subtle pressure\n"
    "L2 = false urgency / FOMO / manufactured scarcity\n"
    "L3 = confidence manipulation / gaslighting / reality denial\n"
    "L4 = identity/reality distortion (persistent, destabilizing)\n"
    "L5 = synthetic persona / voice clone / deepfake impersonation of known person"
)

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
TG_TOKEN      = os.environ.get("RAI_TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID    = os.environ.get("RAI_TELEGRAM_CHAT_ID", "")

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

# ── DB ────────────────────────────────────────────────────────────────────────
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidates (
            id              TEXT PRIMARY KEY,
            source          TEXT,
            post_id         TEXT,
            subreddit       TEXT,
            title           TEXT,
            body            TEXT,
            url             TEXT,
            post_score      INTEGER,
            created_at      REAL,
            fetched_at      REAL,
            haiku_verdict   TEXT,
            haiku_reasoning TEXT,
            haiku_pattern   TEXT,
            haiku_l_class   TEXT,
            status          TEXT DEFAULT 'pending',
            reviewed_at     REAL,
            promoted_at     REAL,
            l_class         TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS threat_library (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_name TEXT,
            description  TEXT,
            l_class      TEXT,
            source_ids   TEXT,
            promoted_at  REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn

def make_cid(source, post_id):
    return hashlib.sha256(f"{source}:{post_id}".encode()).hexdigest()[:16]

def seen(conn, cid):
    return conn.execute("SELECT 1 FROM candidates WHERE id=?", (cid,)).fetchone() is not None

def insert_candidate(conn, **kw):
    cols  = ", ".join(kw.keys())
    ph    = ", ".join("?" * len(kw))
    conn.execute(f"INSERT OR IGNORE INTO candidates ({cols}) VALUES ({ph})", list(kw.values()))
    conn.commit()

def row_to_dict(row):
    return dict(zip(CANDIDATE_COLS, row))

# ── HTTP ──────────────────────────────────────────────────────────────────────
def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        log(f"GET error {url[:80]}: {e}")
        return None

def http_post_json(url, data, headers=None, timeout=25):
    body = json.dumps(data).encode()
    h    = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        log(f"POST error {url[:80]}: {e}")
        return None

# ── Pre-filter ────────────────────────────────────────────────────────────────
def prefilter(title, body):
    text = f"{title} {body}"
    if PREFILTER_NEWS.search(text):
        return False, "news_pattern"
    if PREFILTER_CODE.search(text):
        return False, "code_content"
    if len((body or "").strip()) < MIN_BODY_LEN:
        return False, "too_short"
    return True, None

# ── Haiku ─────────────────────────────────────────────────────────────────────
def classify_haiku(title, body):
    if not ANTHROPIC_KEY:
        log("WARNING: ANTHROPIC_API_KEY not set")
        return None
    snippet = f"Title: {title}\n\nPost: {body[:1500]}"
    payload = {
        "model":      HAIKU_MODEL,
        "max_tokens": 200,
        "system":     [{"type": "text", "text": HAIKU_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        "messages":   [{"role": "user", "content": snippet}],
    }
    headers = {
        "x-api-key":         ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type":      "application/json",
    }
    resp = http_post_json(ANTHROPIC_API, payload, headers=headers)
    if not resp:
        return None
    try:
        raw = resp["content"][0]["text"].strip()
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        log(f"Haiku parse error: {e} | raw: {str(resp)[:200]}")
        return None

# ── Reddit OAuth ──────────────────────────────────────────────────────────────
_reddit_token_cache = {"token": None, "expires": 0}

def reddit_get_token():
    client_id     = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None
    if _reddit_token_cache["token"] and time.time() < _reddit_token_cache["expires"]:
        return _reddit_token_cache["token"]
    import base64
    creds   = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    payload = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req     = urllib.request.Request(
        "https://www.reddit.com/api/v1/access_token",
        data=payload,
        headers={
            "Authorization": f"Basic {creds}",
            "User-Agent":    "RAI-POC2/1.0 (research; u/raipoc2)",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        tok = data.get("access_token")
        if tok:
            _reddit_token_cache["token"]   = tok
            _reddit_token_cache["expires"] = time.time() + data.get("expires_in", 3600) - 60
        return tok
    except Exception as e:
        log(f"Reddit token error: {e}")
        return None

def reddit_api_get(path, token):
    req = urllib.request.Request(
        f"https://oauth.reddit.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent":    "RAI-POC2/1.0 (research; u/raipoc2)",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"Reddit API error {path}: {e}")
        return None

# ── Reddit ────────────────────────────────────────────────────────────────────
def crawl_reddit(conn):
    token = reddit_get_token()
    if not token:
        log(
            "Reddit SKIP — no credentials.\n"
            "  To enable: add to ~/nanoclaw/.env:\n"
            "    REDDIT_CLIENT_ID=<your_app_client_id>\n"
            "    REDDIT_CLIENT_SECRET=<your_app_secret>\n"
            "  Create app at: https://www.reddit.com/prefs/apps\n"
            "  Choose 'script' type, redirect_uri=http://localhost"
        )
        return 0
    total = 0
    for sub in SUBREDDITS:
        data = reddit_api_get(f"/r/{sub}/new.json?limit=100", token)
        if not data:
            log(f"Reddit r/{sub}: no data")
            continue
        posts = data.get("data", {}).get("children", [])
        log(f"Reddit r/{sub}: {len(posts)} posts")
        for item in posts:
            p       = item.get("data", {})
            post_id = p.get("id", "")
            if not post_id:
                continue
            cid = make_cid("reddit", post_id)
            if seen(conn, cid):
                continue
            title = p.get("title", "")
            body  = p.get("selftext", "") or ""
            ok, _ = prefilter(title, body)
            if not ok:
                insert_candidate(conn, id=cid, source="reddit", post_id=post_id,
                                 subreddit=sub, title=title[:500], body=body[:500],
                                 url=f"https://reddit.com{p.get('permalink','')}",
                                 post_score=p.get("score", 0),
                                 created_at=p.get("created_utc", 0),
                                 fetched_at=time.time(), status="filtered")
                continue
            result = classify_haiku(title, body)
            if result is None:
                continue
            is_signal = result.get("verdict") == "victim_signal"
            insert_candidate(
                conn,
                id=cid, source="reddit", post_id=post_id, subreddit=sub,
                title=title[:500], body=body[:3000],
                url=f"https://reddit.com{p.get('permalink','')}",
                post_score=p.get("score", 0),
                created_at=p.get("created_utc", 0), fetched_at=time.time(),
                haiku_verdict=result.get("verdict"),
                haiku_reasoning=result.get("reasoning"),
                haiku_pattern=result.get("pattern"),
                haiku_l_class=result.get("l_class"),
                status="pending" if is_signal else "filtered",
            )
            if is_signal:
                total += 1
                log(f"  SIGNAL r/{sub}: {result.get('pattern','')}")
            time.sleep(0.5)
        time.sleep(2)
    return total

# ── HN ────────────────────────────────────────────────────────────────────────
def _hn_fetch_and_process(conn, query, tag, cutoff):
    """Fetch one HN query/tag combo and classify results. Returns signal count."""
    total = 0
    q   = urllib.parse.quote(query)
    # Note: literal > required — Algolia does not accept %3E for numericFilters
    url = (
        f"https://hn.algolia.com/api/v1/search"
        f"?query={q}&tags={tag}"
        f"&numericFilters=created_at_i>{cutoff}"
        f"&hitsPerPage=50"
    )
    data = http_get(url)
    if not data:
        return 0
    hits = data.get("hits", [])
    for h in hits:
        post_id = h.get("objectID", "")
        if not post_id:
            continue
        cid = make_cid("hn", post_id)
        if seen(conn, cid):
            continue
        title = h.get("title", "") or h.get("story_title", "") or query
        body  = h.get("story_text", "") or h.get("comment_text", "") or ""
        ok, _ = prefilter(title, body)
        if not ok:
            insert_candidate(conn, id=cid, source="hn", post_id=post_id,
                             title=title[:500], body=body[:500],
                             url=f"https://news.ycombinator.com/item?id={post_id}",
                             post_score=h.get("points", 0) or 0,
                             created_at=h.get("created_at_i", 0),
                             fetched_at=time.time(), status="filtered")
            continue
        result = classify_haiku(title, body)
        if result is None:
            continue
        is_signal = result.get("verdict") == "victim_signal"
        insert_candidate(
            conn,
            id=cid, source="hn", post_id=post_id,
            title=title[:500], body=body[:3000],
            url=f"https://news.ycombinator.com/item?id={post_id}",
            post_score=h.get("points", 0) or 0,
            created_at=h.get("created_at_i", 0), fetched_at=time.time(),
            haiku_verdict=result.get("verdict"),
            haiku_reasoning=result.get("reasoning"),
            haiku_pattern=result.get("pattern"),
            haiku_l_class=result.get("l_class"),
            status="pending" if is_signal else "filtered",
        )
        if is_signal:
            total += 1
            log(f"  SIGNAL HN/{post_id} [{tag}]: {result.get('pattern','')}")
        time.sleep(0.3)
    return total

def crawl_hn(conn):
    total  = 0
    cutoff = int(time.time()) - HN_WINDOW_DAYS * 24 * 3600
    for q in HN_QUERIES:
        log(f"HN query: '{q}'")
        total += _hn_fetch_and_process(conn, q, "story",   cutoff)
        total += _hn_fetch_and_process(conn, q, "comment", cutoff)
        time.sleep(0.5)
    log(f"HN total signals: {total}")
    return total

# ── Telegram ──────────────────────────────────────────────────────────────────
def tg_url(path):
    return f"https://api.telegram.org/bot{TG_TOKEN}/{path}"

def tg_send(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    http_post_json(tg_url("sendMessage"), {
        "chat_id":                TG_CHAT_ID,
        "text":                   text,
        "parse_mode":             "HTML",
        "disable_web_page_preview": True,
    })


def fmt_candidate(d):
    src   = f"r/{d['subreddit']}" if d.get("subreddit") else "HN"
    score = d.get("post_score", 0) or 0
    title = (d.get("title") or "").replace("<", "&lt;").replace(">", "&gt;")
    snip  = (d.get("body") or "")[:280].replace("<", "&lt;").replace(">", "&gt;")
    return (
        f"<b>RAI Signal</b> [{src} | {score} pts]\n"
        f"<b>Pattern:</b> {d.get('haiku_pattern') or '?'} "
        f"[{d.get('haiku_l_class') or '?'}]\n"
        f"<b>Haiku:</b> {d.get('haiku_reasoning') or '?'}\n\n"
        f"<b>{title}</b>\n{snip}\n\n"
        f"<a href='{d.get('url','#')}'>View post</a>\n\n"
        f"Reply: <code>y</code> · <code>y l1</code> · <code>n</code>\n"
        f"<i>id: {d['id']}</i>"
    )


def promote_candidate(conn, cid, l_override=None):
    row = conn.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
    if not row:
        return False
    d        = row_to_dict(row)
    final_l  = l_override or d.get("haiku_l_class") or "L0"
    name     = d.get("haiku_pattern") or "Unknown pattern"
    desc     = (d.get("body") or "")[:500]
    conn.execute(
        "INSERT INTO threat_library(pattern_name,description,l_class,source_ids,promoted_at) "
        "VALUES(?,?,?,?,?)",
        (name, desc, final_l, json.dumps([cid]), time.time())
    )
    conn.execute(
        "UPDATE candidates SET status='promoted', l_class=?, promoted_at=?, reviewed_at=? WHERE id=?",
        (final_l, time.time(), time.time(), cid)
    )
    conn.commit()
    return True

def promote_pattern(conn, pid, layer_override=None):
    """Promote a whole PRODUCE-stage pattern (multi-instance) to the threat library."""
    row = conn.execute(
        "SELECT name, mechanism, rai_layer, manipulation_type, instance_ids, card FROM patterns WHERE id=?",
        (pid,)
    ).fetchone()
    if not row:
        return False
    name, mech, layer, vtype, ids_json, card = row
    final_layer = layer_override or layer or "unmapped-consumer-harm"
    desc        = card or mech or name
    conn.execute(
        "INSERT INTO threat_library(pattern_name,description,l_class,source_ids,promoted_at) "
        "VALUES(?,?,?,?,?)",
        (name, desc, final_layer, ids_json or "[]", time.time())
    )
    conn.execute(
        "UPDATE patterns SET status='approved', reviewed_at=? WHERE id=?",
        (time.time(), pid)
    )
    # Mark constituent candidates promoted
    try:
        for cid in json.loads(ids_json or "[]"):
            conn.execute("UPDATE candidates SET status='promoted', promoted_at=? WHERE id=?", (time.time(), cid))
    except Exception:
        pass
    conn.commit()
    return True

# ── Review (CLI) ──────────────────────────────────────────────────────────────
def run_review():
    """Interactive CLI review loop. Run from SSH: python3 rai-poc2.py --review"""
    conn = init_db()
    rows = conn.execute(
        "SELECT * FROM candidates WHERE status='pending' ORDER BY post_score DESC"
    ).fetchall()
    if not rows:
        print("No pending candidates.")
        return
    print(f"\n{'='*60}")
    print(f"RAI POC #2 — Review Mode ({len(rows)} pending)")
    print(f"Commands: y [l0-l4]  approve  |  n  reject  |  s  skip  |  q  quit")
    print(f"{'='*60}\n")
    total_approved = 0
    for row in rows:
        d     = row_to_dict(row)
        score = d.get("post_score", 0) or 0
        src   = f"r/{d['subreddit']}" if d.get("subreddit") else "HN"
        print(f"\n--- Candidate {d['id']} [{src} | {score} pts] ---")
        print(f"Pattern : {d.get('haiku_pattern','?')}")
        print(f"L-class : {d.get('haiku_l_class','?')}")
        print(f"Haiku   : {d.get('haiku_reasoning','?')}")
        print(f"Title   : {d.get('title','')[:120]}")
        print(f"Body    : {d.get('body','')[:400]}")
        print(f"URL     : {d.get('url','')}")
        try:
            cmd = input("\n> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            break
        if cmd == "q":
            break
        elif cmd == "s":
            continue
        elif cmd.startswith("n"):
            conn.execute(
                "UPDATE candidates SET status='rejected', reviewed_at=? WHERE id=?",
                (time.time(), d["id"])
            )
            conn.commit()
            print("Rejected.")
        elif cmd.startswith("y"):
            parts      = cmd.split()
            l_override = None
            if len(parts) > 1 and parts[1].upper() in ("L0","L1","L2","L3","L4"):
                l_override = parts[1].upper()
            if promote_candidate(conn, d["id"], l_override):
                lib = conn.execute("SELECT COUNT(*) FROM threat_library").fetchone()[0]
                tl  = conn.execute(
                    "SELECT pattern_name, l_class FROM threat_library ORDER BY id DESC LIMIT 1"
                ).fetchone()
                print(f"Promoted: {tl[0]} [{tl[1]}] (library: {lib}/10)")
                total_approved += 1
    print(f"\nSession: {total_approved} approved. Run --export to generate threat library.")

def tg_notify_pending(conn):
    """One-way Telegram notification when crawl finds new signals."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    n = conn.execute("SELECT COUNT(*) FROM candidates WHERE status='pending'").fetchone()[0]
    lib = conn.execute("SELECT COUNT(*) FROM threat_library").fetchone()[0]
    if n == 0:
        return
    tg_send(
        f"<b>RAI POC #2: {n} signal(s) pending review</b>\n"
        f"Threat library: {lib}/10\n\n"
        f"SSH in and run:\n"
        f"<code>python3 ~/nanoclaw/rai-poc2.py --review</code>"
    )

# ── Export JSON (Cowork artifact sync) ───────────────────────────────────────
def run_export_json(conn=None):
    import subprocess
    close = conn is None
    if close:
        conn = init_db()
    stats = {}
    for row in conn.execute("SELECT status, COUNT(*) FROM candidates GROUP BY status").fetchall():
        stats[row[0]] = row[1]
    def to_dicts(rows):
        out = [row_to_dict(r) for r in rows]
        for d in out:
            if d.get("body"):
                d["body"] = d["body"][:400]
        return out
    pending  = to_dicts(conn.execute("SELECT * FROM candidates WHERE status='pending' ORDER BY post_score DESC").fetchall())
    approved = to_dicts(conn.execute("SELECT * FROM candidates WHERE status='approved' ORDER BY reviewed_at DESC").fetchall())
    promoted = to_dicts(conn.execute("SELECT * FROM candidates WHERE status='promoted' ORDER BY promoted_at DESC").fetchall())
    lib_rows = conn.execute("SELECT * FROM threat_library ORDER BY promoted_at DESC").fetchall()
    lib_cols = ["id","pattern_name","description","l_class","source_ids","promoted_at"]
    # Patterns (PRODUCE stage output) — only enriched ones, novel first
    pat_cols = ["id","name","mechanism","manipulation_type","rai_layer","novelty",
                "novelty_rationale","instance_ids","instance_count","card","status",
                "created_at","reviewed_at","enriched"]
    try:
        pat_rows = conn.execute(
            "SELECT id,name,mechanism,manipulation_type,rai_layer,novelty,novelty_rationale,"
            "instance_ids,instance_count,card,status,created_at,reviewed_at,enriched "
            "FROM patterns WHERE enriched=1 ORDER BY "
            "CASE novelty WHEN 'novel' THEN 0 WHEN 'variant' THEN 1 ELSE 2 END, instance_count DESC"
        ).fetchall()
        patterns = [dict(zip(pat_cols, r)) for r in pat_rows]
    except Exception:
        patterns = []
    payload = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "stats": stats,
        "pending":  pending,
        "approved": approved,
        "promoted": promoted,
        "patterns": patterns,
        "threat_library": [dict(zip(lib_cols, r)) for r in lib_rows],
    }
    os.makedirs(os.path.dirname(SIGNALS_JSON), exist_ok=True)
    with open(SIGNALS_JSON, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    log(f"Signals JSON: {len(pending)} pending, {len(lib_rows)} library → {SIGNALS_JSON}")
    try:
        subprocess.run(["git","-C",NO5_DIR,"add","-f","logs/rai-poc2-signals.json"], check=True, capture_output=True)
        res = subprocess.run(["git","-C",NO5_DIR,"commit","-m",
            f"[vps] rai-poc2 signals {datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M')}"],
            capture_output=True)
        if res.returncode == 0:
            subprocess.run(["git","-C",NO5_DIR,"push"], check=True, capture_output=True)
            log("no5-context pushed")
        else:
            log("no5-context: nothing new to push")
    except Exception as e:
        log(f"git sync warning: {e}")
    if close:
        conn.close()

def run_process_reviews(conn=None):
    close = conn is None
    if close:
        conn = init_db()
    if not os.path.exists(REVIEWS_JSON):
        if close: conn.close()
        return
    try:
        reviews = json.load(open(REVIEWS_JSON))
    except Exception as e:
        log(f"Review queue parse error: {e}")
        if close: conn.close()
        return
    applied = 0
    for r in reviews:
        rid, action = r.get("id"), r.get("action")
        rtype = r.get("type", "candidate")
        if not rid or action not in ("approve","reject"):
            continue
        if rtype == "pattern":
            # Pattern review: approve promotes the whole pattern to threat_library
            prow = conn.execute("SELECT status FROM patterns WHERE id=?", (rid,)).fetchone()
            if not prow or prow[0] not in ("candidate","approved"):
                continue
            if action == "approve":
                promote_pattern(conn, rid, r.get("rai_layer") or r.get("l_class") or None)
                log(f"Review: promoted PATTERN {rid}")
            else:
                conn.execute("UPDATE patterns SET status='rejected', reviewed_at=? WHERE id=?", (time.time(), rid))
                conn.commit()
                log(f"Review: rejected PATTERN {rid}")
            applied += 1
            continue
        # Candidate review (legacy single-signal path)
        row = conn.execute("SELECT status FROM candidates WHERE id=?", (rid,)).fetchone()
        if not row or row[0] not in ("pending","approved","clustered"):
            continue
        if action == "approve":
            promote_candidate(conn, rid, r.get("l_class") or None)
            log(f"Review: promoted {rid}")
        else:
            conn.execute("UPDATE candidates SET status='rejected', reviewed_at=? WHERE id=?", (time.time(), rid))
            conn.commit()
            log(f"Review: rejected {rid}")
        applied += 1
    if applied:
        log(f"Processed {applied} queued reviews")
        with open(REVIEWS_JSON, "w") as f:
            json.dump([], f)
        run_export_json(conn)
    if close:
        conn.close()

# ── Export (markdown) ─────────────────────────────────────────────────────────
def run_export():
    conn = init_db()
    rows = conn.execute("""
        SELECT tl.id, tl.pattern_name, tl.l_class, tl.description,
               tl.source_ids, tl.promoted_at,
               c.source, c.subreddit, c.title, c.url, c.post_score, c.created_at
        FROM threat_library tl
        LEFT JOIN candidates c
               ON c.id = json_extract(tl.source_ids, '$[0]')
        ORDER BY tl.promoted_at
    """).fetchall()

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    lines = [
        "# RAI Threat Library Delta — POC #2",
        f"_Generated: {today}_",
        f"_Patterns found: {len(rows)}/10_",
        "",
    ]
    for i, r in enumerate(rows, 1):
        _, name, lc, desc, _, prom_at, src, sub, title, url, score, cre_at = r
        src_label = f"reddit/r/{sub}" if sub else (src or "unknown")
        date_str  = (datetime.datetime.fromtimestamp(cre_at).strftime("%Y-%m-%d")
                     if cre_at else "?")
        prom_str  = (datetime.datetime.fromtimestamp(prom_at).strftime("%Y-%m-%d")
                     if prom_at else "?")
        lines += [
            f"## Pattern {i}: {name} [{lc}]",
            f"**Source:** {src_label} · {score} pts · {date_str}",
            f"**Post:** [{title}]({url})" if url else f"**Post:** {title}",
            f"**Signal excerpt:** _{desc[:300]}_",
            f"**Promoted:** {prom_str}",
            "",
        ]

    os.makedirs(os.path.dirname(EXPORT_PATH), exist_ok=True)
    with open(EXPORT_PATH, "w") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))
    print(f"\nWritten to {EXPORT_PATH}")

# ── Status ────────────────────────────────────────────────────────────────────
def run_status():
    conn = init_db()
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM candidates GROUP BY status ORDER BY COUNT(*) DESC"
    ).fetchall()
    lib  = conn.execute("SELECT COUNT(*) FROM threat_library").fetchone()[0]
    print(f"\nRAI POC #2 — Victim Signal Crawler")
    print(f"DB: {DB_PATH}\n")
    print("Candidates:")
    for status, count in rows:
        bar = "#" * min(count, 40)
        print(f"  {status:12s}  {count:4d}  {bar}")
    print(f"\nThreat library:  {lib}/10")
    if lib:
        lib_rows = conn.execute(
            "SELECT pattern_name, l_class, promoted_at FROM threat_library ORDER BY id"
        ).fetchall()
        for lr in lib_rows:
            ts = datetime.datetime.fromtimestamp(lr[2]).strftime("%Y-%m-%d") if lr[2] else "?"
            print(f"  [{lr[1]}] {lr[0]} ({ts})")

# ── Crawl ─────────────────────────────────────────────────────────────────────
def run_crawl():
    conn     = init_db()
    log("=== RAI POC #2 crawl start ===")
    n_reddit = crawl_reddit(conn)
    n_hn     = crawl_hn(conn)
    log(f"=== Done: {n_reddit} Reddit signals + {n_hn} HN signals ===")
    run_process_reviews(conn)   # apply any desktop-queued reviews first
    run_export_json(conn)       # push updated signals.json to no5-context
    run_status()
    tg_notify_pending(conn)
    if n_reddit == 0 and not os.environ.get("REDDIT_CLIENT_ID"):
        print(
            "\nTo unlock Reddit (4 subreddits, much richer signal):\n"
            "  1. Go to https://www.reddit.com/prefs/apps\n"
            "  2. Create app: type=script, redirect_uri=http://localhost\n"
            "  3. Add to ~/nanoclaw/.env:\n"
            "       REDDIT_CLIENT_ID=<14-char id under app name>\n"
            "       REDDIT_CLIENT_SECRET=<27-char secret>\n"
            "  4. Re-run --crawl"
        )

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="RAI POC #2 — Victim Signal Crawler")
    ap.add_argument("--crawl",           action="store_true", help="Fetch + classify + export + process reviews")
    ap.add_argument("--review",           action="store_true", help="Interactive CLI review")
    ap.add_argument("--export",           action="store_true", help="Export threat library markdown")
    ap.add_argument("--export-json",      action="store_true", help="Write signals.json to no5-context + git push")
    ap.add_argument("--process-reviews",  action="store_true", help="Apply review queue from Cowork artifact")
    ap.add_argument("--status",           action="store_true", help="Show counts")
    args = ap.parse_args()

    if args.crawl:
        run_crawl()
    elif args.review:
        run_review()
    elif args.export:
        run_export()
    elif getattr(args, "export_json"):
        run_export_json()
    elif getattr(args, "process_reviews"):
        run_process_reviews()
    elif args.status:
        run_status()
    else:
        ap.print_help()
