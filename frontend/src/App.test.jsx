import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import axios from 'axios';
import { toast } from 'react-toastify';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';

vi.mock('axios');

class FakeSocket {
  static instances = [];
  constructor(url) {
    this.url = url;
    FakeSocket.instances.push(this);
  }
  close() {}
  send(msg) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
}

const quip = (timestamp, text) => ({ timestamp, text, audio_url: `/audio/${timestamp}.mp3` });
const partialJob = (quips) => ({ data: { status: 'analyzing', result: { partial: { quips, ready_until: 20 } } } });
const stylesResponse = { data: { styles: ['hype', 'deadpan', 'broadcast'], default: 'hype' } };

let clock;
beforeEach(() => {
  localStorage.clear(); // a job saved by one test must not be resumed by the next
  FakeSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeSocket);
  URL.createObjectURL = vi.fn(() => 'blob:local');
  URL.revokeObjectURL = vi.fn();
  clock = 0;
  vi.spyOn(Date, 'now').mockImplementation(() => (clock += 10000)); // every call is past the partial-fetch throttle
  vi.spyOn(toast, 'error').mockImplementation(() => {});
});
afterEach(() => {
  cleanup(); // unmount first: a still-mounted App must not call axios after the mocks are reset
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

describe('style dropdown', () => {
  it('lists the server styles and selects the default', async () => {
    axios.get.mockResolvedValue(stylesResponse);
    render(<App />);
    const select = screen.getByLabelText('Commentary style');
    await waitFor(() => expect(select).toHaveValue('hype'));
    expect([...select.options].map((o) => o.textContent)).toEqual(['Hype', 'Deadpan', 'Broadcast']);
  });

  it('shows the server error and keeps uploads disabled when the styles cannot load', async () => {
    axios.get.mockRejectedValue({ response: { data: { detail: 'styles are down' } } });
    const { container } = render(<App />);
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('styles are down', { position: 'top-right' }));
    expect(screen.getByLabelText('Commentary style').options).toHaveLength(0);
    expect(container.querySelector('input[type="file"]')).toBeDisabled();
  });
});

describe('upload size limit', () => {
  const pick = (container, file) => fireEvent.change(container.querySelector('input[type="file"]'), { target: { files: [file] } });

  it('rejects an oversized clip before uploading', async () => {
    axios.get.mockResolvedValue({ data: { ...stylesResponse.data, max_upload_mb: 2048 } });
    const { container } = render(<App />);
    await waitFor(() => expect(screen.getByLabelText('Commentary style')).toHaveValue('hype'));
    const big = new File(['x'], 'big.mp4', { type: 'video/mp4' });
    Object.defineProperty(big, 'size', { value: 3 * 1024 ** 3 });
    pick(container, big);
    expect(toast.error).toHaveBeenCalledWith('This clip is 3 GB; the limit is 2 GB. Trim it on your phone first.', { position: 'top-right' });
    expect(axios.post).not.toHaveBeenCalled();
  });

  it('uploads a clip under the limit', async () => {
    axios.get.mockResolvedValue({ data: { ...stylesResponse.data, max_upload_mb: 2048 } });
    axios.post.mockResolvedValue({ data: { job_id: 'j1' } });
    const { container } = render(<App />);
    await waitFor(() => expect(screen.getByLabelText('Commentary style')).toHaveValue('hype'));
    pick(container, new File(['x'], 'ok.mp4', { type: 'video/mp4' }));
    await waitFor(() => expect(axios.post).toHaveBeenCalled());
  });
});

describe('progressive results', () => {
  const jobGets = (...responses) => {
    const queue = [...responses];
    axios.get.mockImplementation((url) => {
      if (url.endsWith('/api/styles')) return Promise.resolve(stylesResponse);
      return Promise.resolve(queue.length > 1 ? queue.shift() : queue[0]);
    });
  };
  const startJob = async () => {
    axios.get.mockResolvedValue(stylesResponse);
    axios.post.mockResolvedValue({ data: { job_id: 'j1' } });
    const { container } = render(<App />);
    await waitFor(() => expect(screen.getByLabelText('Commentary style')).toHaveValue('hype'));
    const input = container.querySelector('input[type="file"]');
    await act(async () => {
      fireEvent.change(input, { target: { files: [new File(['x'], 'clip.mp4', { type: 'video/mp4' })] } });
    });
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
    return FakeSocket.instances[0];
  };

  it('shows partial quips as windows finish and keeps earlier ones', async () => {
    const socket = await startJob();
    jobGets(partialJob([quip(1, 'first quip')]), partialJob([quip(1, 'first quip'), quip(9, 'second quip')]));

    await act(async () => { socket.send({ status: 'analyzing', progress: 30, message: 'window 1' }); });
    expect(await screen.findByText(/first quip/)).toBeInTheDocument();
    expect(screen.queryByText(/second quip/)).not.toBeInTheDocument();

    await act(async () => { socket.send({ status: 'analyzing', progress: 50, message: 'window 2' }); });
    expect(await screen.findByText(/second quip/)).toBeInTheDocument();
    expect(screen.getByText(/first quip/)).toBeInTheDocument();
  });

  it('shows no commentary list while the response has no partial yet', async () => {
    const socket = await startJob();
    jobGets({ data: { status: 'analyzing', result: null } });
    await act(async () => { socket.send({ status: 'analyzing', progress: 20, message: 'start' }); });
    await waitFor(() => expect(axios.get).toHaveBeenCalledWith(expect.stringContaining('/api/jobs/j1')));
    expect(screen.queryByText('Commentary')).not.toBeInTheDocument();
  });

  it('clears the partial list and shows the error when the job fails', async () => {
    const socket = await startJob();
    jobGets(partialJob([quip(1, 'first quip')]));
    await act(async () => { socket.send({ status: 'analyzing', progress: 30 }); });
    expect(await screen.findByText(/first quip/)).toBeInTheDocument();

    await act(async () => { socket.send({ status: 'error', progress: 30, message: 'The AI model is not reachable' }); });
    expect(screen.queryByText(/first quip/)).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('The AI model is not reachable');
  });

  it('does not apply a late partial response after the job has failed', async () => {
    const socket = await startJob();
    let release;
    axios.get.mockImplementation((url) => (url.endsWith('/api/styles')
      ? Promise.resolve(stylesResponse)
      : new Promise((resolve) => { release = () => resolve(partialJob([quip(1, 'late quip')])); })));
    await act(async () => { socket.send({ status: 'analyzing', progress: 30 }); });
    await act(async () => { socket.send({ status: 'error', progress: 30, message: 'boom' }); });
    await act(async () => { release(); });
    expect(screen.queryByText(/late quip/)).not.toBeInTheDocument();
  });
});

describe('resume after a page refresh', () => {
  const saved = { jobId: 'j9', fileName: 'big.mov', style: 'deadpan' };
  const serve = (jobResponse) => {
    axios.get.mockImplementation((url) => {
      if (url.endsWith('/api/styles')) return Promise.resolve(stylesResponse);
      if (url.endsWith('/labels')) return Promise.resolve({ data: { labels: [], label_options: [], qualities: [] } });
      return jobResponse;
    });
  };

  it('stores the job id, filename and style when an upload starts, and clears it on the next upload', async () => {
    axios.get.mockResolvedValue(stylesResponse);
    axios.post.mockResolvedValueOnce({ data: { job_id: 'j1' } }).mockResolvedValueOnce({ data: { job_id: 'j2' } });
    const { container } = render(<App />);
    await waitFor(() => expect(screen.getByLabelText('Commentary style')).toHaveValue('hype'));
    const input = container.querySelector('input[type="file"]');
    await act(async () => { fireEvent.change(input, { target: { files: [new File(['x'], 'clip.mp4', { type: 'video/mp4' })] } }); });
    expect(JSON.parse(localStorage.getItem('maneframe.job'))).toEqual({ jobId: 'j1', fileName: 'clip.mp4', style: 'hype' });

    // the next upload clears the entry before the new job id is known
    axios.post.mockImplementationOnce(() => { expect(localStorage.getItem('maneframe.job')).toBeNull(); return Promise.resolve({ data: { job_id: 'j2' } }); });
    await act(async () => { FakeSocket.instances[0].send({ status: 'error', progress: 0, message: 'x' }); });
    await act(async () => { fireEvent.change(input, { target: { files: [new File(['x'], 'next.mp4', { type: 'video/mp4' })] } }); });
    expect(JSON.parse(localStorage.getItem('maneframe.job')).jobId).toBe('j2');
  });

  it('reconnects the WebSocket and shows partial quips for a running job', async () => {
    localStorage.setItem('maneframe.job', JSON.stringify(saved));
    serve(Promise.resolve(partialJob([quip(1, 'resumed quip')])));
    const { container } = render(<App />);
    expect(await screen.findByText(/resumed quip/)).toBeInTheDocument();
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
    expect(FakeSocket.instances[0].url).toContain('/ws/analyze/j9');
    expect(container.querySelector('video').getAttribute('src')).toContain('/api/jobs/j9/video');
    await waitFor(() => expect(screen.getByLabelText('Commentary style')).toHaveValue('deadpan'));
  });

  it('shows the result and plays the server copy for a completed job', async () => {
    localStorage.setItem('maneframe.job', JSON.stringify(saved));
    serve(Promise.resolve({ data: { status: 'completed', progress: 100, result: { quips: [quip(2, 'done quip')], total_windows: 3, skipped: [] } } }));
    const { container } = render(<App />);
    expect(await screen.findByText(/done quip/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download video' })).toBeInTheDocument();
    expect(container.querySelector('video').getAttribute('src')).toContain('/api/jobs/j9/video');
    expect(FakeSocket.instances).toHaveLength(0);
  });

  it('requests the export with the chosen aspect and range and names the file after them', async () => {
    localStorage.setItem('maneframe.job', JSON.stringify(saved));
    serve(Promise.resolve({ data: { status: 'completed', progress: 100, result: { quips: [quip(2, 'done quip')], total_windows: 3, skipped: [] } } }));
    render(<App />);
    await screen.findByText(/done quip/);
    axios.post.mockResolvedValueOnce({ data: { url: '/api/jobs/j9/export.mp4?captions=1&aspect=9%3A16&start=2&end=15' } });
    const downloads = [];
    const realCreate = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tag) => {
      const el = realCreate(tag);
      if (tag === 'a') el.click = () => downloads.push({ href: el.href, name: el.download });
      return el;
    });
    fireEvent.change(screen.getByLabelText('Video size'), { target: { value: '9:16' } });
    fireEvent.change(screen.getByLabelText(/Start/), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText(/End/), { target: { value: '15' } });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Download video' })); });
    expect(axios.post).toHaveBeenCalledWith(expect.stringContaining('/api/jobs/j9/export'), null, { params: { captions: 1, aspect: '9:16', start: 2, end: 15 } });
    expect(downloads).toHaveLength(1);
    expect(downloads[0].href).toContain('/api/jobs/j9/export.mp4?captions=1&aspect=9%3A16&start=2&end=15');
    expect(downloads[0].name).toBe('maneframe_big_9x16_2-15s.mp4');
  });

  it('Set start and Set end read the position of the player', async () => {
    localStorage.setItem('maneframe.job', JSON.stringify(saved));
    serve(Promise.resolve({ data: { status: 'completed', progress: 100, result: { quips: [quip(2, 'done quip')], total_windows: 3, skipped: [] } } }));
    const { container } = render(<App />);
    await screen.findByText(/done quip/);
    container.querySelector('video').currentTime = 7.26;
    fireEvent.click(screen.getByRole('button', { name: 'Set start' }));
    expect(screen.getByLabelText(/Start/)).toHaveValue(7.3);
  });

  it('clears the saved job and shows a notice when the server answers 404', async () => {
    localStorage.setItem('maneframe.job', JSON.stringify(saved));
    serve(Promise.reject({ response: { status: 404 } }));
    const { container } = render(<App />);
    expect(await screen.findByRole('status')).toHaveTextContent(/no longer on the server/);
    expect(localStorage.getItem('maneframe.job')).toBeNull();
    expect(container.querySelector('video')).toBeNull();
    expect(FakeSocket.instances).toHaveLength(0);
  });

  it('keeps the saved job when the server is unreachable', async () => {
    localStorage.setItem('maneframe.job', JSON.stringify(saved));
    serve(Promise.reject(new Error('network')));
    render(<App />);
    await waitFor(() => expect(axios.get).toHaveBeenCalledWith(expect.stringContaining('/api/jobs/j9')));
    expect(localStorage.getItem('maneframe.job')).not.toBeNull();
  });
});
