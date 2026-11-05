import { stopLlama } from './stack.js';

export default async function globalTeardown() {
  stopLlama(); // only a llama-server this run started has a pid file
}
