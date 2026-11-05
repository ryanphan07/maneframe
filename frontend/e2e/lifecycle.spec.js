import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { startBackend, stopBackend, E2E_DIR } from './stack.js';
import { CLIP_SHORT, collectErrors, playAndMeasure, probe, shot, upload, waitDone } from './helpers.js';

test.describe.configure({ mode: 'serial' });
test.beforeAll(startBackend);
test.afterAll(stopBackend);

test('b: reload after completion plays, lists commentary and downloads H.264 + AAC', async ({ page }) => {
  const errors = collectErrors(page);
  await upload(page, CLIP_SHORT);
  await waitDone(page);
  await page.reload();
  await waitDone(page);
  await expect(page.getByRole('heading', { name: 'Commentary' })).toBeVisible();
  expect(await playAndMeasure(page)).toBeGreaterThan(1);

  const [download] = await Promise.all([
    page.waitForEvent('download', { timeout: 5 * 60 * 1000 }),
    page.getByRole('button', { name: 'Download video' }).click(),
  ]);
  const file = path.join(E2E_DIR, 'b-download.mp4');
  await download.saveAs(file);
  await shot(page, 'b-after-reload');
  const info = probe(file);
  const codecs = Object.fromEntries(info.streams.map((s) => [s.codec_type, s.codec_name]));
  expect(codecs.video).toBe('h264');
  expect(codecs.audio).toBe('aac');
  const clip = probe(CLIP_SHORT);
  expect(Math.abs(Number(info.format.duration) - Number(clip.format.duration))).toBeLessThan(0.5);
  expect(errors).toEqual([]);
  fs.rmSync(file, { force: true });
});

test('c: after a backend restart the old page says the job is gone', async ({ page }) => {
  await upload(page, CLIP_SHORT);
  await expect(page.getByRole('progressbar')).toBeVisible();
  await stopBackend();
  await startBackend();
  // errors are counted from the reload: the socket the old page held dies with the old backend
  const errors = collectErrors(page);
  await page.reload();
  await expect(page.getByRole('status').filter({ hasText: 'no longer on the server' })).toBeVisible();
  await expect(page.locator('input[type=file]')).toBeEnabled();
  await expect(page.getByRole('heading', { name: 'Maneframe' })).toBeVisible();
  await shot(page, 'c-job-gone');
  // the browser itself logs the 404 of the job lookup; that line is the expected answer, not an app error
  const unexpected = errors.filter((e) => !/Failed to load resource.*404/.test(e));
  expect(unexpected).toEqual([]);
});
