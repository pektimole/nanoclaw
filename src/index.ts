import fs from 'fs';
import path from 'path';
import { spawn } from 'child_process';

import {
  ASSISTANT_NAME,
  CREDENTIAL_PROXY_PORT,
  IDLE_TIMEOUT,
  POLL_INTERVAL,
  TIMEZONE,
  TRIGGER_PATTERN,
} from './config.js';
import { startCredentialProxy } from './credential-proxy.js';
import './channels/index.js';
import {
  getChannelFactory,
  getRegisteredChannelNames,
} from './channels/registry.js';
import {
  ContainerOutput,
  runContainerAgent,
  writeGroupsSnapshot,
  writeTasksSnapshot,
} from './container-runner.js';
import {
  cleanupOrphans,
  ensureContainerRuntimeRunning,
  PROXY_BIND_HOST,
} from './container-runtime.js';
import {
  getAllChats,
  getAllRegisteredGroups,
  getAllSessions,
  getAllTasks,
  getMessagesSince,
  getNewMessages,
  getRegisteredGroup,
  getRouterState,
  initDatabase,
  setRegisteredGroup,
  setRouterState,
  setSession,
  storeChatMetadata,
  storeMessage,
} from './db.js';
import { GroupQueue } from './group-queue.js';
import { resolveGroupFolderPath } from './group-folder.js';
import { startIpcWatcher } from './ipc.js';
import { findChannel, formatMessages, formatOutbound } from './router.js';
import {
  restoreRemoteControl,
  startRemoteControl,
  stopRemoteControl,
} from './remote-control.js';
import {
  isSenderAllowed,
  isTriggerAllowed,
  loadSenderAllowlist,
  shouldDropMessage,
} from './sender-allowlist.js';
import { startSchedulerLoop } from './task-scheduler.js';
import { Channel, NewMessage, RegisteredGroup } from './types.js';
import { logger } from './logger.js';
import {
  rayCheck,
  blockedNotification,
  type Channel as RayChannel,
  type RayScanOutput,
} from './ray-scan.js';
import {
  shouldEscalateToP1,
  runP1Async,
  scanP1,
  type ScanInput,
} from './rai-scan-p1.js';

// Re-export for backwards compatibility during refactor
export { escapeXml, formatMessages } from './router.js';

/** Kill any process holding a port before we try to bind it.
 *  Prevents EADDRINUSE when a previous instance didn't release the port.
 */
async function freePort(port: number): Promise<void> {
  const { execSync } = await import('child_process');
  try {
    const result = execSync(`lsof -ti tcp:${port} 2>/dev/null || true`, {
      encoding: 'utf8',
    }).trim();
    if (result) {
      const pids = result.split('\n').filter(Boolean);
      for (const pid of pids) {
        try {
          process.kill(parseInt(pid, 10), 'SIGKILL');
          logger.warn({ port, pid }, 'Killed stale process holding port');
        } catch {
          // already dead
        }
      }
      // brief wait for OS to release the port
      await new Promise((r) => setTimeout(r, 300));
    }
  } catch {
    // lsof not available or nothing to kill — proceed
  }
}

let lastTimestamp = '';
let sessions: Record<string, string> = {};

// ── Model Triage ────────────────────────────────────────────────────────────
// Default: Haiku. Escalate to Sonnet on complexity signals or explicit trigger.
const MODEL_HAIKU = 'claude-haiku-4-5-20251001';
const MODEL_SONNET = 'claude-sonnet-4-6';
const DEEP_TRIGGER = /^!deep/i;
const HAIKU_TRIGGER = /^!haiku\b/i;
const SONNET_KEYWORDS =
  /(\bdraft\b|\bspec\b|\brefactor\b|\bdebug\b|\bimplement\b|\barchitect\b|(?:^|\s)review\s|explain\s+(?:this\s+)?(?:code|system|architecture)|create\s+(?:a\s+)?(?:script|file|function|app))/i;
const MIN_LENGTH_FOR_SONNET = 900; // raised from 500 — 500 chars = 2-3 sentences

