#!/usr/bin/env python3
"""
rai-reddit-research.py — RAI POC #2: Reddit Community Research Module
OL-325 | 2026-05-30

Three-layer Reddit intelligence:
  L1  Subreddit map     — discover + score ~40 communities by signal density
  L2  Multi-mode crawl  — new + top + rising posts + high-engagement comment threads
  L3  Phrase search     — cross-Reddit keyword search for victim-language phrases

All results feed the same SQLite DB as rai-poc2.py.

Usage:
  python3 rai-reddit-research.py --map        # L1: discover + score subreddits
  python3 rai-reddit-research.py --crawl      # L2: multi-mode post + comment crawl
  python3 rai-reddit-research.py --search     # L3: phrase search across all Reddit
  python3 rai-reddit-research.py --all        # L1 + L2 + L3 in sequence
  python3 rai-reddit-research.py --status     # Show subreddit map + signal counts
"""

import os, sys, json, re, time, sqlite3, hashlib, datetime, argparse
import urllib.request, urllib.parse, base64

BASE_DIR  = "/home/tim/nanoclaw"
NO5_DIR   = "/home/tim/no5-context"
DB_PATH   = f"{BASE_DIR}/data/rai-poc2.db"
MAP_PATH  = f"{BASE_DIR}/data/rai-subreddit-map.json"
LOG_PATH  = f"{BASE_DIR}/logs/rai-reddit.log"

CANDIDATE_COLS = [
    "id","source","post_id","subreddit","title","body","url",
    "post_score","created_at","fetched_at","haiku_verdict",
    "haiku_reasoning","haiku_pattern","haiku_l_class","status",
    "reviewed_at","promoted_at","l_class"
]

# ── Subreddit seed list (L1 map starting point) ───────────────────────────────
SEED_SUBREDDITS = [
    # Direct AI victim
    "ChatGPT", "AIAssistants", "artificial", "ChatGPTPromptEngineering",
    "ClaudeAI", "singularity", "LocalLLaMA",
    # Scam / fraud victim
    "Scams", "Fraud", "phishing", "cybercrime", "onlinefraud",
    # Impersonation / deepfake
    "Deepfakes", "MediaManipulation", "hacking", "privacy",
    # Personal experience / vent
    "TrueOffMyChest", "offmychest", "rant", "tifu", "confession",
    "mildlyinfuriating", "mildlyinteresting",
    # Trust / disinfo
    "conspiracy", "skeptic", "fatFIRE", "technology",
    # High-stakes victim accounts
    "legaladvice", "personalfinance", "relationships",
    "AskReddit", "self",
]

# ── Phrase search terms (L3) ──────────────────────────────────────────────────
PHRASE_SEARCHES = [
    # Victim language — direct
    '"felt off" AI',
    '"that felt wrong" chatgpt',
    '"didn\'t feel right" AI',
    '"realized it wasn\'t real"',
    '"turned out to be AI"',
    '"turned out to be fake"',
    '"thought it was real"',
    # Impersonation
    '"voice clone" scam',
    '"deepfake call" family',
    '"fake call" mom dad voice',
    '"it wasn\'t them" AI',
    '"didn\'t sound like" scam',
    '"account hacked" message friend',
    # Content authenticity
    '"is this real" article AI',
    '"AI generated" fake news',
    '"can\'t tell" real AI',
    '"how do I know" AI real',
    # Manipulation realization
    '"manipulated me" AI chatbot',
    '"gaslighting" chatgpt',
    '"lied to me" AI',
    '"tricked me" AI',
    # Agent overreach / cover-up (2026-07-06, OL-412 gap fix)
    '"without asking" AI agent action',
    '"didn\'t approve" AI agent',
    '"covered up" AI mistake',
    # Undisclosed bot / institutional mimicry (2026-07-06, OL-412 gap fix)
    '"didn\'t tell me it was a bot"',
    '"pretending to be human" AI customer service',
    '"undisclosed" chatbot company',
]

HAIKU_MODEL   = "claude-haiku-4-5-20251001"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"

