#!/usr/bin/env python3
"""
promote_patterns.py — RAI POC #2: Pattern Promotion CLI
OL-367 | 2026-06-04

Usage:
  python3 ~/nanoclaw/promote_patterns.py          # interactive review
  python3 ~/nanoclaw/promote_patterns.py --status # show counts
"""

import os, sys, json, sqlite3, datetime, subprocess, time, argparse

DB_PATH       = "/home/tim/nanoclaw/data/rai-poc2.db"
NO5_DIR       = "/home/tim/no5-context"
LIBRARY_JSONL = f"{NO5_DIR}/logs/rai-threat-library.jsonl"

# Default deck beat by VS-class or rai_layer
BEAT_DEFAULTS = {
    "VS0": "Beat 1",   # impersonation/scams — consumer threat landscape
    "VS1": "Beat 1",   # behavioral nudge — consumer threat
    "VS2": "Beat 2",   # urgency/FOMO — structural proof
    "VS3": "Beat 2",   # gaslighting/epistemic — structural proof
    "VS4": "Beat 2",   # reality erosion — structural proof
    "VS5": "Beat 1",   # synthetic identity — consumer landscape
    "VS6": "Beat 2",   # content authenticity — structural proof
    "L0":  "Beat 3",
    "L1":  "Beat 2",
    "L-1": "Beat 2",
    "L2":  "Beat 3",
    "L3":  "Beat 2",
    "unmapped-consumer-harm": "Beat 1",
}

def load_existing_max():
    if not os.path.exists(LIBRARY_JSONL):
        return 0
    max_n = 0
    with open(LIBRARY_JSONL) as f:
        for line in f:
            try:
                e = json.loads(line)
                n = int(e["id"].replace("TL-", ""))
                if n > max_n:
                    max_n = n
            except Exception:
                pass
    return max_n

def get_source_platform(conn, ids_json):
    try:
        ids = json.loads(ids_json or "[]")
    except Exception:
        ids = []
    if not ids:
        return "unknown"
    ph = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT subreddit, url FROM candidates WHERE id IN ({ph})", ids
    ).fetchall()
    platforms = set()
    for sub, url in rows:
        if sub:
            platforms.add("reddit")
        elif url and "ycombinator" in (url or ""):
            platforms.add("hn")
        else:
            platforms.add("web")
    return "/".join(sorted(platforms)) if platforms else "unknown"

def get_first_seen(conn, ids_json):
    try:
        ids = json.loads(ids_json or "[]")
    except Exception:
        ids = []
    if not ids:
        return datetime.date.today().isoformat()
    ph = ",".join("?" for _ in ids)
    row = conn.execute(
        f"SELECT MIN(created_at) FROM candidates WHERE id IN ({ph})", ids
    ).fetchone()
    ts = row[0] if row and row[0] else None
    if ts:
        return datetime.datetime.fromtimestamp(ts).date().isoformat()
    return datetime.date.today().isoformat()

def git_push():
    try:
        subprocess.run(["git", "-C", NO5_DIR, "add", "logs/rai-threat-library.jsonl"],
                       check=True, capture_output=True)
        msg = f"[cc:{datetime.date.today()}] RAI threat library update — promote_patterns"
        subprocess.run(["git", "-C", NO5_DIR, "commit", "-m", msg],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", NO5_DIR, "push"],
                       check=True, capture_output=True)
        print("Pushed to no5-context.")
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode()[:300] if e.stderr else str(e)
        print(f"Git push failed (non-fatal): {err}")
        print(f"Manual: cd {NO5_DIR} && git add logs/rai-threat-library.jsonl && git commit -m '...' && git push")

