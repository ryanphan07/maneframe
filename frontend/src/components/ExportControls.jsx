import { useState } from 'react';
import { rangeProblem } from '../exportOptions';

// Download button plus its options: captions, aspect (original or vertical 9:16) and an optional start/end in seconds.
// `getCurrentTime` reads the player's position for the "Set start" / "Set end" buttons.
const ExportControls = ({ rendering, onDownload, getCurrentTime }) => {
  const [captions, setCaptions] = useState(true);
  const [aspect, setAspect] = useState('original');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const options = { captions, aspect, start, end };
  const problem = rangeProblem(options);
  const takeTime = (set) => {
    const time = getCurrentTime?.();
    if (Number.isFinite(time)) set(String(Math.round(time * 10) / 10));
  };

  return (
    <div style={{ margin: '1rem 0' }}>
      <button type="button" onClick={() => onDownload(options)} disabled={rendering || Boolean(problem)}>
        {rendering ? 'Rendering video…' : 'Download video'}
      </button>
      <label style={{ marginLeft: '1rem' }}>
        <input type="checkbox" checked={captions} onChange={(e) => setCaptions(e.target.checked)} disabled={rendering} /> Captions
      </label>
      <label style={{ marginLeft: '1rem' }}>
        Size{' '}
        <select value={aspect} onChange={(e) => setAspect(e.target.value)} disabled={rendering} aria-label="Video size">
          <option value="original">Original</option>
          <option value="9:16">Vertical 9:16</option>
        </select>
      </label>
      <div style={{ marginTop: '0.5rem' }}>
        <label>
          Start (s){' '}
          <input type="number" min="0" step="0.1" value={start} placeholder="0" onChange={(e) => setStart(e.target.value)} disabled={rendering} style={{ width: '5rem' }} />
        </label>{' '}
        <button type="button" onClick={() => takeTime(setStart)} disabled={rendering}>Set start</button>
        <label style={{ marginLeft: '1rem' }}>
          End (s){' '}
          <input type="number" min="0" step="0.1" value={end} placeholder="end" onChange={(e) => setEnd(e.target.value)} disabled={rendering} style={{ width: '5rem' }} />
        </label>{' '}
        <button type="button" onClick={() => takeTime(setEnd)} disabled={rendering}>Set end</button>
        {problem && <div role="alert"><small>{problem}</small></div>}
      </div>
      {rendering && <div><small>Rendering can take a while for long clips.</small></div>}
    </div>
  );
};

export default ExportControls;
