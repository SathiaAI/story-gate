import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / ".story-gate"
PY = sys.executable


def run(repo, *args, stdin=None, env=None):
    e = dict(os.environ, STORY_GATE_ROOT=str(repo), OPENROUTER_API_KEY="", HOME=str(repo / "_home"))
    e.pop("OPENROUTER_API_KEY")
    e.update(env or {})
    return subprocess.run([PY, str(repo / ".story-gate/gate.py"), *args], cwd=repo, input=stdin,
                          capture_output=True, text=True, env=e, timeout=120)


class Base(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        shutil.copytree(SRC, self.repo / ".story-gate", ignore=shutil.ignore_patterns("stories", "__pycache__", "*.jsonl", "judge-calibration.json"))
        g = lambda *a: subprocess.run(["git", *a], cwd=self.repo, capture_output=True, check=True)
        g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (self.repo / "app.py").write_text("x = 1\n")
        g("add", "."); g("commit", "-qm", "init")
        g("checkout", "-qb", "feature/SAT-1-thing")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def cfg(self, **kw):
        p = self.repo / ".story-gate/config.json"
        c = json.loads(p.read_text()); c.update(kw); p.write_text(json.dumps(c))

    def fill_ready(self, sid="SAT-1", self_score=0.9, drift="none"):
        sd = self.repo / ".story-gate/stories" / sid
        (sd / "story.md").write_text("---\nid: %s\ntitle: T\nsource: linear:%s\ndepends_on: []\n---\n" % (sid, sid) + "As a user I want X so that Y. " * 20)
        (sd / "context.md").write_text("## PRD\nPRD#2 says X\n## TRD\nTRD#3 rules\n## Upstream handoffs\nNone — no upstream dependencies\n## Prior learnings\nNone — searched: x y\n")
        (sd / "tests.json").write_text(json.dumps({"acceptance_criteria": [{"id": "AC-1", "text": "x", "positive": ["p"], "negative": ["n"], "edge": ["e"], "regression": [], "not_applicable": {"regression": "new module"}, "test_refs": ["test_ac1_x"]}]}))
        import importlib.util
        spec = importlib.util.spec_from_file_location("g", self.repo / ".story-gate/gate.py"); g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
        (sd / "ready.self.json").write_text(json.dumps({"scores": {k: self_score for k in g.READY_Q}, "drift": drift}))
        (sd / "done.self.json").write_text(json.dumps({"scores": {k: self_score for k in g.DONE_Q}, "drift": "none"}))


class TestVerdict(Base):
    def test_self_judge_never_passes(self):
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        r = run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["judge"], "self")
        self.assertEqual(v["overall"], "CONCERNS", r.stdout)

    def test_skeleton_fails(self):
        run(self.repo, "start", "SAT-1")
        run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["overall"], "FAIL")
        for k in ("story_present", "context_present", "test_matrix", "judge_available"):
            self.assertEqual(v["checks"][k]["status"], "FAIL", k)

    def test_missing_test_category_fails(self):
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        p = self.repo / ".story-gate/stories/SAT-1/tests.json"
        t = json.loads(p.read_text()); t["acceptance_criteria"][0]["not_applicable"] = {}; p.write_text(json.dumps(t))
        run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["checks"]["test_matrix"]["status"], "FAIL")

    def test_upstream_handoff_required(self):
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        p = self.repo / ".story-gate/stories/SAT-1/story.md"
        p.write_text(p.read_text().replace("depends_on: []", "depends_on: [SAT-0]"))
        run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["checks"]["upstream_handoffs"]["status"], "FAIL")
        (self.repo / ".story-gate/stories/SAT-0").mkdir(); (self.repo / ".story-gate/stories/SAT-0/handoff.md").write_text("x")
        run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["checks"]["upstream_handoffs"]["status"], "PASS")

    def test_drift_escalates_then_decision(self):
        run(self.repo, "start", "SAT-1"); self.fill_ready(drift="spec_should_change")
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["overall"], "ESCALATED")
        self.assertIn("drift_escalated", (self.repo / ".story-gate/outbox.jsonl").read_text())
        run(self.repo, "decide", "SAT-1", "--drift", "spec", "--by", "Paul", "--note", "PRD outdated")
        run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["overall"], "PASS")

    def test_waiver(self):
        run(self.repo, "start", "SAT-1"); self.fill_ready(self_score=0.5)
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "score", "SAT-1", "ready")
        self.assertEqual(json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())["overall"], "CONCERNS")
        import importlib.util
        spec = importlib.util.spec_from_file_location("g", self.repo / ".story-gate/gate.py"); g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
        for k in g.READY_Q:
            run(self.repo, "waive", "SAT-1", k, "--by", "Paul", "--reason", "pilot")
        run(self.repo, "score", "SAT-1", "ready")
        self.assertEqual(json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())["overall"], "WAIVED")


