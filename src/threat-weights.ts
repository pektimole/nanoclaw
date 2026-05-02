/**
 * threat-weights.ts — RAI Phantom weight loader
 *
 * Loads adaptive weights from JSON files. All scan tiers (P0, P1, P2)
 * read from these files at startup. The Phantom retrain loop writes
 * updated weights back to the same files.
 *
 * Weight files: packages/core/weights/{p0,p1,p2}-weights.json
 * User overrides: ~/.rai/weights/{p0,p1,p2}-weights.json (takes precedence)
 */

import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface WeightMeta {
  version: number;
  updated: string;
  source: string;
}

export interface P0Weights {
  _meta: WeightMeta;
  severity_overrides: Record<string, 'low' | 'medium' | 'high' | 'critical'>;
  verdict_thresholds: {
    block_confidence: number;
    block_confidence_infra: number;
    flag_confidence_high: number;
    flag_confidence_low: number;
    clean_confidence: number;
  };
  pattern_weights: Record<string, number>;
}

export interface P1Weights {
  _meta: WeightMeta;
  escalation_threshold: number;
  p0_trigger_threshold: number;
  layer_severity_weights: Record<
    string,
    {
      base_weight: number;
      critical_multiplier: number;
    }
  >;
}

export interface P2AgentWeight {
  provenance: number;
  'cross-ref': number;
  temporal: number;
  credibility: number;
}

export interface P2Weights {
  _meta: WeightMeta;
  agent_weights: P2AgentWeight;
  consensus_thresholds: {
    confirmed_threat_min_supporting: number;
    likely_threat_min_supporting: number;
    false_positive_min_contradicting: number;
    likely_safe_min_contradicting: number;
    human_review_min_uncertain: number;
  };
  credibility_index: Record<string, { tier: string; weight: number }>;
  p2_trigger: {
    p1_confidence_threshold: number;
    trigger_layers: string[];
  };
}

// ---------------------------------------------------------------------------
// Loader
// ---------------------------------------------------------------------------

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BUNDLED_DIR = path.join(__dirname, 'weights');

function userWeightsDir(): string {
  const home = process.env.HOME || process.env.USERPROFILE || '/tmp';
  return path.join(home, '.rai', 'weights');
}

function loadJSON<T>(filename: string): T {
  // User override takes precedence
  const userPath = path.join(userWeightsDir(), filename);
  if (fs.existsSync(userPath)) {
    return JSON.parse(fs.readFileSync(userPath, 'utf-8')) as T;
  }
  // Fall back to bundled defaults
  const bundledPath = path.join(BUNDLED_DIR, filename);
  return JSON.parse(fs.readFileSync(bundledPath, 'utf-8')) as T;
}

function writeJSON<T>(filename: string, data: T): void {
  const dir = userWeightsDir();
  fs.mkdirSync(dir, { recursive: true });
  const filePath = path.join(dir, filename);
  fs.writeFileSync(filePath, JSON.stringify(data, null, 2) + '\n');
}

// Cached instances (reload on demand)
let _p0: P0Weights | null = null;
let _p1: P1Weights | null = null;
let _p2: P2Weights | null = null;

export function loadP0Weights(forceReload = false): P0Weights {
  if (!_p0 || forceReload) _p0 = loadJSON<P0Weights>('p0-weights.json');
  return _p0;
}

export function loadP1Weights(forceReload = false): P1Weights {
  if (!_p1 || forceReload) _p1 = loadJSON<P1Weights>('p1-weights.json');
  return _p1;
}

export function loadP2Weights(forceReload = false): P2Weights {
  if (!_p2 || forceReload) _p2 = loadJSON<P2Weights>('p2-weights.json');
  return _p2;
}

export function saveP0Weights(weights: P0Weights): void {
  weights._meta.updated = new Date().toISOString();
  weights._meta.version += 1;
  writeJSON('p0-weights.json', weights);
  _p0 = weights;
}

export function saveP1Weights(weights: P1Weights): void {
  weights._meta.updated = new Date().toISOString();
  weights._meta.version += 1;
  writeJSON('p1-weights.json', weights);
  _p1 = weights;
}

export function saveP2Weights(weights: P2Weights): void {
  weights._meta.updated = new Date().toISOString();
  weights._meta.version += 1;
  writeJSON('p2-weights.json', weights);
  _p2 = weights;
}

/**
 * Invalidate all cached weights. Next load will re-read from disk.
 */
export function invalidateCache(): void {
  _p0 = null;
  _p1 = null;
  _p2 = null;
}
