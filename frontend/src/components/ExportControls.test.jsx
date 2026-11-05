import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ExportControls from './ExportControls';

const setup = (props = {}) => {
  const onDownload = vi.fn();
  render(<ExportControls rendering={false} onDownload={onDownload} getCurrentTime={() => 12.34} {...props} />);
  return onDownload;
};

describe('ExportControls', () => {
  it('downloads with the defaults: captions on, original size, no range', () => {
    const onDownload = setup();
    fireEvent.click(screen.getByRole('button', { name: 'Download video' }));
    expect(onDownload).toHaveBeenCalledWith({ captions: true, aspect: 'original', start: '', end: '' });
  });

  it('offers Original and Vertical 9:16 and passes the choice', () => {
    const onDownload = setup();
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['Original', 'Vertical 9:16']);
    fireEvent.change(screen.getByLabelText('Video size'), { target: { value: '9:16' } });
    fireEvent.click(screen.getByRole('button', { name: 'Download video' }));
    expect(onDownload).toHaveBeenCalledWith(expect.objectContaining({ aspect: '9:16' }));
  });

  it('Set start and Set end take the player time (to 0.1 s)', () => {
    const onDownload = setup();
    fireEvent.click(screen.getByRole('button', { name: 'Set start' }));
    expect(screen.getByLabelText(/Start/)).toHaveValue(12.3);
    fireEvent.change(screen.getByLabelText(/End/), { target: { value: '20' } });
    fireEvent.click(screen.getByRole('button', { name: 'Download video' }));
    expect(onDownload).toHaveBeenCalledWith(expect.objectContaining({ start: '12.3', end: '20' }));
    fireEvent.click(screen.getByRole('button', { name: 'Set end' }));
    expect(screen.getByLabelText(/End/)).toHaveValue(12.3);
  });

  it('blocks the download while start is not before end', () => {
    const onDownload = setup();
    fireEvent.change(screen.getByLabelText(/Start/), { target: { value: '9' } });
    fireEvent.change(screen.getByLabelText(/End/), { target: { value: '4' } });
    expect(screen.getByRole('alert')).toHaveTextContent('Start must be before end.');
    expect(screen.getByRole('button', { name: 'Download video' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Download video' }));
    expect(onDownload).not.toHaveBeenCalled();
  });

  it('disables everything while rendering', () => {
    setup({ rendering: true });
    expect(screen.getByRole('button', { name: 'Rendering video…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Set start' })).toBeDisabled();
  });
});
