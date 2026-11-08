# Stage 1 of the focus-region spec: our own labelled browser steps

Fixed on 2026-11-08, before the first recording on any site below. Spec: `docs/specs/V0.6-focus-region.md`,
section 6. Purpose: a set of browser steps in the adapter's own format (SystemOneHarness `state` and
`questions`), each with the right action, recorded once and frozen, so that mechanism A (container context)
and later B can be measured by replaying the same steps to Jev with only the descriptions changed.

## Sites

None of them is in the M5 frozen set (en.wikipedia.org, arxiv.org, pypi.org, w3schools.com,
the-internet.herokuapp.com, scrapethissite.com, quotes.toscrape.com, selenium.dev, testpages.eviltester.com,
gutenberg.org), and no M5 trace is used. All answered 200 from the recording machine on 2026-11-08.

```
site                          kind                           what it adds
books.toscrape.com            practice catalogue             20 identical "Add to basket" per page, category menu, paging
practice.expandtesting.com    practice pages                 a table with Edit and Delete in every row, cards with identical "Try it out", login form
demoqa.com                    practice forms                 date widget, autocomplete, tables; ads present
demoblaze.com                 practice shop                  product cards, categories, paging, a cart
uitestingplayground.com       practice traps                 dynamic ids, overlapped and delayed elements
saucedemo.com                 practice shop, public login    identical "Add to cart" per card; public demo credentials as normal slots
news.ycombinator.com          real, read only                dense list, identical "hide" and "comments" per row;
                                                             the list of a past day (front?day=), not the live front page
docs.python.org               real, read only                deep navigation, search, long pages
```

Three tasks per site, 24 in all, aimed at about 150-200 steps. On the two real sites only reading: search,
open, page through; no account, no form that writes. On practice sites only their demo data.

Amended on 2026-11-08 after the exploration runs, before any task recording: todomvc.com was replaced by
demoblaze.com, because a todo is added only with Enter and `press_key` is not offered, so nothing but links could
be done there; Hacker News is recorded on a past day's list, because the live front page changes between runs.
A second amendment after the draft runs, still before recording: practicesoftwaretesting.com was replaced by
practice.expandtesting.com, because the adapter was offered no control on it at all, even after waiting (only
scroll, wait, finish and escalate). Pages beyond the adapter's 200-option limit, and controls the adapter does
not offer (demoqa's row icons, a password field without a label), are avoided by starting tasks on the page
that holds the target.

## How steps are recorded

`record_steps.py` runs `BrowserRun` with a scripted provider, on the recording machine (Windows, Chrome; never
the machine where each Chrome start counts as a failed logon). The script is the label: each step names its
action by the option's visible description, and where descriptions repeat, by the description `within` a
container (for example "Add to basket" within "A Light in the Attic"). Where several options lead to the same
place (a book's cover link and title link), the step says `any`, and every match is recorded as a right answer.
The provider records, per step:

- the request as SystemOneHarness sent it (`state`, `questions`), unchanged;
- the right answer: the option key per question and the script step that chose it (`action`, description,
  `within`), since keys do not survive a page change and the description does;
- for every offered option, the raw container chain of its node: ancestors up to the document or shadow root,
  each with tag, role, accessible name, id, aria-label, short text, first heading inside it, and position among
  same-tag siblings. Not a formatted description: the format of mechanism A is chosen later, offline, from
  these fields, without recording again;
- the observation id, URL and page signature.

Each site first gets an exploration run that only dumps what the adapter offers, step by step; the task
scripts are written from those dumps. Exploration dumps are not part of the set.

When all 24 tasks are recorded, the step file is frozen by its SHA-256 in this file. A later recording on the
live sites is a new set, not a repair of this one.

## What will be measured on it (stage 2, its own pre-registration)

Jev 1.14 on the frozen steps, the descriptions as recorded against the same descriptions with container
context, paired over the same steps, read by the gate as in `experiments/v06/decision_models/PROTOCOL.md`.
About 200 steps times two variants; the OpenRouter key must be topped up before stage 2 starts.

## Not established by this set

- One labeller and no audit, unlike the public benchmark. One key is right per question unless the script
  names several equivalent ones; another link to the same card counts as wrong.
- Steps follow the script's path, not Jev's: like the public benchmark, a step evaluation that carries over to
  whole tasks no better than it does.
- The sites change; the set is what was recorded on its date.
