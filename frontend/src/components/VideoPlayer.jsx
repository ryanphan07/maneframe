import { useEffect, useRef, useState } from 'react';
import LabelPanel from './LabelPanel';

const STALE_SECONDS = 4;
const DUCKED_VOLUME = 0.3;

const formatTime = (sec) => `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, '0')}`;

const VideoPlayer = ({ videoUrl, jobId, apiBase, commentaryQuips, processingStatus, audioAvailable = true, skipped = { ranges: [], total: 0 } }) => {
  const videoRef = useRef(null);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [audioBlocked, setAudioBlocked] = useState(false);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !commentaryQuips.length) return;

    let queue = [];
    let played = new Set();
    let audio = null;
    let originalVolume = null;

    const restoreVolume = () => {
      if (originalVolume !== null) {
        video.volume = originalVolume;
        originalVolume = null;
      }
    };

    const stopAudio = () => {
      if (audio) {
        const current = audio;
        audio = null;
        current.onended = current.onerror = null;
        current.pause();
      }
    };

    const stopAll = () => {
      queue = [];
      stopAudio();
      restoreVolume();
    };

    const playNext = () => {
      if (audio || video.paused) return;
      while (queue.length) {
        const quip = queue.shift();
        if (video.currentTime - quip.timestamp > STALE_SECONDS) continue;

        const clip = new Audio(quip.audio_url);
        audio = clip;
        if (originalVolume === null) originalVolume = video.volume;
        video.volume = DUCKED_VOLUME;

        const done = () => {
          if (audio !== clip) return;
          audio = null;
          playNext();
          if (!audio) restoreVolume();
        };
        clip.onended = done;
        clip.onerror = done;
        clip.play().catch((err) => {
          if (err?.name === 'NotAllowedError') setAudioBlocked(true);
          done();
        });
        return;
      }
      restoreVolume();
    };

    const handleTimeUpdate = () => {
      const t = video.currentTime;
      let active = -1;
      commentaryQuips.forEach((quip, i) => {
        if (quip.timestamp > t) return;
        active = i;
        if (played.has(i)) return;
        played.add(i);
        if (quip.audio_url && t - quip.timestamp <= STALE_SECONDS) queue.push(quip);
      });
      setActiveIndex(active);
      playNext();
    };

    const handleSeeking = () => {
      stopAll();
      played = new Set();
    };

    const handlePause = () => {
      if (audio) audio.pause();
    };

    const handlePlay = () => {
      if (audio) audio.play().catch(() => {});
      else playNext();
    };

    video.addEventListener('timeupdate', handleTimeUpdate);
    video.addEventListener('seeking', handleSeeking);
    video.addEventListener('pause', handlePause);
    video.addEventListener('play', handlePlay);
    video.addEventListener('ended', stopAll);
    return () => {
      video.removeEventListener('timeupdate', handleTimeUpdate);
      video.removeEventListener('seeking', handleSeeking);
      video.removeEventListener('pause', handlePause);
      video.removeEventListener('play', handlePlay);
      video.removeEventListener('ended', stopAll);
      stopAll();
    };
  }, [commentaryQuips]);

  const seekTo = (timestamp) => {
    if (videoRef.current) videoRef.current.currentTime = timestamp;
  };

  const isCompleted = processingStatus === 'completed';

  return (
    <div>
      <video ref={videoRef} src={videoUrl} controls style={{ maxWidth: '100%' }} />

      {audioBlocked && (
        <p role="status" style={{ color: '#8a6d00' }}>
          The browser blocked commentary audio. Press play on the video to enable it.
        </p>
      )}

      {isCompleted && !audioAvailable && (
        <p role="status" style={{ color: '#8a6d00' }}>
          Audio is unavailable for this clip. Commentary is shown as text only.
        </p>
      )}

      {isCompleted && skipped.ranges.length > 0 && (
        <p role="status" style={{ color: '#8a6d00' }}>
          Skipped {skipped.ranges.length} of {skipped.total} parts because the model failed on them
          ({skipped.ranges.map((r) => `${formatTime(r.start)}-${formatTime(r.end)}`).join(', ')}). Those parts have no commentary.
        </p>
      )}

      {isCompleted && (
        <div className="commentary-display" style={{ marginTop: '1rem' }}>
          <h3>Commentary</h3>
          {commentaryQuips.length === 0 && <p>No commentary was generated.</p>}
          {commentaryQuips.map((quip, index) => (
            <div
              key={index}
              className={index === activeIndex ? 'highlight' : ''}
              onClick={() => seekTo(quip.timestamp)}
              style={{
                marginBottom: '0.5rem',
                padding: '0.5rem',
                borderRadius: '6px',
                cursor: 'pointer',
                background: index === activeIndex ? '#fff3cd' : '#f5f5f5',
              }}
            >
              <strong>{quip.timestamp}s:</strong> {quip.text}
              {!quip.audio_url && <em> (no audio)</em>}
            </div>
          ))}
        </div>
      )}

      {isCompleted && jobId && <LabelPanel videoRef={videoRef} jobId={jobId} apiBase={apiBase} />}
    </div>
  );
};

export default VideoPlayer;
