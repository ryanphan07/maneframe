import cv2
import numpy as np
import pytest

from app.services import framing
from app.services import video_processor as vp


def test_segment_centers_hold_the_last_known_center_and_fill_a_leading_gap():
    boxes = [None, (0.2, 0.0, 0.4, 1.0), None, (0.6, 0.0, 0.8, 1.0)]
    assert framing.segment_centers(boxes) == pytest.approx([0.3, 0.3, 0.3, 0.7])
    assert framing.segment_centers([None, None]) == [0.5, 0.5]


def test_smoothing_ignores_small_moves_and_limits_the_pan_speed():
    window = 0.3
    assert framing.smooth_centers([0.5, 0.52, 0.48, 0.5], 1.5, window) == [0.5] * 4  # inside the dead band
    out = framing.smooth_centers([0.2, 0.9, 0.9, 0.9, 0.9, 0.9], 1.5, window)
    max_step = framing.MAX_PAN_SPEED * window * 1.5
    steps = [b - a for a, b in zip(out, out[1:])]
    assert all(s <= max_step + 1e-9 for s in steps) and steps[0] == pytest.approx(max_step) and out[-1] > out[1]
    assert framing.smooth_centers([], 1.5, window) == []


def test_mode_is_fit_when_the_action_is_wider_than_the_window_for_enough_segments():
    narrow, wide = (0.4, 0, 0.6, 1), (0.1, 0, 0.7, 1)
    assert framing.choose_mode([narrow] * 6, 0.316) == "crop"
    assert framing.choose_mode([wide] * 6, 0.316) == "fit"
    assert framing.choose_mode([wide] * 3 + [narrow] * 3, 0.316) == "fit"  # exactly the share
    assert framing.choose_mode([wide] * 2 + [narrow] * 4, 0.316) == "crop"
    assert framing.choose_mode([None, None], 0.316) == "crop"
    assert framing.choose_mode([None, wide, wide], 0.316) == "fit"  # segments without motion do not count


def test_pan_keyframes_cover_the_clip_and_clamp_to_the_frame():
    keys = framing.pan_keyframes([0.0, 0.5, 1.0], 1.5, 4.5, 1920, 608)
    assert [round(t, 2) for t, _ in keys] == [0.0, 0.75, 2.25, 3.75, 4.5]
    assert [x for _, x in keys] == pytest.approx([0, 0, 656, 1312, 1312])
    short = framing.pan_keyframes([0.5], 1.5, 1.0, 1920, 608)  # one segment longer than the clip
    assert short[0][0] == 0.0 and short[-1][0] == 1.0


def test_pan_expression_is_a_number_for_a_still_window_and_a_balanced_tree_otherwise():
    assert framing.pan_expression([(0.0, 656.0), (5.0, 656.0)]) == "656"
    keys = [(i * 1.5, float(i * 10)) for i in range(1200)]  # a 30 minute clip
    expr = framing.pan_expression(keys)
    depth = max(expr[:i].count("if(") - expr[:i].count("),") for i in range(len(expr)))
    assert depth < 40 and expr.count("if(lt(t,") == 1198


def test_caption_goes_to_the_top_only_when_performers_cover_the_bottom_band():
    assert framing.caption_zone([(0.35, 0.55)]) == "bottom"
    assert framing.caption_zone([(0.55, 0.85)]) == "top"
    assert framing.caption_zone([]) == "bottom"
    assert framing.caption_zone([(0.0, 1.0)]) == "bottom"  # equal cover keeps the default


def test_zone_chooser_looks_only_at_the_segments_under_the_caption():
    boxes = [(0, 0.2, 1, 0.4), (0, 0.6, 1, 0.9), (0, 0.2, 1, 0.4)]
    zone = framing.zone_chooser(boxes, 1.5, lambda v: v)
    assert [zone(0.0, 1.4), zone(1.6, 2.9), zone(3.1, 4.4)] == ["bottom", "top", "bottom"]


def test_motion_track_follows_a_subject_across_segments(tmp_path):
    path = str(tmp_path / "moving.mp4")
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 180))
    for n in range(80):  # 8 s: a bright block on the left until 2.5 s, none until 5.5 s, then on the right
        frame = np.full((180, 320, 3), 20, np.uint8)
        if n < 25 or n >= 55:
            x = 20 + (n % 5) * 4 if n < 25 else 230 + (n % 5) * 4
            cv2.rectangle(frame, (x, 60), (x + 50, 140), (255, 255, 255), -1)
        writer.write(frame)
    writer.release()
    boxes = vp.motion_track(path, 8.0, seg_sec=2.0)
    centers = framing.segment_centers(boxes)
    assert len(boxes) == 4 and boxes[0] and centers[0] < 0.4 < 0.6 < centers[-1]
    part = vp.motion_track(path, 8.0, start=4.0, end=8.0, seg_sec=2.0)  # tracks only the trimmed range
    assert len(part) == 2 and framing.segment_centers(part)[-1] > 0.6