const VOICE_PREFIX = /^\[Voice →/;

function triageModel(prompt: string): string {
  // Explicit trigger: !deep at start of message — always Sonnet
  if (DEEP_TRIGGER.test(prompt.trim())) return MODEL_SONNET;
  if (HAIKU_TRIGGER.test(prompt.trim())) return MODEL_HAIKU; // force downgrade
  // Voice transcripts: always Haiku (spoken language is verbose, length check
  // would incorrectly escalate to Sonnet — voice rarely needs deep reasoning)
  if (VOICE_PREFIX.test(prompt.trim())) return MODEL_HAIKU;
  // Long messages likely need deeper reasoning
  if (prompt.length > MIN_LENGTH_FOR_SONNET) return MODEL_SONNET;
  // Complexity keywords
  if (SONNET_KEYWORDS.test(prompt)) return MODEL_SONNET;
  // Default: Haiku
  return MODEL_HAIKU;
}
// ────────────────────────────────────────────────────────────────────────────

let registeredGroups: Record<string, RegisteredGroup> = {};
let lastAgentTimestamp: Record<string, string> = {};
let messageLoopRunning = false;
let raiEnabled = true; // Toggle via /rai off / !rai on from trusted principal
let raiTestMode = false; // When true, scan Tim's own messages too
let raiScanNext = false; // One-shot: scan the next own message, then auto-reset

const channels: Channel[] = [];
const queue = new GroupQueue();

/** Map a NanoClaw JID to a Ray channel type. */
function rayChannelFromJid(jid: string): RayChannel {
  if (jid.startsWith('tg:')) return 'telegram';
  if (jid.startsWith('slack:')) return 'slack';
  if (jid.startsWith('discord:')) return 'discord';
  return 'whatsapp';
}

/** Append a scan result to the rolling Ray scan log. */
function logRayScan(result: RayScanOutput, channel: string): void {
  try {
    const entry = { ...result, channel, logged_at: new Date().toISOString() };
    fs.appendFileSync(
      path.join(process.cwd(), 'logs', 'ray-scan.log'),
      JSON.stringify(entry) + '\n',
    );
  } catch {
    // Best-effort logging — don't crash the message pipeline
  }
}

function loadState(): void {
  lastTimestamp = getRouterState('last_timestamp') || '';
  const agentTs = getRouterState('last_agent_timestamp');
  try {
    lastAgentTimestamp = agentTs ? JSON.parse(agentTs) : {};
  } catch {
    logger.warn('Corrupted last_agent_timestamp in DB, resetting');
    lastAgentTimestamp = {};
  }
  sessions = getAllSessions();
  registeredGroups = getAllRegisteredGroups();
  logger.info(
    { groupCount: Object.keys(registeredGroups).length },
    'State loaded',
  );
}

function saveState(): void {
  setRouterState('last_timestamp', lastTimestamp);
  setRouterState('last_agent_timestamp', JSON.stringify(lastAgentTimestamp));
}

function registerGroup(jid: string, group: RegisteredGroup): void {
  let groupDir: string;
  try {
    groupDir = resolveGroupFolderPath(group.folder);
  } catch (err) {
    logger.warn(
      { jid, folder: group.folder, err },
      'Rejecting group registration with invalid folder',
    );
    return;
  }

  registeredGroups[jid] = group;
  setRegisteredGroup(jid, group);

  // Create group folder
  fs.mkdirSync(path.join(groupDir, 'logs'), { recursive: true });

  logger.info(
    { jid, name: group.name, folder: group.folder },
    'Group registered',
  );
}

/**
 * Get available groups list for the agent.
 * Returns groups ordered by most recent activity.
 */
export function getAvailableGroups(): import('./container-runner.js').AvailableGroup[] {
  const chats = getAllChats();
  const registeredJids = new Set(Object.keys(registeredGroups));

  return chats
    .filter((c) => c.jid !== '__group_sync__' && c.is_group)
    .map((c) => ({
      jid: c.jid,
      name: c.name,
      lastActivity: c.last_message_time,
      isRegistered: registeredJids.has(c.jid),
    }));
}

/** @internal - exported for testing */
export function _setRegisteredGroups(
  groups: Record<string, RegisteredGroup>,
): void {
  registeredGroups = groups;
}

/**
 * Process all pending messages for a group.
 * Called by the GroupQueue when it's this group's turn.
 */

// ── Ambient Feed Revert Handlers ────────────────────────────────────────────
const REVERT_PATTERN = /^revert\s+(OL-\d+)/i;
const PROPOSAL_REVERT_PATTERN = /^revert\s+(\d+)\s*$/i;
// Explicit positive signal: "+1", "+1,2", "+1 3" — boosts Phantom weight for those proposals.
const PROPOSAL_BOOST_PATTERN = /^\+\s*(\d+(?:\s*[,\s]\s*\d+)*)\s*$/;
const NO5_CONTEXT_DIR = '/home/tim/no5-context';
const RANKING_HISTORY_FILE = path.join(
  NO5_CONTEXT_DIR,
  'logs',
  'ambient-feed-ranking-history.jsonl',
);

async function handleRevertOL(
  olId: string,
  chatJid: string,
  channel: Channel,
): Promise<boolean> {
  const olFile = path.join(NO5_CONTEXT_DIR, '02-open-loops.md');
  try {
    let content = fs.readFileSync(olFile, 'utf-8');
    // Find the line with this OL ID and [auto:] tag
    const escapedId = olId.replace(/[-]/g, '\\-');
    const regex = new RegExp(
      '^\\|\\s*' + escapedId + '\\s*\\|.*\\[auto:.*$',
      'm',
    );
    const match = content.match(regex);
    if (!match) {
      await channel.sendMessage(
        chatJid,
        `${olId} not found or not an auto-generated OL.`,
      );
      return true;
    }
    // Strike it through
    const original = match[0];
    const struck = original
      .replace(`| ${olId} |`, `| ~~${olId}~~ |`)
      .replace(/\| Evaluate \| Medium \|$/, '| ~~Reverted~~ | ~~Done~~ |');
    content = content.replace(original, struck);
    fs.writeFileSync(olFile, content);

    // Git commit
    const { execSync } = await import('child_process');
    execSync(
      `cd ${NO5_CONTEXT_DIR} && git add 02-open-loops.md && git commit -m "ambient-feed: reverted ${olId} [manual]" && git push`,
      { timeout: 30000 },
    );

    await channel.sendMessage(
      chatJid,
      `${olId} reverted and struck from open loops.`,
    );
    logger.info({ olId }, 'Auto-OL reverted via WhatsApp');
    return true;
  } catch (err) {
    logger.error({ olId, err }, 'Failed to revert OL');
    await channel.sendMessage(
      chatJid,
      `Failed to revert ${olId}: ${err instanceof Error ? err.message : String(err)}`,
    );
    return true;
  }
}

// ── Proposal Revert Handler (Level 3 ranking feedback) ──────────────────────
async function handleRevertProposal(
  proposalId: number,
  chatJid: string,
  channel: Channel,
): Promise<boolean> {
  const { execSync } = await import('child_process');

  // 1. Load delivered proposals to get title + affects_ols
  const proposals = getDeliveredProposals();
  const proposal = proposals.find((p) => p.id === proposalId);
  const title = proposal?.title?.slice(0, 60) || `proposal ${proposalId}`;

  // 2. Find the auto-exec commit via git log
  const today = new Date().toISOString().slice(0, 10);
  // Check today and yesterday (digest may have been delivered yesterday)
  const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  let commitHash = '';
  let artifactFile = '';

  for (const date of [today, yesterday]) {
    try {
      const log = execSync(
        `cd ${NO5_CONTEXT_DIR} && git log --oneline --all --grep="auto-exec proposal ${proposalId} \\[${date}\\]" --format="%H" -1`,
        { timeout: 10000, encoding: 'utf-8' },
      ).trim();
      if (log) {
        commitHash = log;
        const stat = execSync(
          `cd ${NO5_CONTEXT_DIR} && git show --stat --format="" ${commitHash}`,
          { timeout: 10000, encoding: 'utf-8' },
        ).trim();
        // Parse " spikes/foo.md | 102 +++" → "spikes/foo.md"
        const fileMatch = stat.match(/^\s*(\S+\.md)\s*\|/m);
        if (fileMatch) artifactFile = fileMatch[1];
        break;
      }
    } catch {
      continue;
    }
  }

  if (!commitHash) {
    await channel.sendMessage(
      chatJid,
      `Proposal ${proposalId} auto-exec commit not found in git log (checked ${today} + ${yesterday}).`,
    );
    return true;
  }

  try {
    // 3. Git revert the commit (creates a new revert commit, non-destructive)
    execSync(
      `cd ${NO5_CONTEXT_DIR} && git revert --no-edit ${commitHash} && git push`,
      { timeout: 30000 },
    );

    // 4. Append reverted record to ranking history
    const record = JSON.stringify({
      kind: 'revert',
      ts: new Date().toISOString(),
      proposal_id: proposalId,
      title,
      commit_reverted: commitHash,
      artifact_file: artifactFile,
      affects_ols: proposal?.affects_ols || [],
      sample_weight: 1.5,
    });
    fs.appendFileSync(RANKING_HISTORY_FILE, record + '\n');

    // 5. Mark existing delivery record as reverted in history
    if (fs.existsSync(RANKING_HISTORY_FILE)) {
      const lines = fs.readFileSync(RANKING_HISTORY_FILE, 'utf-8').split('\n');
      // Only mark the most recent delivery with this proposal_id as reverted
      // (scan backwards, mark first match only)
      let marked = false;
      const updated = [...lines];
      for (let i = updated.length - 1; i >= 0; i--) {
        if (!updated[i].trim() || marked) continue;
        try {
          const r = JSON.parse(updated[i]);
          if (
            r.kind === 'delivery' &&
            r.proposal_id === proposalId &&
            !r.reverted
          ) {
            r.reverted = true;
            r.reverted_at = new Date().toISOString();
            updated[i] = JSON.stringify(r);
            marked = true;
          }
        } catch {
          /* keep line as-is */
        }
      }
      fs.writeFileSync(RANKING_HISTORY_FILE, updated.join('\n'));
    }

    await channel.sendMessage(
      chatJid,
      `Reverted proposal ${proposalId}: "${title}"\n` +
        (artifactFile ? `Artifact removed: ${artifactFile}\n` : '') +
        `Ranking history updated (negative training signal recorded).`,
    );
    logger.info(
      { proposalId, commitHash, artifactFile },
      'Proposal reverted via WhatsApp',
    );
    return true;
  } catch (err) {
    logger.error({ proposalId, commitHash, err }, 'Failed to revert proposal');
    await channel.sendMessage(
      chatJid,
      `Failed to revert proposal ${proposalId}: ${err instanceof Error ? err.message : String(err)}`,
    );
    return true;
  }
}
// ────────────────────────────────────────────────────────────────────────────

// ── Ambient Feed Proposal Reply Handler (Layer C) ───────────────────────────
const PROPOSAL_REPLY_PATTERN = /^\s*(\d+[a-d][\s,]*)+\s*$/i;

// ── @no5 synth on-demand trigger (Phase 3, OL-115) ──────────────────────────
// Accepts: "@no5 synth", "no5 synth", "@no5 synth add FILE", "@no5 synth skip FILE"
// Multiple add/skip tokens allowed, space-separated.
const SYNTH_PATTERN = /^\s*@?no5\s+synth\b(.*)$/i;
const AMBIENT_FEED_SCRIPT = '/home/tim/nanoclaw/ambient-feed-vps.py';

function parseSynthArgs(tail: string): { add: string[]; skip: string[] } {
  const add: string[] = [];
  const skip: string[] = [];
  const tokens = tail.trim().split(/\s+/).filter(Boolean);
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i].toLowerCase();
    if ((t === 'add' || t === 'skip') && i + 1 < tokens.length) {
      (t === 'add' ? add : skip).push(tokens[i + 1]);
      i++;
    }
  }
  return { add, skip };
}