class TestDone(Base):
    def test_done_flow(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        self.assertEqual(run(self.repo, "score", "SAT-1", "ready").returncode, 0)
        (self.repo / "app.py").write_text("x = 2\n")
        r = run(self.repo, "score", "SAT-1", "done")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/done.json").read_text())
        self.assertEqual(v["overall"], "FAIL")
        for k in ("handoff_written", "learnings_recorded", "tests_ran_green"):
            self.assertEqual(v["checks"][k]["status"], "FAIL", k)
        self.assertEqual(v["checks"]["code_changed"]["status"], "PASS")
        self.assertEqual(run(self.repo, "record-tests", "SAT-1", "--", PY, "-c", "import sys; sys.exit(3)").returncode, 1)
        (self.repo / "test_app.py").write_text("def test_ac1_x():\n    assert True\n")
        run(self.repo, "record-tests", "SAT-1", "--", PY, "-c", "print('ok')")
        h = "# Handoff\n" + "".join("## %s\nreal content\n" % s for s in ("What changed", "Interfaces and contracts", "How to verify", "Known limits", "Downstream consumers", "Release and rollback", "Drift decisions"))
        (self.repo / ".story-gate/stories/SAT-1/handoff.md").write_text(h)
        self.assertNotEqual(run(self.repo, "learn", "SAT-1", "--type", "error", "--summary", "x").returncode, 0)  # errors need root cause + rule
        run(self.repo, "learn", "SAT-1", "--type", "error", "--summary", "pytest import path wrong", "--root-cause", "no conftest", "--rule", "add conftest.py at repo root", "--tags", "pytest")
        r = run(self.repo, "score", "SAT-1", "done")
        self.assertEqual(r.returncode, 0, r.stdout)
        (self.repo / "app.py").write_text("x = 5\n")  # code changed after green run -> stale
        r = run(self.repo, "score", "SAT-1", "done")
        self.assertIn("code changed after the last green test run", r.stdout)
        out = run(self.repo, "learnings", "pytest").stdout
        self.assertIn("conftest", out)


class TestHooks(Base):
    PAYLOADS = {
        "claude": {"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"file_path": "app.py"}, "cwd": "."},
        "codex": {"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Update File: app.py\n@@\n-x\n+y\n*** End Patch"}},
        "cursor": {"hook_event_name": "preToolUse", "tool_name": "Write", "tool_input": {"file_path": "app.py"}, "workspace_roots": ["."]},
        "gemini": {"hook_event_name": "BeforeTool", "tool_name": "write_file", "tool_input": {"file_path": "app.py"}},
        "windsurf": {"agent_action_name": "pre_write_code", "tool_info": {"file_path": "app.py"}},
        "grok": {"hookEventName": "PreToolUse", "toolName": "Edit", "toolInput": {"file_path": "app.py"}, "workspaceRoot": "."},
    }

    def test_warn_mode_never_blocks_and_is_visible(self):
        for cl, p in self.PAYLOADS.items():
            r = run(self.repo, "hook", "--client", cl, "--event", "pre", stdin=json.dumps(p))
            self.assertEqual(r.returncode, 0, cl)
            self.assertIn("SAT-1 READY gate is not run", r.stdout, cl)
            if cl != "windsurf":
                json.loads(r.stdout)  # valid JSON for JSON clients

    def test_enforce_blocks_with_exit_2(self):
        self.cfg(mode="enforce")
        for cl, p in self.PAYLOADS.items():
            r = run(self.repo, "hook", "--client", cl, "--event", "pre", stdin=json.dumps(p))
            self.assertEqual(r.returncode, 2, cl)
            self.assertIn("BLOCKED", r.stderr, cl)

    def test_enforce_point_only(self):
        self.cfg(enforce_points=["pre_edit"])
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(self.PAYLOADS["claude"]))
        self.assertEqual(r.returncode, 2)
        r = run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin="{}")
        self.assertEqual(r.returncode, 0)

    def test_exempt_paths_allowed(self):
        self.cfg(mode="enforce")
        p = {"tool_name": "Write", "tool_input": {"file_path": str(self.repo / ".story-gate/stories/SAT-1/story.md")}}
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(p)).returncode, 0)
        p = {"tool_name": "Write", "tool_input": {"file_path": ".github/workflows/x.yml"}}
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(p)).returncode, 2)

    def test_pass_allows_edit(self):
        self.cfg(mode="enforce", judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(self.PAYLOADS["claude"]))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_stop_blocks_until_done_and_has_loop_guard(self):
        self.cfg(mode="enforce", judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        (self.repo / "app.py").write_text("x = 3\n")
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin="{}").returncode, 2)
        # enforce mode keeps blocking even on the client's retry; warn mode lets the retry through
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin='{"stop_hook_active": true}').returncode, 2)
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin='{"stop_hook_active": true}').returncode, 2)
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin='{"stop_hook_active": true}').returncode, 0)  # loop cap
        self.cfg(mode="warn", enforce_points=[])
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin='{"stop_hook_active": true}').returncode, 0)
        self.assertEqual(run(self.repo, "hook", "--client", "cursor", "--event", "stop", stdin='{"loop_count": 1}').returncode, 0)

    def test_garbage_stdin_does_not_crash(self):
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin="not json")
        self.assertEqual(r.returncode, 0)


