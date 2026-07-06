#!/usr/bin/env node
/**
 * vps-runner.mjs — standalone red-team runner for NanoClaw VPS.
 *
 * Imports NanoClaw's deployed scanners directly (no workspace deps).
 * Loads payload bundle from same directory, runs suite, writes
 * corrections to scan-log at 0.5x weight on divergence.
 *
 * Expected layout on VPS:
 *   /home/tim/nanoclaw/dist/ray-scan.js        (NanoClaw's compiled P0)
 *   /home/tim/nanoclaw/dist/rai-scan-p1.js     (NanoClaw's compiled P1)
 *   /home/tim/nanoclaw/dist/scan-log.js        (NanoClaw's compiled scan-log)
 *   /home/tim/nanoclaw/redteam/vps-runner.mjs  (this file)
 *   /home/tim/nanoclaw/redteam/payloads.json   (bundled payloads)
 *
 * Usage (VPS):
 *   node /home/tim/nanoclaw/redteam/vps-runner.mjs [--p1] [--write-corrections]
 *
 * Exit code: 0 on suite pass, 1 on any failure, 2 on runner error.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DIST = path.resolve(__dirname, '..', 'dist');

// ---- Dynamic imports from NanoClaw's compiled dist ----
const { rayScan } = await import(path.join(DIST, 'ray-scan.js'));
const { scanP1, shouldEscalateToP1 } = await import(path.join(DIST, 'rai-scan-p1.js'));
const { ScanLog } = await import(path.join(DIST, 'scan-log.js'));

// ---- Config from CLI args ----
const args = new Set(process.argv.slice(2));
const enableP1 = args.has('--p1');
const writeCorrections = args.has('--write-corrections');
const jsonOutput = args.has('--json');

// ---- Load bundled payloads ----
const bundlePath = path.join(__dirname, 'payloads.json');
if (!fs.existsSync(bundlePath)) {
  console.error(`[redteam] payloads bundle not found at ${bundlePath}`);
  process.exit(2);
}
const { payloads, generated_at, count } = JSON.parse(fs.readFileSync(bundlePath, 'utf-8'));

if (!jsonOutput) {
  console.log(
    `[redteam] VPS suite starting: ${count} payloads (bundle from ${generated_at}), P1=${enableP1 ? 'on' : 'off'}${writeCorrections ? ', write-corrections=on' : ''}`,
  );
}

// ---- Run suite ----
const sessionId = `redteam-vps-${randomUUID()}`;
const results = [];
const runStart = Date.now();

for (const p of payloads) {
  const startMs = Date.now();

  const p0Result = await rayScan({
    source: { channel: 'artifact', pipeline_stage: 'ingest', is_forward: true },
    payload: { type: 'text', content: p.payload },
    context: { session_id: sessionId, host_environment: 'api' },
  });

  let actualVerdict = p0Result.verdict;
  let actualLabels = p0Result.threat_layers.map((t) => t.label);
  let tierUsed = 'p0';
  const scanId = p0Result.scan_id;

  const wantsP1 = enableP1 && (p.p1_required === true || shouldEscalateToP1(p0Result.verdict, p0Result.confidence));
  if (wantsP1) {
    try {
      const p1Result = await scanP1({
        scan_id: p0Result.scan_id,
        timestamp: new Date().toISOString(),
        source: { channel: 'artifact', pipeline_stage: 'ingest', sender: null, origin_url: null, is_forward: true },
        payload: { type: 'text', content: p.payload },
        context: { session_id: sessionId, prior_scan_ids: [], host_environment: 'api' },
        p0_result: {
          verdict: p0Result.verdict,
          matched_patterns: p0Result.threat_layers.map((t) => t.label),
          confidence: p0Result.confidence,
        },
      });
      const sev = { clean: 0, flagged: 1, blocked: 2 };
      if (sev[p1Result.verdict] > sev[actualVerdict]) actualVerdict = p1Result.verdict;
      actualLabels = [...new Set([...actualLabels, ...p1Result.threat_layers.map((t) => t.label)])];
      tierUsed = 'p0+p1';
    } catch (err) {
      // fail-open
    }
  }

  const verdictPass = actualVerdict === p.expected_verdict;
  const labelMissed = p.expected_label !== undefined && !actualLabels.some((l) => l === p.expected_label);
  const pass = verdictPass && !labelMissed;

  if (!pass && writeCorrections) {
    try {
      new ScanLog().logCorrection({
        timestamp: new Date().toISOString(),
        scan_id: scanId,
        corrected_tier: tierUsed === 'p0+p1' ? 'p1' : 'p0',
        corrected_verdict: p.expected_verdict,
        correction_source: 'manual',
        reason: `redteam-vps synthetic: payload ${p.id} expected ${p.expected_verdict}, got ${actualVerdict}`,
        sample_weight: 0.5,
      });
    } catch {
      // Never block on scan-log write
    }
  }

  results.push({
    payload_id: p.id,
    layer: p.layer,
    expected_verdict: p.expected_verdict,
    actual_verdict: actualVerdict,
    actual_labels: actualLabels,
    tier_used: tierUsed,
    pass,
    label_missed: labelMissed,
    latency_ms: Date.now() - startMs,
    scan_id: scanId,
  });
}

// ---- Report ----
const passed = results.filter((r) => r.pass).length;
const failed = results.length - passed;
const avgLatency = results.reduce((s, r) => s + r.latency_ms, 0) / Math.max(1, results.length);
const totalDuration = Date.now() - runStart;

const report = {
  timestamp: new Date().toISOString(),
  host: 'nanoclaw-vps',
  total: results.length,
  passed,
  failed,
  avg_latency_ms: Number(avgLatency.toFixed(2)),
  total_duration_ms: totalDuration,
  failures: results.filter((r) => !r.pass),
};

// Also append to a daily report log for dashboards
const logDir = path.join(process.env.HOME || '/tmp', '.rai', 'redteam-runs');
fs.mkdirSync(logDir, { recursive: true });
const logFile = path.join(logDir, `${new Date().toISOString().slice(0, 10)}.jsonl`);
fs.appendFileSync(logFile, JSON.stringify(report) + '\n');

if (jsonOutput) {
  console.log(JSON.stringify(report, null, 2));
} else {
  const pct = ((passed / results.length) * 100).toFixed(1);
  console.log(`[redteam] ${passed}/${results.length} passed (${pct}%), ${avgLatency.toFixed(1)}ms avg, ${totalDuration}ms total`);
  if (failed > 0) {
    console.log(`[redteam] failures (${failed}):`);
    for (const f of report.failures) {
      const reason = f.label_missed
        ? `expected label not found in [${f.actual_labels.join(', ')}]`
        : `expected "${f.expected_verdict}", got "${f.actual_verdict}"`;
      console.log(`  ✗ ${f.payload_id} (${f.layer}): ${reason}`);
    }
  }
  console.log(`[redteam] report appended to ${logFile}`);
}

process.exit(failed === 0 ? 0 : 1);
