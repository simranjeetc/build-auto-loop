#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""build-auto-loop: the outer loop bmad-build-auto expects, over stories.yaml.

Each phase of each story runs in a fresh headless agent process. All state lives
in files: the story spec's frontmatter (`status:`, `baseline_revision:`, written by
bmad-build-auto) and approval/verdict files outside the repo. This script decides
what runs next and when to stop; it never edits the repo itself.

Usage (from the target repo):
  build-auto-loop.py <spec_folder> [--max-rounds N] [--budget USD] [--dry-run]
  build-auto-loop.py <spec_folder> --approve <story_id>   # clear a spec_checkpoint
  build-auto-loop.py <spec_folder> --status               # table, no agent runs
  build-auto-loop.py <spec_folder> --verify <story_id> [--fix]

Phases per story, routed by its status (bmad-build-auto resumes from status):
  none|draft            -> PLAN_CMD  "... Halt after planning."  -> ready-for-dev
  ready-for-dev|in-*    -> IMPL_CMD  implement, review, commit   -> done
  done (this run)       -> VERIFY_CMD blind, before/after trees  -> PASS | FAIL | WEAK
  FAIL (< MAX_FAILS)    -> IMPL_CMD again with the failing rows (fresh step-04 pass)
Planning runs as its own process only when PLAN_CMD differs from IMPL_CMD or the
story has spec_checkpoint; otherwise one IMPL_CMD run does plan and build.

Env (commands are shell-quoted strings; the prompt is appended as the last arg):
  IMPL_CMD            default: AGENT_CMD, else `claude -p` with PERMISSION_MODE
                      (default auto), MODEL and --budget
  PLAN_CMD            default: IMPL_CMD
  VERIFY_CMD          optional; e.g. 'codex exec -s read-only --ephemeral --skip-git-repo-check'
  MAX_FAILS           verifier FAILs per story before handing back (default 3)
  SKILL               prompt prefix that makes the agent run the skill (default /bmad-build-auto;
                      use whatever your harness understands, e.g. 'Use the bmad-build-auto skill.')

Verdict, computed here from the verifier's CHECK rows, not taken from it:
  any after=FAIL -> FAIL;  no kind=new row with before=FAIL -> WEAK;  else PASS
  (a new row that already passes on the before-tree is noted, not fatal, when another new row
  goes FAIL -> PASS)

Exit codes: 0 finished or paused on a checkpoint; 1 error (agent or verifier did not
run cleanly, dirty tree, WEAK checks); 2 blocked, stalled, or MAX_FAILS reached.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

EXIT_OK, EXIT_ERR, EXIT_STOP = 0, 1, 2
ACTIVE = {"ready-for-dev", "in-progress", "in-review"}


class Stop(Exception):
    """End the run with an exit code and a notification."""

    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


# ---- output ----------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def notify(msg: str) -> None:
    log(msg)
    if shutil.which("osascript"):
        safe = msg.replace('"', "'")
        subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "build-auto-loop"'],
                       capture_output=True)


# ---- git -------------------------------------------------------------------
def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def dirty_paths(cwd: Path | None = None) -> set[str]:
    out = subprocess.run(["git", "status", "--porcelain", "-z", "-uall"], cwd=cwd, check=True,
                         capture_output=True, text=True).stdout
    return {e[3:] for e in out.split("\0") if len(e) > 3}


def is_clean(cwd: Path | None = None) -> bool:
    return not dirty_paths(cwd)


# ---- stories ---------------------------------------------------------------
@dataclass
class Story:
    id: str
    title: str
    description: str
    spec_checkpoint: bool
    done_checkpoint: bool
    invoke_dev_with: str