class TestInstallCI(Base):
    def test_install_idempotent_and_merges(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude/settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]}}))
        (self.repo / "AGENTS.md").write_text("# Mine\nkeep me\n")
        for _ in range(2):
            self.assertEqual(run(self.repo, "install").returncode, 0)
        s = json.loads((self.repo / ".claude/settings.json").read_text())
        self.assertEqual(s["permissions"]["allow"], ["Bash(ls)"])
        self.assertEqual(len(s["hooks"]["PreToolUse"]), 2)
        a = (self.repo / "AGENTS.md").read_text()
        self.assertIn("keep me", a); self.assertEqual(a.count("story-gate:start"), 1)
        for f in (".codex/hooks.json", ".cursor/hooks.json", ".gemini/settings.json", ".devin/hooks.json", ".grok/hooks/story-gate.json", "CLAUDE.md", "GEMINI.md", ".github/workflows/story-gate.yml"):
            self.assertTrue((self.repo / f).exists(), f)
        self.assertIn(".story-gate/.active", (self.repo / ".gitignore").read_text())

    def test_ci_warn_vs_enforce(self):
        (self.repo / "app.py").write_text("x = 9\n")
        r = run(self.repo, "ci")
        self.assertEqual(r.returncode, 0); self.assertIn("::warning", r.stdout)
        self.cfg(enforce_points=["ci"])
        r = run(self.repo, "ci")
        self.assertEqual(r.returncode, 1); self.assertIn("::error", r.stdout)

    def test_publish_command_sink(self):
        run(self.repo, "start", "SAT-1")
        out = self.repo / "sink.txt"
        self.cfg(sinks=[{"type": "repo"}, {"type": "command", "command": "%s -c \"import sys; open(r'%s','a').write(sys.stdin.read())\"" % (PY, out)}])
        self.assertEqual(run(self.repo, "publish").returncode, 0)
        self.assertIn("story_gate.started", out.read_text())
        self.assertIn("nothing new delivered", run(self.repo, "publish").stdout)
        self.assertEqual(out.read_text().count("story_gate.started"), 1)  # no duplicates


class TestHardening(Base):
    def test_traversal_and_gate_files_not_exempt(self):
        self.cfg(mode="enforce")
        for f in (".story-gate/../app.py", ".story-gate/stories/SAT-1/ready.json", ".story-gate/gate.py", ".story-gate/config.json", "../outside.py"):
            p = {"tool_name": "Write", "tool_input": {"file_path": f}}
            self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(p)).returncode, 2, f)
        p = {"tool_name": "apply_patch", "tool_input": {"command": "*** Update File: notes.md\n*** Move to: app.py\n"}}
        self.assertEqual(run(self.repo, "hook", "--client", "codex", "--event", "pre", stdin=json.dumps(p)).returncode, 2)

    def test_self_judge_cannot_unlock_edits(self):
        self.cfg(mode="enforce")
        run(self.repo, "start", "SAT-1"); self.fill_ready(self_score=1.0); run(self.repo, "score", "SAT-1", "ready")
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(TestHooks.PAYLOADS["claude"]))
        self.assertEqual(r.returncode, 2)

    def test_stale_ready_after_story_edit(self):
        self.cfg(mode="enforce", judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        p = self.repo / ".story-gate/stories/SAT-1/story.md"; p.write_text(p.read_text() + "\nnew scope")
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(TestHooks.PAYLOADS["claude"]))
        self.assertEqual(r.returncode, 2); self.assertIn("out of date", r.stderr)

    def test_structural_checks_cannot_be_waived(self):
        run(self.repo, "start", "SAT-1")
        r = run(self.repo, "waive", "SAT-1", "test_matrix", "--by", "agent", "--reason", "x")
        self.assertNotEqual(r.returncode, 0)

    def test_bad_payloads_and_files_never_crash_in_warn(self):
        for raw in ('"x"', "null", "[]", "{}"):
            self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=raw).returncode, 0, raw)
        run(self.repo, "start", "SAT-1")
        (self.repo / ".story-gate/stories/SAT-1/ready.json").write_text("{corrupt")
        (self.repo / ".story-gate/stories/SAT-1/decisions.jsonl").write_text("garbage\n")
        (self.repo / "app.py").write_text("x = 7\n")
        self.assertEqual(run(self.repo, "ci").returncode, 0)
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin="{}").returncode, 0)
        run(self.repo, "score", "SAT-1", "ready")

    def test_master_base_fallback_and_title_ids(self):
        subprocess.run(["git", "branch", "-m", "main", "master"], cwd=self.repo, capture_output=True)
        (self.repo / "app.py").write_text("x = 8\n")
        self.assertIn("1 code files", run(self.repo, "ci").stdout)
        subprocess.run(["git", "checkout", "-qb", "fix-encoding"], cwd=self.repo, capture_output=True)
        out = run(self.repo, "ci", env={"PR_TITLE": "Fix UTF-8 handling"}).stdout
        self.assertIn("no story id", out)