async function handleProposalBoost(
  idsRaw: string,
  chatJid: string,
  channel: Channel,
): Promise<void> {
  const ids = idsRaw
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  const script =
    "import sys, ambient_ranking; " +
    "ids=[int(x) for x in sys.argv[1:]]; " +
    "n=ambient_ranking.mark_positive(ids); " +
    "print(n)";
  const args = ['-c', script, ...ids];
  const child = spawn('python3', args, { cwd: '/home/tim/nanoclaw' });
  let out = '';
  child.stdout.on('data', (d) => (out += d.toString()));
  await new Promise<void>((resolve) =>
    child.on('close', () => resolve()),
  );
  const n = parseInt(out.trim(), 10) || 0;
  await channel.sendMessage(
    chatJid,
    n > 0
      ? `Boosted ${n} proposal(s): ${ids.join(', ')}. Phantom will weight these as strong positives.`
      : `No matching delivery records found for: ${ids.join(', ')}.`,
  );
  logger.info({ ids, updated: n }, 'Proposal boost recorded');
}

async function handleSynthTrigger(
  tail: string,
  chatJid: string,
  channel: Channel,
): Promise<void> {
  const { add, skip } = parseSynthArgs(tail);
  const cliArgs: string[] = [AMBIENT_FEED_SCRIPT, '--synth-only'];
  for (const f of add) {
    cliArgs.push('--add-file', f);
  }
  for (const f of skip) {
    cliArgs.push('--skip-file', f);
  }
  const modelHint = add.length > 0 ? 'Sonnet (extended)' : 'Haiku (core)';
  const parts = [`Synthesis gestartet (${modelHint}).`];
  if (add.length) parts.push(`+ ${add.join(', ')}`);
  if (skip.length) parts.push(`- ${skip.join(', ')}`);
  parts.push('Proposals in ~1-3 min.');
  await channel.sendMessage(chatJid, parts.join(' '));

  const child = spawn('python3', cliArgs, {
    detached: true,
    stdio: 'ignore',
    cwd: '/home/tim/nanoclaw',
  });
  child.unref();
  logger.info({ add, skip }, 'Ambient Feed synth-only triggered via WhatsApp');
}

interface ProposalOption {
  id: string;
  action: string;
  executor: string;
  cost: string;
  reversible: boolean;
}

interface ParsedProposal {
  id: number;
  title: string;
  from_signals: string[];
  why_it_matters: string;
  affects_ols: string[];
  horizon: string;
  options: ProposalOption[];
  recommendation: string;
  recommendation_reasoning: string;
}

const DELIVERED_PROPOSALS_FILE = path.join(
  NO5_CONTEXT_DIR,
  'logs',
  'last-delivered-proposals.json',
);

function getDeliveredProposals(): ParsedProposal[] {
  // Read proposals from the last digest that was actually delivered to Tim.
  // This ensures Tim's reply always maps to the proposals he saw, not the most recent file on disk.
  if (!fs.existsSync(DELIVERED_PROPOSALS_FILE)) return [];
  try {
    const data = JSON.parse(fs.readFileSync(DELIVERED_PROPOSALS_FILE, 'utf-8'));
    const proposalsObj = data.proposals || {};
    return typeof proposalsObj === 'object' && proposalsObj.proposals
      ? proposalsObj.proposals
      : [];
  } catch {
    return [];
  }
}

function parseProposalChoices(
  text: string,
): Array<{ proposalId: number; optionId: string }> {
  const choices: Array<{ proposalId: number; optionId: string }> = [];
  for (const m of text.matchAll(/(\d+)([a-d])/gi)) {
    choices.push({
      proposalId: parseInt(m[1], 10),
      optionId: m[2].toLowerCase(),
    });
  }
  return choices;
}

