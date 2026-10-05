import os, re, shutil, subprocess, tempfile, unittest, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / ".story-gate"
UV = shutil.which("uv")
NET_HINTS = ("dns error", "failed to fetch", "connection", "network", "tls", "timed out", "error sending request", "proxy")


def _ver(text, pat):
    m = re.search(pat, text, re.M)
    assert m, "version not found"
    return m.group(1)


def _skip_if_network(tc, proc):
    out = (proc.stdout + proc.stderr).lower()
    if proc.returncode != 0 and any(h in out for h in NET_HINTS):
        tc.skipTest("uv could not reach the network for the build backend:\n" + out[-400:])


class Version(unittest.TestCase):
    def test_pyproject_matches_gate(self):
        py = _ver((ROOT / "pyproject.toml").read_text(), r'^version\s*=\s*"([^"]+)"')
        gate = _ver((SRC / "gate.py").read_text(), r'^VERSION\s*=\s*"([^"]+)"')
        self.assertEqual(py, gate)

    def test_every_pinned_release_matches_gate(self):
        """The installers, docs, skills and plugin manifests all point to this release, so a release can't ship half-bumped."""
        import json
        gate = _ver((SRC / "gate.py").read_text(), r'^VERSION\s*=\s*"([^"]+)"')
        tag = "v" + gate
        for f in ("install.sh", "install.ps1"):
            text = (ROOT / f).read_text(encoding="utf-8")
            self.assertRegex(text, r"STORY_GATE_REF.*%s" % re.escape(tag), f)  # the default release
            self.assertIn("story-gate/%s/%s" % (tag, f), text, f)               # the one-liner in its header
        for f in ("README.md", "docs/guide.md", "skills/story-gate/SKILL.md", ".story-gate/SKILL.md", "plugin/skills/story-gate/SKILL.md"):
            pins = set(re.findall(r"story-gate(?:@|/)(v\d+\.\d+\.\d+)", (ROOT / f).read_text(encoding="utf-8")))
            self.assertTrue(pins, f)
            self.assertEqual(pins, {tag}, f)
        for f in ("plugin.json", ".claude-plugin/plugin.json", "plugin/.claude-plugin/plugin.json", "gemini-extension.json"):
            self.assertEqual(json.loads((ROOT / f).read_text(encoding="utf-8"))["version"], gate, f)

    def test_installers_are_safe_by_construction(self):
        """Pinned source, fail on errors, official uv installer only, and no sudo or admin rights."""
        sh, ps = (ROOT / "install.sh").read_text(encoding="utf-8"), (ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("set -eu", sh)
        self.assertIn('$ErrorActionPreference = "Stop"', ps)
        for text in (sh, ps):
            self.assertIn("git+https://github.com/SathiaAI/story-gate@", text)
            self.assertNotIn("sudo", text); self.assertNotIn("RunAs", text)
            for url in re.findall(r"https://[^\s\"')|]+", text):
                self.assertTrue(url == "https://astral.sh/uv" or url.startswith(("https://astral.sh/uv/", "https://github.com/SathiaAI/story-gate",
                                                "https://raw.githubusercontent.com/SathiaAI/story-gate/", "https://git-scm.com/")), url)


@unittest.skipUnless(UV, "uv not on PATH")
class Build(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def env(self):
        e = dict(os.environ, UV_CACHE_DIR=str(self.tmp / "cache"), UV_TOOL_DIR=str(self.tmp / "tools"),
                 UV_TOOL_BIN_DIR=str(self.tmp / "bin"))
        return e

    def run_uv(self, *args):
        p = subprocess.run(["uv", *args], cwd=ROOT, capture_output=True, text=True, env=self.env(), timeout=600)
        _skip_if_network(self, p)
        return p

    def test_wheel_contents(self):
        out = self.tmp / "dist"
        p = self.run_uv("build", "--wheel", "-o", str(out))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        wheels = list(out.glob("*.whl"))
        self.assertEqual(len(wheels), 1)
        names = set(zipfile.ZipFile(wheels[0]).namelist())
        expect = {"story_gate/cli.py", "story_gate/__init__.py", "story_gate/_payload/PROTOCOL.md", "story_gate/_payload/SKILL.md"}
        expect |= {"story_gate/_payload/" + f.name for f in SRC.glob("*.py")}
        expect |= {"story_gate/_payload/vendor/" + f.name for f in (SRC / "vendor").iterdir() if f.is_file()}
        self.assertIn("story_gate/_payload/gate.py", expect)
        self.assertEqual(sorted(expect - names), [])
        for n in names:
            self.assertFalse(n.endswith("config.json"), n)
            self.assertNotIn("stories/", n)
            self.assertNotIn("__pycache__", n)
            self.assertFalse(n.endswith(".jsonl"), n)

    def test_installed_command_runs(self):
        p = self.run_uv("tool", "install", "--force", str(ROOT))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        exe = next((self.tmp / "bin").glob("story-gate*"))
        r = subprocess.run([str(exe)], capture_output=True, text=True, env=self.env(), timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("story-gate", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
