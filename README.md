<p align="center">
  <img src="docs/assets/ReflexMesh-hero.png" alt="ReflexMesh: a task from an agent goes to a router, which picks one route (API/MCP tool, CLI/script, browser/desktop, model/agent, macro/workflow) through a gate of permissions and limits; a verifier checks the goal and either confirms the result or stops and hands the task back" width="100%">
</p>

<h1 align="center">ReflexMesh</h1>

<p align="center"><b>Decide how an AI agent's task should be done — API, CLI, model or browser — keep it inside permissions and limits, and count it as done only when a verifier confirms. Early research code for people building agents.</b></p>

<p align="center">
  <a href="https://www.python.org/"><img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB.svg?logo=python&logoColor=white"></a>
  <a href="pyproject.toml"><img alt="Version 0.2.0" src="https://img.shields.io/badge/version-0.2.0-blue.svg"></a>
  <a href="#status"><img alt="Status: early research" src="https://img.shields.io/badge/status-early%20research-orange.svg"></a>
  <a href="pyproject.toml"><img alt="Runtime dependencies: none" src="https://img.shields.io/badge/runtime%20deps-none-2E7D57.svg"></a>
</p>

<p align="center"><b>English</b> | <a href="README.ru.md">Русский</a></p>

```sh
python -m venv .venv && .venv/bin/python -m pip install -e .
.venv/bin/reflexmesh route --input examples/route-task.json
```

ReflexMesh is a universal decision-making and execution-coordination layer for AI agents: selecting tools and executors, enforcing constraints, and producing a verifiable result. Target paths include API/MCP tools, CLI/scripts, models and agents, computer use, macros and workflows. Jev is meant for bounded choice, the LLM for planning and generating content; code controls permissions and execution, and a verifier checks the result. The browser is the first applied scenario; desktop is added after V1.0. Interactive interfaces do not define the project's boundaries. This is the target concept; its currently implemented scope is described below.

**What exists today:** task routing (a fixed-order stub by default, or the real JevRouter over HTTP), a measured routing study, and a controlled-browser runtime that is still being built. API/MCP, CLI, desktop, macro and workflow executors are target directions, not integrations — see [Status](#status).

## Why ReflexMesh?

- **If** your agent can do a job several ways — a direct API or MCP tool, a script, a model, a browser — **then** ReflexMesh is meant to choose among the ways you allow and let code, not the model, own permissions and execution.
- **If** "the agent says it is done" is not good enough, **then** completion here is assigned by the runtime and accepted only when a verifier confirms the goal. The V0.4 spike showed why: the harness's own `completed` status was wrong in both directions, and a verifier over server-side state was right in both observed cases.
- **If** you want an honest "none of these routes fits", **then** the adapter offers Jev a `NONE` candidate; in the V0.3b study this raised Jev's correct-refusal rate from 22% (previous adapter) to 96% over the same refusal cases, with no loss on ordinary tasks (the 25% in the V0.3 report is that study's separate 8-case subset).
- **If** routing has to be cheap and fast, **then** the V0.3 study reports Jev at accuracy 1.00 (the same as gpt-5.5) against 0.65 for hand-written rules, while, compared with the gpt-5.5 reference, Jev's p50 latency is 0.6 s against 6.4 s and its cost roughly $0.00002 against $0.002 per decision (the rules are faster and free but far weaker on paraphrased tasks) — figures as recorded in [`docs/STATUS.md`](docs/STATUS.md) and the [V0.3 report](docs/research/V0.3-routing.md).

It is **not** a finished agent framework, your own LLM, or a new RPA platform; it is a thin integration runtime and decision layer around existing components. No general task execution ships today: a selected route is a recommendation, never a completed job, and the browser runtime is still a controlled spike on a fixture site. Fully offline work and support for every OS are not promised — the Jev path assumes a cloud provider. An unsupported task gets an explicit refusal or a hand-back.

## Features

