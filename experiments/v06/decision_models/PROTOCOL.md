# Decision models on browser steps, through OpenRouter: protocol

Fixed on 2026-11-08, before any model was called. The question: does a decision model on OpenRouter choose
browser actions better than Jev, judged the way the ReflexMesh gate would use its answers?

This is an offline step evaluation on a public dataset. It runs no browser and no ReflexMesh adapter, and it
uses none of the M5 sites or traces: section 17 forbids tuning on the frozen set, and choosing a model on it
would be tuning.

## Data

`Mlchemist/myjev-browser-decisions`, revision `8c0c5d532458f7ba25c9684b267694848e815d5a`, split `benchmark`
(`data/benchmark.jsonl`, published SHA-256 `f95b894c…36bd37`), restricted to the 3,239 ids in
`data/benchmark_jev_answered_ids.txt`. Each record is one browser step in the request format
browser-use/jev-ultrafast sends to Jev, with human gold labels; websites unseen by the dataset's own model.
Licence CC BY-NC-SA 4.0: downloaded at run time on the run machine, never committed here. Results committed
here carry ids, model answers and aggregates only.

What it does not test: DONE, BLOCKED and WAIT (none in the benchmark gold), `go_back` and tab switching
(not in this format), slot choice for typing (the format leaves the text to another model), and the
ReflexMesh request format itself (SystemOneHarness questions); a model that wins here still has to be tried
in the adapter.

## Samples

- Pilot: 30 ids, `random.Random(1).sample(sorted(ids), 30)`. Used only to check that a model answers this
  format at all (rate of valid answers), and its tokens, cost and latency. Not used to compare accuracy.
- Main: 300 ids, `random.Random(2).sample(sorted(ids - pilot), 300)`.

## Models

Pilot: every decision model on OpenRouter's catalogue (`/api/v1/models?output_modalities=decisions`,
2026-11-08) that makes general typed decisions: Jev 1.13 and 1.14, mammoth-1, elephant-1, joey-1, Clef,
Clef-flash, d1.1, d1-ultra, Microsoft Decision-1, Reflex (Morph), Drex v1.5, v2.0 and max, gpt-6-luna-decisions,
pplx-decider v1.2 and flash v1.2, Kev-4B, Tev1-4B, Solar Decide and its flash. Left out by purpose: Clairic
(extraction), Clef-omni (audio and video), Span (behaviour monitoring), classify-1 (classifier), Parallel
decisions (web grounding), jev-router (routing).

Main: Jev 1.14 (the provider ReflexMesh uses, `~typesafe/jev-latest`) and Jev 1.13 (to check the scorer
against the dataset's published Jev 1.13 figure), plus every pilot model with at least 90% valid answers,
as many as the key's budget allows. If the budget forces a cut, models are kept in order of pilot cost per
step, cheapest first; pilot accuracy does not decide.

## Request and scoring

The record's `request` body is sent unchanged except `model`, to `POST https://openrouter.ai/api/alpha/decisions`,
one request per step, at most 3 attempts on 429, 5xx or a network error. Any other answer is recorded as an error.

Per step, with `op` the model's `operation` choice and `t` its answer to `<op>_target` when `op` takes a target:

- operation correct: `op == gold.operation`;
- element correct: the answer to the gold operation's target question is one of the gold keys;
- step correct: operation correct and, if the gold operation takes a target, element correct;
- confidence as the ReflexMesh gate reads it: `weakest = min(c_op, p_t)`, where `c_op` is the operation
  answer's `confidence` (else the probability of its choice) and `p_t` the probability of the chosen target;
- the gate: threshold 0.7 for CLICK, TYPE_TEXT and SELECT (`write` in the adapter), 0.5 for SCROLL and WAIT
  (`read`). A DONE or BLOCKED answer is not gated (finish and escalate in SystemOneHarness).

Reported per model, over all steps of the sample (an error counts as a wrong step):

- step success rate, operation accuracy, element accuracy;
- gate outcome: passed and correct (the model would act alone and rightly), passed and wrong (a wrong action
  dispatched), held (a handoff);
- coverage at 95% precision over `weakest`, and ECE (10 bins) of `c_op` against operation correctness;
- paired difference in step success against Jev 1.14 on the same steps, with a 95% bootstrap interval;
- valid-answer rate, input tokens, cost, latency p50 and p95, by source split as well.

Primary measure: steps passed and correct, and passed and wrong, at the ReflexMesh thresholds.

## Not established by this protocol

Whether a better step score here carries over to whole tasks in ReflexMesh: that needs the adapter, the F
checks and a new comparison with its own frozen manifest, not a repeat of M5.

## Stage 0 of the focus-region spec (added 2026-11-08, before its first call)

The causal test of `docs/specs/V0.6-focus-region.md`, section 6. Same 300 main-sample steps, Jev 1.14
(`~typesafe/jev-latest`) only, every target question cut to K options with `stage0_prune.py` (the gold
operation's question keeps all gold keys; others random, seeded by K, step id and question; `state.elements`
cut to the kept elements; order and numbering kept), K = 15 and K = 40. Compared with the Jev 1.14 full-list run
of the main sample, paired over the same steps (`stage0_compare.py`): step success and the gate outcomes, with
a 95% bootstrap interval. Gold-informed on purpose; random neighbours are easier than similar ones, so the
result is an upper bound of what shortening can give. Read: does a shorter list raise "acted alone and rightly"
without raising "wrong action passed"?
