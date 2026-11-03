import { useCallback, useEffect, useRef, useState } from 'react';
import axios from 'axios';
import VideoPlayer from './components/VideoPlayer';
import { ToastContainer, toast } from 'react-toastify';
import 'react-toastify/dist/ReactToastify.css';

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000';
const WS_BASE = API_BASE.replace(/^http/, 'ws');

const MAX_RECONNECTS = 5;
const MAX_BACKOFF_MS = 10000;
const POLL_INTERVAL_MS = 5000;
const MAX_POLL_FAILURES = 60; // about 5 minutes without reaching the server

// Must match the files in backend/app/prompts/styles/ (the server rejects other values).
const STYLES = [
  { value: 'hype', label: 'Hype' },
  { value: 'deadpan', label: 'Deadpan' },
  { value: 'broadcast', label: 'Broadcast' },
];

const PHASE_LABELS = {
  uploading: 'Uploading',
  extracting: 'Extracting frames',
  analyzing: 'Analyzing video',
  synthesizing: 'Generating audio',
  completed: 'Done',
  error: 'Failed',
};

const errorDetail = (error, fallback) => {
  const detail = error?.response?.data?.detail;
  return typeof detail === 'string' && detail ? detail : fallback;
};

const exportFileName = (originalName) => `maneframe_${originalName.replace(/\.[^.]*$/, '')}.mp4`;

