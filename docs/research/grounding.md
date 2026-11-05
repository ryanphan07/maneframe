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

## Arm b: two-step facts (`--facts`, commentary prompt unchanged, hash e8695b3d)
Per window, a first call (the same frames, schema-constrained, temperature 0.2) returns for each frame the lions as closed-enum strings ("blue left crouched": color, left/center/right, pose; 60 values) and whether drummers are visible; `format_facts` turns that into one line per frame and `llm.analyze_window(..., facts=...)` appends it to the commentary call's intro text. Eval-only (`--facts`); the job API is unchanged. A first schema with one object per lion (817 completion tokens) was too slow and was replaced by the compact enum one before any run. Prompt tokens per window: facts call 1951, commentary call about 3287 (arm r: 3253), completion tokens 217 + 124.

Pool: both runs plus 15 random lines from each baseline run (drift check: 15 of 30 yes = 50%, against 45% for the full blind baseline; with arm a's sample the two checks give 40% and 50%, so the rubric held within noise).

| | lines | yes | no | unclear | grounded (Wilson 95%) | s/window |
|---|---|---|---|---|---|---|
| b run 1 | 59 | 37 | 9 | 13 | 63% (50-74) | 66.5 |
| b run 2 | 60 | 31 | 9 | 20 | 52% (39-64) | 99.6 |
| b pooled | 119 | 68 | 18 | 33 | 57% (48-66) | 83.1 |

Failure types, baseline pooled (119) vs b pooled (119): inv 16 vs 19, pose 20 vs 8, face 19 vs 9, who 6 vs 9, unread 4 vs 6.

Verdict: **not a winner, reverted** (nothing to revert: flag only, defaults unchanged). It fails both parts of the rule. Grounding: +18 points on run 1 but +7 on run 2, so not >= 10 on each run. Time: 66.5 and 99.6 s per window against about 32 s (arm r's 31-34 s, used as the baseline proxy), 2.0x and 3.1x, over the 1.5x limit (about 49 s). Run 2 was 50% slower than run 1 with identical token counts in both calls (1951 / 3285-3289), so the gap is not from the prompts; the cause was not determined (host load is a guess, nothing was measured), but run 1 alone is already over the limit, so it does not change the verdict.
What it did: pose errors fell from 20 to 8 and face claims from 19 to 9, which is the direction the facts call was meant to push. Invented events did not fall (19 vs 16) and wrong-lion/wrong-time errors rose (9 vs 6): the commentary call takes the facts at face value but the lines are often one beat late ("drops" when the lion is already down).
Worth a later look: the pooled gain is +12 points (57% vs 45%) but the intervals overlap (48-66 vs 37-54), so on this evidence it is not even a clear grounding gain, and it costs 2.6x the time per window.

Examples:
- 04_action t=7.5, b: "Scrolls unfurl! Pink says BEARS. Yellow has red. Blue spells CAL. Stage is lit!" yes.
- 04_action t=0.8, b: "Pink on the left, low to the ground. Blue on the right, also crouched. Yellow center is standing tall." yes (every position and pose matches).
- 03_close t=20.8, b: "Pink and yellow holding the low horse stance. Discipline." no, pose (pink is upright at t).

## Arm c: Qwen3.5-9B vs Qwen3.6-35B (default config, hash e8695b3d)
No arm won, so "best of r/a/b" is the unchanged default; arm c is the 9B (Qwen3.5-9B-Q4_K_M + mmproj-Qwen3.5-9B-F16, llama-server restarted with `-c 8192 -np 1 -ngl 99 --jinja`) against the 35B baseline on the same prompt and frames (6 frames at 896 px). Prompt tokens per call about 3100, completion about 127.

Pool: both 9B runs plus 15 random lines from each baseline run (drift check: 14 of 30 yes = 47%, against 45% for the full blind baseline).

| | lines | yes | no | unclear | grounded (Wilson 95%) | s/window |
|---|---|---|---|---|---|---|
| c run 1 | 56 | 22 | 9 | 25 | 39% (28-52) | 50.4 |
| c run 2 | 58 | 25 | 8 | 25 | 43% (31-56) | 48.1 |
| c pooled | 114 | 47 | 17 | 50 | 41% (33-50) | 49.3 |

Failure types, baseline pooled (119) vs c pooled (114): inv 16 vs 16, pose 20 vs 10, face 19 vs 15, who 6 vs 4, unread 4 vs 22.

Verdict: **not a winner, no change** (the 35B stays the model for real runs). The 9B is 4 points below the 35B baseline (41% vs 45%, intervals overlap) and slower in this setup (49 s per window against about 32 s for the 35B, 1.5x; the cause was not investigated). Its mistakes are different: fewer wrong poses (10 vs 20) but many more lines about motion the stills cannot show ("unread" 22 vs 4: stretches, bobs, head dips). The 9B stays the development model; this does not change that.

Examples:
- 04_action t=25.8, c: "Blue lion rises on the right. Look at that power!" yes.
- 04_action t=32.4, c: "Yellow lion stays low and steady while the others move!" no, pose (yellow stands upright).
- 05_ending t=24.2, c: "The stage is empty. The lion has officially left the building." no, inv (two drummers are still carrying the drum off).

## Session 37 summary
| arm | pooled grounded | per-run vs baseline (45%) | s/window | result |
|---|---|---|---|---|
| r narrow rules | 52% | +7, +8 | 31-34 | not a winner |
| a 10 frames at 640 px | 37% | -9, -7 | 30-40 | not a winner |
| b two-step facts | 57% | +18, +7 | 67-100 | not a winner (time and run 2) |
| c 9B model | 41% | -6, -2 | 48-50 | not a winner |
No default changed. Combinations were not tried: the rule allows them only if each arm won alone. Best candidates for a later session (not run): arm r's face/gaze ban plus a ban on micro-motion claims (its "unread" lines went from 4 to 21), and arm b with a cheaper facts call (it halved pose errors but cost 2x the time).


# Session 38: owner labels, rubric v3, glossary and examples measured

## Judge check against the owner
The owner labeled 20 lines (every third line) in `logs/grounding/20261104-031755.csv`. That sheet is the 9B run 2 of session 37 (arm c2, `eval/results/s37c2`), not a baseline run as the session prompt said. Its lines were in the arm c blind pool, so they were matched by clip and text to the blind verdicts of session 37 (all 20 found).

Agreement: **8 of 20** (40%). Every disagreement (line; owner's call and reason; the session's blind call):

| line | owner | session |
|---|---|---|
| 01 t=0.8 "Blue lion gets the show off to a loud start!" | yes: blue enters first | unclear (inv) |
| 01 t=12.5 "Blue lion goes low, like it's waiting for a snack." | unclear: blue is going to sleep, purple next | yes |
| 01 t=22.5 "Grey lion lifts head. It's waking up from its nap!" | yes: purple (not grey) is starting to wake up | unclear (unread) |
| 01 t=33.8 "Yellow lion stays centered, like a mascot waiting for a dunk." | no: yellow is moving around | yes |
| 02 t=31.5 "Musicians are locked in. That drum roll is serious business." | yes: technically correct, the beat has been there all along | unclear (unread) |
| 03 t=7.5 "Yellow lion just woke up. Stretching like it forgot the drum beat." | yes: yellow is behind the other two, not on the beat | unclear (inv) |
| 03 t=14.2 "Blue lion rears up! Tail dancer lifts head dancer like a basketball dunk." | no: no stunt or lift in the frames | yes |
| 03 t=25.8 "Blue and Yellow lions lock up! A sudden, sticky handshake." | no: they are in line, one behind the other (depth), no handshake | yes |
| 03 t=32.2 "Blue lion tries to lift Yellow, but the hug is too sticky!" | no: all in line, no hug or lift | unclear (inv) |
| 04 t=32.4 "Yellow lion stays low and steady while the others move!" | unclear: the lions are bowing | no (pose) |
| 05 t=19.2 "Blue lion's ears perk up. Something smells good over there." | yes (true, but the lions running off stage is the thing to note) | unclear (face) |
| 05 t=29.2 "Two drummers finish up as the stage goes dark. Curtain call!" | yes: drummers are the last seen and are leaving | no (inv) |

Pattern: the session was too strict on color names, ear and face actions the strip shows, figurative readings and drummers leaving (7 of 12), and too lenient on contact claims: lions standing in a line seen from the front overlap in the picture, which is not a hug, lock or lift (3 of 12). Two calls (t=12.5, t=32.4) are close to a coin flip either way. The session that judged is also the one being checked, so this tests the rubric, not independence.

## Rubric v3 (used from session 38 on)
Judge a line against the strip (frames at t-1.5, t, t+1.5); humor and idiom are ignored.
- **yes**: every concrete claim matches the frames, or the statement is true even if trivial ("the beat has been there all along").
- Acceptable under yes: a wrong color name when the lion is unambiguous (pink called purple, grey or white); ear, face, mouth and head actions when the strip shows them; a figurative reading of a visible behavior ("forgot the beat" for a lion behind the beat, "bailed" for a lion leaving).
- **no**: a claim the frames contradict: wrong lion, wrong pose or direction, a lift, hug, lock, hop, stunt or banner drop that is not there. Lions overlapping in depth are not in contact. Drummers carrying the drum off are not "still playing", and "the main event is coming" after the dance ended is a no.
- **unclear**: the claim cannot be told from the stills (micro-motions the strip does not show, a head shake, a scroll in the mouth read as a grin).
- Failure types: inv, pose, face, who, unread (as in session 37) plus **named** = the line names a specific move or routine part (7-star, bows, Modern 1, head spin, a stunt) that the strip does not show.
- **notable** yes/no, a second label: is this the moment worth saying here? The owner treats entrances, stunts, scroll presentations, formation changes and exits as notable and "true but not exciting" as not. Recorded only; no decision rule uses it yet.

v3 is looser than v2 on face and micro-motion lines and stricter on contact lines, so session 38 rates are not strictly comparable with session 37's. The baseline was judged again under v3 for that reason.

## Prompt with owner glossary and examples (hash 5d0d5557) against the baseline
Under test: `lion_dance_glossary.txt` (owner's sections Moves, Stunts, Routine with nested steps) and `examples.txt` (4 owner lines) in the system prompt. Everything else at defaults (35B UD-Q3_K_XL, hype, 6 frames at 896 px, LLM_CTX 8192, LLM_MAX_TOKENS 500). Two runs on the 5-clip set (`eval/results/s38n1`, `s38n2`, `--prepared-dir`), then one blind pool of 237 lines: both session 36 baseline runs (`s36a`, `s36b`, the session 37 baseline) plus the two new runs, judged under rubric v3 (`logs/grounding/blind/pool_s38`).

Tokens and time: prompt tokens per call 5350-6349 (mean 5710), against a mean of about 3250 for the baseline prompt, so the glossary and examples add about 2460 tokens. Completion tokens mean 122 and 116 (max 147). Headroom under LLM_CTX 8192 at the largest call: 8192 - 6349 - 500 (LLM_MAX_TOKENS) = 1343 tokens. The first window of a clip, which has no earlier-window context, measured 6170 in a one-window probe. That is under the 7000 line, so the glossary was not trimmed. Free RAM at the start: 1.01 GB (run 1), 2.21 GB (run 2), with llama-server holding about 4 GB. Seconds per window 39.9 and 40.6 (1.8% apart, no rerun). The baseline's own seconds per window were not recorded in session 36; arm r's 31.3-34.4 s is the proxy, so the new prompt is about 25% slower, in line with the extra prompt tokens.

| | lines | yes | no | unclear | grounded (Wilson 95%) | notable | s/window |
|---|---|---|---|---|---|---|---|
| baseline A | 60 | 34 | 16 | 10 | 57% (44-68) | 28% | n/a |
| baseline B | 59 | 31 | 18 | 10 | 53% (40-65) | 29% | n/a |
| baseline pooled | 119 | 65 | 34 | 20 | 55% (46-63) | 29% (21-37) | proxy 31-34 |
| new run 1 | 60 | 35 | 13 | 12 | 58% (46-70) | 27% | 39.9 |
| new run 2 | 58 | 32 | 13 | 13 | 55% (42-67) | 26% | 40.6 |
| new pooled | 118 | 67 | 26 | 25 | 57% (48-65) | 26% (19-35) | 40.3 |

Failure types (lines that were not yes), baseline pooled vs new pooled: pose 15 vs 15, inv 14 vs 6, face 13 vs 12, unread 7 vs 13, who 5 vs 3, named 0 vs 2.

Mistake claims (a line saying a lion erred or is late; counted by hand): baseline 2 ("late for a meeting", "stuck in neutral"), new 1 ("officially late to the party"). The model did not copy the owner's "oopsie" or "lost" examples; no new line says oopsie or lost. The word list in `blind_review.py` also catches "behind" used for position ("right behind it", "behind the defense"), which is not a mistake.

Result: no change in grounding (+2 points pooled, intervals overlap almost fully, run-to-run noise is about 13 points), so the 10-point drop trigger did not fire and no fix is proposed. Invented events fell (14 to 6) and unreadable micro-motion rose (7 to 13). The new risk is named moves the strip does not show: "Here come the head spins!" (04_action t=32.0) and "bowing low to the drummers" (05_ending t=8.0), 2 lines of 118. Notable lines stayed at about a quarter of all lines, so the examples did not make the lines more eventful. The cost is about 25% more time and a prompt of 5.7K of 8.2K tokens.

Examples (arm shown after un-blinding):
- 04_action t=7.5, new run 1: "Scrolls revealed! Purple says 'Go Bears'!" yes, notable.
- 04_action t=32.0, new run 2: "Here come the head spins! Look at that momentum!" no, named.
- 03_close t=20.8, new run 1: "Blue lion is officially late to the party. Look at that head shake!" unclear, face (the only mistake claim in the new runs).

Owner humor sheet (not waited for): `logs/grounding/owner_humor/pool_s38/` has 15 random baseline lines and 15 new lines, shuffled, with the origin in `key.csv` only. Fill the `rating (1-5)` column of `sheet.csv`, or read `index.html`, which shows each line with its strip.


# Session 39: arm s (slang and pop culture), TTS voice check

## Rubric v3 addendum (written before arm s was judged)
A comparison or slang label ("aura farming", "like Mario Kart", "Fanum tax collected", "the lions logged off") is not a factual claim; judge only the observed part of the line against the strip. The comparison counts as a claim only when it asserts a pose, motion or event: "Leg day" on lions that are crouched is yes; "Mario Kart drift" on a lion that did not move is no (failure type pose). A comparison does not turn a wrong observation into a yes ("Fanum tax collected" on an envelope no lion is on is no).

## Arm s: slang and pop culture (`backend/scripts/eval_arms/s_slang.txt`, appended to hype with `--rules-file`)
Two runs on the 5-clip set (`eval/results/s39s1`, `s39s2`; 35B UD-Q3_K_XL, hype, glossary and examples on, everything else default). Blind pool `pool_s39`: all 119 arm s lines plus a 30-line drift sample (15 random lines from each s38 default run, `s38n1`, `s38n2`), 149 lines, one judge (this session), rubric v3 plus the addendum above, un-blinded afterwards.

| | lines | yes | no | unclear | grounded (Wilson 95%) | notable | s/window | prompt tokens mean (max) |
|---|---|---|---|---|---|---|---|---|
| arm s run 1 | 60 | 39 | 7 | 14 | 65% (52-76) | 35% | 41.3 | 6019 (6631) |
| arm s run 2 | 59 | 42 | 4 | 13 | 71% (59-81) | 22% | 40.2 | 6026 (6632) |
| arm s pooled | 119 | 81 | 11 | 27 | 68% (59-76) | 28% | 40.8 | 6023 |
| drift sample (s38 default) | 30 | 16 | 3 | 11 | 53% (36-70) | 30% | 40.3 (s38) | 5710 (6349) |

Failure types (lines that were not yes): arm s unread 22, pose 7, named 4, inv 3, who 1, face 1; drift sample unread 8, named 3, who 2, pose 1. The arm text adds about 310 prompt tokens; headroom at the largest call is 8192 - 6632 - 500 = 1060 tokens. Mistake-claim words: 0 in both. The drift sample is small (30 lines, interval 36-70), so the 15-point difference in favor of arm s is not a finding; the s38 default pooled over 118 lines was 57%.

Humor terms (`blind_review.py terms`, whole runs): lines with a slang term 45 of 60 and 51 of 59 (96 of 119, 81%; the s38 default runs: 0 and 1 of 118); lines with a reference from the prompt's list 0 and 1 (one "NBA dunk"); references repeated within a run 0. Other comparisons outside the list appeared (Yoshi jump, scoreboard, kickoff, starting pistol). The rule "never reuse a slang term that appears in the recent quips" did not hold: 108 repeated uses over the two runs ("main character" 28 times, "locked in" 23, "NPC" 13, "chat" 10, "aura" 18 with "aura farming").

What went wrong (examples, arm shown after un-blinding, all arm s): "Pink lion is farming aura by crouching low" (02_drummers t=24.2, pink is rearing, no/pose); "Blue lion enters center stage, setting the tempo!" and "Blue lion hits center stage" (01_opening, the blue lion enters at the right by the drummers, no/pose); "Blue lion drops the scroll. Fanum tax collected." (pink dropped its scroll, blue still held its own, no/who); "Yellow lion on the red envelope. Fanum tax collected." (04_action t=7.5, yellow stands with a red scroll, no/pose; the prompt's own example lines were copied almost word for word several times, e.g. this one twice and "Chat, are they buffering?" twice). Unread lines were mostly head shakes, wiggles and "stacking" that the strips cannot show.

## Humor sheet and decision rule (verdict pending the owner's ratings)
`logs/grounding/owner_humor/pool_s39/`: 40 lines, shuffled: 30 random arm s lines and 10 anchors from the rated `pool_s38` sheet (`blind_review.py humor --anchors <rated sheet>`); the origin and the old anchor rating are in `key.csv` only. All 10 anchors drawn were rated 2 by the owner (27 of the 30 lines in s38 were 2), so the anchor mean in this sheet is expected to be near 2.0 and condition (a) needs an arm s mean of about 2.7 or more.

Decision rule, fixed before the ratings: arm s becomes `hype.txt` only if (a) the owner mean on arm s lines is at least 0.7 above the mean on the 10 anchor lines in the same sheet, and (b) the pooled grounded rate is no more than 10 points below the drift sample's. Condition (b) holds (68% against 53%, above rather than below). Condition (a) is open. `hype.txt` was not changed. Two things for the owner to weigh when rating: slang appears in 81% of lines and repeats heavily, and almost no named reference appears, so the line is mostly "pose + slang label".

# Session 40: arm s2 (rotating terms per window)

## Arm s verdict: still open
`owner_humor/pool_s39/sheet.csv` had 0 of 40 ratings when checked (session 40 and again at the start of 40b), so it was not unblinded and `hype.txt` is unchanged. **pool_s39 is superseded by `pool_s40`** (one combined sheet, below); arm s is rated there.

## Arm s2 (`eval_arms/s2_slang.txt` + `--rotate-terms eval_arms/s_terms.txt`, eval only)
Problem from arm s: slang in 81% of lines, 108 repeats of slang terms, 1 reference line. s2 drops the two fixed lists from the rules file (`s2_slang.txt` = `s_slang.txt` minus them) and instead appends to each window's intro "Options for this window (use at most one slang term and one reference per line; use at least one reference this window if it fits what you see): slang: a, b, c. references: x, y." Per window the picker takes 3 slang terms and 2 references at random from `s_terms.txt` (31 slang, 24 references; arm s's lists plus new ones), skipping any term found in the quips actually produced in the run's last 3 windows (an alias match: "simba" counts as The Lion King; if too few unused terms remain it tops up from the used ones). Seeded: `--seed` (runs used 401 and 402), the seed and every window's picks are in `<csv>.stats.json` under `rotate_terms`. Two runs on the 5-clip set (`eval/results/s40s2a`, `s40s2b`; 35B UD-Q3_K_XL, hype, glossary and examples on, everything else default).

### Terms (`blind_review.py terms ... --terms-file s_terms.txt`; same merged lists for every run, so the arms are comparable)
| run | lines | lines with slang | lines with a reference | distinct slang | distinct refs | slang repeats | ref repeats |
|---|---|---|---|---|---|---|---|
| arm s run 1 | 60 | 45 | 0 | 13 | 0 | 42 | 0 |
| arm s run 2 | 59 | 51 | 1 | 13 | 1 | 55 | 0 |
| s2 run 1 | 60 | 29 | 24 | 20 | 21 | 10 | 5 |
| s2 run 2 | 60 | 26 | 21 | 20 | 16 | 9 | 6 |
Repeats = mentions minus distinct terms in the run. s2 uses a reference in 38% of lines (arm s 1%), slang in 46% (arm s 81%), and repeats slang about 5x less. Caveat: "a Twitch stream" and "a Twitch chat" both match "Twitch" and the slang "chat", so reference/slang counts have a few double hits.

### Grounding (blind pool `pool_s40`, 150 lines: 120 s2 lines + a 30-line drift sample, 15 random from each of `s38n1`, `s38n2`; one judge, rubric v3 + the session 39 addendum, un-blinded afterwards)
| | lines | yes | no | unclear | grounded (Wilson 95%) | notable | s/window | prompt tokens mean (max) |
|---|---|---|---|---|---|---|---|---|
| s2 run 1 | 60 | 33 | 7 | 20 | 55% (42-67) | 23% | 42.6 | 5982 (6602) |
| s2 run 2 | 60 | 33 | 7 | 20 | 55% (42-67) | 15% | 40.4 | 5980 (6609) |
| s2 pooled | 120 | 66 | 14 | 40 | 55% (46-64) | 19% | 41.5 | 5981 |
| drift sample | 30 | 15 | 3 | 12 | 50% (33-67) | 17% | | |
Failure types (lines not yes): s2 unread 31, pose 10, named 5, inv 4, who 3, face 1; drift unread 9, pose 4, face 1, who 1. Mistake-claim words 0 (drift 1). Headroom at the largest call: 8192 - 6609 - 500 = 1083 tokens. s2 is 55% against 50% for the drift sample (same ballpark as the s38 default's 57% over 118 lines); the 30-line drift interval is wide. Arm s was 68% (59-76) in session 39; the difference from s2 is inside the noise of one judge on 120 lines, but s2's "unread" count is higher: reference lines name poses stills cannot show ("Beyblade spin" 62, "head spin" 46, "Mulan pose"). Not-yes examples: "Pink lion just pulled a Beyblade spin" (02_drummers t=17.5, pink rears then drops, no spin, no/named), "Yellow lion's head spin is pure main character energy" (04_action t=27.5, no/named), "Blue lion just crash out and dropped the scroll" (04_action t=13.5, blue still holds the scroll, no/inv), "Purple lion just dropped the scroll like a hot potato" (04_action t=11.0, no/inv), "Blue lion just crashed out. The floor is lava" (05_ending t=10.8, blue rears, no/pose). The model copied "Chat, are they buffering?" from the prompt examples again ("Yellow lion head down. Chat, are they buffering?" while the head is up). Best s2 lines: "Stage is empty. No cap, it's over." (05_ending t=36.0), "Scrolls revealed! Purple says 'Go Bears'!" (04_action t=7.5), "Pink lion living rent free on the shoulders!" (02_drummers t=7.5, pink lifted high).

### Owner humor sheet (`logs/grounding/owner_humor/pool_s40/`, one sheet to rate once)
60 lines shuffled, origin only in `key.csv`: 25 random arm s lines (pool_s39: s39s1, s39s2), 25 random s2 lines, 10 rated pool_s38 anchors (8 rated 2, 2 rated 3). `index.html` has a 1-5 selector per line, keeps ratings in the browser, and "Save ratings" writes `sheet.csv` (Chrome/Edge: pick the file named on the page, overwrite; other browsers download `sheet.csv`, move it into the folder). The pool_s39 sheet is superseded.

### Decision rules (fixed now, applied next session from the pool_s40 ratings)
- Arm s: owner mean on arm s lines >= anchor mean + 0.7, and grounding rule (b) (holds: 68% vs 53%).
- Arm s2: owner mean on s2 lines >= anchor mean + 0.7, and >= arm s mean + 0.3, and pooled grounded no more than 10 points below the drift sample (holds: 55% vs 50%).
- The best passing arm goes into `hype.txt`. If s2 wins, its rotation later moves into `llm.py` for real jobs and the term file into `prompts/`.
