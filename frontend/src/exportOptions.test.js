import { describe, expect, it } from 'vitest';
import { exportFileName, exportParams, rangeProblem } from './exportOptions';

describe('exportParams', () => {
  it('sends only captions for the defaults, as before', () => {
    expect(exportParams()).toEqual({ captions: 1 });
    expect(exportParams({ captions: false, aspect: 'original', start: '', end: '' })).toEqual({ captions: 0 });
  });

  it('adds aspect, start and end when they are set', () => {
    expect(exportParams({ captions: true, aspect: '9:16', start: '2.5', end: '17' })).toEqual({ captions: 1, aspect: '9:16', start: 2.5, end: 17 });
    expect(exportParams({ aspect: 'original', start: '0', end: '' })).toEqual({ captions: 1, start: 0 });
  });
});

describe('exportFileName', () => {
  it('keeps the plain name for the defaults', () => {
    expect(exportFileName('hoco_40s.mp4')).toBe('maneframe_hoco_40s.mp4');
  });

  it('includes the aspect and the range', () => {
    expect(exportFileName('clip.mov', { aspect: '9:16' })).toBe('maneframe_clip_9x16.mp4');
    expect(exportFileName('clip.mov', { start: '2.5', end: '17' })).toBe('maneframe_clip_2.5-17s.mp4');
    expect(exportFileName('clip.mov', { aspect: '9:16', start: '', end: '15' })).toBe('maneframe_clip_9x16_0-15s.mp4');
    expect(exportFileName('clip.mov', { start: '30' })).toBe('maneframe_clip_30-ends.mp4');
  });
});

describe('rangeProblem', () => {
  it('accepts empty and ordered ranges and names the bad ones', () => {
    expect(rangeProblem({})).toBeNull();
    expect(rangeProblem({ start: '1', end: '' })).toBeNull();
    expect(rangeProblem({ start: '5', end: '5' })).toMatch(/before/);
    expect(rangeProblem({ start: '9', end: '3' })).toMatch(/before/);
    expect(rangeProblem({ start: '-1', end: '' })).toMatch(/negative/);
  });
});