- **Explicit routing contract** — a Task goes in; a decision comes out as JSON with a clear status (selected, refusal, confirmation needed, adapter error) and distinct exit codes `0` / `3` / `4` / `5` / `2`.
- **Honest stub by default** — the default provider picks from the intersection of `capabilities` and `allowed_routes` in a fixed order and marks itself `is_stub=true`, `execution_performed=false`; it never analyses the goal.
- **Real routing through JevRouter** — an opt-in HTTP adapter to a local JevRouter; on any error the model is **not** silently replaced by the stub. The V0.2 live gate is closed for OpenRouter; direct typesafe is not verified.
- **Measured, not assumed** — routing was evaluated against a fixed protocol (V0.3, verdict "useful") and refusal behaviour separately (V0.3b); the results and their limits are published under [`docs/research/`](docs/research/).
- **Code-owned control** — selecting a route never authorizes an action; form submission, publishing and other external effects are not permitted by the choice itself.
- **Controlled browser runtime (in progress)** — a V0.4 spike of SystemOneHarness + Browser Use under runtime-owned veto, cancellation and deadline passed its checks; the V0.5 execution code (supervisor, TextSlots, verifier predicates, isolated test site) exists locally and its acceptance gates are still open.
- **No runtime dependencies** — the package has none beyond Python 3.11+.

## How it works

<p align="center">
  <img src="docs/assets/ReflexMesh-how-it-works.png" alt="ReflexMesh: a user hands a new task to a router robot, which picks one tool from API, browser, desktop and CLI for the job; a verifier examines the result and stops the line unless there is proof" width="100%">
</p>

The picture shows the target design. Of the tools drawn, only a controlled browser path is being built; the others are directions, not implemented integrations.

1. **A task arrives** from an agent. Pi is the first intended client, but the core stays independent of it, so other agents and programs can send the same tasks.
2. **The router picks a route** from those the task declares allowed. Jev makes this bounded choice; the current CUA / LLM / PERCEPTION catalogue is an experimental set for V0, not the final classification.
3. **A gate owned by code** enforces permissions and limits before anything runs. Choosing a route authorizes nothing by itself.
4. **An executor acts** — for now, only a controlled browser executor is in scope.
5. **A verifier checks the goal.** The run counts as done only if the verifier confirms it; otherwise it stops with a reason, or hands the task back to the calling agent.

## Quick start

