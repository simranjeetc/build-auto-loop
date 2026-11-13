"""End-to-end tests with fake agents: no model is called.

Run: python3 -m unittest discover tests   (needs git and uv on PATH)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "build-auto-loop.py"

STORIES = """\
- id: s1
  title: shout flag
  description: greet.sh --shout Ana prints HELLO, ANA!
"""

# Fake bmad-build-auto: planning writes the spec uncommitted (as the real skill does);
# a build run sets status done and commits code and spec together.
FAKE_AGENT = """\
import subprocess, sys
from pathlib import Path
prompt = sys.argv[-1]
Path(sys.argv[1]).write_text(prompt)
spec = Path("specs/f/stories/s1-shout.md")
base = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
if "Halt after planning" in prompt:
    spec.write_text(f"---\\nstatus: ready-for-dev\\nbaseline_revision: {base}\\n---\\n")
    sys.exit(0)
if not spec.exists():
    spec.write_text(f"---\\nstatus: ready-for-dev\\nbaseline_revision: {base}\\n---\\n")
spec.write_text(spec.read_text().replace("ready-for-dev", "done"))
with open("greet.sh", "a") as fh:
    fh.write("# change\\n")
subprocess.run(["git", "add", "-A"], check=True)
subprocess.run(["git", "commit", "-qm", "s1"], check=True)
"""

# Fake verifier: FAKE_MODE=weak, else FAIL until call number FAKE_PASS_AT.
FAKE_VERIFY = """\
import os, sys
from pathlib import Path
c = Path(sys.argv[1]); n = int(c.read_text() or 0) + 1 if c.exists() else 1; c.write_text(str(n))
if os.environ.get("FAKE_MODE") == "weak":
    print("CHECK s1: before=PASS after=PASS kind=new cmd=x result=y")
elif n < int(os.environ.get("FAKE_PASS_AT", "1")):
    print("- `CHECK s1: before=FAIL after=FAIL kind=new cmd=./greet.sh --shout Ana result=hello`")
else:
    print("CHECK s1: before=FAIL after=PASS kind=new cmd=./greet.sh --shout Ana result=HELLO, ANA!")
print("CHECK k1: before=PASS after=PASS kind=kept cmd=./greet.sh result=ok")
"""


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / "specs/f/stories").mkdir(parents=True)
        (self.repo / "specs/f/stories.yaml").write_text(STORIES)
        (self.repo / "greet.sh").write_text("echo hi\n")
        (self.tmp / "agent.py").write_text(FAKE_AGENT)
        (self.tmp / "verify.py").write_text(FAKE_VERIFY)
        self.prompt = self.tmp / "prompt"
        self.count = self.tmp / "count"
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}
        self.env = {**os.environ, **env, "XDG_STATE_HOME": str(self.tmp / "state")}
        for k in ("PLAN_CMD", "IMPL_CMD", "AGENT_CMD", "VERIFY_CMD", "FAKE_MODE", "FAKE_PASS_AT"):
            self.env.pop(k, None)
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.repo, env=getattr(self, "env", None),
                              check=True, capture_output=True, text=True).stdout.strip()

    def run_loop(self, *args, **env):
        agent = f"{sys.executable} {self.tmp / 'agent.py'} {self.prompt}"
        e = {**self.env, "IMPL_CMD": agent,
             "VERIFY_CMD": f"{sys.executable} {self.tmp / 'verify.py'} {self.count}", **env}
        r = subprocess.run([str(SCRIPT), "specs/f", *args], cwd=self.repo, env=e,
                           capture_output=True, text=True)
        self.out = r.stdout + r.stderr
        return r.returncode

    def status(self):
        return (self.repo / "specs/f/stories/s1-shout.md").read_text()

    def assert_no_worktree(self):
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1, self.out)

    def test_status_and_dry_run(self):
        self.assertEqual(self.run_loop("--status"), 0, self.out)
        self.assertIn("s1", self.out)
        self.assertEqual(self.run_loop("--dry-run"), 0, self.out)
        self.assertIn("/bmad-build-auto spec folder: specs/f story id: s1", self.out)

    def test_split_plan_build_verify_pass(self):
        plan = f"{sys.executable} {self.tmp / 'agent.py'} {self.tmp / 'plan-prompt'}"
        self.assertEqual(self.run_loop(PLAN_CMD=plan), 0, self.out)
        self.assertIn("Halt after planning", (self.tmp / "plan-prompt").read_text())
        self.assertIn("status: done", self.status())
        self.assertIn("verdict PASS", self.out)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assert_no_worktree()

    def test_fail_then_fix_then_pass(self):
        self.assertEqual(self.run_loop(FAKE_PASS_AT="2"), 0, self.out)
        self.assertIn("fix1", self.out)
        fix = self.prompt.read_text()
        self.assertIn("FAIL 1 of 3", fix)
        self.assertIn("CHECK s1: before=FAIL after=FAIL", fix)
        self.assertNotIn("CHECK k1", fix)
        self.assert_no_worktree()

    def test_max_fails_hands_back(self):
        self.assertEqual(self.run_loop(FAKE_PASS_AT="99"), 2, self.out)
        self.assertEqual(self.count.read_text(), "3")
        self.assert_no_worktree()

    def test_weak_checks(self):
        self.assertEqual(self.run_loop(FAKE_MODE="weak"), 1, self.out)
        self.assertIn("prove nothing", self.out)

    def test_verifier_without_rows(self):
        self.assertEqual(self.run_loop(VERIFY_CMD="true"), 1, self.out)
        self.assertIn("no CHECK rows", self.out)
        self.assert_no_worktree()

    def test_dirty_tree_refused(self):
        (self.repo / "stray.txt").write_text("x")
        self.assertEqual(self.run_loop(), 1, self.out)
        self.assertIn("stray.txt", self.out)

    def test_invalid_yaml_reported_cleanly(self):
        (self.repo / "specs/f/stories.yaml").write_text(STORIES + "- id: s2\n  description: prints usage: x\n")
        self.git("commit", "-qam", "bad yaml")
        self.assertEqual(self.run_loop("--status"), 1, self.out)
        self.assertIn("not valid YAML", self.out)
        self.assertNotIn("Traceback", self.out)

    def test_custom_skill_prefix(self):
        self.assertEqual(self.run_loop("--dry-run", SKILL="Use the bmad-build-auto skill."), 0, self.out)
        self.assertIn("Use the bmad-build-auto skill. spec folder: specs/f", self.out)

    def test_spec_checkpoint_pauses_until_approved(self):
        (self.repo / "specs/f/stories.yaml").write_text(STORIES + "  spec_checkpoint: true\n")
        self.git("commit", "-qam", "checkpoint")
        self.assertEqual(self.run_loop(), 0, self.out)
        self.assertIn("--approve s1", self.out)
        self.assertIn("ready-for-dev", self.status())
        self.assertEqual(self.run_loop("--approve", "s1"), 0, self.out)
        self.assertEqual(self.run_loop(), 0, self.out)
        self.assertIn("status: done", self.status())


if __name__ == "__main__":
    unittest.main()
