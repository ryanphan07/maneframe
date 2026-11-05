# Eval set and baseline (session 36)

## The set
Five segments of 35 to 40 s cut from `hoco2026.MOV` (4K HEVC HDR, 120 fps, 327 s, one fixed wide stage shot: blue, yellow and pink lions, drummers at the right) with stream copy (`ffmpeg -ss <start> -i hoco2026.MOV -t <len> -map 0:v:0 -map 0:a:0 -c copy`). They live outside the repo in `C:\Users\ryanp\Downloads\maneframe_eval\` (about 630 MB each); `eval/clips.txt` lists them (that file is git-ignored). Run with `--prepared-dir` so each clip goes through the app's prepare step (upright SDR H.264, cached in `<clip folder>\prepared\`) first, as a real job does.

| clip | source range | what happens |
|---|---|---|
| hoco2026_01_opening | 0:00-0:35 | Blue lion enters from the right beside the drummers, yellow then pink follow; all three settle into low poses. |
| hoco2026_02_drummers | 1:45-2:20 | Drummers drum at right while the pink lion rears up beside them; blue and yellow rise and drop. |
| hoco2026_03_close | 2:30-3:05 | Lions touch and overlap: blue and pink side by side, then blue and yellow in a hug or lift. |
| hoco2026_04_action | 3:55-4:30 | Lions rear up tall, unfurl banners (BEARS, red, CAL), then crouch around a red envelope. |
| hoco2026_05_ending | 4:50-5:26 | Lions exit left, drummers carry the drum off, the stage is empty for the last 5 s. |

## Baseline: Qwen3.6-35B-A3B UD-Q3_K_XL, hype, prompt e8695b3d, 4 windows per clip
Two runs of the identical prompt (`eval/results/s36a`, `s36b`). `grounding_check.py` strips (frames at t-1.5, t, t+1.5) were judged by the Claude session, one judge, with one rule: a line is **yes** if its concrete claims (which lion, pose, position, direction, props, who is on stage) match the frames, humour and idioms ignored; **no** if a concrete claim contradicts them; **unclear** if it rests on something the frames cannot show (faces, ears, eyes, gaze, head shakes) or on invented detail. Rate = yes / all lines.

| run | lines | yes | no | unclear | grounded |
|---|---|---|---|---|---|
| A | 60 | 26 | 12 | 22 | 43% (95% Wilson CI 32-56%) |
| B | 59 | 33 | 10 | 16 | 56% (43-68%) |
| both | 119 | 59 | 22 | 38 | **50% (41-58%)** |

Per clip, yes/lines (A, B): opening 3/12, 7/11; drummers 5/12, 7/12; close 5/12, 4/12; action 6/12, 9/12; ending 7/12, 6/12.

**Run-to-run noise: 13 points** (43% vs 56%) from the same prompt on the same clips, with no two lines alike. The CIs treat lines as independent; lines within a window share their frames, so the true interval is wider. Earlier single-clip results (docs/research/grounding.md: 58-67% baseline, differences of 8 to 16 points between prompts) sit inside this noise. A prompt change needs a gain well above 13 points on this set, or several runs, to count. The 50% here is lower than the 62% on hoco_40s; this set has more lion-on-lion contact and a banner sequence, and different judging was used, so do not read it as a regression.

## Failure types (60 lines that were not "yes", both runs)
| type | count | share | examples |
|---|---|---|---|
| Invented event, prop or story | 18 | 30% | "hops over yellow", "crashed into", "final cymbal crash", "drummers still playing" (they are carrying the drum), "tail dancer holds the banner" |
| Pose, motion or direction wrong at t | 17 | 28% | "goes airborne" (rising, not off the floor), "makes a break to the right" (moves left), "pink drops" while reared, "blue takes a knee" while standing tall |
| Face, head, ear, gaze claims the frames cannot show | 16 | 27% | "head twitches", "ears perk up", "squints", "shakes head", "swaying head" |
| Wrong lion or wrong time | 5 | 8% | "pink makes a grand entrance" when yellow enters; "yellow enters" when it was on stage; "takes the stage first" |
| Pose unreadable from stills | 4 | 7% | "takes a knee", "rears back up" at a mid pose |

Patterns worth acting on:
- The model does not know the clip is ending: on the empty stage it says "the main event is loading up" (5 lines across both runs). A rule such as "if the stage is empty or only the drum crew remains, say the performance is over" is testable.
- Motion verbs for micro-movements (head, ears, eyes) are 27% of the misses and are never checkable from frames 1.5 s apart; a previous attempt to forbid them (grounding.md v2, v3) moved the error to pose labels. The prompt-only route is exhausted; the second-pass "pose facts" idea in grounding.md is the next candidate.
- Reliable: crouched / reared / standing poses of a named lion, stage position, who is on stage, drummers carrying the drum off, empty stage.

## Judging caveats
One judge, strips at 1500 px wide where small lions are hard to read, and borderline calls (mid poses, idioms) sit with the judge: moving the five or six borderline lines changes a run's rate by about 8 points, which is the same size as the noise. The review sheets are in `logs/grounding/` (git-ignored) with a reason per line.

## Owner funniness ratings (session 26 runs)
The owner rated lines from the session 26 runs on `hoco_40s.mp4` (1 to 5, 5 funniest; `eval/results/s26-9b.csv`, `s26-35b.csv`):

| model | style | rated | mean |
|---|---|---|---|
| 9B | hype | 11 | 2.45 |
| 9B | deadpan | 12 | 2.67 |
| 35B | hype | 12 | 2.75 |
| 35B | deadpan | 12 | 2.75 |
| both | hype | 23 | 2.61 |
| both | deadpan | 24 | 2.71 |

About 12 lines per cell, so the differences between styles and models are within noise.