class TestRegressions(Base):
    def test_green_run_survives_git_add_and_commit(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        (self.repo / "new_mod.py").write_text("y = 1\n"); (self.repo / "test_new_mod.py").write_text("def test_ac1_x():\n    pass\n")
        run(self.repo, "record-tests", "SAT-1", "--", PY, "-c", "print(1)")
        h = "# H\n" + "".join("## %s\nreal\n" % x for x in ("What changed", "Interfaces and contracts", "How to verify", "Known limits", "Downstream consumers", "Release and rollback", "Drift decisions"))
        (self.repo / ".story-gate/stories/SAT-1/handoff.md").write_text(h)
        run(self.repo, "learn", "SAT-1", "--type", "none", "--summary", "no new learnings")
        self.assertEqual(run(self.repo, "score", "SAT-1", "done").returncode, 0)
        subprocess.run(["git", "add", "-A"], cwd=self.repo); subprocess.run(["git", "commit", "-qm", "work"], cwd=self.repo)
        r = run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin="{}")
        self.assertEqual(r.stdout.strip(), "{}", r.stdout)

    def test_gate_files_blocked_even_after_ready(self):
        self.cfg(mode="enforce", judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        for f in (".story-gate/stories/SAT-1/done.json", ".story-gate/config.json", ".story-gate/gate.py", str(self.repo / ".story-gate/stories/SAT-1/test_results.json")):
            p = {"tool_name": "Write", "tool_input": {"file_path": f}}
            r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(p))
            self.assertEqual(r.returncode, 2, f)
        p = {"tool_name": "Write", "tool_input": {"file_path": ".story-gate/stories/SAT-1/handoff.md"}}
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(p)).returncode, 0)

    def test_ci_survives_corrupt_config_in_warn(self):
        (self.repo / ".story-gate/config.json").write_text("")
        (self.repo / "app.py").write_text("x = 4\n")
        r = run(self.repo, "ci")
        self.assertEqual(r.returncode, 1); self.assertIn("unreadable", r.stdout)  # fail closed: mode unknown


class TestCheckpointAndTrace(Base):
    def test_traceability_required(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        (self.repo / "app.py").write_text("x = 2\n")  # code but no test named test_ac1_x
        run(self.repo, "score", "SAT-1", "done")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/done.json").read_text())
        self.assertEqual(v["checks"]["traceability"]["status"], "FAIL")
        self.assertIn("| AC-1 |", (self.repo / ".story-gate/stories/SAT-1/trace.md").read_text())

    def test_checkpoint_without_judge_is_unknown_and_post_hook_quiet(self):
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        r = run(self.repo, "checkpoint", "SAT-1")
        self.assertIn("UNKNOWN", r.stdout); self.assertEqual(r.returncode, 1)
        self.cfg(checkpoint={"every_edits": 2})
        for _ in range(3):
            r = run(self.repo, "hook", "--client", "claude", "--event", "post", stdin=json.dumps(TestHooks.PAYLOADS["claude"]))
            self.assertEqual(r.returncode, 0); json.loads(r.stdout)
        self.assertEqual(json.loads((self.repo / ".story-gate/.edits").read_text())["SAT-1"], 3)

    def test_off_course_contains_edits_when_enforced(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True}, enforce_points=["checkpoint"])
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        sd = self.repo / ".story-gate/stories/SAT-1"
        (sd / "checkpoints.jsonl").write_text(json.dumps({"at": "2026-10-02T10:00:00Z", "status": "OFF_COURSE", "why": ["drift: work_should_change"]}) + "\n")
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(TestHooks.PAYLOADS["claude"]))
        self.assertEqual(r.returncode, 2); self.assertIn("OFF COURSE", r.stderr)
        run(self.repo, "decide", "SAT-1", "--phase", "build", "--drift", "story", "--by", "Paul", "--note", "x")
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(TestHooks.PAYLOADS["claude"]))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_install_writes_skills_for_all_clients(self):
        run(self.repo, "install")
        for d in (".agents/skills/story-gate/SKILL.md", ".claude/skills/story-gate/SKILL.md"):
            self.assertIn("name: story-gate", (self.repo / d).read_text())
        self.assertIn("PostToolUse", (self.repo / ".codex/hooks.json").read_text())


