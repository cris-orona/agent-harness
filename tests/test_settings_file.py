"""Each tool fills unset env values from the settings file (AGENT_HARNESS_ENV_FILE)."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _isolate  # noqa: F401

BIN = Path(__file__).resolve().parents[1] / "bin"
TOOLS = ["agent-bus", "polar-notify", "herdr-deliver", "prod-op", "harness-alarm", "harness-canary"]
PROBE = r'''
import importlib.machinery, importlib.util, json, os, sys
loader = importlib.machinery.SourceFileLoader("tool", sys.argv[1])
spec = importlib.util.spec_from_loader("tool", loader)
mod = importlib.util.module_from_spec(spec)
try:
    loader.exec_module(mod)
except SystemExit:
    pass
print(json.dumps({k: os.environ.get(k) for k in ("HS_PLAIN", "HS_QUOTED", "HS_CMD", "HS_SET", "HS_EXPORT", "HS_HASH")}))
'''


class SettingsFile(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="settings-home-")
        self.file = os.path.join(self.home, "env")
        with open(self.file, "w") as f:
            f.write("# a comment\n"
                    "HS_PLAIN=~/a/b\n"
                    "HS_QUOTED='two words `x`'\n"
                    "HS_CMD='~/venv/bin/python3 -m pkg --dir ~/data'\n"
                    "HS_SET=from-file\n"
                    "export HS_EXPORT=\"yes\"\n"
                    "not a setting\n"
                    "HS_HASH=a#b\n")

    def probe(self, tool, env_file):
        env = dict(os.environ, HOME=self.home, AGENT_HARNESS_ENV_FILE=env_file, HS_SET="from-env")
        for k in ("HS_PLAIN", "HS_QUOTED", "HS_CMD", "HS_EXPORT", "HS_HASH"):
            env.pop(k, None)
        r = subprocess.run([sys.executable, "-c", PROBE, str(BIN / tool)], env=env, capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        import json
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_every_tool_reads_the_file(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                got = self.probe(tool, self.file)
                self.assertEqual(got["HS_PLAIN"], self.home + "/a/b")
                self.assertEqual(got["HS_QUOTED"], "two words `x`")
                self.assertEqual(got["HS_CMD"], "%s/venv/bin/python3 -m pkg --dir %s/data" % (self.home, self.home))
                self.assertEqual(got["HS_SET"], "from-env")  # the env wins
                self.assertEqual(got["HS_EXPORT"], "yes")
                self.assertEqual(got["HS_HASH"], "a#b")

    def test_empty_setting_turns_the_file_off(self):
        got = self.probe("agent-bus", "")
        self.assertIsNone(got["HS_PLAIN"])

    def test_missing_file_is_fine(self):
        got = self.probe("harness-alarm", os.path.join(self.home, "nope"))
        self.assertIsNone(got["HS_PLAIN"])

    def test_copies_are_the_same(self):
        blocks = set()
        for tool in TOOLS:
            src = (BIN / tool).read_text()
            start = src.index("def _load_settings_file():")
            end = src.index("_load_settings_file()\n", start + 30)
            blocks.add(src[start:end])
        self.assertEqual(len(blocks), 1)


if __name__ == "__main__":
    unittest.main()
