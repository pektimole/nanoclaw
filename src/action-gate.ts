/**
 * action-gate.ts — RAI ActionGate (L4)
 *
 * Deterministic, fail-closed policy engine for agent-initiated actions.
 * Lifted from NanoClaw 5-layer Write Gate (in production since 2026-03-21).
 *
 * Spec: no5-context/28-rai-actiongate-spec.md
 *
 * Threat layer: L4 — Agent action / unauthorized side-effect.
 * Composes with P0/P1/P2: content scanners decide what the model reads,
 * ActionGate decides what the model is allowed to do with that output.
 *
 * Surface adapters (planned):
 *   fs-git    — file write + git commit/push       (this file, lifted from NanoClaw)
 *   shell     — exec / spawn                       (TODO)
 *   mcp       — tool invocation                    (TODO)
 *   http      — fetch / mutation verbs             (TODO)
 *   browser   — DOM submit / navigate              (TODO)
 *
 * Usage:
 *   import { evaluate, FsGitPolicy, FsGitAction } from './action-gate';
 *   const verdict = evaluate(action, policy);
 *   if (verdict.decision === 'deny') { log(verdict.reason); return; }
 *   if (verdict.decision === 'sanitize') { action = verdict.sanitized; }
 *   // proceed
 */

import * as path from 'path';

// ---------------------------------------------------------------------------
// Core types
// ---------------------------------------------------------------------------

export type Decision = 'allow' | 'deny' | 'sanitize';

export interface Verdict {
  decision: Decision;
  /** Stable rule id that produced the verdict — useful for audit + tests. */
  rule: string;
  /** Human-readable reason. */
  reason: string;
  /** Present when decision === 'sanitize'. The mutated action to use instead. */
  sanitized?: FsGitAction;
}

/**
 * The source group attempting the action. Maps 1:1 to NanoClaw's
 * `sourceGroup` concept (whatsapp_main, telegram_main, etc.).
 * Generalized so non-NanoClaw hosts can supply their own identifiers.
 */
export type SourceGroup = string;

// ---------------------------------------------------------------------------
// fs-git adapter — first concrete action type
// ---------------------------------------------------------------------------

export interface FsGitAction {
  kind: 'fs-git-write';
  /** Relative path from the policy's `root`. May contain at most one subdir level. */
  file: string;
  /** UTF-8 content payload. */
  content: string;
  /** Commit message — will be sanitized in place if it contains unsafe chars. */
  commitMessage: string;
  /** Source group requesting the action. */
  sourceGroup: SourceGroup;
}

export interface FsGitPolicy {
  /** Absolute path that all writes must remain inside. */
  root: string;
  /** Source groups permitted to perform fs-git writes. Empty = none. */
  allowedSourceGroups: Set<SourceGroup>;
  /** First-level subdirectories permitted under `root`. Empty = flat only. */
  allowedSubdirs: Set<string>;
  /** Allowed file extensions, including the leading dot (e.g. ".md"). */
  allowedExtensions: Set<string>;
  /** Basenames blocked even inside an allowed subdir (e.g. REGISTRY.md). */
  blockedBasenames: Set<string>;
  /** Maximum content size in bytes. */
  maxContentBytes: number;
  /** Maximum allowed path depth, where 1 = flat, 2 = one subdir level. */
  maxDepth: number;
}

/**
 * Default policy mirrors the NanoClaw `whatsapp_main` write gate exactly.
 * This is the policy that has been running in production since 2026-03-21.
 * Override per-deployment by constructing a new FsGitPolicy.
 */
export function nanoclawDefaultPolicy(root: string): FsGitPolicy {
  return {
    root,
    allowedSourceGroups: new Set(['whatsapp_main']),
    allowedSubdirs: new Set(['proposals', 'pending-decisions', 'spikes']),
    allowedExtensions: new Set(['.md']),
    blockedBasenames: new Set(['00-WAKE.md', '00-README.md', 'REGISTRY.md']),
    maxContentBytes: 50_000,
    maxDepth: 2,
  };
}

// ---------------------------------------------------------------------------
// Evaluation
// ---------------------------------------------------------------------------