class TestRound1Fixes(Base):
    def test_story_files_never_satisfy_traceability(self):
        """CodeRabbit 4166059695: committed tests.json must not make its own test_refs 'found'."""
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        (self.repo / "app.py").write_text("x = 2\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo); subprocess.run(["git", "commit", "-qm", "w"], cwd=self.repo)
        run(self.repo, "score", "SAT-1", "done")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/done.json").read_text())
        self.assertEqual(v["checks"]["traceability"]["status"], "FAIL")

    def test_learnings_tolerates_bad_lines(self):
        (self.repo / ".story-gate/learnings.jsonl").write_text("<<<<<<< HEAD\n{\"summary\": \"no id\"}\n")
        r = run(self.repo, "learnings", "x")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_red_run_invalidates_done(self):
        """Codex 4166101862: a later red run on unchanged code must invalidate a DONE PASS."""
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        (self.repo / "test_app.py").write_text("def test_ac1_x():\n    assert True\n")
        run(self.repo, "record-tests", "SAT-1", "--", PY, "-c", "print(1)")
        h = "# H\n" + "".join("## %s\nreal\n" % x for x in ("What changed", "Interfaces and contracts", "How to verify", "Known limits", "Downstream consumers", "Release and rollback", "Drift decisions"))
        (self.repo / ".story-gate/stories/SAT-1/handoff.md").write_text(h)
        run(self.repo, "learn", "SAT-1", "--type", "none", "--summary", "none")
        self.assertEqual(run(self.repo, "score", "SAT-1", "done").returncode, 0)
        run(self.repo, "record-tests", "SAT-1", "--", PY, "-c", "import sys; sys.exit(1)")
        self.assertIn("DONE gate", run(self.repo, "hook", "--client", "claude", "--event", "stop", stdin="{}").stdout)

    def test_handoff_requires_drift_section_and_version(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("g", self.repo / ".story-gate/gate.py"); g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
        self.assertIn("## Drift decisions", g.HANDOFF_SECTIONS)
        self.assertEqual(g.VERSION, "0.3.0")
        self.assertIn("head.sha", g.CI_YML); self.assertIn("persist-credentials: false", g.CI_YML)


class TestRound2Fixes(Base):
    def test_prior_learnings_none_needs_search_evidence(self):
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        p = self.repo / ".story-gate/stories/SAT-1/context.md"
        p.write_text(p.read_text().replace("None — searched: x y", "None"))
        run(self.repo, "score", "SAT-1", "ready")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())
        self.assertEqual(v["checks"]["context_present"]["status"], "FAIL")

    def test_exec_bit_change_invalidates_test_run(self):
        if os.name == "nt":
            self.skipTest("POSIX exec bit")
        run(self.repo, "start", "SAT-1")
        (self.repo / "tool.sh").write_text("echo hi\n"); os.chmod(self.repo / "tool.sh", 0o755)
        run(self.repo, "record-tests", "SAT-1", "--", PY, "-c", "print(1)")
        fp1 = json.loads((self.repo / ".story-gate/stories/SAT-1/test_results.json").read_text())["fingerprint"]
        os.chmod(self.repo / "tool.sh", 0o644)
        import importlib.util
        os.environ["STORY_GATE_ROOT"] = str(self.repo)
        try:
            spec = importlib.util.spec_from_file_location("g2", self.repo / ".story-gate/gate.py"); g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
            self.assertNotEqual(fp1, g.work_fingerprint())
        finally:
            os.environ.pop("STORY_GATE_ROOT")

    def test_duplicate_sinks_get_own_cursor(self):
        run(self.repo, "start", "SAT-1")
        o1, o2 = self.repo / "s1.txt", self.repo / "s2.txt"
        cmd = lambda o: "%s -c \"import sys; open(r'%s','a').write(sys.stdin.read())\"" % (PY, o)
        self.cfg(sinks=[{"type": "command", "command": cmd(o1)}, {"type": "command", "command": cmd(o2)}])
        run(self.repo, "publish")
        self.assertIn("story_gate.started", o1.read_text()); self.assertIn("story_gate.started", o2.read_text())

    def test_cursor_stop_warning_visible(self):
        run(self.repo, "start", "SAT-1")
        (self.repo / "app.py").write_text("x = 11\n")
        r = run(self.repo, "hook", "--client", "cursor", "--event", "stop", stdin='{"loop_count": 0, "workspace_roots": ["."]}')
        self.assertEqual(r.returncode, 0); self.assertIn("followup_message", r.stdout)


def load_gate(repo):
    import importlib.util
    os.environ["STORY_GATE_ROOT"] = str(repo)
    sys.path.insert(0, str(repo / ".story-gate"))
    for m in ("sg_judges", "sg_github"):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location("gate_mod_%d" % id(repo), repo / ".story-gate/gate.py")
    g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
    return g


class MockJudge:
    """Tiny local server that speaks the Jev decisions format and the OpenAI chat format."""
    def __init__(self, model="typesafe/jev-1.13", p=0.95, broken=False):
        import http.server, threading
        me = self
        self.model, self.p, self.broken, self.last = model, p, broken, None

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
                me.last = body
                if self.path.endswith("/chat/completions"):
                    props = body["response_format"]["json_schema"]["schema"]["properties"]
                    ans = {k: ({"noul": me.p} if "noul" in v["properties"] else {"choice": v["properties"]["choice"]["enum"][0], "confidence": 0.9}) for k, v in props.items()}
                    out = {"model": me.model, "choices": [{"message": {"content": json.dumps(ans)}}]}
                else:
                    ans = {}
                    for k, q in body["questions"].items():
                        ans[k] = {"noul": me.p} if q["type"] == "noul" else {"choice": "none", "confidence": 0.9}
                    if me.broken:
                        ans.pop(next(iter(ans)))
                    out = {"model": me.model, "answers": ans}
                raw = json.dumps(out).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


class TestJudges(Base):
    def score_with(self, judge_cfg, p=0.95, model="typesafe/jev-1.13", broken=False):
        m = MockJudge(model=model, p=p, broken=broken)
        try:
            self.cfg(judge=dict(judge_cfg, base_url=m.url + ("/decisions" if judge_cfg["provider"] != "openai-compatible" else "")))
            run(self.repo, "start", "SAT-1"); self.fill_ready()
            for f in ("ready.self.json", "done.self.json"):
                (self.repo / ".story-gate/stories/SAT-1" / f).unlink()
            r = run(self.repo, "score", "SAT-1", "ready", env={"JUDGE_API_KEY": "k"})
            return json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text()), r
        finally:
            m.close()

    def test_jev_via_proxy_can_pass(self):
        v, r = self.score_with({"provider": "decisions-proxy", "api_key_env": "JUDGE_API_KEY"})
        self.assertEqual((v["judge"], v["overall"]), ("jev", "PASS"), r.stdout)

    def test_non_jev_model_behind_proxy_is_emulated_and_capped(self):
        v, r = self.score_with({"provider": "decisions-proxy", "api_key_env": "JUDGE_API_KEY"}, model="some-other-model")
        self.assertEqual((v["judge"], v["overall"]), ("emulated", "CONCERNS"), r.stdout)

    def test_openai_compatible_is_capped(self):
        v, r = self.score_with({"provider": "openai-compatible", "api_key_env": "JUDGE_API_KEY", "model": "local-7b"})
        self.assertEqual((v["judge"], v["overall"]), ("emulated", "CONCERNS"), r.stdout)

    def test_incomplete_answers_fail_closed(self):
        v, r = self.score_with({"provider": "decisions-proxy", "api_key_env": "JUDGE_API_KEY"}, broken=True)
        self.assertNotEqual(v["judge"], "jev")
        self.assertIn("omitted", v.get("jev_error") or "")

    def test_validate_rejects_bad_probabilities(self):
        import importlib
        sys.path.insert(0, str(SRC)); J = importlib.import_module("sg_judges")
        qs = {"a": {"type": "noul", "instructions": "x"}}
        self.assertIsNone(J.validate({"a": {"noul": 1.7}}, qs)[0])
        self.assertIsNone(J.validate({"a": {"noul": float("nan")}}, qs)[0])
        self.assertIsNotNone(J.validate({"a": {"noul": 0.3}}, qs)[0])
        self.assertIsNone(J.validate({"a": {"noul": True}}, qs)[0])                      # bool is not a probability
        cq = {"c": {"type": "choice", "criteria": ["none", "minor"], "instructions": "x"}}
        self.assertIsNone(J.validate({"c": {"choice": ["none"], "confidence": 0.9}}, cq)[0])  # unhashable choice
        self.assertIsNotNone(J.validate({"c": {"choice": "none", "confidence": 0.9}}, cq)[0])

    def test_jev_model_id_is_matched_strictly(self):
        import importlib
        sys.path.insert(0, str(SRC)); J = importlib.import_module("sg_judges")
        for ok in ("typesafe/jev-1.13", "jev", "TypeSafe/Jev-1.13-20260901"):
            self.assertTrue(J.JEV_MODEL.match(ok), ok)
        for bad in ("jevil-7b", "my-jev", "typesafe/jev-1.13-evil", "", "gpt-jev"):
            self.assertFalse(J.JEV_MODEL.match(bad), bad)

    def test_missing_model_in_response_is_not_jev(self):
        v, r = self.score_with({"provider": "decisions-proxy", "api_key_env": "JUDGE_API_KEY"}, model="")
        self.assertEqual((v["judge"], v["overall"]), ("emulated", "CONCERNS"), r.stdout)

    def test_emulated_request_has_no_temperature_by_default(self):
        m = MockJudge(model="local-7b")
        try:
            self.cfg(judge={"provider": "openai-compatible", "api_key_env": "JUDGE_API_KEY", "model": "local-7b", "base_url": m.url})
            run(self.repo, "start", "SAT-1"); self.fill_ready()
            run(self.repo, "score", "SAT-1", "ready", env={"JUDGE_API_KEY": "k"})
            self.assertNotIn("temperature", m.last)
            self.assertNotIn("minimum", json.dumps(m.last["response_format"]))
        finally:
            m.close()


