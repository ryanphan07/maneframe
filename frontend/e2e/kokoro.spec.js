// MC-6: offline Kokoro audio in the browser. The backend runs with TTS_ENGINE=kokoro set by environment variable.
import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { ROOT, startBackend, stopBackend, readEnvFile } from './stack.js';
import { CLIP_SHORT, apiJob, jobId, shot, upload, waitDone } from './helpers.js';

const setting = (key, fallback) => process.env[key] || readEnvFile()[key] || fallback;
const abs = (p) => (path.isAbsolute(p) ? p : path.join(ROOT, p));
const files = [setting('KOKORO_MODEL_PATH', 'models/kokoro/kokoro-v1.0.onnx'), setting('KOKORO_VOICES_PATH', 'models/kokoro/voices-v1.0.bin')].map(abs);
const python = path.join(ROOT, 'backend', 'venv', 'Scripts', 'python.exe');
const hasPackage = () => { try { execFileSync(python, ['-c', 'import kokoro_onnx'], { stdio: 'ignore' }); return true; } catch { return false; } };

const missing = files.filter((f) => !fs.existsSync(f));
const skipReason = missing.length ? `Kokoro files missing: ${missing.join(', ')}` : (!hasPackage() ? 'the kokoro-onnx package is not installed in backend/venv' : null);

test.skip(!!skipReason, skipReason || '');
test.beforeAll(() => startBackend({ TTS_ENGINE: 'kokoro' }));
test.afterAll(stopBackend);

test('MC-6: Kokoro quips are .wav files and the page plays them (Audio.play called)', async ({ page }) => {
  await page.addInitScript(() => {
    window.__audio = [];
    const origPlay = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function play(...args) {
      if (!(this instanceof HTMLVideoElement)) window.__audio.push(this.src);
      return origPlay.apply(this, args);
    };
  });
  await upload(page, CLIP_SHORT);
  await waitDone(page);
  const { result } = await apiJob(page, await jobId(page));
  const withAudio = result.quips.filter((q) => q.audio_url);
  expect(withAudio.length, 'at least one quip has audio').toBeGreaterThan(0);
  for (const q of withAudio) expect(new URL(q.audio_url, 'http://x').pathname).toMatch(/\.wav$/);
  await expect(page.getByText('(no audio)')).toHaveCount(0);
  await expect(page.getByText('Audio is unavailable')).toHaveCount(0);

  const res = await page.request.get(withAudio[0].audio_url);
  expect(res.status()).toBe(200);
  expect((await res.body()).subarray(0, 4).toString()).toBe('RIFF');

  const video = page.locator('video');
  await expect.poll(() => video.evaluate((v) => v.readyState), { timeout: 30000 }).toBeGreaterThanOrEqual(2);
  await video.evaluate((v, t) => { v.currentTime = t; }, Math.max(0, withAudio[0].timestamp - 0.3));
  await video.evaluate((v) => v.play());
  await expect.poll(() => page.evaluate(() => window.__audio.length), { timeout: 15000 }).toBeGreaterThan(0);
  expect(await page.evaluate(() => window.__audio[0])).toContain('.wav');
  await shot(page, 'mc6-kokoro');
});
