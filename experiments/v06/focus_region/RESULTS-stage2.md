# Stage 2 result: mechanism A on the frozen stage 1 set (2026-11-08)

Protocol: `PROTOCOL-stage2.md`. Jev (`~typesafe/jev-latest`, served as 1.14) through OpenRouter, 138 frozen steps,
passes B1, A1, A2 in that order; $0.081 in all.

## Result

Targeted steps (102, a choice of a target), in percent of steps, paired against B1, 95% bootstrap interval:

```
pass                               right   acted alone right     wrong passed   held
B1  as recorded                    93.1    75.5                  3.9            20.6
A1  container label, spec rule     93.1    73.5  -2.0 [-5.9, +2.0]  3.9          22.5
A2  label, repeated siblings too   93.1    76.5  +1.0 [-2.9, +5.9]  3.9          19.6
```

On the 20 steps whose right option shares its description with another option, all three passes gave the same
outcome (right 95%, acted alone right 70%). Over all 138 steps the picture is the same (A1 -2.2, A2 +0.7 points of
acting alone rightly, both intervals across zero). Full table: `results/stage2-score.txt`.

**Mechanism A shows no effect on this set.** Jev is already right on 93% of targeted steps and on 19 of the 20
repeated-description steps without the label. A likely reason, not tested: in ReflexMesh's request the page
summary lists every control by its number in page order among the surrounding text, so the container is
already there; the public benchmark's format, where the earlier association was seen, offers a flat list.

## Deviations from the protocol

- B2 (the repeat of B1 for the noise floor) was not run: each pass cost about $0.027 rather than the estimated
  $0.015, and the replay stopped at the $0.08 cap after A2. The differences are read against their intervals only.
- The pre-registered scorer keyed steps by (task, step number); auto steps keep the script's step number, so rows
  collided (114 "targeted" instead of 102) and replies overwrote each other. Found on the first scoring, before any
  reading of the effect; fixed by numbering rows in file order (`seq`). The replies had been written one per row in
  row order, and the scorer checks that task and step line up for every row before pairing them. The first,
  wrong, output showed A1 at -5.3 points; it is superseded by the table above.

## Exploration outside the protocol

Of the 21 held targeted steps in B1, 18 were right: 3 with their weakest judgment between 0.3 and 0.5, 15 between
0.5 and 0.7. Lowering the write threshold, fitted on the same steps:

```
write threshold   acted alone right   wrong passed
0.7 (now)         75.5                3.9
0.6               80.4                5.9
0.5               90.2                6.9
```

On this set the lever for acting alone is the threshold, not the description: a choice of risk for the owner,
to be checked on other steps before any change.

## Not established

- Run-to-run noise of Jev on identical requests (B2 not run).
- Whether the set is representative: practice sites, a scripted path, 138 steps; Jev scores 93% here against 51%
  on the public benchmark's steps.
- Effects on whole tasks, other models, and mechanism B.

## After the review of #20

Codex found two faults in the tools that produced these numbers. Both are fixed in the tools; the recorded numbers
above do not change.

- The replay checked its budget after each whole pass, so a pass that crossed the cap was still sent in full. That is
  what happened here: B1 and A1 cost $0.023 and $0.029, A2 started at $0.052 and finished at $0.081, $0.0014 (1.8%)
  over the $0.08 cap. The replay now checks before each request, counts what earlier invocations recorded, and with
  several workers overshoots by at most the requests already in flight. Under the fixed replay A2 would have stopped
  at about step 131 of 138 (an estimate from the costs above, not a run), so A2 would have scored its last steps as
  not sent. The replies kept here were all paid for and are scored as recorded.
- The scorer counted an answer that named an action but left out its required target or value as passing the gate
  (`passed_wrong` when the action was right), where the live path refuses it and nothing is done. It now scores such
  an answer, and one naming an action or target that was not offered, as an error. Rescoring the recorded replies
  with the fixed scorer gives a summary file identical to the earlier one (same SHA-256), so none of the 414 replies
  was incomplete and no figure above moves.
