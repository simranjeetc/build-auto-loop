# build-auto-loop

An outer loop for [BMAD](https://github.com/bmad-code-org/BMAD-METHOD)'s `bmad-build-auto` skill.
It drives a list of stories to done, one fresh agent process per phase, and checks each finished
story with a blind verifier that runs every check on the code before and after the change.

```
stories.yaml ─► pick first story not done
                 │
  none/draft ────┤ PLAN_CMD   "Halt after planning."      ─► ready-for-dev
  ready/in-* ────┤ IMPL_CMD   implement, review, commit   ─► done
  done ──────────┤ VERIFY_CMD blind, before + after trees ─► CHECK rows
                 │   PASS ─► next story
                 │   FAIL ─► IMPL_CMD again with the failing rows (up to MAX_FAILS)
                 │   WEAK ─► stop: the checks pass without the change, so prove nothing
```

The script holds no state of its own about progress. It reads `status:` and `baseline_revision:`
from the story specs `bmad-build-auto` writes, so a stopped run resumes where it left off.

## Requirements

- git and [uv](https://docs.astral.sh/uv/) (uv installs the one dependency, PyYAML)
- BMAD installed in the target repo with the `bmad-build-auto` skill (`npx bmad-method install`)
- at least one headless agent CLI: `claude`, `opencode`, `codex`, or anything that takes a prompt
  as its last argument

## Inputs

| Input | Written by | Used for |
|---|---|---|
| `<spec_folder>` argument | you | the folder that holds everything below |
| `<spec_folder>/stories.yaml` | you | story order, checkpoints, extra build text; the verifier sees `title` and `description` |
| `<spec_folder>/SPEC.md` (optional) | you | given to the verifier as the shared goal |
| `<spec_folder>/stories/<id>-*.md` | `bmad-build-auto` | the loop reads `status:` and `baseline_revision:`; the verifier never sees these files |
| env vars (`IMPL_CMD`, `PLAN_CMD`, `VERIFY_CMD`, `SKILL`, ...) | you | which agent and model runs each phase |
| flags (`--dry-run`, `--approve`, `--verify`, ...) | you | mode for this run |

## Run it

No install needed:

```sh
uv run https://raw.githubusercontent.com/simranjeetc/build-auto-loop/main/build-auto-loop.py specs/my-feature --dry-run
```

Or keep a copy on your PATH:

```sh
curl -fsSLo ~/.local/bin/build-auto-loop https://raw.githubusercontent.com/simranjeetc/build-auto-loop/main/build-auto-loop.py
chmod +x ~/.local/bin/build-auto-loop
```

Run from inside the target repo, on a clean working tree:

```sh
build-auto-loop specs/my-feature                     # run stories until done or a stop
build-auto-loop specs/my-feature --dry-run           # print the next command and prompt
build-auto-loop specs/my-feature --status            # story table, runs nothing
build-auto-loop specs/my-feature --approve s1        # clear s1's spec_checkpoint
build-auto-loop specs/my-feature --verify s1 [--fix] # re-verify a finished story
```

Options: `--max-rounds N` (default 20), `--budget USD` (per run, default `claude` command only).

## The story list

`<spec_folder>/stories.yaml`, a list or `{stories: [...]}`:

```yaml
- id: s1
  title: shout flag
  description: ./greet.sh --shout Ana prints HELLO, ANA! and test.sh covers it.
  spec_checkpoint: false   # true: stop after planning until --approve
  done_checkpoint: false   # true: stop after this story is done
  invoke_dev_with: ""      # extra text appended to the build prompt
```

The verifier sees only `title`, `description` and `<spec_folder>/SPEC.md` if present, never the
implementer's plan. Write the description as something checkable.

## Choosing models per phase

Each variable is a shell-quoted command; the prompt is appended as the last argument.

| Variable | Default | Example |
|---|---|---|
| `IMPL_CMD` | `AGENT_CMD`, else `claude -p` | `opencode run -m <provider/model> --auto --format json` |
| `PLAN_CMD` | `IMPL_CMD` | `claude --model opus --output-format stream-json --verbose -p` |
| `VERIFY_CMD` | none (no verification) | `codex exec -s read-only --ephemeral --skip-git-repo-check` |
| `MAX_FAILS` | `3` | |
| `SKILL` | `/bmad-build-auto` | `Use the bmad-build-auto skill.` for harnesses without slash commands |
| `PERMISSION_MODE`, `MODEL` | `auto`, unset | apply to the default `claude` command only |

A verifier from a different model family than the implementer catches more: the failure modes
are less correlated.

## The verdict

The verifier prints one row per check:

```
CHECK shout: before=FAIL after=PASS kind=new cmd=./greet.sh --shout Ana result=HELLO, ANA!
```

The script computes the verdict from the rows and does not take one from the verifier:

| Rows | Verdict | What happens |
|---|---|---|
| any `after=FAIL` | FAIL | failing rows go back to `IMPL_CMD` as review findings |
| no `kind=new` row, or a new row with `before=PASS` | WEAK | stop, exit 1 |
| otherwise | PASS | next story |

The before-tree is a detached `git worktree` at the story's `baseline_revision`, removed after
each run. The verifier is told not to write; the script stops if either tree changed.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | all stories done, round cap reached, or paused at a checkpoint |
| 1 | an agent or verifier did not run cleanly, dirty tree, or WEAK checks |
| 2 | story blocked or stalled, or `MAX_FAILS` reached |

## Where things are kept

Outside the repo, so the working tree stays clean:
`${XDG_STATE_HOME:-~/.local/state}/build-auto-loop/<repo>/<spec_folder>/`

- `logs/`: one log per agent run
- `approved/`: `--approve` markers
- `verdicts/<id>`: one JSON line per verification, with its rows

## Safety

The default `claude` command runs with `--permission-mode auto`, so agents edit files and run
commands unattended. Start with `--dry-run`, use a throwaway branch, and set `--budget`. The
script commits nothing itself and never pushes; `bmad-build-auto` commits each story locally.

## Future enhancements

- **Other spec formats.** Today the only input is BMAD's `stories.yaml`. Planned: read the task
  lists from [OpenSpec](https://github.com/Fission-AI/OpenSpec) changes and
  [Spec Kit](https://github.com/github/spec-kit) features as stories.
- **`REVIEW_CMD`.** Today one `IMPL_CMD` run implements, reviews and commits inside
  `bmad-build-auto`. Planned: move the review into its own phase, so a different model or harness
  reviews the diff, and send its findings back to `IMPL_CMD` the same way FAIL rows go back.
  Still open: whether the skill can stop before its own review, or the review runs after the
  commit.

## Tests

```sh
python3 -m unittest discover tests
```

The tests use fake agents and a fake verifier; no model is called.

## License

MIT. BMAD and `bmad-build-auto` are a separate project under their own license.
