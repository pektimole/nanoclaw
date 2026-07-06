/**
 * scan-log.ts — RAI scan verdict log
 *
 * Append-only JSONL log of all scan verdicts across P0/P1/P2.
 * This is the training data source for the Phantom retrain loop.
 *
 * Each entry records the original verdict, and optionally a correction
 * (user override or cross-tier contradiction) that serves as a labeled
 * training sample for weight adjustment.
 *
 * Storage: ~/.rai/scan-log/rai-scans.jsonl
 */

import * as fs from 'fs';
import * as path from 'path';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ScanTier = 'p0' | 'p1' | 'p2';
export type CorrectionSource = 'user_override' | 'cross_tier' | 'manual';

export interface ScanLogEntry {
  timestamp: string;
  scan_id: string;
  tier: ScanTier;
  channel: string;
  verdict: string;
  confidence: number;
  recommended_action: string;
  threat_layers: Array<{
    layer: string;
    label: string;
    severity: string;
  }>;
  /** Pattern labels that matched (P0 only). */
  matched_patterns?: string[];
  /** P1 latency in ms, if applicable. */
  latency_ms?: number;
  /** Agent verdicts (P2 only). */
  agent_verdicts?: Array<{
    agent: string;
    verdict: string;
    confidence: number;
  }>;
  /** Which surface produced this scan event (nanoclaw | browser_extension | mobile_pwa) */
  surface?: string;
}

export interface CorrectionEntry {
  timestamp: string;
  scan_id: string;
  /** Which tier's verdict was corrected. */
  corrected_tier: ScanTier;
  /** What the user/system says the correct verdict should be. */
  corrected_verdict: string;
  /** Who/what generated the correction. */
  correction_source: CorrectionSource;
  /** Which tier contradicted (for cross_tier corrections). */
  contradicting_tier?: ScanTier;
  /** Free-text reason from user override. */
  reason?: string;
  /** Weight multiplier for training (default 1.0, user overrides get 3.0). */
  sample_weight: number;
}

// ---------------------------------------------------------------------------
// Logger
// ---------------------------------------------------------------------------

export class ScanLog {
  private scanLogPath: string;
  private correctionLogPath: string;
  private dirEnsured = false;

  constructor(logDir?: string) {
    const dir = logDir ?? defaultLogDir();
    this.scanLogPath = path.join(dir, 'rai-scans.jsonl');
    this.correctionLogPath = path.join(dir, 'rai-corrections.jsonl');
  }

  private ensureDir(): void {
    if (!this.dirEnsured) {
      fs.mkdirSync(path.dirname(this.scanLogPath), { recursive: true });
      this.dirEnsured = true;
    }
  }

  /** Log a scan verdict. */
  logScan(entry: ScanLogEntry): void {
    this.ensureDir();
    fs.appendFileSync(this.scanLogPath, JSON.stringify(entry) + '\n');
  }

  /** Log a correction (user override or cross-tier contradiction). */
  logCorrection(entry: CorrectionEntry): void {
    this.ensureDir();
    fs.appendFileSync(this.correctionLogPath, JSON.stringify(entry) + '\n');
  }

  /** Read all scan entries. */
  readScans(): ScanLogEntry[] {
    if (!fs.existsSync(this.scanLogPath)) return [];
    return fs
      .readFileSync(this.scanLogPath, 'utf-8')
      .split('\n')
      .filter((line) => line.trim())
      .map((line) => JSON.parse(line) as ScanLogEntry);
  }

  /** Read all correction entries. */
  readCorrections(): CorrectionEntry[] {
    if (!fs.existsSync(this.correctionLogPath)) return [];
    return fs
      .readFileSync(this.correctionLogPath, 'utf-8')
      .split('\n')
      .filter((line) => line.trim())
      .map((line) => JSON.parse(line) as CorrectionEntry);
  }

  /** Get corrections for a specific scan_id. */
  getCorrectionsForScan(scanId: string): CorrectionEntry[] {
    return this.readCorrections().filter((c) => c.scan_id === scanId);
  }

  /** Count total scans and corrections (for retrain trigger logic). */
  stats(): { scans: number; corrections: number } {
    return {
      scans: this.readScans().length,
      corrections: this.readCorrections().length,
    };
  }

  getScanLogPath(): string {
    return this.scanLogPath;
  }
  getCorrectionLogPath(): string {
    return this.correctionLogPath;
  }
}

// ---------------------------------------------------------------------------
// Default location
// ---------------------------------------------------------------------------

function defaultLogDir(): string {
  const home = process.env.HOME || process.env.USERPROFILE || '/tmp';
  return path.join(home, '.rai', 'scan-log');
}

// ---------------------------------------------------------------------------
// Singleton
// ---------------------------------------------------------------------------

let _default: ScanLog | null = null;

export function getDefaultScanLog(): ScanLog {
  if (!_default) _default = new ScanLog();
  return _default;
}