HAIKU_SYSTEM = (
    "You analyze social media posts for AI manipulation and AI-enabled deception victim signals.\n\n"
    "Classify whether this post describes a person's DIRECT PERSONAL EXPERIENCE in any of:\n\n"
    "1. AI INTERACTION MANIPULATION: felt manipulated/deceived/nudged by an AI chatbot\n"
    "2. CONTENT/LINK AUTHENTICITY DOUBT: questioned whether shared content was real or AI-generated\n"
    "3. IMPERSONATION / SYNTHETIC PERSONA: received a call or message from someone claiming to be a known "
    "person but felt wrong, or turned out to be AI/cloned voice/fake account\n\n"
    "NOT: academic analysis, news reporting, third-person accounts, code questions, "
    "general quality complaints without personal experience.\n\n"
    "JSON only:\n"
    "{\"verdict\":\"victim_signal\",\"pattern\":\"one-line description\","
    "\"l_class\":\"L1\",\"reasoning\":\"under 20 words\"}\n"
    "OR {\"verdict\":\"not_signal\",\"pattern\":null,\"l_class\":null,\"reasoning\":\"under 20 words\"}\n\n"
    "L0=impersonation/social-engineering L1=behavioral-nudge/dark-pattern "
    "L2=false-urgency/FOMO L3=gaslighting/confidence-manipulation "
    "L4=reality-distortion L5=voice-clone/deepfake/synthetic-persona"
)

PREFILTER_NEWS = re.compile(
    r'\b(study shows|researchers find|according to|published in|percent of|'
    r'scientists say|new research|survey found|data shows|report finds|in a paper)\b', re.I
)
PREFILTER_CODE = re.compile(r'```')
MIN_BODY_LEN   = 50
CRAWL_DAYS     = 30

# ── Env + auth ────────────────────────────────────────────────────────────────
def load_env(path=f"{BASE_DIR}/.env"):
    if not os.path.exists(path): return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

load_env()
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
REDDIT_CLIENT   = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_SECRET   = os.environ.get("REDDIT_CLIENT_SECRET", "")
GOOGLE_API_KEY  = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID   = os.environ.get("GOOGLE_CSE_ID", "")
SERPER_API_KEY  = os.environ.get("SERPER_API_KEY", "")

_token_cache = {"token": None, "expires": 0}

def reddit_token():
    if not REDDIT_CLIENT or not REDDIT_SECRET:
        return None
    if _token_cache["token"] and time.time() < _token_cache["expires"]:
        return _token_cache["token"]
    creds   = base64.b64encode(f"{REDDIT_CLIENT}:{REDDIT_SECRET}".encode()).decode()
    payload = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req     = urllib.request.Request(
        "https://www.reddit.com/api/v1/access_token",
        data=payload,
        headers={"Authorization": f"Basic {creds}",
                 "User-Agent": "RAI-Research/1.0 (research; u/raipoc2)",
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        tok = data.get("access_token")
        if tok:
            _token_cache.update({"token": tok, "expires": time.time() + data.get("expires_in", 3600) - 60})
        return tok
    except Exception as e:
        log(f"Reddit token error: {e}")
        return None

def reddit_get(path, token, params=None):
    if token:
        base = "https://oauth.reddit.com"
        hdrs = {"Authorization": f"Bearer {token}", "User-Agent": "RAI-Research/1.0 by pektimole"}
    else:
        base = "https://www.reddit.com"
        hdrs = {"User-Agent": "RAI-Research/1.0 by pektimole"}
        time.sleep(1)
    url = f"{base}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"Reddit GET {path}: {e}")
        return None

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg):
    ts   = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f: f.write(line + "\n")
    except Exception: pass

# ── HTTP helpers ──────────────────────────────────────────────────────────────
def http_post_json(url, data, headers=None, timeout=25):
    body = json.dumps(data).encode()
    h    = {"Content-Type": "application/json"}
    if headers: h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        log(f"POST error: {e}")
        return None

# ── DB ────────────────────────────────────────────────────────────────────────
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # Candidates table (shared with rai-poc2.py)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS candidates (
            id TEXT PRIMARY KEY, source TEXT, post_id TEXT, subreddit TEXT,
            title TEXT, body TEXT, url TEXT, post_score INTEGER,
            created_at REAL, fetched_at REAL, haiku_verdict TEXT,
            haiku_reasoning TEXT, haiku_pattern TEXT, haiku_l_class TEXT,
            status TEXT DEFAULT 'pending', reviewed_at REAL, promoted_at REAL, l_class TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS threat_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT, pattern_name TEXT,
            description TEXT, l_class TEXT, source_ids TEXT, promoted_at REAL
        )""")
    # Subreddit map table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subreddit_map (
            name          TEXT PRIMARY KEY,
            subscribers   INTEGER,
            signal_rate   REAL DEFAULT 0.0,
            total_crawled INTEGER DEFAULT 0,
            total_signals INTEGER DEFAULT 0,
            last_crawled  REAL,
            active        INTEGER DEFAULT 1
        )""")
    conn.commit()
    return conn

