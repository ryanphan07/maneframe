import { expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { E2E_DIR, ROOT } from './stack.js';

const DOWNLOADS = path.join(os.homedir(), 'Downloads');
export const CLIP_SHORT = process.env.E2E_CLIP_SHORT || path.join(DOWNLOADS, 'shorter_test.mp4');
export const CLIP_LONG = process.env.E2E_CLIP_LONG || path.join(DOWNLOADS, 'hoco_40s.mp4');

export const ffprobeBin = () => process.env.FFPROBE_PATH || 'ffprobe';

export const probe = (file) => JSON.parse(execFileSync(ffprobeBin(), ['-v', 'error', '-show_entries',
  'stream=codec_type,codec_name:format=duration', '-of', 'json', file], { encoding: 'utf8' }));

export const shot = async (page, name) => {
  fs.mkdirSync(E2E_DIR, { recursive: true });
  await page.screenshot({ path: path.join(E2E_DIR, `${name}.png`), fullPage: true });
};

// Console errors and uncaught page errors, from the moment this is called.
export const collectErrors = (page) => {
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  return errors;
};

export const upload = async (page, clip) => {
  await page.goto('/');
  const input = page.locator('input[type=file]');
  await expect(input).toBeEnabled();
  await input.setInputFiles(clip);
};

export const jobId = (page) => page.evaluate(() => JSON.parse(localStorage.getItem('maneframe.job')).jobId);

export const quipRows = (page) => page.locator('.commentary-display > div');

export const waitDone = (page) => expect(page.locator('strong', { hasText: /^Done$/ })).toBeVisible({ timeout: 12 * 60 * 1000 });

// Plays the video for up to `ms` and returns how far currentTime moved (stops early once it passed 1.5 s).
export const playAndMeasure = async (page, ms = 8000) => {
  const video = page.locator('video');
  await expect.poll(() => video.evaluate((v) => v.readyState), { timeout: 30000 }).toBeGreaterThanOrEqual(2);
  return video.evaluate(async (v, wait) => {
    const start = v.currentTime;
    await v.play();
    const until = performance.now() + wait;
    while (performance.now() < until && v.currentTime - start < 1.5) await new Promise((r) => setTimeout(r, 100));
    const moved = v.currentTime - start;
    v.pause();
    return moved;
  }, ms);
};

export { ROOT };
