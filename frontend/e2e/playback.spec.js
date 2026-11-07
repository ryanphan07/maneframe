import { test, expect } from '@playwright/test';
import { startBackend, stopBackend } from './stack.js';
import { CLIP_LONG, collectErrors, jobId, playAndMeasure, quipRows, shot, upload, waitDone } from './helpers.js';

test.describe.configure({ mode: 'serial' });
let savedJob = null; // the localStorage entry test a's upload wrote, replayed in d's fresh context
test.beforeAll(() => startBackend());
test.afterAll(stopBackend);

// a and d share one real run of the 40 s clip: a reloads it mid-run, d plays the finished job.
test('a: reload mid-run keeps progress, quips and playback', async ({ page }) => {
  const errors = collectErrors(page);
  await upload(page, CLIP_LONG);
  await expect(quipRows(page).first()).toBeVisible({ timeout: 10 * 60 * 1000 }); // first partial quips
  savedJob = await page.evaluate(() => localStorage.getItem('maneframe.job'));
  const before = await quipRows(page).count();
  const progressBefore = Number(await page.getByRole('progressbar').getAttribute('aria-valuenow'));
  await shot(page, 'a1-before-reload');

  await page.reload();
  await expect(page.getByRole('progressbar')).toBeVisible();
  await expect(quipRows(page).first()).toBeVisible();
  expect(await quipRows(page).count()).toBeGreaterThanOrEqual(before);
  await expect.poll(async () => Number(await page.getByRole('progressbar').getAttribute('aria-valuenow')), { timeout: 10 * 60 * 1000 })
    .toBeGreaterThan(progressBefore);
  expect(await playAndMeasure(page)).toBeGreaterThan(1);
  await shot(page, 'a2-after-reload');

  await waitDone(page);
  await shot(page, 'a3-finished');
  expect(errors).toEqual([]);
});

test('d: a quip starts within 1 s of its timestamp and ducks the video', async ({ page }) => {
  expect(savedJob).toBeTruthy();
  await page.addInitScript((job) => {
    localStorage.setItem('maneframe.job', job);
    window.__audioLog = [];
    const origPlay = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function play(...args) {
      if (!(this instanceof HTMLVideoElement)) {
        const v = document.querySelector('video');
        window.__audioLog.push({ src: this.src, videoTime: v.currentTime, videoVolume: v.volume });
      }
      return origPlay.apply(this, args);
    };
  }, savedJob);
  await page.goto('/');
  await waitDone(page);
  const id = await jobId(page);
  const { result } = await (await page.request.get(`/api/jobs/${id}`)).json();
  const quip = result.quips.find((q) => q.audio_url);
  expect(quip, 'the finished job has at least one quip with audio').toBeTruthy();

  const video = page.locator('video');
  await expect.poll(() => video.evaluate((v) => v.readyState), { timeout: 30000 }).toBeGreaterThanOrEqual(2);
  await video.evaluate((v, t) => { v.volume = 1; v.currentTime = t; }, Math.max(0, quip.timestamp - 0.5));
  await expect.poll(() => video.evaluate((v) => v.seeking)).toBe(false);
  await video.evaluate((v) => v.play());
  await expect.poll(() => page.evaluate(() => window.__audioLog.length), { timeout: 15000 }).toBeGreaterThan(0);
  const entry = await page.evaluate(() => window.__audioLog[0]);
  await shot(page, 'd-quip-playing');
  expect(entry.src).toContain(quip.audio_url);
  expect(entry.videoTime - quip.timestamp).toBeGreaterThanOrEqual(-0.1); // not early
  expect(entry.videoTime - quip.timestamp).toBeLessThanOrEqual(1);
  // the duck is applied right after audio.play() is called
  await expect.poll(() => video.evaluate((v) => v.volume)).toBeCloseTo(0.3, 2);
  // and released when the quip ends
  await expect.poll(() => video.evaluate((v) => v.volume), { timeout: 20000 }).toBe(1);
});