function buildProposalAgentPrompt(
  proposal: ParsedProposal,
  option: ProposalOption,
): string {
  return [
    'You are executing an approved proposal from the No5 Ambient Feed synthesis layer.',
    '',
    '*Proposal:* ' + proposal.title,
    '*Horizon:* ' + proposal.horizon,
    '*Signals:* ' + proposal.from_signals.join(', '),
    '*Affects OLs:* ' + proposal.affects_ols.join(', '),
    '*Why:* ' + proposal.why_it_matters,
    '',
    '*Approved action (option ' + option.id + '):* ' + option.action,
    '',
    '',
    'HOW TO WRITE FILES:',
    'Call the mcp__nanoclaw__context_update tool with these parameters:',
    '  file: the relative path (e.g. "pending-decisions/2026-04-09-my-doc.md" or "spikes/my-spike.md")',
    '  content: the full file content as a string',
    '  commit_message: a short description of what was written',
    '',
    'NEVER write files using bash, cat, echo, or any filesystem command.',
    'NEVER write to /workspace/. The ONLY way to persist output is mcp__nanoclaw__context_update.',
    'If you write to /workspace/ instead of using context_update, your work will be LOST.',
    '',
    'RULES:',
    '- Allowed file paths: proposals/, pending-decisions/, spikes/',
    '- Do NOT edit 02-open-loops.md or 01-decision-log.md',
    '- Do NOT make external API calls',
    '- Execute ONLY the approved action, nothing more',
    '- Keep output brief: confirm what you wrote and where',
  ].join('\n');
}

async function handleProposalReply(
  text: string,
  chatJid: string,
  channel: Channel,
  group: RegisteredGroup,
): Promise<boolean> {
  const choices = parseProposalChoices(text);
  if (choices.length === 0) return false;

  const proposals = getDeliveredProposals();
  if (proposals.length === 0) {
    await channel.sendMessage(
      chatJid,
      'No delivered proposals found. Was a digest sent?',
    );
    return true;
  }

  const results: string[] = [];
  const nanoTasks: Array<{ proposal: ParsedProposal; option: ProposalOption }> =
    [];

  for (const choice of choices) {
    const proposal = proposals.find((p) => p.id === choice.proposalId);
    if (!proposal) {
      results.push(
        '*' + choice.proposalId + choice.optionId + '*: proposal not found',
      );
      continue;
    }
    const option = proposal.options.find((o) => o.id === choice.optionId);
    if (!option) {
      results.push(
        '*' + choice.proposalId + choice.optionId + '*: option not found',
      );
      continue;
    }

    if (choice.optionId === 'd') {
      results.push(
        '*' +
          choice.proposalId +
          'd*: dropped "' +
          proposal.title.slice(0, 60) +
          '"',
      );
    } else if (option.executor === 'tim') {
      results.push(
        '*' + choice.proposalId + choice.optionId + '*: noted for Tim',
      );
    } else if (option.executor === 'nano') {
      nanoTasks.push({ proposal, option });
      results.push(
        '*' + choice.proposalId + choice.optionId + '*: executing...',
      );
    }
  }

  // Immediate ack
  const ackLines = ['*Proposal choices received:*', '', ...results];
  if (nanoTasks.length > 0) {
    ackLines.push('', '_' + nanoTasks.length + ' nano task(s) starting..._');
  }
  await channel.sendMessage(chatJid, ackLines.join('\n'));

  // Execute nano tasks via agent container
  for (const task of nanoTasks) {
    const agentPrompt = buildProposalAgentPrompt(task.proposal, task.option);
    try {
      const formatted = formatMessages(
        [
          {
            id: 'proposal-' + Date.now(),
            chat_jid: chatJid,
            sender: 'system',
            sender_name: 'system',
            content: agentPrompt,
            timestamp: new Date().toISOString(),
            is_from_me: false,
          },
        ],
        TIMEZONE,
      );
      const output = await runAgent(
        group,
        formatted,
        chatJid,
        async (result) => {
          if (result.result) {
            const raw =
              typeof result.result === 'string'
                ? result.result
                : JSON.stringify(result.result);
            const cleaned = raw
              .replace(/<internal>[\s\S]*?<\/internal>/g, '')
              .trim();
            if (cleaned) {
              await channel.sendMessage(chatJid, cleaned);
            }
          }
        },
        { model: MODEL_HAIKU, skipSession: true },
      );
      if (output === 'error') {
        await channel.sendMessage(
          chatJid,
          'Proposal ' + task.proposal.id + ' execution failed.',
        );
      }
    } catch (err) {
      logger.error(
        { proposalId: task.proposal.id, err },
        'Proposal agent failed',
      );
      await channel.sendMessage(
        chatJid,
        'Failed: proposal ' +
          task.proposal.id +
          ' — ' +
          (err instanceof Error ? err.message : String(err)),
      );
    }
  }

  logger.info(
    { choices: choices.length, nanoTasks: nanoTasks.length },
    'Proposal reply processed',
  );
  return true;
}
// ────────────────────────────────────────────────────────────────────────────

