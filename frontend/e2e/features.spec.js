// MC-1 (label panel), MC-2 (style dropdown), MC-3 (export controls, captions on and off) in a real browser.
// One 12 s cut of hoco_40s is analysed once with Deadpan; the export and label tests reuse that job.
import { test, expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import { startBackend, stopBackend } from './stack.js';
import { CLIP_LONG, CLIP_SHORT, apiJob, collectErrors, downloadExport, ffmpegBin, jobId, makeClip, probe, shot, waitDone } from './helpers.js';

test.describe.configure({ mode: 'serial' });
test.beforeAll(() => startBackend());
test.afterAll(stopBackend);

let savedJob = null;
let quips = [];
const CLIP_NAME = 'cut12.mp4';
const ROW = '.label-panel div[style*="cursor"]';

const frameAt = (file, t) => execFileSync(ffmpegBin(), ['-v', 'error', '-ss', String(t), '-i', file, '-frames:v', '1', '-f', 'image2pipe', '-c:v', 'bmp', '-'],
  { maxBuffer: 64 * 1024 * 1024 });

const reopen = async (page) => {
  expect(savedJob).toBeTruthy();
  await page.addInitScript((job) => localStorage.setItem('maneframe.job', job), savedJob);
  await page.goto('/');
  await waitDone(page);
  await expect.poll(() => page.locator('video').evaluate((v) => v.readyState), { timeout: 30000 }).toBeGreaterThanOrEqual(2);
};

const streams = (file) => {
  const info = probe(file);
  const v = info.streams.find((s) => s.codec_type === 'video');
  const a = info.streams.find((s) => s.codec_type === 'audio');
  return { codecs: [v.codec_name, a.codec_name], size: [v.width, v.height], duration: Number(info.format.duration) };
};

test('MC-2: Deadpan in the dropdown reaches the job (result.style)', async ({ page }) => {
  const errors = collectErrors(page);
  const clip = makeClip(CLIP_NAME, CLIP_LONG, 12);
  await page.goto('/');
  await expect(page.getByLabel('Commentary style').locator('option')).toHaveText(['Broadcast', 'Deadpan', 'Hype']);
  await page.getByLabel('Commentary style').selectOption({ label: 'Deadpan' });
  await page.locator('input[type=file]').setInputFiles(clip);
  await waitDone(page);
  const { result } = await apiJob(page, await jobId(page));
  expect(result.style).toBe('deadpan');
  quips = result.quips;
  expect(quips.length).toBeGreaterThan(0);
  savedJob = await page.evaluate(() => localStorage.getItem('maneframe.job'));
  await shot(page, 'mc2-deadpan');
  expect(errors).toEqual([]);
});

test('MC-2: Hype (the default) reaches the job (result.style)', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByLabel('Commentary style')).toHaveValue('hype');
  await page.getByLabel('Commentary style').selectOption({ label: 'Hype' });
  await page.locator('input[type=file]').setInputFiles(CLIP_SHORT);
  await waitDone(page);
  const { result } = await apiJob(page, await jobId(page));
  expect(result.style).toBe('hype');
  await shot(page, 'mc2-hype');
});

test('MC-3: Set start / Set end while playing, Vertical 9:16, captions on', async ({ page }) => {
  await reopen(page);
  const video = page.locator('video');

  await video.evaluate((v) => { v.currentTime = 2; return v.play(); });
  await page.getByRole('button', { name: 'Set start' }).click();
  const start = Number(await page.getByLabel(/Start \(s\)/).inputValue());
  expect(start).toBeGreaterThanOrEqual(2);

  await video.evaluate((v) => { v.currentTime = 8; return v.play(); });
  await page.getByRole('button', { name: 'Set end' }).click();
  const end = Number(await page.getByLabel(/End \(s\)/).inputValue());
  expect(end).toBeGreaterThanOrEqual(8);
  await page.getByRole('combobox', { name: 'Video size' }).selectOption('9:16');
  await shot(page, 'mc3-controls');

  const { file, name } = await downloadExport(page, 'mc3-vertical.mp4');
  expect(name).toBe(`maneframe_${CLIP_NAME.replace('.mp4', '')}_9x16_${start}-${end}s.mp4`);
  const s = streams(file);
  expect(s.codecs).toEqual(['h264', 'aac']);
  expect(s.size).toEqual([1080, 1920]);
  expect(Math.abs(s.duration - (end - start))).toBeLessThan(0.5);
  fs.rmSync(file, { force: true });
});

