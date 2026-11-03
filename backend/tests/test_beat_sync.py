import numpy as np
import pytest

pytest.importorskip("librosa")

from scripts import beat_sync  # noqa: E402

SR = 22050
FPS = 30.0


def click_track(bpm: float, seconds: float):
    import librosa

    times = np.arange(0.5, seconds - 0.5, 60.0 / bpm)
    return librosa.clicks(times=times, sr=SR, length=int(seconds * SR), click_freq=1000, click_duration=0.05), times


def motion_at(times, seconds: float):
    """Motion energy with a short bump at each given time."""
    t = np.arange(int(seconds * FPS)) / FPS
    energy = np.full_like(t, 0.01)
    for c in times:
        energy += np.exp(-0.5 * ((t - c) / 0.04) ** 2)
    return energy


@pytest.mark.parametrize("offset", [0.0, 0.07, -0.05])
def test_recovers_known_offset_from_click_track(offset):
    y, beats = click_track(120, 20)
    result = beat_sync.analyze_signals(y, SR, motion_at(beats + offset, 20), FPS)
    assert result["beat_summary"]["count"] >= 15
    assert result["onset_summary"]["median"] == pytest.approx(offset, abs=0.035)
    assert result["beat_summary"]["median"] == pytest.approx(offset, abs=0.05)


def test_percent_within_100ms_separates_tight_from_loose():
    y, beats = click_track(120, 20)
    tight = beat_sync.analyze_signals(y, SR, motion_at(beats + 0.03, 20), FPS)
    loose = beat_sync.analyze_signals(y, SR, motion_at(beats + 0.2, 20), FPS)
    assert tight["onset_summary"]["percent_within"] == 100.0
    assert loose["onset_summary"]["percent_within"] == 0.0


def test_peaks_found_at_bump_times():
    energy = motion_at([1.0, 2.5, 4.0], 6)
    peaks, _ = beat_sync.find_motion_peaks(energy, FPS)
    assert list(np.round(peaks, 1)) == [1.0, 2.5, 4.0]


def test_nearest_offsets_are_signed():
    offsets = beat_sync.nearest_offsets(np.array([1.05, 1.9]), np.array([1.0, 2.0]))
    assert offsets == pytest.approx([0.05, -0.1])


def test_flat_motion_and_empty_inputs_give_no_offsets():
    peaks, _ = beat_sync.find_motion_peaks(np.zeros(100), FPS)
    assert len(peaks) == 0
    assert len(beat_sync.nearest_offsets(np.array([1.0]), np.array([]))) == 0
    assert beat_sync.summarize(np.array([]))["median"] is None


def test_chance_baseline_matches_window_to_grid_ratio():
    refs = np.arange(0.0, 60.0, 0.5)
    peaks = refs[::4] + 0.01
    chance = beat_sync.chance_percent_within(peaks, refs, 60.0, trials=300)
    assert 30 < chance["mean"] < 50  # 0.2 s window over a 0.5 s grid ~ 40%
    assert beat_sync.summarize(beat_sync.nearest_offsets(peaks, refs))["percent_within"] == 100.0


def test_covered_by_drops_peaks_outside_beat_span():
    kept = beat_sync.covered_by(np.array([0.0, 1.0, 5.0, 9.0]), np.array([1.0, 2.0, 3.0, 4.0]))
    assert list(kept) == [1.0]
