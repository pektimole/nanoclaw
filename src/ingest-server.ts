/**
 * ingest-server.ts — RAI cross-surface scan event ingest endpoint
 *
 * Receives scan verdicts from remote surfaces (browser extension, mobile PWA)
 * and writes them to the local ScanLog so dream-phase can merge across surfaces.
 *
 * Phase 1: Tim-only. Single bearer token auth via RAI_INGEST_TOKEN env var.
 * Phase 2 will add: multi-tenant tokens, rate limiting, differential privacy.
 *
 * Routes:
 *   POST /ingest/scan-event  — accept a ScanLogEntry, 204 on success
 *   GET  /ingest/health      — liveness check, 200 JSON
 *
 * Environment:
 *   RAI_INGEST_TOKEN  — required bearer token (if empty, auth is disabled)
 *   RAI_INGEST_PORT   — TCP port (default 3847)
 */

import * as http from 'http';
import {
  getDefaultScanLog,
  type ScanLog,
  type ScanLogEntry,
} from './scan-log.js';

// ---------------------------------------------------------------------------
// Payload validation
// ---------------------------------------------------------------------------

function validateEntry(body: unknown): ScanLogEntry | null {
  if (typeof body !== 'object' || body === null) return null;
  const e = body as Record<string, unknown>;
  if (typeof e.timestamp !== 'string') return null;
  if (typeof e.scan_id !== 'string') return null;
  if (!['p0', 'p1', 'p2'].includes(e.tier as string)) return null;
  if (typeof e.channel !== 'string') return null;
  if (typeof e.verdict !== 'string') return null;
  if (typeof e.confidence !== 'number') return null;
  if (typeof e.recommended_action !== 'string') return null;
  if (!Array.isArray(e.threat_layers)) return null;
  return e as unknown as ScanLogEntry;
}

// ---------------------------------------------------------------------------
// Server factory
// ---------------------------------------------------------------------------

export function createIngestServer(
  scanLog?: ScanLog,
  token?: string,
): http.Server {
  const log = scanLog ?? getDefaultScanLog();
  const tok =
    token !== undefined ? token : (process.env.RAI_INGEST_TOKEN ?? '');

  return http.createServer((req, res) => {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader(
      'Access-Control-Allow-Headers',
      'Content-Type, Authorization',
    );
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');

    if (req.method === 'OPTIONS') {
      res.writeHead(204);
      res.end();
      return;
    }

    if (req.url === '/ingest/health') {
      if (req.method !== 'GET') {
        res.writeHead(405);
        res.end();
        return;
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok', ts: new Date().toISOString() }));
      return;
    }

    if (req.url !== '/ingest/scan-event') {
      res.writeHead(404);
      res.end();
      return;
    }

    if (req.method !== 'POST') {
      res.writeHead(405);
      res.end();
      return;
    }

    // Bearer token auth (skipped when tok is empty, e.g. in tests)
    if (tok) {
      const authHeader = req.headers['authorization'] ?? '';
      if (authHeader !== `Bearer ${tok}`) {
        res.writeHead(401);
        res.end();
        return;
      }
    }

    let body = '';
    req.on('data', (chunk: Buffer) => {
      body += chunk.toString();
    });
    req.on('end', () => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(body);
      } catch {
        res.writeHead(400);
        res.end();
        return;
      }
      const entry = validateEntry(parsed);
      if (!entry) {
        res.writeHead(400);
        res.end();
        return;
      }
      log.logScan(entry);
      res.writeHead(204);
      res.end();
    });
  });
}

// ---------------------------------------------------------------------------
// CLI entry — npx tsx packages/core/ingest-server.ts
// ---------------------------------------------------------------------------

const runningAsCli =
  typeof process !== 'undefined' &&
  process.argv[1] != null &&
  (process.argv[1].endsWith('ingest-server.ts') ||
    process.argv[1].endsWith('ingest-server.js'));

if (runningAsCli) {
  const port = parseInt(process.env.RAI_INGEST_PORT ?? '3847', 10);
  const server = createIngestServer();
  server.listen(port, () => {
    process.stdout.write(`[rai-ingest] listening on port ${port}\n`);
  });
  process.on('SIGTERM', () => server.close());
  process.on('SIGINT', () => server.close());
}