test('MC-3: Original size, captions on and off differ in the burned-in text', async ({ page }) => {
  await reopen(page);
  const q = quips.find((x) => x.audio_url) || quips[0];
  // no range here: the server drops a quip whose audio would run past an explicit end, so a full-length export
  // is the one with a guaranteed caption at q.timestamp
  const clipSeconds = streams(makeClip(CLIP_NAME, CLIP_LONG, 12)).duration;

  const on = await downloadExport(page, 'mc3-captions-on.mp4');
  await page.getByRole('checkbox', { name: 'Captions' }).uncheck();
  const off = await downloadExport(page, 'mc3-captions-off.mp4');
  expect(on.name).toBe('maneframe_cut12.mp4');
  expect(off.name).toBe(on.name);

  for (const f of [on.file, off.file]) {
    const s = streams(f);
    expect(s.codecs).toEqual(['h264', 'aac']);
    expect(s.size).toEqual([1920, 1080]);
    expect(Math.abs(s.duration - clipSeconds)).toBeLessThan(0.5);
  }
  // 0.5 s into the quip: the captions-on frame has text pixels the captions-off frame lacks
  const t = q.timestamp + 0.5;
  const a = frameAt(on.file, t);
  const b = frameAt(off.file, t);
  let differing = 0;
  for (let i = 54; i < a.length; i += 1) if (Math.abs(a[i] - b[i]) > 40) differing += 1;
  expect(differing, 'captions-on frame differs from captions-off frame').toBeGreaterThan(2000);
  await shot(page, 'mc3-original');
  fs.rmSync(on.file, { force: true });
  fs.rmSync(off.file, { force: true });
});

test('MC-1: add label pauses, click seeks, CSV row, survives a reload, delete', async ({ page }) => {
  const errors = collectErrors(page);
  await reopen(page);
  const video = page.locator('video');
  await video.evaluate((v) => { v.currentTime = 3; return v.play(); });
  await expect.poll(() => video.evaluate((v) => v.currentTime)).toBeGreaterThan(3.2);
  await page.getByRole('button', { name: 'Add label at current time' }).click();
  await expect.poll(() => video.evaluate((v) => v.paused)).toBe(true);
  const pausedAt = await video.evaluate((v) => v.currentTime);
  const form = page.locator('form');
  await expect(form).toBeVisible();
  const labelOptions = await form.getByLabel('Label').locator('option').allTextContents();
  const label = labelOptions.length > 1 ? labelOptions[1] : labelOptions[0];
  await form.getByLabel('Label').selectOption(label);
  await form.getByLabel('Commentary quality').selectOption({ index: 1 });
  const quality = await form.getByLabel('Commentary quality').inputValue();
  await form.getByPlaceholder('Note (optional)').fill('e2e note, with comma');
  await shot(page, 'mc1-form');
  await form.getByRole('button', { name: 'Save' }).click();

  const row = page.locator(ROW).filter({ hasText: 'e2e note' });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText(label);
  const t = Number((await row.locator('strong').textContent()).replace('s:', ''));
  expect(Math.abs(t - pausedAt)).toBeLessThan(0.02);

  await video.evaluate((v) => { v.currentTime = 0.5; });
  await row.click();
  await expect.poll(() => video.evaluate((v) => v.currentTime)).toBeCloseTo(t, 1);

  const href = await page.getByRole('link', { name: 'Download CSV' }).getAttribute('href');
  const csv = await (await page.request.get(href)).text();
  expect(csv).toContain('e2e note, with comma');
  expect(csv).toContain(label);
  expect(csv).toContain(quality);
  expect(csv.trim().split(/\r?\n/)).toHaveLength(2);

  await page.reload();
  await waitDone(page);
  await expect(page.locator(ROW).filter({ hasText: 'e2e note' })).toHaveCount(1);
  await shot(page, 'mc1-after-reload');

  await page.getByRole('button', { name: /Delete label at/ }).click();
  await expect(page.locator(ROW)).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Download CSV' })).toHaveCount(0);
  expect(errors).toEqual([]);
});
