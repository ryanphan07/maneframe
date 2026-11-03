import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
  FakeSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeSocket);
  URL.createObjectURL = vi.fn(() => 'blob:local');
  URL.revokeObjectURL = vi.fn();
  clock = 0;
  vi.spyOn(Date, 'now').mockImplementation(() => (clock += 10000)); // every call is past the partial-fetch throttle
  vi.spyOn(toast, 'error').mockImplementation(() => {});
});
afterEach(() => {
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