def make_cid(source, post_id):
    return hashlib.sha256(f"{source}:{post_id}".encode()).hexdigest()[:16]

def seen(conn, cid):
    return conn.execute("SELECT 1 FROM candidates WHERE id=?", (cid,)).fetchone() is not None

def insert_candidate(conn, **kw):
    cols = ", ".join(kw.keys())
    ph   = ", ".join("?" * len(kw))
    conn.execute(f"INSERT OR IGNORE INTO candidates ({cols}) VALUES ({ph})", list(kw.values()))
    conn.commit()

def update_subreddit_stats(conn, name, crawled, signals):
    existing = conn.execute(
        "SELECT total_crawled, total_signals FROM subreddit_map WHERE name=?", (name,)
    ).fetchone()
    if existing:
        tc = existing[0] + crawled
        ts = existing[1] + signals
        rate = ts / tc if tc else 0.0
        conn.execute(
            "UPDATE subreddit_map SET total_crawled=?, total_signals=?, signal_rate=?, last_crawled=? WHERE name=?",
            (tc, ts, rate, time.time(), name)
        )
    conn.commit()

# ── Pre-filter ────────────────────────────────────────────────────────────────
def prefilter(title, body):
    text = f"{title} {body}"
    if PREFILTER_NEWS.search(text): return False
    if PREFILTER_CODE.search(text): return False
    if len((body or "").strip()) < MIN_BODY_LEN: return False
    return True

