import fs from 'fs';
import path from 'path';
import { logger } from './logger.js';

const MAC_ALIVE_FILE = '/tmp/mac-alive';
const MAC_ALIVE_THRESHOLD_S = 300; // 5 minutes
const MAC_TRANSCRIBE_URL = 'http://localhost:9877/transcribe';
// VPS fallback: the yt_transcriber Flask service (ffmpeg -> 16kHz WAV -> whisper).
// Replaces the old raw transcribe.sh, which fed opus to whisper unconverted and
// returned garbage ("(speaking in foreign language)") for WhatsApp voice notes.
const VPS_TRANSCRIBE_URL = 'http://127.0.0.1:7700/transcribe';
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
    logger.info(
      { duration, lang, length: transcript.length },
      'Mac transcription complete',
    );
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
async function transcribeViaVPS(audioPath: string): Promise<string | null> {
  try {
    const audioData = fs.readFileSync(audioPath);
    const form = new FormData();
    form.append('file', new Blob([audioData]), path.basename(audioPath));
    const res = await fetch(VPS_TRANSCRIBE_URL, {
      method: 'POST',
      body: form,
      signal: AbortSignal.timeout(300000), // 5 min for long audio
    });
    if (!res.ok) {
      logger.error({ status: res.status }, 'VPS transcription failed');
      return null;
    }
    const data = (await res.json()) as { transcript?: string; error?: string };
    if (data.error) {
      logger.error({ error: data.error }, 'VPS transcription error');
      return null;
    }
    const transcript = (data.transcript || '').trim();
    logger.info({ length: transcript.length }, 'VPS transcription complete');
    return transcript || null;
  } catch (err) {
    logger.error({ err }, 'VPS transcription error');
    return null;
  }
}

/**
 * Transcribe audio file. VPS Flask service is the primary path (reliable,
 * same host, proper ffmpeg->16kHz WAV conversion). The Mac transcriber is only
 * a last-resort fallback if VPS fails AND the Mac is online, to avoid the 60s
 * stall that Mac-first routing caused when the Mac endpoint was unresponsive.
 * Returns { transcript, source } or null on failure.
 */
export async function transcribeAudio(
  audioPath: string,
): Promise<{ transcript: string; source: 'mac' | 'vps' } | null> {
  logger.info('Routing transcription to VPS transcriber service');
  const transcript = await transcribeViaVPS(audioPath);
  if (transcript) {
    return { transcript, source: 'vps' };
  }

  // Last-resort fallback: Mac transcriber, only if it reports online.
  if (isMacOnline()) {
    logger.warn('VPS transcription failed, trying Mac fallback');
    const macTranscript = await transcribeViaMac(audioPath);
    if (macTranscript) {
      return { transcript: macTranscript, source: 'mac' };
    }
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
