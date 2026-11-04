# Grounding of commentary (session 35)

Question: how often is a quip about something visible at its timestamp, and can a prompt rule raise that rate?

## Method
- `backend/scripts/grounding_check.py extract <eval csv> --clip-dir <dir>` writes, per quip, a strip of the frames at t-1.5 s, t and t+1.5 s (cropped like the model's frames) and a review sheet in `logs/grounding/` (git-ignored). `summarize <sheet>` prints yes / no / unclear and the rate per prompt_hash and style.
- Judged by the Claude session, looking at the strips: **yes** = every claim in the line matches the frames; **no** = a named lion does something it does not do, a lion is swapped, or motion is claimed that the frames contradict; **unclear** = the claim cannot be told from the frames (for example a head shake from stills) or is invented detail. Rate = yes / all lines (unclear counts against).
- Clip hoco_40s.mp4, Qwen3.6-35B-A3B UD-Q3_K_XL, 4 windows, 12 quips per style per run. One judge, one clip, one run per prompt: a difference of 1 to 2 lines (8 to 16 points) is noise.

## Results
| prompt_hash | prompt | style | yes | no | unclear | rate |
|---|---|---|---|---|---|---|
| e8695b3d | baseline | hype | 7 | 3 | 2 | 58% |
| a4a9b663 | baseline | deadpan | 8 | 2 | 2 | 67% |
| d85b07a6 | v2 rules | hype | 6 | 3 | 3 | 50% |
| 38c85ab7 | v2 rules | deadpan | 8 | 3 | 1 | 67% |
| d8d3aef3 | v3 closed list | hype | 6 | 3 | 3 | 50% |
| cce5dbd5 | v3 closed list | deadpan | 5 | 5 | 2 | 42% |

Baseline overall 15/24 (62%). Neither variant beat it, so **no prompt change was kept**; `styles/*.txt` are unchanged.

## Failure patterns (baseline)
1. Motion claimed from stills: "head shake", "stepped forward", "airborne", "wakes up". Frames about 1.5 s apart cannot show a shake.
2. Lion swapped: the pink lion gets the yellow lion's rear-up (hype t 5.8, deadpan t 7.5).
3. A moving lion called still (deadpan t 20.8, "blue stillness" while blue rises).
4. Rear-up and "airborne" used for a lean.
What is reliable: crouched / reared-up / standing poses of a named lion, and static "holds the pose" lines.

## Variants tried (appended to hype.txt and deadpan.txt, then reverted)
- v2, four bullets: check the named lion's pose at t, prefer single-frame poses, claim motion only if two frames differ, no speed/emotion claims. Still produced "bobs its head", "adjusts its ears", "yellow wakes up".
- v3, closed list of allowed claims (crouched, upright, reared up, leaning, head down, still, stage position), no motion verbs, no ears/eyes/feelings. It removed motion verbs in hype but the model then mislabeled poses and positions ("Blue... center stage" for the left lion, "all three stand" while two rear up), and deadpan fell to 42%.
Reading: the model's errors are mostly wrong perception of pose, not license to embellish, so rules in the prompt do not fix them. Lines also got no more bland by count, but with the rate unchanged there is nothing to trade.

## Before / after examples (same moment, baseline vs variant)
| t | baseline | variant |
|---|---|---|
| 5.8 hype | Pink lion goes airborne! That is a serious rear-up! (no) | v2: Pink lion rears up! A true acrobatic power play! (no) |
| 7.5 deadpan | Pink lion rears up on its hind legs. Dramatic pause. (no) | v3: Blue lion stands tall. Yellow crouches. Pink leans left. (no, yellow is the one reared) |
| 20.8 hype | Yellow lion is shaking that head! Looking for the snack! (unclear) | v3: Blue lion's taking the spotlight. Center stage, baby! (no) |
| 10.8 deadpan | The blue lion holds the pose. It is quite the statue. (yes) | v3: The yellow lion is the only one refusing to take a knee. (no, all crouched) |
| 39.3 hype | Blue lion goes vertical! Towering over California! (yes) | v3: Blue lion's rear-up! The sky is the limit! (yes) |

## Ideas not tried
- A second pass that gives the model one frame and asks only "which lions are reared up / crouched / standing" and feeds that as facts into the commentary call (costs one more LLM call per window).
- Fewer, more separated frames per window are not the issue here; denser frames (FRAMES_PER_WINDOW above 4) would let motion claims be checked, at a speed cost.
- Larger sample (more clips, a second judge) before any rule is accepted; the current n is too small to detect a 10-point change.
- Proposed diff for `commentary_system.txt`: none; the rules tested did not help.

# Session 37: four arms on the 5-clip eval set (35B UD-Q3_K_XL, hype)

Baseline = session 36's two runs (prompt e8695b3d, 6 frames at 896 px, `eval/results/s36a`, `s36b`). Every arm is a flag of `scripts/eval_commentary.py`; no default changed unless stated. 2 runs per arm, all 5 clips (4 windows each, 20 windows, about 60 lines per run).

## Method
- Blind review: `scripts/blind_review.py make` pools the lines of the baseline and the arm runs into one shuffled set of pages (id, clip, t, line, frame strip at t-1.5, t, t+1.5); arm, run, prompt hash and model are only in `key.csv`. The Claude session judged every line from the pages with the session 36 rubric (yes / no / unclear; unclear counts against), then `unblind` joined the key. One judge.
- The baseline was re-judged blind (its lines were pooled with the first arm), so its rate differs from session 36's unblinded 50%: 54/119 = 45% here. Later arms are pooled with their own lines only (the arms depend on each other, so one pool at the end was not possible); the blind baseline verdicts from the first pool are reused, so those later comparisons are not blind with respect to the baseline.
- Decision rule (fixed beforehand): an arm wins only if it beats baseline by >= 10 points on each of its 2 runs and stays within 1.5x the current seconds per window.
- Failure types: inv = invented event/prop/story, pose = pose/motion/direction wrong at t, face = face/head/ear/gaze claim, who = wrong lion or time, unread = pose or motion that stills cannot show (small movements: stirs, twitches, stretches, lifts head). In session 36 these micro-motions were counted under face or pose; here they have their own bucket, so type counts are not directly comparable with `eval_set.md`.

## Arm r: narrow rules (`scripts/eval_arms/r_narrow_rules.txt`, appended to hype; prompt hash 54c93a21)
Rules: no head-shake plays and no claims about faces, eyes, gaze, ears or expressions; empty stage or ended dance: say so or stay quiet, never invent what comes next.

| | lines | yes | no | unclear | grounded (Wilson 95%) |
|---|---|---|---|---|---|
| baseline A (blind) | 60 | 25 | 20 | 15 | 42% (30-54) |
| baseline B (blind) | 59 | 29 | 18 | 12 | 49% (37-62) |
| baseline pooled | 119 | 54 | 38 | 27 | 45% (37-54) |
| r run 1 | 60 | 31 | 13 | 16 | 52% (39-64) |
| r run 2 | 60 | 32 | 13 | 15 | 53% (41-65) |
| r pooled | 120 | 63 | 26 | 31 | 52% (44-61) |

Failure types (lines that were not yes), baseline pooled vs r pooled: face 19 vs 2, inv 16 vs 17, pose 20 vs 16, who 6 vs 1, unread 4 vs 21. Time: 31.3 s and 34.4 s per window; prompt tokens 2886-3900 per call (mean 3253). The baseline's own timing was not recorded; BENCHMARKS.md gives 39-49 s per call for this model, so r is not slower.

Verdict: **not a winner, reverted** (the rules are not in `styles/hype.txt`). Gain over the pooled baseline is +6 and +8 points; against the matched baseline run it is +10 (A) and +4 (B). The rule did what it said on its target: face/gaze claims fell from 19 to 2. But the model moved to micro-motion verbs the stills cannot show ("stirs", "twitching in its sleep", "lifts its head"), so "unread" rose from 4 to 21 and the grounded rate barely moved. The "empty stage / do not invent what comes next" half helped less than hoped: lines like "the beat is about to drop" and "still dropping the beat" survived (next-event lines 7 in the baseline, 2 in r).

Examples (arm shown after un-blinding):
- 05_ending t=33.9, r: "Just the lights left. The show is over." yes (empty stage).
- 05_ending t=29.1, baseline: "Two drummers holding center. The main event is coming!" no, inv (they are carrying the drum off, stage left).
- 01_opening t=25.8, r: "Blue lion is twitching in its sleep like it chased a dream squirrel!" unclear (micro motion).

## Arm a: more visual evidence (`--frames 10 --frame-width 640`, prompt unchanged, hash e8695b3d)
10 frames per window at 640 px instead of 6 at 896. Prompt tokens per call 2102-3559 (mean about 2790), under the 8K context and below arm r's mean of 3253 (the baseline's token counts were not recorded; its config is the same).

