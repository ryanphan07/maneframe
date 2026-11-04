import { describe, expect, it } from 'vitest';
import { uploadSizeError } from './uploadLimit';

const GB = 1024 ** 3;

describe('uploadSizeError', () => {
  it('names the clip size and the limit', () => {
    expect(uploadSizeError(3.2 * GB, 2048)).toBe('This clip is 3.2 GB; the limit is 2 GB. Trim it on your phone first.');
  });
  it('uses MB for small limits', () => {
    expect(uploadSizeError(300 * 1024 * 1024, 100)).toBe('This clip is 300 MB; the limit is 100 MB. Trim it on your phone first.');
  });
  it('accepts a file exactly at the limit', () => {
    expect(uploadSizeError(2 * GB, 2048)).toBeNull();
  });
  it('skips the check when the server did not report a limit', () => {
    expect(uploadSizeError(50 * GB, undefined)).toBeNull();
  });
});