class TestV03Integrity(Base):
    def test_decision_bound_to_evidence(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(drift="spec_should_change")
        run(self.repo, "score", "SAT-1", "ready")
        run(self.repo, "decide", "SAT-1", "--drift", "spec", "--by", "Paul", "--note", "ok")
        run(self.repo, "score", "SAT-1", "ready")
        self.assertEqual(json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())["overall"], "PASS")
        p = self.repo / ".story-gate/stories/SAT-1/story.md"; p.write_text(p.read_text() + "\nchanged scope")
        run(self.repo, "score", "SAT-1", "ready")
        self.assertEqual(json.loads((self.repo / ".story-gate/stories/SAT-1/ready.json").read_text())["overall"], "ESCALATED")

    def test_escalation_event_even_when_failing(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(drift="architect_must_decide")
        (self.repo / ".story-gate/stories/SAT-1/tests.json").write_text("{}")  # also FAIL structurally
        run(self.repo, "score", "SAT-1", "ready")
        self.assertIn("drift_escalated", (self.repo / ".story-gate/outbox.jsonl").read_text())

    def test_concerns_do_not_pass_unless_accepted(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(self_score=0.5)
        self.assertEqual(run(self.repo, "score", "SAT-1", "ready").returncode, 1)
        self.cfg(accept_concerns=True)
        self.assertEqual(run(self.repo, "score", "SAT-1", "ready").returncode, 0)

    def test_spec_change_makes_ready_stale(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True}, mode="enforce", spec_files=["docs/PRD.md"])
        (self.repo / "docs").mkdir(); (self.repo / "docs/PRD.md").write_text("v1")
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        hook = lambda: run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(TestHooks.PAYLOADS["claude"]))
        self.assertEqual(hook().returncode, 0)
        (self.repo / "docs/PRD.md").write_text("v2 - different requirement")
        self.assertEqual(hook().returncode, 2)
        self.assertIn("OUT OF DATE", run(self.repo, "status").stdout)

    def test_unreadable_config_blocks_hooks(self):
        (self.repo / ".story-gate/config.json").write_text("{broken")
        r = run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(TestHooks.PAYLOADS["claude"]))
        self.assertEqual(r.returncode, 2); self.assertIn("unreadable", r.stderr)

    def test_shell_guard(self):
        self.cfg(mode="enforce")
        sh = lambda c: run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps({"tool_name": "Bash", "tool_input": {"command": c}})).returncode
        self.assertEqual(sh("echo '{}' > .story-gate/config.json"), 2)
        self.assertEqual(sh("python3 -c \"open('.story-gate/gate.py','w').write('')\""), 2)
        self.assertEqual(sh("sed -i 's/1/2/' app.py"), 2)          # code write before READY
        self.assertEqual(sh("pytest -q"), 0)                        # running tests is fine
        self.assertEqual(sh("python .story-gate/gate.py start SAT-1"), 0)
        self.assertEqual(sh("python .story-gate/gate.py start SAT-1; echo x > .story-gate/config.json"), 2)

    def test_protected_via_symlink_and_case(self):
        self.cfg(mode="enforce", judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        os.symlink(self.repo / ".story-gate", self.repo / "alias")
        p = {"tool_name": "Write", "tool_input": {"file_path": "alias/gate.py"}}
        self.assertEqual(run(self.repo, "hook", "--client", "claude", "--event", "pre", stdin=json.dumps(p)).returncode, 2)

    def test_active_story_scoped_to_branch(self):
        run(self.repo, "start", "SAT-1")
        subprocess.run(["git", "checkout", "-qb", "chore-cleanup"], cwd=self.repo, capture_output=True)
        self.assertIn("no active story", run(self.repo, "status").stdout)

    def test_source_rejects_shell_metacharacters(self):
        self.cfg(sources=[{"type": "command", "command": "echo {id}"}])
        r = run(self.repo, "source", "X; touch pwned #")
        self.assertNotEqual(r.returncode, 0); self.assertFalse((self.repo / "pwned").exists())

    def test_deleted_test_does_not_count(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        (self.repo / "test_app.py").write_text("def test_ac1_x():\n    assert True\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo); subprocess.run(["git", "commit", "-qm", "t"], cwd=self.repo)
        subprocess.run(["git", "checkout", "-qb", "feature/SAT-1-b"], cwd=self.repo)
        (self.repo / "test_app.py").unlink()
        (self.repo / "app.py").write_text("x = 3  # test_ac1_x mentioned in production code\n")
        run(self.repo, "start", "SAT-1"); self.fill_ready(); run(self.repo, "score", "SAT-1", "ready")
        run(self.repo, "score", "SAT-1", "done")
        v = json.loads((self.repo / ".story-gate/stories/SAT-1/done.json").read_text())
        self.assertEqual(v["checks"]["traceability"]["status"], "FAIL")

    def test_untracked_files_are_evidence(self):
        g = load_gate(self.repo)
        (self.repo / "brand_new.py").write_text("def feature():\n    return 42\n")
        _, files, diff = g.diff_against("main")
        self.assertIn("brand_new.py", files); self.assertIn("return 42", diff)
        os.environ.pop("STORY_GATE_ROOT")

    def test_ci_uses_ci_test_results(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        (self.repo / "app.py").write_text("x = 5\n")
        td = self.repo / "_ci"; td.mkdir()
        (td / "results.json").write_text(json.dumps({"exit_code": 1, "command": "pytest"}))
        r = run(self.repo, "ci", "--tests", str(td))
        self.assertIn("tests failed or did not run in CI", r.stdout)

    def test_ci_missing_test_results_never_uses_agent_results(self):
        self.cfg(judge={"jev": False, "allow_self_judge_pass": True})
        run(self.repo, "start", "SAT-1"); self.fill_ready()
        (self.repo / ".story-gate/stories/SAT-1/test_results.json").write_text(json.dumps({"exit_code": 0, "command": "pytest"}))
        (self.repo / "app.py").write_text("x = 5\n")
        td = self.repo / "_ci"; td.mkdir()
        r = run(self.repo, "ci", "--tests", str(td))
        self.assertIn("CI test results are missing", r.stdout)
        tr = json.loads((self.repo / ".story-gate/stories/SAT-1/test_results.json").read_text())
        self.assertIsNone(tr["exit_code"])

    def test_ci_workflow_never_copies_pr_code_into_the_judge_job(self):
        g = load_gate(self.repo); os.environ.pop("STORY_GATE_ROOT")
        self.assertNotIn('cp ".story-gate/', g.CI_YML)
        self.assertIn("exit 0", g.TRUSTED_COPY)
        self.assertIn("rm -f .story-gate/config.json .story-gate/judge-calibration.json", g.TRUSTED_COPY)
        self.assertIn("SG_BASE_REF: ${{ github.event.pull_request.base.ref }}", g.CI_YML)
        self.assertIn("BASE: ${{ github.event.pull_request.base.sha }}", g.AUDIT_YML)
        self.assertIn(".claude/settings.json", g.GATE_FILES)


class TestGitHubLogic(unittest.TestCase):
    def setUp(self):
        import importlib
        sys.path.insert(0, str(SRC)); self.G = importlib.import_module("sg_github")

    def test_codeowners(self):
        self.assertEqual(self.G.codeowners("# x\n*.js @web\n* @Paul @SathiaAI/core\n"), (["Paul"], ["SathiaAI/core"]))

    def test_junit_and_refs(self):
        d = Path(tempfile.mkdtemp())
        (d / "j.xml").write_text('<testsuite><testcase classname="tests.test_auth" name="test_ok"/><testcase classname="tests.test_auth" name="test_bad"><failure/></testcase><testcase name="test_skip"><skipped/></testcase></testsuite>')
        res = self.G.junit(d / "j.xml")
        self.assertEqual(self.G.ref_outcome("test_ok", res), "passed")
        self.assertEqual(self.G.ref_outcome("test_bad", res), "failed")
        self.assertEqual(self.G.ref_outcome("test_skip", res), "skipped")
        self.assertEqual(self.G.ref_outcome("test_nope", res), "missing")
        (d / "evil.xml").write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite/>')
        with self.assertRaises(ValueError):
            self.G.junit(d / "evil.xml")

    def acc(self, reviews, author="agent-bot[bot]", head_author="agent-bot[bot]", owners="* @Paul\n"):
        G = self.G
        ctx = {"repo": "o/r", "number": 1, "head_sha": "abc1234", "author": author}
        G.call = lambda m, p, t=None, b=None, accept=None: (200, {"author": {"login": head_author}, "committer": {"login": "web-flow"}}, {})
        G.paged = lambda p, t: reviews
        return G.acceptance(ctx, "t", owners)

    def rv(self, login, state="APPROVED", sha="abc1234", typ="User"):
        return {"user": {"login": login, "type": typ}, "state": state, "commit_id": sha}

    def test_acceptance_rules(self):
        self.assertTrue(self.acc([self.rv("Paul")])["accepted"])
        self.assertFalse(self.acc([self.rv("Paul", sha="old0000")])["accepted"])          # stale approval
        self.assertFalse(self.acc([self.rv("someone")])["accepted"])                      # not a code owner
        self.assertFalse(self.acc([self.rv("Paul", typ="Bot")])["accepted"])              # bots never accept
        self.assertFalse(self.acc([self.rv("Paul")], author="Paul")["accepted"])          # own PR
        self.assertFalse(self.acc([self.rv("Paul")], head_author="Paul")["accepted"])     # approver pushed the last commit
        self.assertFalse(self.acc([self.rv("Paul"), self.rv("coderabbitai[bot]", "CHANGES_REQUESTED", typ="Bot")])["accepted"])
        self.assertFalse(self.acc([self.rv("Paul")], owners="")["accepted"])               # no code owners at all
        G = self.G
        ctx = {"repo": "o/r", "number": 1, "head_sha": "abc1234", "author": "agent-bot[bot]"}
        G.paged = lambda p, t: [self.rv("Paul")]
        G.call = lambda m, p, t=None, b=None, accept=None: (200, {"author": {"login": "agent-bot[bot]"}, "committer": {"login": "Paul"}}, {})
        self.assertFalse(G.acceptance(ctx, "t", "* @Paul\n")["accepted"])                # approver committed the last commit
        G.call = lambda m, p, t=None, b=None, accept=None: (404, {"message": "Not Found"}, {})
        self.assertFalse(G.acceptance(ctx, "t", "* @Paul\n")["accepted"])                # can't verify the head commit
        G.call = lambda m, p, t=None, b=None, accept=None: (200, {"author": None, "committer": {"login": "agent-bot[bot]"}}, {})
        self.assertFalse(G.acceptance(ctx, "t", "* @Paul\n")["accepted"])                # unlinked author email

    def test_unresolved_threads_follow_pages(self):
        G = self.G
        pages = [{"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [{"isResolved": True, "comments": {"nodes": [{"author": {"login": "coderabbitai"}}]}}]},
                 {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"isResolved": False, "comments": {"nodes": [{"author": {"login": "coderabbitai"}}]}}]}]
        G.graphql = lambda q, v, t: {"repository": {"pullRequest": {"reviewThreads": pages[0 if v["a"] is None else 1]}}}
        self.assertEqual(G.unresolved_threads({"repo": "o/r", "number": 1}, "t", ["coderabbitai[bot]"]), 1)

    def test_private_key_file_mode(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions")
        p = Path(tempfile.mkdtemp()) / "k.pem"
        self.G.write_private(p, "secret")
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_ruleset_shape(self):
        r = self.G.ruleset_json()
        pr = [x for x in r["rules"] if x["type"] == "pull_request"][0]["parameters"]
        self.assertTrue(pr["require_code_owner_review"] and pr["dismiss_stale_reviews_on_push"] and pr["require_last_push_approval"])
        self.assertEqual(r["bypass_actors"], [])

    def test_manifest_has_no_dangerous_permissions(self):
        m = self.G.manifest("x", "http://127.0.0.1:1/callback")
        self.assertNotIn("workflows", m["default_permissions"]); self.assertNotIn("administration", m["default_permissions"])
        self.assertFalse(m["public"]); self.assertFalse(m["hook_attributes"]["active"])


class TestInstallV03(Base):
    def test_managed_workflows(self):
        run(self.repo, "install")
        for f in (".github/workflows/story-gate.yml", ".github/workflows/story-gate-audit.yml"):
            self.assertTrue((self.repo / f).read_text().startswith("# managed by story-gate"))
        own = self.repo / ".github/workflows/story-gate.yml"; own.write_text("name: mine\n")
        out = run(self.repo, "install").stdout
        self.assertIn("not managed", out); self.assertEqual(own.read_text(), "name: mine\n")


if __name__ == "__main__":
    unittest.main()
