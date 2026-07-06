#!/usr/bin/env python3
"""
build_artifact.py — Rebake the RAI Signal Viewer artifact with fresh data.
OL-325 | 2026-05-30

Reads logs/rai-poc2-signals.json + the HTML template, injects current data as the
embedded snapshot, writes the artifact to no5-context/assets, commits + pushes.
Run as the last step of each crawl so the artifact is always current when opened.

Usage: python3 build_artifact.py
"""
import os, json, subprocess, datetime

NO5_DIR        = "/home/tim/no5-context"
BASE_DIR       = "/home/tim/nanoclaw"
TEMPLATE       = f"{BASE_DIR}/rai-signal-viewer.template.html"
SIGNALS_JSON   = f"{NO5_DIR}/logs/rai-poc2-signals.json"
THREAT_LIB     = f"{NO5_DIR}/logs/rai-threat-library.jsonl"
OUT_HTML       = f"{NO5_DIR}/assets/rai-signal-viewer.html"
SIGNALS_URL    = os.environ.get("RAI_SIGNALS_URL", "")

def main():
    if not os.path.exists(TEMPLATE):
        print(f"Template missing: {TEMPLATE}")
        return
    if not os.path.exists(SIGNALS_JSON):
        print(f"Signals missing: {SIGNALS_JSON}")
        return
    tpl  = open(TEMPLATE).read()
    data = json.load(open(SIGNALS_JSON))
    # Inject threat_library from jsonl (overrides empty list in signals.json)
    if os.path.exists(THREAT_LIB):
        tlib = []
        with open(THREAT_LIB) as f:
            for line in f:
                line = line.strip()
                if line:
                    tlib.append(json.loads(line))
        data['threat_library'] = tlib
        print(f"Threat library: {len(tlib)} entries injected")
    embedded = json.dumps(data, separators=(",", ":"))
    html = tpl.replace("__EMBEDDED_DATA__", embedded)
    html = html.replace("__SIGNALS_URL__", SIGNALS_URL)
    os.makedirs(os.path.dirname(OUT_HTML), exist_ok=True)
    with open(OUT_HTML, "w") as f:
        f.write(html)
    print(f"Artifact rebaked: {OUT_HTML} ({len(html):,} bytes, "
          f"{len(data.get('patterns',[]))} patterns, "
          f"{len(data.get('threat_library',[]))} TL entries)")
    # Commit + push
    try:
        subprocess.run(["git","-C",NO5_DIR,"add","-f","assets/rai-signal-viewer.html"],
                       check=True, capture_output=True)
        res = subprocess.run(["git","-C",NO5_DIR,"commit","-m",
            f"[vps] rai signal viewer rebake {datetime.datetime.now().strftime('%Y-%m-%dT%H:%M')}"],
            capture_output=True)
        if res.returncode == 0:
            subprocess.run(["git","-C",NO5_DIR,"push"], check=True, capture_output=True)
            print("Artifact pushed to no5-context")
        else:
            print("Artifact: nothing new to commit")
    except Exception as e:
        print(f"git sync warning: {e}")

if __name__ == "__main__":
    main()
