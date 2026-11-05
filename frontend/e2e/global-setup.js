import { execSync } from 'node:child_process';
import path from 'node:path';
import { ensureLlama, ROOT } from './stack.js';

export default async function globalSetup() {
  // the backend serves frontend/dist, so build it fresh
  execSync('npm run build', { cwd: path.join(ROOT, 'frontend'), stdio: 'ignore' });
  await ensureLlama();
}
