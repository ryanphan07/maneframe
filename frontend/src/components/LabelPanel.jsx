import { useCallback, useEffect, useState } from 'react';
import axios from 'axios';

const formatTime = (sec) => `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, '0')}`;

const errorDetail = (error, fallback) => {
  const detail = error?.response?.data?.detail;
  return typeof detail === 'string' && detail ? detail : fallback;
};

const LabelPanel = ({ videoRef, jobId, apiBase }) => {
  const [labels, setLabels] = useState([]);
  const [options, setOptions] = useState({ label_options: [], qualities: [] });
  const [draft, setDraft] = useState(null);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);

  const base = `${apiBase}/api/jobs/${jobId}/labels`;

  const load = useCallback(async () => {
    try {
      const { data } = await axios.get(base);
      setLabels(data.labels);
      setOptions({ label_options: data.label_options, qualities: data.qualities });
      setError(null);
    } catch (err) {
      setError(errorDetail(err, 'Could not load labels.'));
    }
  }, [base]);

  useEffect(() => {
    let cancelled = false;
    axios.get(base).then(({ data }) => {
      if (cancelled) return;
      setLabels(data.labels);
      setOptions({ label_options: data.label_options, qualities: data.qualities });
    }).catch((err) => {
      if (!cancelled) setError(errorDetail(err, 'Could not load labels.'));
    });
    return () => {
      cancelled = true;
    };
  }, [base]);

  const openForm = () => {
    const video = videoRef.current;
    if (!video || !options.label_options.length) return;
    video.pause();
    setDraft({
      t: Math.round(video.currentTime * 100) / 100,
      label: options.label_options[0],
      quality: options.qualities[0],
      note: '',
    });
    setError(null);
  };

  const save = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      await axios.post(base, draft);
      setDraft(null);
      await load();
    } catch (err) {
      setError(errorDetail(err, 'Could not save the label.'));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (id) => {
    try {
      await axios.delete(`${base}/${id}`);
      await load();
    } catch (err) {
      setError(errorDetail(err, 'Could not delete the label.'));
    }
  };

  const seekTo = (t) => {
    if (videoRef.current) videoRef.current.currentTime = t;
  };

  return (
    <div className="label-panel" style={{ marginTop: '1rem' }}>
      <h3>Labels</h3>
      <button type="button" onClick={openForm} disabled={!options.label_options.length || !!draft}>
        Add label at current time
      </button>
      {labels.length > 0 && (
        <a href={`${base}.csv`} download style={{ marginLeft: '1rem' }}>
          Download CSV
        </a>
      )}

      {error && (
        <p role="alert" style={{ color: '#8a1c17' }}>
          {error}
        </p>
      )}

      {draft && (
        <form onSubmit={save} style={{ margin: '0.5rem 0', padding: '0.5rem', background: '#f5f5f5', borderRadius: '6px' }}>
          <div>
            <strong>At {formatTime(draft.t)} ({draft.t}s)</strong>
          </div>
          <label>
            Label{' '}
            <select value={draft.label} onChange={(e) => setDraft({ ...draft, label: e.target.value })}>
              {options.label_options.map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
          </label>{' '}
          <label>
            Commentary quality{' '}
            <select value={draft.quality} onChange={(e) => setDraft({ ...draft, quality: e.target.value })}>
              {options.qualities.map((q) => (
                <option key={q} value={q}>
                  {q}
                </option>
              ))}
            </select>
          </label>
          <div>
            <input
              type="text"
              value={draft.note}
              maxLength={500}
              placeholder="Note (optional)"
              onChange={(e) => setDraft({ ...draft, note: e.target.value })}
              style={{ width: '24rem', maxWidth: '100%' }}
            />
          </div>
          <button type="submit" disabled={saving}>
            {saving ? 'Saving…' : 'Save'}
          </button>{' '}
          <button type="button" onClick={() => setDraft(null)} disabled={saving}>
            Cancel
          </button>
        </form>
      )}

      {labels.map((l) => (
        <div
          key={l.id}
          onClick={() => seekTo(l.t)}
          style={{ marginTop: '0.5rem', padding: '0.5rem', borderRadius: '6px', cursor: 'pointer', background: '#f5f5f5' }}
        >
          <strong>{l.t}s:</strong> {l.label} ({l.quality}){l.note && <> - {l.note}</>}{' '}
          <button
            type="button"
            aria-label={`Delete label at ${l.t}s`}
            onClick={(e) => {
              e.stopPropagation();
              remove(l.id);
            }}
          >
            Delete
          </button>
        </div>
      ))}
    </div>
  );
};

export default LabelPanel;