function App() {
  const [commentaryQuips, setCommentaryQuips] = useState([]);
  const [audioAvailable, setAudioAvailable] = useState(true);
  const [skipped, setSkipped] = useState({ ranges: [], total: 0 });
  const [videoUrl, setVideoUrl] = useState(null);
  const [processingStatus, setProcessingStatus] = useState('idle');
  const [progress, setProgress] = useState(0);
  const [statusMessage, setStatusMessage] = useState('');
  const [errorMessage, setErrorMessage] = useState('');
  const [exportJob, setExportJob] = useState(null);
  const [captions, setCaptions] = useState(true);
  const [rendering, setRendering] = useState(false);
  const [style, setStyle] = useState('hype');

  const socketRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const pollTimerRef = useRef(null);
  const attemptsRef = useRef(0);
  const jobIdRef = useRef(null);
  const terminalRef = useRef(false);
  const closedRef = useRef(false);
  const objectUrlRef = useRef(null);

  const teardownSocket = useCallback(() => {
    closedRef.current = true;
    clearTimeout(reconnectTimerRef.current);
    reconnectTimerRef.current = null;
    clearTimeout(pollTimerRef.current);
    pollTimerRef.current = null;
    const socket = socketRef.current;
    socketRef.current = null;
    if (socket) {
      socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
      socket.close();
    }
  }, []);

  const fail = useCallback((message) => {
    terminalRef.current = true;
    setProcessingStatus('error');
    setErrorMessage(message);
    toast.error(message, { position: 'top-right' });
  }, []);

  const fetchResult = useCallback(async (jobId) => {
    try {
      const { data } = await axios.get(`${API_BASE}/api/jobs/${jobId}`);
      if (jobIdRef.current !== jobId) return;
      setCommentaryQuips(Array.isArray(data.result?.quips) ? data.result.quips : []);
      setAudioAvailable(data.result?.audio_available !== false);
      setSkipped({
        ranges: Array.isArray(data.result?.skipped) ? data.result.skipped : [],
        total: data.result?.total_windows || 0,
      });
      setProcessingStatus('completed');
      setProgress(100);
    } catch (error) {
      if (jobIdRef.current !== jobId) return;
      fail(errorDetail(error, 'Failed to fetch the commentary result.'));
    }
  }, [fail]);

  // Long jobs outlive a flaky WebSocket: once reconnects are exhausted, keep asking the server for the job state.
  const startPolling = useCallback((jobId) => {
    let failures = 0;
    const poll = async () => {
      if (closedRef.current || terminalRef.current || jobIdRef.current !== jobId) return;
      try {
        const { data } = await axios.get(`${API_BASE}/api/jobs/${jobId}`);
        if (closedRef.current || terminalRef.current || jobIdRef.current !== jobId) return;
        failures = 0;
        if (data.status === 'completed') {
          terminalRef.current = true;
          setStatusMessage(data.message || '');
          fetchResult(jobId);
          return;
        }
        if (data.status === 'error') {
          fail(data.message || 'Video analysis failed.');
          return;
        }
        setProcessingStatus(data.status);
        setProgress(data.progress ?? 0);
        setStatusMessage(data.message || '');
      } catch (error) {
        if (error?.response?.status === 404) {
          fail('The server no longer has this job (it may have restarted).');
          return;
        }
        failures += 1;
        if (failures >= MAX_POLL_FAILURES) {
          fail('Lost connection to the server.');
          return;
        }
      }
      pollTimerRef.current = setTimeout(poll, POLL_INTERVAL_MS);
    };
    poll();
  }, [fail, fetchResult]);

  const connectSocket = useCallback((jobId) => {
    const open = () => {
      const socket = new WebSocket(`${WS_BASE}/ws/analyze/${jobId}`);
      socketRef.current = socket;

      socket.onmessage = (event) => {
        attemptsRef.current = 0;
        let msg;
        try {
          msg = JSON.parse(event.data);
        } catch {
          return;
        }

        if (msg.status === 'completed') {
          terminalRef.current = true;
          setStatusMessage(msg.message || '');
          fetchResult(jobId);
        } else if (msg.status === 'error') {
          fail(msg.message || 'Video analysis failed.');
        } else if (msg.status) {
          setProcessingStatus(msg.status);
          setProgress(msg.progress ?? 0);
          setStatusMessage(msg.message || '');
        }
      };

      socket.onclose = () => {
        if (closedRef.current || terminalRef.current || socketRef.current !== socket) return;
        if (attemptsRef.current >= MAX_RECONNECTS) {
          setStatusMessage('Live updates lost, checking the server every few seconds');
          startPolling(jobId);
          return;
        }
        const delay = Math.min(1000 * 2 ** attemptsRef.current, MAX_BACKOFF_MS);
        attemptsRef.current += 1;
        reconnectTimerRef.current = setTimeout(() => {
          if (!closedRef.current && !terminalRef.current) open();
        }, delay);
      };
    };
    open();
  }, [fail, fetchResult, startPolling]);

  useEffect(() => {
    return () => {
      teardownSocket();
      if (objectUrlRef.current) {
        URL.revokeObjectURL(objectUrlRef.current);
        objectUrlRef.current = null;
      }
    };
  }, [teardownSocket]);

  const handleVideoUpload = async (event) => {
    const input = event.target;
    const file = input.files[0];
    if (!file) return;

    teardownSocket();
    jobIdRef.current = null;
    terminalRef.current = false;
    attemptsRef.current = 0;
    setCommentaryQuips([]);
    setAudioAvailable(true);
    setSkipped({ ranges: [], total: 0 });
    setErrorMessage('');
    setStatusMessage('');
    setProgress(0);
    setExportJob(null);
    setProcessingStatus('uploading');

    const formData = new FormData();
    formData.append('file', file);
    formData.append('style', style);

    try {
      const { data } = await axios.post(`${API_BASE}/api/upload`, formData);
      const jobId = data.job_id;
      jobIdRef.current = jobId;
      setExportJob({ jobId, fileName: exportFileName(file.name) });

      const url = URL.createObjectURL(file);
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
      objectUrlRef.current = url;
      setVideoUrl(url);

      closedRef.current = false;
      connectSocket(jobId);
    } catch (error) {
      console.error('Error uploading video:', error);
      fail(errorDetail(error, 'Failed to upload video.'));
    } finally {
      input.value = '';
    }
  };

  const handleDownload = async () => {
    if (!exportJob || rendering) return;
    setRendering(true);
    try {
      // render on the server first (errors come back as JSON), then let the browser stream the file from its URL
      const { data } = await axios.post(`${API_BASE}/api/jobs/${exportJob.jobId}/export`, null, {
        params: { captions: captions ? 1 : 0 },
      });
      const link = document.createElement('a');
      link.href = new URL(data.url, API_BASE).href;
      link.download = exportJob.fileName;
      document.body.appendChild(link);
      link.click();
      link.remove();
    } catch (error) {
      toast.error(errorDetail(error, 'Failed to render the video.'), { position: 'top-right' });
    } finally {
      setRendering(false);
    }
  };

  const inFlight = processingStatus !== 'idle' && processingStatus !== 'completed' && processingStatus !== 'error';

  return (
    <div className="App">
      <header className="App-header">
        <h1>Maneframe</h1>
        <input type="file" accept="video/*" onChange={handleVideoUpload} disabled={inFlight} />
        <label style={{ marginLeft: '0.75rem' }}>
          Style{' '}
          <select value={style} onChange={(e) => setStyle(e.target.value)} disabled={inFlight} aria-label="Commentary style">
            {STYLES.map(({ value, label }) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>

        {processingStatus !== 'idle' && (
          <div style={{ margin: '1rem 0', maxWidth: '360px' }}>
            <div style={{ marginBottom: '0.5rem' }}>
              <strong>{PHASE_LABELS[processingStatus] || processingStatus}</strong>
              {statusMessage && processingStatus !== 'error' && <span> – {statusMessage}</span>}
            </div>
            <div
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={progress}
              style={{ background: '#eee', borderRadius: '4px', overflow: 'hidden' }}
            >
              <div
                style={{
                  width: `${progress}%`,
                  background: processingStatus === 'error' ? '#d32f2f' : '#4caf50',
                  height: '8px',
                  transition: 'width 0.3s',
                }}
              />
            </div>
            <small>{progress}%</small>
          </div>
        )}

        {errorMessage && (
          <div role="alert" style={{ margin: '1rem 0', padding: '0.5rem 1rem', background: '#fdecea', border: '1px solid #f5c2c0', borderRadius: '4px', color: '#8a1c17' }}>
            {errorMessage}
          </div>
        )}

        {processingStatus === 'completed' && exportJob && (
          <div style={{ margin: '1rem 0' }}>
            <button type="button" onClick={handleDownload} disabled={rendering}>
              {rendering ? 'Rendering video…' : 'Download video'}
            </button>
            <label style={{ marginLeft: '1rem' }}>
              <input
                type="checkbox"
                checked={captions}
                onChange={(e) => setCaptions(e.target.checked)}
                disabled={rendering}
              />{' '}
              Captions
            </label>
            {rendering && <div><small>Rendering can take a while for long clips.</small></div>}
          </div>
        )}

        {videoUrl && (
          <div>
            <VideoPlayer
              key={videoUrl}
              videoUrl={videoUrl}
              jobId={exportJob?.jobId}
              apiBase={API_BASE}
              commentaryQuips={commentaryQuips}
              audioAvailable={audioAvailable}
              skipped={skipped}
              processingStatus={processingStatus}
            />
          </div>
        )}
      </header>
      <ToastContainer />
    </div>
  );
}

export default App;
