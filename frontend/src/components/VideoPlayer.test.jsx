import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import VideoPlayer from './VideoPlayer';

const API = 'http://api.test';
const LOCAL = 'blob:local-file';

const player = (props = {}) => (
  <VideoPlayer videoUrl={LOCAL} jobId="j1" apiBase={API} commentaryQuips={[]} processingStatus="analyzing" {...props} />
);
const videoOf = (container) => container.querySelector('video');
const setVideo = (video, values) => {
  Object.entries(values).forEach(([key, value]) => Object.defineProperty(video, key, { value, configurable: true, writable: true }));
};

describe('playback fallback to the server copy', () => {
  it('keeps the local file when it decodes', () => {
    const { container } = render(player());
    const video = videoOf(container);
    setVideo(video, { videoWidth: 640 });
    fireEvent.loadedMetadata(video);
    expect(video).toHaveAttribute('src', LOCAL);
  });

  it('switches to the server URL on a video error', () => {
    const { container } = render(player());
    fireEvent.error(videoOf(container));
    expect(videoOf(container)).toHaveAttribute('src', `${API}/api/jobs/j1/video`);
  });

  it('switches to the server URL when the video has no decodable frames (videoWidth 0)', () => {
    const { container } = render(player());
    const video = videoOf(container);
    setVideo(video, { videoWidth: 0 });
    fireEvent.loadedMetadata(video);
    expect(video).toHaveAttribute('src', `${API}/api/jobs/j1/video`);
  });

  it('waits for the conversion (no src, notice shown) until the server copy exists', () => {
    const { container, rerender } = render(player({ processingStatus: 'extracting' }));
    fireEvent.error(videoOf(container));
    expect(videoOf(container)).not.toHaveAttribute('src');
    expect(screen.getByRole('status')).toHaveTextContent(/Converting it for playback/);
    rerender(player({ processingStatus: 'analyzing' }));
    expect(videoOf(container)).toHaveAttribute('src', `${API}/api/jobs/j1/video`);
  });

  it('reports a failure when the server copy cannot be played either', () => {
    const { container } = render(player());
    fireEvent.error(videoOf(container));
    fireEvent.error(videoOf(container));
    expect(screen.getByRole('alert')).toHaveTextContent(/could not be played/);
  });
});

describe('commentary playback while the list grows', () => {
  let clips;
  beforeEach(() => {
    clips = [];
    vi.stubGlobal('Audio', class {
      constructor(url) {
        this.url = url;
        clips.push(this);
      }
      play() { return Promise.resolve(); }
      pause() {}
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  const quip = (timestamp, text) => ({ timestamp, text, audio_url: `/audio/${timestamp}.mp3` });
  const tick = (video, time) => {
    setVideo(video, { currentTime: time });
    act(() => { fireEvent.timeUpdate(video); });
  };

  it('shows the partial list and the ready-until notice', () => {
    render(player({ commentaryQuips: [quip(1, 'first')], readyUntil: 65 }));
    expect(screen.getByText('first')).toBeInTheDocument();
    expect(screen.getByText(/Commentary ready up to 1:05/)).toBeInTheDocument();
  });

  it('plays a quip once and never replays it after the list refreshes', () => {
    const a = quip(1, 'first');
    const { container, rerender } = render(player({ commentaryQuips: [a], readyUntil: 4 }));
    const video = videoOf(container);
    setVideo(video, { paused: false, volume: 1 });

    tick(video, 1.5);
    expect(clips.map((c) => c.url)).toEqual(['/audio/1.mp3']);
    act(() => { clips[0].onended(); });

    rerender(player({ commentaryQuips: [a, quip(3, 'second')], readyUntil: 8 }));
    tick(video, 3.5);
    expect(clips.map((c) => c.url)).toEqual(['/audio/1.mp3', '/audio/3.mp3']); // the new quip plays, the old one does not repeat
    act(() => { clips[1].onended(); });

    rerender(player({ commentaryQuips: [a, quip(3, 'second'), quip(6, 'third')], readyUntil: 12 }));
    tick(video, 3.6);
    expect(clips).toHaveLength(2);
  });
});
