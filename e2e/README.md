# seer end-to-end scenarios

Real seer runs against real models: one scenario per thing seer should do well
(plain questions, piped output, `seer help`, `seer do`, streaming, raw output,
watch mode, and brave mode in its various forms). Use it to:

- check that a change to a prompt, a default model or brave mode didn't make
  seer worse;
- compare models (local ones, API models, the subscription CLIs) on the same tasks;
- hand the results to an LLM for grading (see [GRADING.md](GRADING.md)).

It isn't part of `pytest` or CI: it's slow, needs a model, and some of what it
checks takes judgement. Run it on demand, before a release or after changing
how seer talks to models.

## Running

```bash
e2e/run.sh --list                          # scenarios and their features
e2e/run.sh                                 # all scenarios, provider from your config
e2e/run.sh -p ollama -m gemma4:26b --repeat 3
e2e/run.sh -p claude-cli --only brave      # one feature…
e2e/run.sh --only brave-big-log -v         # …or one scenario, events as they happen
e2e/run.sh --only brave-big-log -vv        # …or watch its terminal live
```

`e2e/run.sh` syncs the virtualenv (so `.venv/bin/seer`, the seer under test,
matches the code), then runs `e2e/run.py` with your arguments; `--help` lists
them. Without `-v` you get one line per run; `-v` adds each model call (time,
reasoning, reply), command, prompt and answer as they happen, and why a run
failed; `-vv` shows the scenario's terminal instead.

`-p`/`-m` are passed to seer as is, so any provider in your
`~/.config/seer/config.yaml` works. Models vary from run to run, so use
`--repeat 3` or more when comparing. A full run of all 35 scenarios makes about
60–80 model calls: free with a local model, a share of your plan's limits with
the subscription CLIs, and real money with API keys.

Each run gets:
- a fresh copy of the fixture workspace (`fixtures.py`: a small git repo,
  logs, data files, a broken `config.yaml`, a prompt-injection file);
- its own cache directory, so `seer help` sees the scenario's `context`, not
  your terminal;
- a pseudo-terminal, so `[Y/n/e]` prompts are answered from the scenario's
  `answers` (any prompt beyond them gets `n`).

Brave-mode writes land in the throwaway workspace. Nothing touches your files.

## Results

`e2e/results/<label>/<timestamp>/` (git-ignored):

- `summary.md`: pass rates of the mechanical checks, average calls, commands and time per scenario
- `scenarios.yaml`: a copy of the scenarios these results belong to
- `<scenario>/<n>/trace.jsonl`: everything seer did (set by `SEER_TRACE`, see `src/seer/trace.py`)
- `<scenario>/<n>/output.txt`: the terminal output, as the user saw it
- `<scenario>/<n>/result.json`: the checks and metrics

Mechanical checks only catch the obvious problems: wrong mode, a missing
answer, too many calls, an unexpected prompt, a deleted file. For the rest
(invented facts, poor commands, seer bugs versus model weaknesses), give an
LLM the results directory and [GRADING.md](GRADING.md):

> Grade the seer e2e results in e2e/results/ollama-gemma4_26b/20261009-101500
> following e2e/GRADING.md.

To compare models, give it several results directories.

## Adding a scenario

Add an entry to `scenarios.yaml`. The fields are documented at the top of the
file. Guidelines:

- Write `run` exactly as a user would type it, pipes included.
- Base expected answers on the fixture's facts (`fixtures.FACTS`), not on your
  machine, and add to the fixture if a scenario needs something new.
- Keep `checks` to what a script can judge without false failures. Put
  everything else in `expect`, written for a grader who only sees the trace.
- Run the new scenario a few times with a strong model before relying on it.
  If a strong model fails it, the scenario is probably what's wrong.
