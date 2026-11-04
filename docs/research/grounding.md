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