Python 3.11+. From a clone (Bash; PowerShell equivalents are in [Running](#running)):

```sh
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/reflexmesh route --input examples/route-task.json
```

The example input declares `CUA` and `LLM` as both capable and allowed. The output from the default stub looks like this:

```json
{"schema_version": "0.1", "task_id": "browser-demo", "status": "selected", "route": "CUA", "eligible_routes": ["CUA", "LLM"], "reason_code": "stub_fixed_order", "provider": "stub", "is_stub": true, "execution_performed": false, "confidence": null}
```

This is the stub's fixed order, not an analysis of the goal, and nothing was executed. To route through the real JevRouter, see [JevRouter (V0.2)](#jevrouter-v02). To run the tests: `python -m unittest discover -s tests -v`.

## Status

**Current verified milestone: V0.4.** The [V0.4 report](docs/research/V0.4-spike.md)
documents the controlled SystemOneHarness + Browser Use spike. The V0.5 execution
code is in progress; its deterministic runtime and fixture checks do not yet close
the real-browser or Jev acceptance gates. See the [V0.5 specification](docs/specs/V0.5.md)
and [local run guide](docs/guides/V0.5-local-run.md). Earlier V0.2 live routing
evidence remains in the [live report](docs/research/V0.2-live-openrouter.md).

## Direction and documents

- [North Star](docs/NORTH_STAR.md) — the product goal and signs of usefulness.
- [Target architecture](docs/architecture/TARGET_ARCHITECTURE.md) — macro/meso/micro, the runtime and executors.
- [Invariants](docs/architecture/INVARIANTS.md) — mandatory constraints.
- [V0–V2 plan](docs/ROADMAP.md) — stages and exit criteria.
- [Current status](docs/STATUS.md) — implementation, evidence and open gates.
- [Documentation map](docs/README.md) — specifications, reports and update rules.

V1.0 is the first finished browser MVP from Pi/MCP; its scope is not widened.
V2.0 is the target of the first coherent system with several classes of executors,
selected tasks without a GUI, browser/desktop and recovery. The concrete set of
adapters and scenarios is determined after V1.0, with no promise to connect them all.
Pi remains a separate client. The choice of ClawBridge, Skyvern and Ui.Vision will be
refined after checking need and compatibility; empty directories do not mean support.

## Running

Python 3.11+. Install into a virtual environment (Bash):

```sh
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/reflexmesh route --input examples/route-task.json
```

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\reflexmesh.exe route --input examples/route-task.json
```


Without installing the package (Bash):

```sh
PYTHONPATH=src python -m reflexmesh route --input examples/route-task.json
```

PowerShell without installing:

```powershell
$env:PYTHONPATH = "src"
python -m reflexmesh route --input examples/route-task.json
```

`--input -` reads JSON from stdin. By default a stub is used:
the route is selected from the intersection of
`capabilities` and `allowed_routes` in the order CUA, LLM, PERCEPTION.
The stub does not analyse the goal; the `provider` field equals `stub`, `is_stub=true`,
`execution_performed=false`, `confidence=null`. Declaring capabilities
does not mean executors are connected. The result is not task execution.

Exit codes: `0` — route selected, `3` — refusal, `4` — confirmation required,
`5` — adapter/provider error, `2` — input error.
The decision (including `failed` from the adapter, exit 5) is JSON on stdout.
An argument/read/validation error (exit 2) is JSON on stderr, and stdout is empty.
`--help` and `--version` print plain text.

## Checks

```sh
python -m unittest discover -s tests -v
```

Specifications: [V0.1](docs/specs/V0.1.md), [V0.2](docs/specs/V0.2.md).
Evidence: [V0.1 report](docs/research/V0.1-validation.md),
[V0.2 report](docs/research/V0.2-validation.md). They refer to the versions and
environments named in the reports, not to an arbitrary state of main.

## JevRouter (V0.2)

The default stub is kept; the real integration is switched on explicitly.
Install the upstream separately (Node.js 20+, Git):

```sh
git clone https://github.com/BillionsBobby/JevRouter.git
cd JevRouter
git checkout f944acb6530621bced023352e2358a63218bf4d9
npm ci --ignore-scripts
npm run build
node dist/cli.js serve --provider openrouter --port 8787
```

Before starting the server, set `OPENROUTER_API_KEY` in its environment.
You can create one on the [OpenRouter keys page](https://openrouter.ai/settings/keys):
there is no separate JevRouter key. If you already have a direct TypeSafe key,
set `TYPESAFE_API_KEY` or `JEV_API_KEY` and use `--provider typesafe`.
Keys are not passed in ReflexMesh's Task or CLI.
The server uses its own policy and saves receipts in `.jevrouter/`
in its working directory; take that into account when choosing the directory and the content of tasks.
With a real provider the goal is sent to a cloud model.

In a second terminal from the ReflexMesh directory, after installing into `.venv` (Bash):

```sh
.venv/bin/python -m reflexmesh route --provider jevrouter --input examples/route-task.json
```

In PowerShell use `.\.venv\Scripts\python.exe -m reflexmesh` with the same arguments.

`--jev-url http://127.0.0.1:8787` and `--timeout 30` are available (socket I/O,
not an overall deadline). On error the model is not replaced by the stub. On a
`needs_confirmation` reply no further actions are performed. The adapter offers Jev
the `NONE` candidate: if no allowed route fits, the answer is
`abstained` with `upstream_no_fitting_route` ([ADR-0003](docs/architecture/ADR-0003-none-candidate-refusal.md)).
`--no-none-candidate` restores the previous behaviour.

Without a key you can run the upstream with `--provider demo` and the client with
`--allow-demo`. The reply will be marked `is_stub=true`. Without this flag demo is rejected.
An example with two routes may produce an honest refusal because of a low score.

Checking the integration without keys after building the upstream:

```sh
python scripts/check_jevrouter_http.py --upstream-dir /path/to/JevRouter
```

The script starts and stops a local demo server itself in a temporary directory.

- [V0.2 specification](docs/specs/V0.2.md)
- [Step-by-step V0.2 live run on Windows](docs/guides/V0.2-live-run-windows.md)
- [V0.2 validation report](docs/research/V0.2-validation.md)
- [V0.2 live run via OpenRouter](docs/research/V0.2-live-openrouter.md) and [extended scenarios](docs/research/V0.2-live-extended-openrouter.md)