def run_review():
    conn = sqlite3.connect(DB_PATH)
    max_tl = load_existing_max()

    rows = conn.execute(
        "SELECT id, name, rai_layer, manipulation_type, novelty, instance_count, instance_ids, card "
        "FROM patterns WHERE status='candidate' ORDER BY instance_count DESC"
    ).fetchall()

    if not rows:
        print("No candidate patterns to review. Run --status to check state.")
        return

    print(f"\n{'='*65}")
    print(f"RAI POC #2 — Pattern Review  ({len(rows)} candidates, {max_tl}/10 promoted)")
    print(f"Commands:  y [beat1|beat2|beat3]  approve  |  n  reject  |  s  skip  |  q  quit")
    print(f"           (y alone uses default deck beat shown per pattern)")
    print(f"{'='*65}\n")

    session_approved = 0

    for pid, name, rai_layer, vs_class, novelty, count, ids_json, card in rows:
        default_beat = BEAT_DEFAULTS.get(vs_class) or BEAT_DEFAULTS.get(rai_layer) or "Beat 1"

        print(f"\n{'─'*65}")
        print(f"Pattern #{pid}  [{count} signals | {novelty} | {rai_layer} / {vs_class}]")
        print(f"Name      : {name}")
        print(f"DeckBeat  : {default_beat} (default, override with y beat2)")
        print()
        if card:
            for i, line in enumerate(card.split("\n")[:10]):
                print(f"  {line}")
            if len(card.split("\n")) > 10:
                print("  ...")
        print()

        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            break

        if cmd in ("q", "quit"):
            break
        elif cmd in ("s", "skip"):
            continue
        elif cmd.startswith("n"):
            conn.execute(
                "UPDATE patterns SET status='rejected', reviewed_at=? WHERE id=?",
                (time.time(), pid)
            )
            conn.commit()
            print("Rejected.")
        elif cmd.startswith("y"):
            parts = cmd.split()
            deck_beat = default_beat
            if len(parts) > 1:
                b = parts[1].lower()
                if "1" in b: deck_beat = "Beat 1"
                elif "2" in b: deck_beat = "Beat 2"
                elif "3" in b: deck_beat = "Beat 3"

            conn.execute(
                "UPDATE patterns SET status='approved', reviewed_at=? WHERE id=?",
                (time.time(), pid)
            )
            conn.commit()

            max_tl += 1
            tl_id = f"TL-{max_tl:03d}"
            source_platform = get_source_platform(conn, ids_json)
            first_seen = get_first_seen(conn, ids_json)

            entry = {
                "id":              tl_id,
                "name":            name,
                "l_class":         rai_layer,
                "vs_class":        vs_class,
                "signal_type":     "victim-reported",
                "source_platform": source_platform,
                "first_seen":      first_seen,
                "promoted":        datetime.date.today().isoformat(),
                "description":     (card or name)[:800],
                "deck_beat":       deck_beat,
                "source_signals":  (json.loads(ids_json or "[]") if ids_json else [])[:5],
            }
            with open(LIBRARY_JSONL, "a") as fh:
                fh.write(json.dumps(entry) + "\n")

            session_approved += 1
            print(f"Promoted: {tl_id} — {name[:55]} → {deck_beat}  (library: {max_tl}/10)")

    conn.close()

    if session_approved > 0:
        print(f"\nSession: {session_approved} approved.")
        try:
            push = input("Push to no5-context git? [y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            push = "n"
        if push == "y":
            git_push()
        else:
            print(f"Not pushed. Manual: cd {NO5_DIR} && git add logs/rai-threat-library.jsonl && git commit -m '[cc:{datetime.date.today()}] threat library update' && git push")
    else:
        print("\nNothing promoted this session.")

def run_status():
    conn = sqlite3.connect(DB_PATH)
    candidate = conn.execute("SELECT COUNT(*) FROM patterns WHERE status='candidate'").fetchone()[0]
    approved  = conn.execute("SELECT COUNT(*) FROM patterns WHERE status='approved'").fetchone()[0]
    rejected  = conn.execute("SELECT COUNT(*) FROM patterns WHERE status='rejected'").fetchone()[0]
    max_tl = load_existing_max()
    print(f"\nRAI Threat Library — Status ({datetime.date.today()})")
    print(f"  Library promoted : {max_tl}/10 patterns")
    print(f"  DB candidate     : {candidate}")
    print(f"  DB approved      : {approved}")
    print(f"  DB rejected      : {rejected}")
    print(f"  JSONL path       : {LIBRARY_JSONL}")
    if os.path.exists(LIBRARY_JSONL):
        count = sum(1 for _ in open(LIBRARY_JSONL))
        print(f"  JSONL entries    : {count}")
    if approved > 0:
        rows = conn.execute(
            "SELECT id, name, rai_layer FROM patterns WHERE status='approved' ORDER BY reviewed_at"
        ).fetchall()
        print("\n  Promoted patterns:")
        for i, (pid, name, layer) in enumerate(rows, 1):
            print(f"    {i:2}. [{layer}] {name[:55]}")
    conn.close()

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="RAI POC #2 Pattern Promotion CLI")
    ap.add_argument("--status", action="store_true", help="Show library status")
    args = ap.parse_args()
    if args.status:
        run_status()
    else:
        run_review()