# ── Haiku classifier ──────────────────────────────────────────────────────────
def classify(title, body):
    if not ANTHROPIC_KEY: return None
    snippet = f"Title: {title}\n\nPost: {body[:1500]}"
    resp = http_post_json(ANTHROPIC_API, {
        "model": HAIKU_MODEL, "max_tokens": 200,
        "system": [{"type": "text", "text": HAIKU_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": snippet}]
    }, headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"})
    if not resp: return None
    try:
        raw = resp["content"][0]["text"].strip()
        raw = re.sub(r"^```json\s*", "", raw); raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        log(f"Classify parse: {e}")
        return None

def process_post(conn, post_id, title, body, url, score, created_at, subreddit, source="reddit"):
    cid = make_cid(source, post_id)
    if seen(conn, cid): return False, False
    if not prefilter(title, body):
        insert_candidate(conn, id=cid, source=source, post_id=post_id, subreddit=subreddit,
                         title=title[:500], body=body[:500], url=url,
                         post_score=score, created_at=created_at,
                         fetched_at=time.time(), status="filtered")
        return True, False
    result = classify(title, body)
    if result is None: return True, False
    is_signal = result.get("verdict") == "victim_signal"
    insert_candidate(conn,
        id=cid, source=source, post_id=post_id, subreddit=subreddit,
        title=title[:500], body=body[:3000], url=url,
        post_score=score, created_at=created_at, fetched_at=time.time(),
        haiku_verdict=result.get("verdict"), haiku_reasoning=result.get("reasoning"),
        haiku_pattern=result.get("pattern"), haiku_l_class=result.get("l_class"),
        status="pending" if is_signal else "filtered"
    )
    if is_signal:
        log(f"  SIGNAL [{subreddit}]: {result.get('pattern','')}")
    time.sleep(0.5)
    return True, is_signal

# ── L1: Subreddit map ─────────────────────────────────────────────────────────
def run_map():
    token = reddit_token()
    conn = init_db()
    log(f"L1 Subreddit map: seeding {len(SEED_SUBREDDITS)} subreddits")

    # Seed from list
    for name in SEED_SUBREDDITS:
        existing = conn.execute("SELECT name FROM subreddit_map WHERE name=?", (name,)).fetchone()
        if existing: continue
        data = reddit_get(f"/r/{name}/about.json", token)
        if not data: continue
        sub = data.get("data", {})
        subs = sub.get("subscribers", 0)
        conn.execute(
            "INSERT OR IGNORE INTO subreddit_map(name, subscribers) VALUES(?,?)",
            (name, subs)
        )
        conn.commit()
        log(f"  Seeded r/{name}: {subs:,} subscribers")
        time.sleep(0.3)

    # Discover related subs via Reddit search
    for query in ["AI scam victim", "deepfake detection", "voice clone", "chatgpt manipulation"]:
        data = reddit_get("/subreddits/search.json", token,
                          params={"q": query, "limit": 10, "sort": "relevance"})
        if not data: continue
        for item in data.get("data", {}).get("children", []):
            s    = item.get("data", {})
            name = s.get("display_name", "")
            subs = s.get("subscribers", 0)
            if not name or subs < 1000: continue
            existing = conn.execute("SELECT name FROM subreddit_map WHERE name=?", (name,)).fetchone()
            if existing: continue
            conn.execute(
                "INSERT OR IGNORE INTO subreddit_map(name, subscribers) VALUES(?,?)",
                (name, subs)
            )
            conn.commit()
            log(f"  Discovered r/{name}: {subs:,} subs")
            time.sleep(0.2)
        time.sleep(1)

    total = conn.execute("SELECT COUNT(*) FROM subreddit_map").fetchone()[0]
    log(f"L1 complete: {total} subreddits mapped")
    conn.close()

# ── L2: Multi-mode crawl ──────────────────────────────────────────────────────
def crawl_subreddit(conn, token, name, modes=("new", "top"), limit=100):
    crawled = signals = 0
    for mode in modes:
        params = {"limit": limit}
        if mode == "top":
            params["t"] = "month"
        data = reddit_get(f"/r/{name}/{mode}.json", token, params=params)
        if not data: continue
        posts = data.get("data", {}).get("children", [])
        log(f"  r/{name}/{mode}: {len(posts)} posts")
        for item in posts:
            p       = item.get("data", {})
            post_id = p.get("id", "")
            if not post_id: continue
            title = p.get("title", "")
            body  = p.get("selftext", "") or ""
            url   = f"https://reddit.com{p.get('permalink','')}"
            score = p.get("score", 0)
            cre   = p.get("created_utc", 0)
            # Skip posts older than CRAWL_DAYS
            if cre and cre < time.time() - CRAWL_DAYS * 86400: continue
            ok, is_sig = process_post(conn, post_id, title, body, url, score, cre, name)
            if ok: crawled += 1
            if is_sig: signals += 1

            # For high-engagement posts (>50 comments), crawl top comments too
            if p.get("num_comments", 0) > 50 and not is_sig:
                crawl_comments(conn, token, name, post_id, title)

        time.sleep(1)
    update_subreddit_stats(conn, name, crawled, signals)
    return crawled, signals

def crawl_comments(conn, token, sub, post_id, post_title, limit=25):
    data = reddit_get(f"/r/{sub}/comments/{post_id}.json", token,
                      params={"limit": limit, "depth": 2, "sort": "top"})
    if not data or not isinstance(data, list) or len(data) < 2: return
    comments = data[1].get("data", {}).get("children", [])
    for item in comments:
        c = item.get("data", {})
        if item.get("kind") != "t1": continue
        body    = c.get("body", "") or ""
        cid_src = c.get("id", "")
        if not cid_src or len(body.strip()) < MIN_BODY_LEN: continue
        title = f"[Comment on: {post_title[:80]}]"
        url   = f"https://reddit.com/r/{sub}/comments/{post_id}/-/{cid_src}"
        score = c.get("score", 0)
        cre   = c.get("created_utc", 0)
        process_post(conn, f"comment_{cid_src}", title, body, url, score, cre, sub)

def run_crawl():
    token = reddit_token()
    conn     = init_db()
    # Prioritize: active subs ordered by signal_rate desc, then subscribers desc
    subs = conn.execute(
        "SELECT name FROM subreddit_map WHERE active=1 ORDER BY signal_rate DESC, subscribers DESC"
    ).fetchall()
    if not subs:
        log("No subreddits in map. Run --map first.")
        conn.close()
        return
    log(f"L2 Multi-mode crawl: {len(subs)} subreddits")
    total_c = total_s = 0
    for (name,) in subs:
        c, s = crawl_subreddit(conn, token, name, modes=("new", "top"))
        total_c += c
        total_s += s
        time.sleep(2)
    log(f"L2 done: {total_c} crawled, {total_s} signals")
    conn.close()


# ── Serper.dev Search (L3 backend) ───────────────────────────────────────────
def google_search_reddit(query, num=10):
    """Search Reddit via Serper.dev (2500 free queries, no billing)."""
    if not SERPER_API_KEY:
        return None
    body = json.dumps({"q": f"site:reddit.com {query}", "num": min(num, 10)}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=body,
        headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        # Normalize to Google CSE format: {items: [{title, link, snippet}]}
        return {"items": [{"title": x.get("title",""), "link": x.get("link",""), "snippet": x.get("snippet","")} for x in data.get("organic", [])]}
    except Exception as e:
        log(f"Serper error for '{query[:40]}': {e}")
        return None

def parse_reddit_url(url):
    """Extract (subreddit, post_id) from a Reddit URL."""
    m = re.search(r"/r/([^/]+)/comments/([a-z0-9]+)/", url)
    if m:
        return m.group(1), m.group(2)
    return "unknown", None

# ── L3: Phrase search ─────────────────────────────────────────────────────────
def run_search():
    token = reddit_token()
    if not SERPER_API_KEY:
        print("Serper API key required.\n  Add SERPER_API_KEY to ~/nanoclaw/.env")
        return
    conn    = init_db()
    total_s = 0
    log(f"L3 Phrase search (Google CSE): {len(PHRASE_SEARCHES)} queries")
    for phrase in PHRASE_SEARCHES:
        data = google_search_reddit(phrase)
        if not data: continue
        items = data.get("items", [])
        log(f"  '{phrase[:40]}': {len(items)} results")
        for item in items:
            link    = item.get("link", "")
            title   = item.get("title", "")
            snippet = item.get("snippet", "") or ""
            sub, post_id = parse_reddit_url(link)
            if not post_id: continue
            _, is_sig = process_post(conn, post_id, title, snippet, link, 0, 0, sub)
            if is_sig: total_s += 1
        time.sleep(1)
    log(f"L3 done: {total_s} signals from phrase search (Google CSE)")
    conn.close()

# ── Status ────────────────────────────────────────────────────────────────────
def run_status():
    conn = init_db()
    # Signal counts
    rows = conn.execute("SELECT status, COUNT(*) FROM candidates WHERE source='reddit' GROUP BY status").fetchall()
    print(f"\nRAI Reddit Research — DB: {DB_PATH}")
    print("\nReddit candidates:")
    for s, c in rows:
        print(f"  {s:12s}  {c:4d}  {'#'*min(c,40)}")
    # Top subreddits by signal rate
    subs = conn.execute(
        "SELECT name, signal_rate, total_signals, total_crawled, last_crawled "
        "FROM subreddit_map WHERE total_crawled > 0 ORDER BY signal_rate DESC LIMIT 15"
    ).fetchall()
    if subs:
        print(f"\nTop subreddits by signal rate:")
        for name, rate, sigs, crawled, last in subs:
            last_str = datetime.datetime.fromtimestamp(last).strftime("%m-%d") if last else "?"
            print(f"  r/{name:25s}  {rate:.1%}  ({sigs}/{crawled})  last:{last_str}")
    # Total map size
    total_map = conn.execute("SELECT COUNT(*) FROM subreddit_map").fetchone()[0]
    print(f"\nSubreddit map: {total_map} communities")
    conn.close()

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="RAI Reddit Community Research Module")
    ap.add_argument("--map",    action="store_true", help="L1: discover + score subreddits")
    ap.add_argument("--crawl",  action="store_true", help="L2: multi-mode post + comment crawl")
    ap.add_argument("--search", action="store_true", help="L3: phrase search across Reddit")
    ap.add_argument("--all",    action="store_true", help="Run L1 + L2 + L3")
    ap.add_argument("--status", action="store_true", help="Show map + signal stats")
    args = ap.parse_args()

    # unauthenticated fallback active - no creds required
    if args.all:
        run_map()
        run_crawl()
        run_search()
    elif args.map:
        run_map()
    elif args.crawl:
        run_crawl()
    elif args.search:
        run_search()
    elif args.status:
        run_status()
    else:
        ap.print_help()
