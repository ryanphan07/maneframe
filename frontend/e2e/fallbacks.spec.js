// MC-5 (polling fallback after the WebSocket fails) and MC-4 (player falls back to the server's H.264 copy).
import { test, expect } from '@playwright/test';
import { startBackend, stopBackend } from './stack.js';
import { CLIP_LONG, CLIP_SHORT, apiJob, collectErrors, jobId, makeClip, playAndMeasure, shot, upload, waitDone } from './helpers.js';

test.describe.configure({ mode: 'serial' });
test.beforeAll(() => startBackend());
test.afterAll(stopBackend);

test('MC-5: with the WebSocket failing every time, polling finishes the job', async ({ page }) => {
  const errors = collectErrors(page);
  let sockets = 0;
  // the page's WebSocket opens, then the "server" closes it at once: no message ever arrives
  await page.routeWebSocket(/\/ws\/analyze\//, (ws) => { sockets += 1; ws.close(); });
  const polled = []; // statuses answered by GET /api/jobs/{id} once all 6 socket tries are used up
  page.on('response', async (r) => {
    if (sockets < 6 || r.request().method() !== 'GET' || !/\/api\/jobs\/[^/]+$/.test(new URL(r.url()).pathname)) return;
    polled.push((await r.json().catch(() => ({}))).status);
  });

  await upload(page, CLIP_SHORT);
  // the "Live updates lost" text is replaced by the first poll's message, so count the socket tries instead
  await expect.poll(() => sockets, { timeout: 90 * 1000 }).toBe(6); // the first try plus MAX_RECONNECTS (5)
  await shot(page, 'mc5-polling');
  await waitDone(page);
  expect(polled.length).toBeGreaterThan(0);
  expect(polled).toContain('completed'); // with every socket closed, only polling can have delivered "completed"
  await expect(page.getByRole('heading', { name: 'Commentary' })).toBeVisible();
  const { result } = await apiJob(page, await jobId(page));
  expect(result.quips.length).toBeGreaterThan(0);
  await shot(page, 'mc5-done');
  expect(errors).toEqual([]);
});

let hevcFile = null;
let fellBack = null;

test('MC-4: a 10 s HEVC clip plays, from the server copy when the browser cannot decode it', async ({ page }) => {
  hevcFile = makeClip('hevc10.mp4', CLIP_LONG, 10, ['-c:v', 'libx265', '-tag:v', 'hvc1', '-pix_fmt', 'yuv420p', '-c:a', 'aac']);
  await upload(page, hevcFile);
  await waitDone(page);
  const video = page.locator('video');
  expect(await playAndMeasure(page)).toBeGreaterThan(1);
  const src = await video.evaluate((v) => v.currentSrc);
  fellBack = /\/api\/jobs\/[^/]+\/video$/.test(src);
  const canHevc = await page.evaluate(() => document.createElement('video').canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"'));
  test.info().annotations.push({ type: 'hevc', description: `canPlayType=${JSON.stringify(canHevc)}, played from ${fellBack ? 'server copy' : 'local blob'}` });
  await shot(page, 'mc4-natural');
  await expect(page.getByRole('alert').filter({ hasText: 'could not be played' })).toHaveCount(0);
});

test('MC-4 (forced): when the local file cannot be decoded the player switches to the server copy', async ({ page }) => {
  test.skip(fellBack === true, 'the browser could not decode HEVC, so the natural test already used the fallback');
  // This Chromium decodes HEVC, so break the local blob URL instead: the video error event drives the same switch.
  // It exercises the fallback path, not HEVC decoding.
  await page.addInitScript(() => {
    const create = URL.createObjectURL.bind(URL);
    URL.createObjectURL = (obj) => (obj instanceof File ? 'blob:http://127.0.0.1:8000/not-a-real-blob' : create(obj));
  });
  await upload(page, hevcFile);
  await waitDone(page);
  expect(await playAndMeasure(page)).toBeGreaterThan(1);
  expect(await page.locator('video').evaluate((v) => v.currentSrc)).toMatch(/\/api\/jobs\/[^/]+\/video$/);
  await shot(page, 'mc4-forced');
});
