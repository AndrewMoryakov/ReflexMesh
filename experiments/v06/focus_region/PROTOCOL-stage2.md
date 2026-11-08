# Stage 2 of the focus-region spec: mechanism A on the frozen stage 1 set

Fixed on 2026-11-08, before any model call on the set. Spec: `docs/specs/V0.6-focus-region.md`, sections 3 and 6.

## Question

Does adding a short description of each target's container to its option text make Jev act alone and rightly
more often, without passing more wrong actions, on the adapter's own steps?

## Data

The stage 1 set, SHA-256 `0b31dd655965b9f933b4c9dcc3870077e445ee59acf986089da014d7891f06fa` (138 steps,
`PROTOCOL-stage1.md`). Each step is replayed as recorded: the SystemOneHarness `state` and `questions`, sent to
`POST https://openrouter.ai/api/alpha/decisions` with model `~typesafe/jev-latest`, the provider ReflexMesh uses.
No browser and no site is touched.

## Passes, in this order

- `B1`: questions as recorded.
- `A1`: every option of a choice question that has a recorded container chain gets ` (in <label>)` appended,
  `<label>` from `container_label(chain)` in `stage2_replay.py`, the rule of the spec's section 3 as written.
- `A2`: the same with `container_label(chain, repeated=True)`: an ancestor among three or more same-tag siblings
  also counts as an item. Declared with A1, before any call, after a dry run of the labels showed that the spec's
  rule gives saucedemo's cards (plain `div`s) only "main". A2 is secondary.
- `B2`: B1 again, to see how much Jev's answers move between identical calls.

Only option texts change; state, question texts, keys and order stay as recorded.

## Scoring (`stage2_score.py`)

Per step, the right answer is the recorded gold: the action, and for its parameters the recorded keys (several
when the script marked them equivalent). A step is right when Jev's `next_action` is the gold action and each
gold parameter's choice is among the gold keys. The gate reads as SystemOneHarness does: the weakest of the
action's `confidence` and the probabilities of the chosen action's parameters, threshold 0.7 for click, type,
select and go back, 0.5 for scroll and wait; `finish` passes when `goal_reached` is at least 0.5; `escalate`
counts as held. Outcomes: passed and right ("acted alone rightly"), passed and wrong, held, error.

Subsets: all 138 steps; the 102 with a target choice (primary); the 20 where the right option's description is
shared by another option. Differences against B1 are paired over the same steps, with a 95% bootstrap interval.

Primary reading: A1 against B1 on the targeted steps, "acted alone rightly" up and "passed wrong" not up. B2
against B1 is the noise floor; a difference smaller than it is not read as an effect.

## Budget

About 0.35 M input tokens per pass at $0.042 per million; the replay stops after $0.08 in all.

## Not established by this stage

Effects on whole tasks (A changes no trajectory here; every step is the recorded one), on other models, and on
mechanism B.