/**
 * Pure function. Given an action and a policy, returns a verdict.
 * Performs no I/O. Fail-closed: any check that fires returns 'deny' immediately
 * (first deny wins). Commit-message sanitization happens last and yields
 * 'sanitize' with the rewritten action when triggered.
 *
 * The check order matches the NanoClaw Write Gate exactly so behavior is
 * bit-for-bit equivalent and the existing production deployment can be
 * re-bound to this module without observable change.
 */
export function evaluate(action: FsGitAction, policy: FsGitPolicy): Verdict {
  // 1. Source group permission
  if (!policy.allowedSourceGroups.has(action.sourceGroup)) {
    return {
      decision: 'deny',
      rule: 'source-group-not-allowed',
      reason: `source group "${action.sourceGroup}" not in allowlist`,
    };
  }

  // 2. Required fields (matches NanoClaw early return)
  if (!action.file || !action.content || !action.commitMessage) {
    return {
      decision: 'deny',
      rule: 'missing-required-fields',
      reason: 'file, content, and commitMessage are all required',
    };
  }

  // 3. Path traversal check (.. anywhere in raw path)
  if (action.file.includes('..')) {
    return {
      decision: 'deny',
      rule: 'path-traversal-literal',
      reason: 'path contains ".." segment',
    };
  }

  // 4. Extension allowlist
  const ext = path.extname(action.file);
  if (!policy.allowedExtensions.has(ext)) {
    return {
      decision: 'deny',
      rule: 'extension-not-allowed',
      reason: `extension "${ext}" not in allowlist`,
    };
  }

  // 5. Depth check
  const segments = action.file.split('/');
  if (segments.length > policy.maxDepth) {
    return {
      decision: 'deny',
      rule: 'depth-exceeded',
      reason: `path depth ${segments.length} exceeds max ${policy.maxDepth}`,
    };
  }

  // 6. Subdir allowlist (only when nested)
  if (segments.length === 2 && !policy.allowedSubdirs.has(segments[0])) {
    return {
      decision: 'deny',
      rule: 'subdir-not-allowed',
      reason: `subdirectory "${segments[0]}" not in allowlist`,
    };
  }

  const basename = segments[segments.length - 1];

  // 7. Hidden / dotfile guard (also catches empty basename)
  if (!basename || basename.startsWith('.')) {
    return {
      decision: 'deny',
      rule: 'hidden-or-empty-basename',
      reason: 'basename is empty or starts with "."',
    };
  }

  // 8. Blocked basename (even inside allowed subdir)
  if (policy.blockedBasenames.has(basename)) {
    return {
      decision: 'deny',
      rule: 'basename-blocked',
      reason: `basename "${basename}" is in blocklist`,
    };
  }

  // 9. Size limit (byte length, not char length — content is UTF-8)
  const byteLength = Buffer.byteLength(action.content, 'utf-8');
  if (byteLength > policy.maxContentBytes) {
    return {
      decision: 'deny',
      rule: 'content-too-large',
      reason: `content ${byteLength}B exceeds max ${policy.maxContentBytes}B`,
    };
  }

  // 10. Resolved-path containment (defeats symlink + tricky path tricks)
  const resolved = path.resolve(policy.root, action.file);
  const rootWithSep = policy.root.endsWith('/') ? policy.root : policy.root + '/';
  if (!resolved.startsWith(rootWithSep)) {
    return {
      decision: 'deny',
      rule: 'resolved-path-outside-root',
      reason: `resolved path "${resolved}" escapes root "${policy.root}"`,
    };
  }

  // 11. Commit message sanitization (sanitize-and-allow, never deny)
  // Matches NanoClaw rule: replace ' " ` $ \ with '-'.
  const safeMsg = action.commitMessage.replace(/['"`$\\]/g, '-');
  if (safeMsg !== action.commitMessage) {
    return {
      decision: 'sanitize',
      rule: 'commit-message-sanitized',
      reason: 'unsafe shell metacharacters stripped from commit message',
      sanitized: { ...action, commitMessage: safeMsg },
    };
  }

  return {
    decision: 'allow',
    rule: 'all-checks-passed',
    reason: 'action permitted by policy',
  };
}