async function processGroupMessages(chatJid: string): Promise<boolean> {
  const group = registeredGroups[chatJid];
  if (!group) return true;

  const channel = findChannel(channels, chatJid);
  if (!channel) {
    logger.warn({ chatJid }, 'No channel owns JID, skipping messages');
    return true;
  }

  const isMainGroup = group.isMain === true;

  const sinceTimestamp = lastAgentTimestamp[chatJid] || '';
  const missedMessages = getMessagesSince(
    chatJid,
    sinceTimestamp,
    ASSISTANT_NAME,
  );

  if (missedMessages.length === 0) return true;

  // For non-main groups, check if trigger is required and present
  if (!isMainGroup && group.requiresTrigger !== false) {
    const allowlistCfg = loadSenderAllowlist();
    const hasTrigger = missedMessages.some(
      (m) =>
        TRIGGER_PATTERN.test(m.content.trim()) &&
        (m.is_from_me || isTriggerAllowed(chatJid, m.sender, allowlistCfg)),
    );
    if (!hasTrigger) return true;
  }

  const prompt = formatMessages(missedMessages, TIMEZONE);

  // Intercept: handle revert OL-XXX without spawning agent container
  const lastMsg = missedMessages[missedMessages.length - 1];
  const revertMatch = lastMsg?.content?.trim().match(REVERT_PATTERN);
  if (revertMatch && lastMsg.is_from_me) {
    lastAgentTimestamp[chatJid] = lastMsg.timestamp;
    saveState();
    await handleRevertOL(revertMatch[1], chatJid, channel);
    return true;
  }

  // Intercept: handle "revert N" (proposal-level revert, Level 3 ranking feedback)
  const proposalRevertMatch = lastMsg?.content
    ?.trim()
    .match(PROPOSAL_REVERT_PATTERN);
  if (proposalRevertMatch && lastMsg.is_from_me) {
    lastAgentTimestamp[chatJid] = lastMsg.timestamp;
    saveState();
    await handleRevertProposal(
      parseInt(proposalRevertMatch[1], 10),
      chatJid,
      channel,
    );
    return true;
  }

  // Intercept: @no5 synth on-demand trigger (Phase 3, OL-115)
  const synthMatch = lastMsg?.content?.trim().match(SYNTH_PATTERN);
  if (synthMatch && lastMsg.is_from_me) {
    lastAgentTimestamp[chatJid] = lastMsg.timestamp;
    saveState();
    await handleSynthTrigger(synthMatch[1] || '', chatJid, channel);
    return true;
  }

  // Intercept: "+N" explicit positive signal for Phantom ranker
  const boostMatch = lastMsg?.content?.trim().match(PROPOSAL_BOOST_PATTERN);
  if (boostMatch && lastMsg.is_from_me) {
    lastAgentTimestamp[chatJid] = lastMsg.timestamp;
    saveState();
    await handleProposalBoost(boostMatch[1], chatJid, channel);
    return true;
  }

  // Intercept: handle proposal reply (e.g. "1a 2c 3d") without spawning generic agent
  const proposalMatch = lastMsg?.content?.trim().match(PROPOSAL_REPLY_PATTERN);
  if (proposalMatch && lastMsg.is_from_me) {
    lastAgentTimestamp[chatJid] = lastMsg.timestamp;
    saveState();
    await handleProposalReply(lastMsg.content.trim(), chatJid, channel, group);
    return true;
  }

  // Advance cursor so the piping path in startMessageLoop won't re-fetch
  // these messages. Save the old cursor so we can roll back on error.
  const previousCursor = lastAgentTimestamp[chatJid] || '';
  lastAgentTimestamp[chatJid] =
    missedMessages[missedMessages.length - 1].timestamp;
  saveState();

  const selectedModel = triageModel(prompt);
  logger.info(
    {
      group: group.name,
      messageCount: missedMessages.length,
      model: selectedModel,
    },
    'Processing messages',
  );

  // Track idle timer for closing stdin when agent is idle
  let idleTimer: ReturnType<typeof setTimeout> | null = null;

  const resetIdleTimer = () => {
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      logger.debug(
        { group: group.name },
        'Idle timeout, closing container stdin',
      );
      queue.closeStdin(chatJid);
    }, IDLE_TIMEOUT);
  };

  await channel.setTyping?.(chatJid, true);
  let hadError = false;
  let outputSentToUser = false;

  const output = await runAgent(group, prompt, chatJid, async (result) => {
    // Streaming output callback — called for each agent result
    if (result.result) {
      const raw =
        typeof result.result === 'string'
          ? result.result
          : JSON.stringify(result.result);
      // Strip <internal>...</internal> blocks — agent uses these for internal reasoning
      const text = raw.replace(/<internal>[\s\S]*?<\/internal>/g, '').trim();
      logger.info({ group: group.name }, `Agent output: ${raw.slice(0, 200)}`);
      if (text) {
        await channel.sendMessage(chatJid, text);
        outputSentToUser = true;
      }
      // Only reset idle timer on actual results, not session-update markers (result: null)
      resetIdleTimer();
    }

    if (result.status === 'success') {
      queue.notifyIdle(chatJid);
    }

    if (result.status === 'error') {
      hadError = true;
    }
  });

  await channel.setTyping?.(chatJid, false);
  if (idleTimer) clearTimeout(idleTimer);

  if (output === 'error' || hadError) {
    // If we already sent output to the user, don't roll back the cursor —
    // the user got their response and re-processing would send duplicates.
    if (outputSentToUser) {
      logger.warn(
        { group: group.name },
        'Agent error after output was sent, skipping cursor rollback to prevent duplicates',
      );
      return true;
    }
    // Roll back cursor so retries can re-process these messages
    lastAgentTimestamp[chatJid] = previousCursor;
    saveState();
    logger.warn(
      { group: group.name },
      'Agent error, rolled back message cursor for retry',
    );
    return false;
  }

  return true;
}

async function runAgent(
  group: RegisteredGroup,
  prompt: string,
  chatJid: string,
  onOutput?: (output: ContainerOutput) => Promise<void>,
  overrides?: { model?: string; skipSession?: boolean },
): Promise<'success' | 'error'> {
  const isMain = group.isMain === true;
  const sessionId = overrides?.skipSession ? undefined : sessions[group.folder];

  // Update tasks snapshot for container to read (filtered by group)
  const tasks = getAllTasks();
  writeTasksSnapshot(
    group.folder,
    isMain,
    tasks.map((t) => ({
      id: t.id,
      groupFolder: t.group_folder,
      prompt: t.prompt,
      schedule_type: t.schedule_type,
      schedule_value: t.schedule_value,
      status: t.status,
      next_run: t.next_run,
    })),
  );

  // Update available groups snapshot (main group only can see all groups)
  const availableGroups = getAvailableGroups();
  writeGroupsSnapshot(
    group.folder,
    isMain,
    availableGroups,
    new Set(Object.keys(registeredGroups)),
  );

  // Wrap onOutput to track session ID from streamed results
  const wrappedOnOutput = onOutput
    ? async (output: ContainerOutput) => {
        if (output.newSessionId) {
          sessions[group.folder] = output.newSessionId;
          setSession(group.folder, output.newSessionId);
        }
        await onOutput(output);
      }
    : undefined;

  try {
    const output = await runContainerAgent(
      group,
      {
        prompt,
        sessionId,
        groupFolder: group.folder,
        chatJid,
        isMain,
        assistantName: ASSISTANT_NAME,
        model: overrides?.model || triageModel(prompt),
      },
      (proc, containerName) =>
        queue.registerProcess(chatJid, proc, containerName, group.folder),
      wrappedOnOutput,
    );

    if (output.newSessionId) {
      sessions[group.folder] = output.newSessionId;
      setSession(group.folder, output.newSessionId);
    }

    if (output.status === 'error') {
      logger.error(
        { group: group.name, error: output.error },
        'Container agent error',
      );
      return 'error';
    }

    return 'success';
  } catch (err) {
    logger.error({ group: group.name, err }, 'Agent error');
    return 'error';
  }
}

