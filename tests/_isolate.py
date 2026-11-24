"""Import first. No test may reach the real server or the real ~/.local/state: role local, a temp bus.db."""
import os, tempfile
from pathlib import Path

os.environ['AGENT_BUS_ROLE'] = 'local'
os.environ['AGENT_BUS_BIN'] = str(Path(__file__).resolve().parents[1] / "bin/agent-bus")
DEFAULT_DB = os.path.join(tempfile.mkdtemp(prefix='bus-test-'), 'bus.db')
os.environ.setdefault('AGENT_BUS_DB', DEFAULT_DB)
os.environ.setdefault('AGENT_BUS_PARKED_DIR', os.path.join(os.path.dirname(DEFAULT_DB), 'parked'))
os.environ.setdefault('AGENT_BUS_INBOX', os.path.join(os.path.dirname(DEFAULT_DB), 'inbox.md'))  # never the router's
os.environ.setdefault('HERDR_DELIVER_BIN', '/usr/bin/false')  # a test that types must use its own fake
os.environ.setdefault('AGENT_BUS_WAKE_SETTLE', '0')  # tests park and wake at once
os.environ.setdefault('AGENT_BUS_ROUTER_HOST', 'local')  # never the real router-host switch file
os.environ.setdefault('AGENT_BUS_RESUME_BUDGET', '0')  # no resume budget unless a test sets one
# Never a live premium-model check: AGENT_BUS_MODEL_FULL_CMD unset means "not full", so the model a test expects
# does not change when the real weekly limit is used up. A test that needs "full" sets it itself.
os.environ.pop('AGENT_BUS_MODEL_FULL_CMD', None)

# Never a real Polar. A test that calls it without its own stub would send a real card on every suite run.
# 1. A fake `polar` first on PATH, also POLAR_BIN (the polar command that agent-bus and polar-notify use): it sends
#    nothing, writes the call to POLAR_GUARD_LOG and exits 97. Child processes (the agent-bus CLI) get it too.
# 2. In this process, subprocess.run never starts the real polar (~/.local/bin/polar, or a command over ssh with
#    it): it writes the call to the log and answers 97.
# 3. Every test that called polar without its own stub (polar_stub, polar_rec, or a fake of its own) fails.
_BIN = os.path.join(os.path.dirname(DEFAULT_DB), 'bin')
os.makedirs(_BIN, exist_ok=True)
os.environ['PATH'] = _BIN + os.pathsep + os.environ.get('PATH', '')
import re as _re, subprocess as _subprocess, unittest as _unittest
POLAR_GUARD_LOG = os.path.join(os.path.dirname(DEFAULT_DB), 'polar-guard.log')
FAKE_POLAR = os.path.join(_BIN, 'polar')
with open(FAKE_POLAR, 'w') as _fh:
    _fh.write('#!/bin/sh\necho "$0 $*" >> "%s"\necho "test guard: no real polar in tests" >&2\nexit 97\n'
              % POLAR_GUARD_LOG)
os.chmod(FAKE_POLAR, 0o755)
os.environ['POLAR_BIN'] = FAKE_POLAR
_HOME = os.path.expanduser('~')
_REAL_POLAR = _re.compile(r'(?:~|%s)/\.local/bin/polar(?![\w.-])' % _re.escape(_HOME))


def _polar_call(args):
    """The command text when `args` would start the real polar (or the fake over ssh), else None."""
    parts = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args or [])
    text = ' '.join(os.fsdecode(p) if isinstance(p, (bytes, os.PathLike)) else str(p) for p in parts)
    return text if _REAL_POLAR.search(text) or (FAKE_POLAR in text and FAKE_POLAR != text.split(' ')[0]) else None


_real_run = _subprocess.run


def _guarded_run(*a, **k):
    text = _polar_call(a[0] if a else k.get('args'))
    if text is None:
        return _real_run(*a, **k)
    with open(POLAR_GUARD_LOG, 'a') as fh:
        fh.write(text.replace('\n', ' ')[:300] + '\n')
    return _subprocess.CompletedProcess(a[0] if a else k.get('args'), 97, '' if k.get('text') else b'',
                                        'test guard: no real polar in tests' if k.get('text') else b'')


_subprocess.run = _guarded_run


def _polar_lines():
    try:
        with open(POLAR_GUARD_LOG) as fh:
            return fh.read().splitlines()
    except FileNotFoundError:
        return []


_real_test_run = _unittest.TestCase.run


def _run_with_polar_guard(self, result=None):
    seen = len(_polar_lines())

    def check():
        new = _polar_lines()[seen:]
        if new:
            raise AssertionError('this test called polar without a stub (polar_stub / polar_rec): ' + new[0][:200])
    self.addCleanup(check)  # the first cleanup runs last: after the test's own cleanups
    return _real_test_run(self, result)


_unittest.TestCase.run = _run_with_polar_guard
os.environ.setdefault('HARNESS_ALARM', '0')  # no test may push or notify the owner; a test of a wire-in uses its own fake bin
os.environ['HARNESS_ALARM_SLACK_TOKEN_FILE'] = '/nonexistent/harness-alarm-test-token'  # never the real bot token
os.environ['HARNESS_ALARM_SLACK_URL'] = 'http://127.0.0.1:9/never'