def load_stories(spec_folder: Path) -> list[Story]:
    try:
        data = yaml.safe_load((spec_folder / "stories.yaml").read_text()) or []
    except yaml.YAMLError as e:
        raise Stop(EXIT_ERR, f"{spec_folder}/stories.yaml is not valid YAML "
                             f"(quote values that contain ': '):\n{e}") from None
    if isinstance(data, dict):
        data = data.get("stories", [])
    return [Story(id=str(s["id"]), title=s.get("title") or "", description=s.get("description") or "",
                  spec_checkpoint=bool(s.get("spec_checkpoint", False)),
                  done_checkpoint=bool(s.get("done_checkpoint", False)),
                  invoke_dev_with=s.get("invoke_dev_with") or "") for s in data]


class SpecFiles:
    """Reads (never writes) the story specs bmad-build-auto keeps under stories/."""

    def __init__(self, spec_folder: Path):
        self.dir = spec_folder / "stories"

    def path(self, sid: str) -> Path | None:
        matches = sorted(self.dir.glob(f"{sid}-*.md"))
        if len(matches) > 1:
            raise Stop(EXIT_STOP, f"story {sid}: more than one stories/{sid}-*.md")
        return matches[0] if matches else None

    def frontmatter(self, sid: str) -> dict | None:
        p = self.path(sid)
        if p is None:
            return None
        m = re.match(r"---\n(.*?)\n---", p.read_text(), re.S)
        try:
            fm = yaml.safe_load(m.group(1)) if m else None
        except yaml.YAMLError:
            fm = None
        return fm if isinstance(fm, dict) else {}

    def status(self, sid: str) -> str:
        fm = self.frontmatter(sid)
        if fm is None:
            return "none"
        return str(fm.get("status") or "unknown")

    def baseline(self, sid: str) -> str | None:
        fm = self.frontmatter(sid) or {}
        b = fm.get("baseline_revision")
        return str(b) if b else None


# ---- agents ----------------------------------------------------------------
@dataclass
class Runner:
    """One phase's command. The prompt is appended as the last argument."""

    name: str
    argv: list[str]

    @classmethod
    def from_env(cls, name: str, vars: tuple[str, ...], budget: str | None) -> Runner:
        cmd = next((os.environ[v] for v in vars if os.environ.get(v)), None)
        if cmd:
            return cls(name, shlex.split(cmd))
        argv = ["claude", "--permission-mode", os.environ.get("PERMISSION_MODE", "auto"),
                "--output-format", "stream-json", "--verbose"]
        if os.environ.get("MODEL"):
            argv += ["--model", os.environ["MODEL"]]
        if budget:
            argv += ["--max-budget-usd", budget]
        return cls(name, argv + ["-p"])

    def run(self, prompt: str, logfile: Path) -> int:
        with logfile.open("w") as fh:
            return subprocess.run([*self.argv, prompt], stdout=fh, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL).returncode

    def __str__(self) -> str:
        return shlex.join(self.argv)


# ---- verifier --------------------------------------------------------------
ROW = re.compile(r"^CHECK (?P<id>[^:]+): before=(?P<before>PASS|FAIL) after=(?P<after>PASS|FAIL) "
                 r"kind=(?P<kind>new|kept) (?P<rest>.*)$")


@dataclass
class Check:
    id: str
    before: str
    after: str
    kind: str
    line: str

    @property
    def failing(self) -> bool:
        return self.after == "FAIL" or (self.kind == "new" and self.before == "PASS")


def parse_rows(text: str) -> list[Check]:
    """CHECK rows from verifier output; the last row per id wins (codex repeats its answer)."""
    rows: dict[str, Check] = {}
    for raw in text.splitlines():
        line = re.sub(r"^\s*(?:[-*]\s+)?`?", "", raw).rstrip("`").rstrip()
        if m := ROW.match(line):
            rows[m["id"]] = Check(m["id"], m["before"], m["after"], m["kind"], line)
    return list(rows.values())


def verdict_of(rows: list[Check]) -> str:
    if any(r.after == "FAIL" for r in rows):
        return "FAIL"
    if not any(r.kind == "new" and r.before == "FAIL" for r in rows):
        return "WEAK"
    return "PASS"