Pool: both runs plus 15 random lines from each baseline run (a rubric-drift check; the 30 baseline lines were 12 yes = 40%, against 45% for the full blind baseline, so the rubric held within noise).

| | lines | yes | no | unclear | grounded (Wilson 95%) | s/window |
|---|---|---|---|---|---|---|
| a run 1 | 59 | 21 | 18 | 20 | 36% (25-48) | 40.1 |
| a run 2 | 60 | 23 | 17 | 20 | 38% (27-51) | 30.1 |
| a pooled | 119 | 44 | 35 | 40 | 37% (29-46) | 35.1 |

Failure types, baseline pooled (119) vs a pooled (119): inv 16 vs 26, pose 20 vs 18, face 19 vs 10, who 6 vs 2, unread 4 vs 19.

Verdict: **not a winner, reverted** (defaults unchanged). It is 8 and 7 points below the baseline, not above. Small lions in 640 px frames are harder to read than in 896 px ones, so more frames did not buy more evidence; the extra frames came with more invented events (26 vs 16) and more micro-motion claims ("stretches", "pops up"). It is no slower than the baseline (35 s per window).

Examples:
- 04_action t=7.5, a: "Scrolls drop! Bears versus Cal. It's a rivalry show!" no, inv (the scrolls are already unfurled in the first frame).
- 05_ending t=36.1, a: "Empty stage. Loud drums. The California vibe stays." no, inv (empty and silent: no drummers on stage).
- 03_close t=11.5, a: "Pink lion enters the pit, ready to pounce!" no, inv.
