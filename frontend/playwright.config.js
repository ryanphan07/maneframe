import { defineConfig } from '@playwright/test';

// Real-stack browser checks: `npm run test:e2e` (needs the model files from backend/.env; see docs/CONVENTIONS.md).
// E2E_CHANNEL=msedge runs Edge instead of the bundled Chromium (which has no H.264 decoder).
export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.spec.js',
  workers: 1,
  fullyParallel: false,
  timeout: 15 * 60 * 1000,
  expect: { timeout: 30 * 1000 },
  reporter: [['list']],
  globalSetup: './e2e/global-setup.js',
  globalTeardown: './e2e/global-teardown.js',
  use: {
    baseURL: 'http://127.0.0.1:8000',
    channel: process.env.E2E_CHANNEL || undefined,
    launchOptions: { args: ['--autoplay-policy=no-user-gesture-required'] },
  },
});