VERIFY_PROMPT = """You are a blind verifier for one story. Do not create, edit or delete any file anywhere.

Story {id}: {title}
Requirement: {description}
{spec_note}
After-tree (the change): the current directory, {root}, at {head}.
Before-tree (no change): {before_tree}, at {base}.
Do not open anything under {spec_folder}/stories/: that is the implementer's own plan.

1. From the requirement and project spec alone, list the checks that prove it.
   kind=new checks the new behaviour; kind=kept checks behaviour that must survive.
2. Run every check in the before-tree, then in the after-tree.
3. Read git diff {base}..{head} and add checks only for risks it shows.
4. Print one row per check, as a plain line with nothing before it, in this format:
   CHECK <short-id>: before=<PASS|FAIL> after=<PASS|FAIL> kind=<new|kept> cmd=<command> result=<what you saw>
   Report before-tree results honestly. At least one new check must fail on the before-tree;
   a check that passes on both trees is behaviour that survives, so mark it kind=kept."""

FIX_PROMPT = """{invoke}

A blind verifier from another model family failed this story (FAIL {n} of {cap}).
Triage each failing check below as a review finding in this pass. Fix the code, not the
check; if a check contradicts the story's intent, say so in the triage log.
{rows}"""


# ---- the loop --------------------------------------------------------------
class Loop:
    def __init__(self, args: argparse.Namespace):
        self.root = Path(git("rev-parse", "--show-toplevel"))
        os.chdir(self.root)
        self.folder = args.spec_folder.rstrip("/")
        self.spec_folder = Path(self.folder)
        if not (self.spec_folder / "stories.yaml").is_file():
            raise Stop(EXIT_ERR, f"no {self.folder}/stories.yaml")
        self.stories = load_stories(self.spec_folder)
        if not self.stories:
            raise Stop(EXIT_ERR, "stories.yaml has no stories")
        self.specs = SpecFiles(self.spec_folder)
        # State outside the repo: a file inside would dirty the tree and build-auto HALTs on that.
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        self.state = base / "build-auto-loop" / self.root.name / self.folder.replace("/", "_")
        for d in ("logs", "approved", "verdicts"):
            (self.state / d).mkdir(parents=True, exist_ok=True)
        self.before_tree = self.state / "before-tree"
        self.plan = Runner.from_env("plan", ("PLAN_CMD", "IMPL_CMD", "AGENT_CMD"), args.budget)
        self.impl = Runner.from_env("impl", ("IMPL_CMD", "AGENT_CMD"), args.budget)
        vcmd = os.environ.get("VERIFY_CMD")
        self.verify_cmd = Runner("verify", shlex.split(vcmd)) if vcmd else None
        self.max_fails = int(os.environ.get("MAX_FAILS", "3"))
        self.skill = os.environ.get("SKILL", "/bmad-build-auto")
        self.args = args

    def logfile(self, sid: str, tag: str) -> Path:
        stem = self.state / "logs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{sid}-{tag}"
        n, p = 1, stem.with_suffix(".log")
        while p.exists():  # two runs in one second must not share a log
            n += 1
            p = stem.parent / f"{stem.name}-{n}.log"
        return p

    def invoke(self, s: Story) -> str:
        return f"{self.skill} spec folder: {self.folder} story id: {s.id}"

    def require_clean(self, why: str, s: Story | None = None) -> None:
        # A plan-only run leaves the story's new spec uncommitted; step-04 commits it with the code.
        own = self.specs.path(s.id) if s else None
        dirty = dirty_paths() - ({own.as_posix()} if own else set())
        if dirty:
            raise Stop(EXIT_ERR, f"working tree dirty {why}: {' '.join(sorted(dirty))}")

    def run_phase(self, runner: Runner, s: Story, prompt: str, tag: str) -> str:
        """Run one agent phase; return the story's status afterwards."""
        self.require_clean("before " + tag, s)
        before = self.specs.status(s.id)
        lf = self.logfile(s.id, tag)
        log(f"story {s.id}: {tag} with {runner.name} ({runner.argv[0]}), status {before}")
        if self.args.dry_run:
            print(f"--- {runner} <prompt> ---\n{prompt}\n" + "-" * 20)
            raise Stop(EXIT_OK, "dry run")
        rc = runner.run(prompt, lf)
        after = self.specs.status(s.id)
        log(f"story {s.id}: {before} -> {after} (exit {rc}, log {lf})")
        if rc != 0:
            raise Stop(EXIT_ERR, f"{runner.name} exited {rc} on story {s.id}; see {lf}")
        return after

    # -- verifier
    def verify(self, s: Story, base: str) -> tuple[str, list[Check]]:
        assert self.verify_cmd
        head = git("rev-parse", "HEAD")
        self.drop_before_tree()
        git("worktree", "add", "--detach", str(self.before_tree), base)
        try:
            spec_md = self.spec_folder / "SPEC.md"
            prompt = VERIFY_PROMPT.format(
                id=s.id, title=s.title, description=s.description, root=self.root, head=head, base=base,
                before_tree=self.before_tree, spec_folder=self.folder,
                spec_note=f"Project spec: {spec_md}" if spec_md.is_file() else "")
            lf = self.logfile(s.id, "verify")
            log(f"story {s.id}: verify {base[:7]}..{head[:7]} with {self.verify_cmd.argv[0]}")
            rc = self.verify_cmd.run(prompt, lf)
            if not is_clean() or git("rev-parse", "HEAD") != head or not is_clean(self.before_tree):
                raise Stop(EXIT_ERR, f"verifier changed a tree on story {s.id}; inspect before continuing")
        finally:
            self.drop_before_tree()
        rows = parse_rows(lf.read_text(errors="replace"))
        if rc != 0 or not rows:
            raise Stop(EXIT_ERR, f"verifier did not finish on story {s.id} (exit {rc}, no CHECK rows); see {lf}")
        v = verdict_of(rows)
        with (self.state / "verdicts" / s.id).open("a") as fh:
            fh.write(json.dumps({"at": time.strftime("%F %T"), "verdict": v, "base": base, "head": head,
                                 "verifier": str(self.verify_cmd), "log": str(lf),
                                 "rows": [r.line for r in rows]}) + "\n")
        log(f"story {s.id}: verdict {v} ({len(rows)} checks, log {lf})")
        for r in rows:
            print(f"  {r.line}")
        idle = sum(r.kind == "new" and r.before == "PASS" for r in rows)
        if v == "PASS" and idle:
            log(f"story {s.id}: {idle} new check(s) already pass on the before-tree; counted as kept")
        if v == "WEAK":
            raise Stop(EXIT_ERR, f"story {s.id}: checks do not fail on the before-tree, so they prove nothing; see {lf}")
        return v, rows

    def drop_before_tree(self) -> None:
        if self.before_tree.exists():
            subprocess.run(["git", "worktree", "remove", "--force", str(self.before_tree)], capture_output=True)
        subprocess.run(["git", "worktree", "prune"], capture_output=True)

    def verify_and_fix(self, s: Story, fix: bool = True) -> None:
        base = self.specs.baseline(s.id)
        if not base:
            raise Stop(EXIT_ERR, f"story {s.id}: no baseline_revision in its spec")
        fails = 0
        while True:
            v, rows = self.verify(s, base)
            if v == "PASS":
                return
            fails += 1
            if not fix or fails >= self.max_fails:
                raise Stop(EXIT_STOP, f"story {s.id}: {fails} verifier FAIL(s), handing back; "
                                      f"history in {self.state / 'verdicts' / s.id}")
            prompt = FIX_PROMPT.format(invoke=self.invoke(s), n=fails, cap=self.max_fails,
                                       rows="\n".join(r.line for r in rows if r.failing))
            st = self.run_phase(self.impl, s, prompt, f"fix{fails}")
            if st != "done":
                raise Stop(EXIT_STOP, f"story {s.id}: fix pass ended at status '{st}'")
            self.require_clean(f"after fix pass on story {s.id}")

    # -- commands
    def status_table(self) -> None:
        print(f"{'ID':<12} {'STATUS':<14} CHECKPOINTS")
        for s in self.stories:
            cps = ",".join(n for n, on in (("spec", s.spec_checkpoint), ("done", s.done_checkpoint)) if on)
            print(f"{s.id:<12} {self.specs.status(s.id):<14} {cps}")

    def story(self, sid: str) -> Story:
        for s in self.stories:
            if s.id == sid:
                return s
        raise Stop(EXIT_ERR, f"no story {sid} in stories.yaml")

    def run(self) -> None:
        split_plan = self.plan.argv != self.impl.argv
        last_key = None
        for rnd in range(1, self.args.max_rounds + 1):
            s = next((s for s in self.stories if self.specs.status(s.id) != "done"), None)
            if s is None:
                raise Stop(EXIT_OK, "all stories done")
            st = self.specs.status(s.id)
            if st == "blocked":
                raise Stop(EXIT_STOP, f"story {s.id} is blocked: see {self.specs.path(s.id)}")
            if st not in {"none", "draft"} | ACTIVE:
                raise Stop(EXIT_STOP, f"story {s.id}: unrecognized status '{st}'")
            # The same story at the same status twice in a row is a stall, not progress.
            if (s.id, st) == last_key:
                raise Stop(EXIT_STOP, f"story {s.id} stalled at status '{st}'")
            last_key = (s.id, st)
            log(f"round {rnd}/{self.args.max_rounds}: story {s.id}")
            extra = f"\n\n{s.invoke_dev_with}" if s.invoke_dev_with else ""

            if st in {"none", "draft"} and (split_plan or s.spec_checkpoint):
                st = self.run_phase(self.plan, s, f"{self.invoke(s)}\nHalt after planning.{extra}", "plan")
                if st in {"none", "draft"}:
                    continue  # stall check catches a second no-progress round
            if s.spec_checkpoint and not (self.state / "approved" / s.id).exists():
                raise Stop(EXIT_OK, f"story {s.id}: review {self.specs.path(s.id)}, then run: "
                                    f"{sys.argv[0]} {self.folder} --approve {s.id}")
            if st == "blocked":
                continue
            st = self.run_phase(self.impl, s, f"{self.invoke(s)}{extra}", "impl")
            if st != "done":
                continue
            if self.verify_cmd:
                self.verify_and_fix(s)
            if s.done_checkpoint:
                raise Stop(EXIT_OK, f"story {s.id} done; done_checkpoint pause. Re-run to continue.")
        raise Stop(EXIT_OK, f"round cap {self.args.max_rounds} reached")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("spec_folder")
    ap.add_argument("--max-rounds", type=int, default=20)
    ap.add_argument("--budget", help="claude --max-budget-usd per agent run (default claude runner only)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--approve", metavar="STORY_ID")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--verify", metavar="STORY_ID")
    ap.add_argument("--fix", action="store_true", help="with --verify: run the fix loop on FAIL")
    args = ap.parse_args()
    try:
        loop = Loop(args)
        if args.approve:
            (loop.state / "approved" / args.approve).touch()
            log(f"approved spec for story {args.approve}")
        elif args.status:
            loop.status_table()
        elif args.verify:
            if not loop.verify_cmd:
                raise Stop(EXIT_ERR, "--verify needs VERIFY_CMD")
            loop.require_clean("before verify")
            loop.verify_and_fix(loop.story(args.verify), fix=args.fix)
        else:
            loop.run()
        return EXIT_OK
    except Stop as e:
        (log if e.code == EXIT_OK and str(e) == "dry run" else notify)(str(e))
        return e.code
    except subprocess.CalledProcessError as e:
        notify(f"git failed: {shlex.join(e.cmd)}: {e.stderr.strip()}")
        return EXIT_ERR


if __name__ == "__main__":
    sys.exit(main())