async function startMessageLoop(): Promise<void> {
  if (messageLoopRunning) {
    logger.debug('Message loop already running, skipping duplicate start');
    return;
  }
  messageLoopRunning = true;

  logger.info(`NanoClaw running (trigger: @${ASSISTANT_NAME})`);

  while (true) {
    try {
      const jids = Object.keys(registeredGroups);
      const { messages, newTimestamp } = getNewMessages(
        jids,
        lastTimestamp,
        ASSISTANT_NAME,
      );

      if (messages.length > 0) {
        logger.info({ count: messages.length }, 'New messages');

        // Advance the "seen" cursor for all messages immediately
        lastTimestamp = newTimestamp;
        saveState();

        // Deduplicate by group
        const messagesByGroup = new Map<string, NewMessage[]>();
        for (const msg of messages) {
          const existing = messagesByGroup.get(msg.chat_jid);
          if (existing) {
            existing.push(msg);
          } else {
            messagesByGroup.set(msg.chat_jid, [msg]);
          }
        }

        for (const [chatJid, groupMessages] of messagesByGroup) {
          const group = registeredGroups[chatJid];
          if (!group) continue;

          const channel = findChannel(channels, chatJid);
          if (!channel) {
            logger.warn({ chatJid }, 'No channel owns JID, skipping messages');
            continue;
          }

          const isMainGroup = group.isMain === true;
          const needsTrigger = !isMainGroup && group.requiresTrigger !== false;

          // For non-main groups, only act on trigger messages.
          // Non-trigger messages accumulate in DB and get pulled as
          // context when a trigger eventually arrives.
          if (needsTrigger) {
            const allowlistCfg = loadSenderAllowlist();
            const hasTrigger = groupMessages.some(
              (m) =>
                TRIGGER_PATTERN.test(m.content.trim()) &&
                (m.is_from_me ||
                  isTriggerAllowed(chatJid, m.sender, allowlistCfg)),
            );
            if (!hasTrigger) continue;
          }

          // Pull all messages since lastAgentTimestamp so non-trigger
          // context that accumulated between triggers is included.
          const allPending = getMessagesSince(
            chatJid,
            lastAgentTimestamp[chatJid] || '',
            ASSISTANT_NAME,
          );
          const messagesToSend =
            allPending.length > 0 ? allPending : groupMessages;
          const formatted = formatMessages(messagesToSend, TIMEZONE);

          if (queue.sendMessage(chatJid, formatted)) {
            logger.debug(
              { chatJid, count: messagesToSend.length },
              'Piped messages to active container',
            );
            lastAgentTimestamp[chatJid] =
              messagesToSend[messagesToSend.length - 1].timestamp;
            saveState();
            // Show typing indicator while the container processes the piped message
            channel
              .setTyping?.(chatJid, true)
              ?.catch((err) =>
                logger.warn({ chatJid, err }, 'Failed to set typing indicator'),
              );
          } else {
            // No active container — enqueue for a new one
            queue.enqueueMessageCheck(chatJid);
          }
        }
      }
    } catch (err) {
      logger.error({ err }, 'Error in message loop');
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL));
  }
}

/**
 * Startup recovery: check for unprocessed messages in registered groups.
 * Handles crash between advancing lastTimestamp and processing messages.
 */
function recoverPendingMessages(): void {
  for (const [chatJid, group] of Object.entries(registeredGroups)) {
    const sinceTimestamp = lastAgentTimestamp[chatJid] || '';
    const pending = getMessagesSince(chatJid, sinceTimestamp, ASSISTANT_NAME);
    if (pending.length > 0) {
      logger.info(
        { group: group.name, pendingCount: pending.length },
        'Recovery: found unprocessed messages',
      );
      queue.enqueueMessageCheck(chatJid);
    }
  }
}

function ensureContainerSystemRunning(): void {
  ensureContainerRuntimeRunning();
  cleanupOrphans();
}

