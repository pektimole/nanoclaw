import fs from 'fs';
import path from 'path';
import { execSync } from 'child_process';
import { logger } from './logger.js';

const MAC_ALIVE_FILE = '/tmp/mac-alive';
const MAC_ALIVE_THRESHOLD_S = 300; // 5 minutes
const MAC_TRANSCRIBE_URL = 'http://localhost:9877/transcribe';
const WHISPER_SCRIPT = '/home/tim/whisper.cpp/transcribe.sh';
const AUDIO_DIR = '/tmp/nanoclaw-audio';

// Ensure audio directory exists
fs.mkdirSync(AUDIO_DIR, { recursive: true });

/**
 * Check if Mac is online by reading the heartbeat timestamp.
 */
export function isMacOnline(): boolean {
  try {
    const ts = parseInt(fs.readFileSync(MAC_ALIVE_FILE, 'utf-8').trim(), 10);
    const now = Math.floor(Date.now() / 1000);
    const delta = now - ts;
    const online = delta < MAC_ALIVE_THRESHOLD_S;
    logger.info({ delta, online }, 'Mac heartbeat check');
    return online;
  } catch {
    logger.info('No mac-alive file, Mac considered offline');
    return false;
  }
}

/**
 * Transcribe audio via Mac (reverse SSH tunnel → localhost:9877).
 * Returns transcript text or null on failure.
 */
async function transcribeViaMac(audioPath: string): Promise<string | null> {
  try {
    const audioData = fs.readFileSync(audioPath);
    const res = await fetch(MAC_TRANSCRIBE_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'audio/opus' },
      body: audioData,
      signal: AbortSignal.timeout(60000), // 60s timeout
    });
    if (!res.ok) {
      logger.error({ status: res.status }, 'Mac transcription failed');
      return null;
    }
    const transcript = await res.text();
    const duration = res.headers.get('X-Duration') || '?';
    const lang = res.headers.get('X-Language') || '?';
    logger.info({ duration, lang, length: transcript.length }, 'Mac transcription complete');
    return transcript.trim();
  } catch (err) {
    logger.error({ err }, 'Mac transcription error');
    return null;
  }
}

/**
 * Transcribe audio via VPS whisper.cpp (local fallback).
 * Returns transcript text or null on failure.
 */
function transcribeViaVPS(audioPath: string): string | null {
  try {
    const result = execSync(`${WHISPER_SCRIPT} ${audioPath} auto`, {
      timeout: 300000, // 5 min timeout for long audio
      encoding: 'utf-8',
    }).trim();
    logger.info({ length: result.length }, 'VPS transcription complete');
    return result;
  } catch (err) {
    logger.error({ err }, 'VPS transcription error');
    return null;
  }
}

/**
 * Transcribe audio file. Routes to Mac if online, otherwise VPS.
 * Returns { transcript, source } or null on failure.
 */
export async function transcribeAudio(
  audioPath: string,
): Promise<{ transcript: string; source: 'mac' | 'vps' } | null> {
  const macOnline = isMacOnline();

  if (macOnline) {
    logger.info('Routing transcription to Mac');
    const transcript = await transcribeViaMac(audioPath);
    if (transcript) {
      return { transcript, source: 'mac' };
    }
    // Mac failed, fall through to VPS
    logger.warn('Mac transcription failed, falling back to VPS');
  }

  logger.info('Routing transcription to VPS whisper.cpp');
  const transcript = transcribeViaVPS(audioPath);
  if (transcript) {
    return { transcript, source: 'vps' };
  }

  return null;
}

/**
 * Save audio buffer to disk for transcription.
 * Returns the file path.
 */
export function saveAudioFile(buffer: Buffer, messageId: string): string {
  const filePath = path.join(AUDIO_DIR, `${messageId}.opus`);
  fs.writeFileSync(filePath, buffer);
  return filePath;
}

/**
 * Clean up audio file after transcription.
 */
export function cleanupAudioFile(filePath: string): void {
  try {
    fs.unlinkSync(filePath);
  } catch {
    // Best effort
  }
}