async function main(): Promise<void> {
  ensureContainerSystemRunning();
  initDatabase();
  logger.info('Database initialized');
  loadState();
  restoreRemoteControl();

  // Start credential proxy — release stale port holder first
  await freePort(CREDENTIAL_PROXY_PORT);
  // Start credential proxy (containers route API calls through this)
  const proxyServer = await startCredentialProxy(
    CREDENTIAL_PROXY_PORT,
    PROXY_BIND_HOST,
  );

  // Graceful shutdown handlers
  const shutdown = async (signal: string) => {
    logger.info({ signal }, 'Shutdown signal received');
    proxyServer.close();
    await queue.shutdown(10000);
    for (const ch of channels) await ch.disconnect();
    process.exit(0);
  };
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('unhandledRejection', (reason) => {
    logger.fatal(
      { reason },
      'Unhandled promise rejection — triggering shutdown',
    );
    shutdown('unhandledRejection').catch(() => process.exit(1));
  });
  process.on('uncaughtException', (err) => {
    logger.fatal({ err }, 'Uncaught exception — triggering shutdown');
    shutdown('uncaughtException').catch(() => process.exit(1));
  });

  // Handle /remote-control and /remote-control-end commands
  async function handleRemoteControl(
    command: string,
    chatJid: string,
    msg: NewMessage,
  ): Promise<void> {
    const group = registeredGroups[chatJid];
    if (!group?.isMain) {
      logger.warn(
        { chatJid, sender: msg.sender },
        'Remote control rejected: not main group',
      );
      return;
    }

    const channel = findChannel(channels, chatJid);
    if (!channel) return;

    if (command === '/remote-control') {
      const result = await startRemoteControl(
        msg.sender,
        chatJid,
        process.cwd(),
      );
      if (result.ok) {
        await channel.sendMessage(chatJid, result.url);
      } else {
        await channel.sendMessage(
          chatJid,
          `Remote Control failed: ${result.error}`,
        );
      }
    } else {
      const result = stopRemoteControl();
      if (result.ok) {
        await channel.sendMessage(chatJid, 'Remote Control session ended.');
      } else {
        await channel.sendMessage(chatJid, result.error);
      }
    }
  }

  // Channel callbacks (shared by all channels)
  const channelOpts = {
    onMessage: (chatJid: string, msg: NewMessage) => {
      // Remote control commands — intercept before storage
      const trimmed = msg.content.trim();
      if (trimmed === '/remote-control' || trimmed === '/remote-control-end') {
        handleRemoteControl(trimmed, chatJid, msg).catch((err) =>
          logger.error({ err, chatJid }, 'Remote control command error'),
        );
        return;
      }

      // Sender allowlist drop mode: discard messages from denied senders before storing
      if (!msg.is_from_me && !msg.is_bot_message && registeredGroups[chatJid]) {
        const cfg = loadSenderAllowlist();
        if (
          shouldDropMessage(chatJid, cfg) &&
          !isSenderAllowed(chatJid, msg.sender, cfg)
        ) {
          if (cfg.logDenied) {
            logger.debug(
              { chatJid, sender: msg.sender },
              'sender-allowlist: dropping message (drop mode)',
            );
          }
          return;
        }
      }

      // RAI slash commands (trusted principal only)
      // Strip invisible Unicode (zero-width spaces etc.) that WhatsApp injects
      const raiCmd = msg.content
        .replace(/[\u200B-\u200F\u2028-\u202F\uFEFF]/g, '')
        .trim()
        .toLowerCase();
      if (raiCmd.startsWith('/rai-')) {
        logger.debug(
          {
            raiCmd,
            is_from_me: msg.is_from_me,
            is_bot_message: msg.is_bot_message,
            sender: msg.sender,
          },
          'RAI command received',
        );
      }
      if (msg.is_from_me && !msg.is_bot_message) {
        if (raiCmd === '/rai-off') {
          raiEnabled = false;
          const ch = findChannel(channels, chatJid);
          if (ch)
            ch.sendMessage(
              chatJid,
              'RAI disabled. Send /rai-on to re-enable.',
            ).catch(() => {});
          return;
        }
        if (raiCmd === '/rai-on') {
          raiEnabled = true;
          const ch = findChannel(channels, chatJid);
          if (ch) ch.sendMessage(chatJid, 'RAI re-enabled.').catch(() => {});
          return;
        }
        if (raiCmd === '/rai-test') {
          raiTestMode = !raiTestMode;
          const ch = findChannel(channels, chatJid);
          const status = raiTestMode
            ? 'ON — your messages will be scanned'
            : 'OFF — principal exemption restored';
          if (ch)
            ch.sendMessage(chatJid, `RAI test mode ${status}`).catch(() => {});
          return;
        }
        if (
          raiCmd === '/rai-scan' ||
          raiCmd.startsWith('/rai-scan\n') ||
          raiCmd.startsWith('/rai-scan ')
        ) {
          // If payload follows the command on the same message, scan it directly
          const inlinePayload = msg.content
            .replace(/[\u200B-\u200F\u2028-\u202F\uFEFF]/g, '')
            .trim()
            .replace(/^\/rai-scan\s*/i, '')
            .trim();
          if (inlinePayload) {
            // Explicit scan: run P0 + P1, wait for both, send one consolidated verdict
            const rayChannel = rayChannelFromJid(chatJid);
            (async () => {
              const ch = findChannel(channels, chatJid);
              try {
                const { scanResult: p0Result } = await rayCheck(
                  inlinePayload,
                  rayChannel,
                  chatJid,
                  'test-principal',
                  false,
                );

                // Always run P1 for explicit scans
                const p1Input: ScanInput = {
                  scan_id: p0Result.scan_id,
                  timestamp: new Date().toISOString(),
                  source: {
                    channel: rayChannel as ScanInput['source']['channel'],
                    pipeline_stage: 'ingest',
                    sender: 'test-principal',
                    origin_url: null,
                    is_forward: false,
                  },
                  payload: { type: 'text', content: inlinePayload },
                  context: {
                    session_id: chatJid,
                    prior_scan_ids: [],
                    host_environment: 'nanoclaw',
                  },
                  p0_result: {
                    verdict: p0Result.verdict,
                    matched_patterns: p0Result.raw_signals,
                    confidence: p0Result.confidence,
                  },
                };

                const p1Result = await scanP1(p1Input);

                // Merge: use the more severe verdict
                const verdictRank = {
                  clean: 0,
                  flagged: 1,
                  blocked: 2,
                } as const;
                const finalVerdict =
                  verdictRank[p1Result.verdict] > verdictRank[p0Result.verdict]
                    ? p1Result.verdict
                    : p0Result.verdict;
                const finalConfidence = Math.max(
                  p0Result.confidence,
                  p1Result.confidence,
                );

                // Merge threat layers (dedupe by layer+label)
                const allThreats = [
                  ...p0Result.threat_layers,
                  ...p1Result.threat_layers,
                ];
                const seen = new Set<string>();
                const mergedThreats = allThreats.filter((t) => {
                  const key = `${t.layer}:${t.label}`;
                  if (seen.has(key)) return false;
                  seen.add(key);
                  return true;
                });

                const layers = mergedThreats
                  .map(
                    (t) =>
                      `  ${t.layer}: ${t.label} (${'severity' in t ? t.severity : 'unknown'})`,
                  )
                  .join('\n');

                const emoji =
                  finalVerdict === 'clean'
                    ? '\u2705'
                    : finalVerdict === 'blocked'
                      ? '\uD83D\uDEE1\uFE0F'
                      : '\u26A0\uFE0F';

                const explanation =
                  p1Result.verdict !== 'clean'
                    ? p1Result.explanation
                    : p0Result.explanation;

                const lines = [
                  `${emoji} RAI: ${finalVerdict}`,
                  ...(layers ? [`${layers}`] : []),
                  explanation,
                ];
                if (ch)
                  ch.sendMessage(chatJid, lines.join('\n')).catch(() => {});
              } catch (err) {
                logger.error({ err }, 'RAI inline scan error');
                if (ch)
                  ch.sendMessage(chatJid, 'RAI scan failed. Check logs.').catch(
                    () => {},
                  );
              }
            })();
          } else {
            // No payload — arm for next message
            raiScanNext = true;
            const ch = findChannel(channels, chatJid);
            if (ch)
              ch.sendMessage(
                chatJid,
                'RAI will scan your next message. Send it now.',
              ).catch(() => {});
          }
          return;
        }
        if (raiCmd === '/rai-status') {
          const ch = findChannel(channels, chatJid);
          const lines = [
            `RAI: ${raiEnabled ? 'enabled' : 'disabled'}`,
            `P1: active`,
            `Test mode: ${raiTestMode ? 'ON' : 'OFF'}`,
            `Scan next: ${raiScanNext ? 'armed' : 'no'}`,
          ];
          if (ch) ch.sendMessage(chatJid, lines.join('\n')).catch(() => {});
          return;
        }
      }

      // Never scan bot's own output (prevents notification → scan → notification loop)
      if (msg.is_bot_message) {
        storeMessage(msg);
        return;
      }

      // Ray scan — skip for own messages unless test mode or scan-next is active
      if (msg.is_from_me) {
        if (!raiTestMode && !raiScanNext) {
          storeMessage(msg);
          return;
        }
        // Test mode or scan-next: fall through to scan pipeline
        if (raiScanNext) raiScanNext = false; // auto-reset one-shot
      }

      // Ray scan — check inbound messages before storing
      if (!raiEnabled) {
        storeMessage(msg);
        return;
      }
      const rayChannel = rayChannelFromJid(chatJid);
      // In test mode, mask the sender so isExempt() doesn't skip the scan
      const scanSender =
        raiTestMode && msg.is_from_me ? 'test-principal' : (msg.sender ?? null);
      rayCheck(msg.content, rayChannel, chatJid, scanSender, false)
        .then(({ pass, warningBlock, scanResult }) => {
          if (scanResult.verdict !== 'clean') {
            logRayScan(scanResult, rayChannel);
            logger.info(
              {
                chatJid,
                verdict: scanResult.verdict,
                scanId: scanResult.scan_id,
              },
              `Ray scan: ${scanResult.explanation}`,
            );
          }
          if (!pass) {
            // Blocked — notify Tim and drop the message
            const notification = blockedNotification(scanResult, msg.sender);
            const mainJids = Object.entries(registeredGroups)
              .filter(([, g]) => g.isMain)
              .map(([jid]) => jid);
            for (const mainJid of mainJids) {
              const ch = findChannel(channels, mainJid);
              if (ch) ch.sendMessage(mainJid, notification).catch(() => {});
            }
            return;
          }
          // Prepend RAI warning block to message content so the agent sees the flag
          if (warningBlock) {
            msg.content = warningBlock + '\n\n' + msg.content;
          }
          storeMessage(msg);

          // P1 escalation: fire async if P0 flagged or low-confidence clean
          if (shouldEscalateToP1(scanResult.verdict, scanResult.confidence)) {
            const p1Input: ScanInput = {
              scan_id: scanResult.scan_id,
              timestamp: new Date().toISOString(),
              source: {
                channel: rayChannel as ScanInput['source']['channel'],
                pipeline_stage: 'ingest',
                sender: msg.sender ?? null,
                origin_url: null,
                is_forward: false,
              },
              payload: { type: 'text', content: msg.content },
              context: {
                session_id: chatJid,
                prior_scan_ids: [],
                host_environment: 'nanoclaw',
              },
              p0_result: {
                verdict: scanResult.verdict,
                matched_patterns: scanResult.raw_signals,
                confidence: scanResult.confidence,
              },
            };
            runP1Async(
              p1Input,
              async (p1Result) => {
                logRayScan(p1Result as unknown as RayScanOutput, rayChannel);
                logger.warn(
                  {
                    chatJid,
                    scanId: p1Result.scan_id,
                    verdict: p1Result.verdict,
                  },
                  `Ray P1 flagged: ${p1Result.explanation}`,
                );
              },
              async (p1Result) => {
                logRayScan(p1Result as unknown as RayScanOutput, rayChannel);
                logger.error(
                  { chatJid, scanId: p1Result.scan_id },
                  `Ray P1 blocked (retroactive): ${p1Result.explanation}`,
                );
                const p1Layers = p1Result.threat_layers
                  .map(
                    (t: { layer: string; label: string; severity: string }) =>
                      `  ${t.layer}: ${t.label} (${t.severity})`,
                  )
                  .join('\n');
                const notification = `\uD83D\uDEE1\uFE0F RAI: blocked (retroactive)\n${p1Layers ? p1Layers + '\n' : ''}${p1Result.explanation}`;
                const mainJids = Object.entries(registeredGroups)
                  .filter(([, g]) => g.isMain)
                  .map(([jid]) => jid);
                for (const mainJid of mainJids) {
                  const ch = findChannel(channels, mainJid);
                  if (ch) ch.sendMessage(mainJid, notification).catch(() => {});
                }
              },
            ).catch((err) => logger.error({ err }, 'Ray P1 async error'));
          }
        })
        .catch((err) => {
          logger.error(
            { err, chatJid },
            'Ray scan error, storing message anyway',
          );
          storeMessage(msg);
        });
    },
    onChatMetadata: (
      chatJid: string,
      timestamp: string,
      name?: string,
      channel?: string,
      isGroup?: boolean,
    ) => storeChatMetadata(chatJid, timestamp, name, channel, isGroup),
    registeredGroups: () => registeredGroups,
  };

  // Create and connect all registered channels.
  // Each channel self-registers via the barrel import above.
  // Factories return null when credentials are missing, so unconfigured channels are skipped.
  for (const channelName of getRegisteredChannelNames()) {
    const factory = getChannelFactory(channelName)!;
    const channel = factory(channelOpts);
    if (!channel) {
      logger.warn(
        { channel: channelName },
        'Channel installed but credentials missing — skipping. Check .env or re-run the channel skill.',
      );
      continue;
    }
    channels.push(channel);
    await channel.connect();
  }
  if (channels.length === 0) {
    logger.fatal('No channels connected');
    process.exit(1);
  }

  // Start subsystems (independently of connection handler)
  startSchedulerLoop({
    registeredGroups: () => registeredGroups,
    getSessions: () => sessions,
    queue,
    onProcess: (groupJid, proc, containerName, groupFolder) =>
      queue.registerProcess(groupJid, proc, containerName, groupFolder),
    sendMessage: async (jid, rawText) => {
      const channel = findChannel(channels, jid);
      if (!channel) {
        logger.warn({ jid }, 'No channel owns JID, cannot send message');
        return;
      }
      const text = formatOutbound(rawText);
      if (text) await channel.sendMessage(jid, text);
    },
  });
  startIpcWatcher({
    sendMessage: (jid, text) => {
      const channel = findChannel(channels, jid);
      if (!channel) throw new Error(`No channel for JID: ${jid}`);
      return channel.sendMessage(jid, text);
    },
    registeredGroups: () => registeredGroups,
    registerGroup,
    syncGroups: async (force: boolean) => {
      await Promise.all(
        channels
          .filter((ch) => ch.syncGroups)
          .map((ch) => ch.syncGroups!(force)),
      );
    },
    getAvailableGroups,
    writeGroupsSnapshot: (gf, im, ag, rj) =>
      writeGroupsSnapshot(gf, im, ag, rj),
  });
  queue.setProcessMessagesFn(processGroupMessages);
  recoverPendingMessages();
  startMessageLoop().catch((err) => {
    logger.fatal({ err }, 'Message loop crashed unexpectedly');
    process.exit(1);
  });
}

// Guard: only run when executed directly, not when imported by tests
const isDirectRun =
  process.argv[1] &&
  new URL(import.meta.url).pathname ===
    new URL(`file://${process.argv[1]}`).pathname;

if (isDirectRun) {
  main().catch((err) => {
    logger.error({ err }, 'Failed to start NanoClaw');
    process.exit(1);
  });
}
