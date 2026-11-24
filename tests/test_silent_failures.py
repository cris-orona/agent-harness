"""The wire-ins that make a delivery failure loud (agent-bus and polar-notify): consecutive-failure counters. Every outside call is a fake; the real alarm never fires (HARNESS_ALARM=0 from _isolate)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: F401,E402  (role local, temp bus.db, HARNESS_ALARM=0)
import importlib.machinery, importlib.util, json, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / 'bin'


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def fake(path, code=0, log=None):
    """A shell script that records its arguments in `log` and exits `code`."""
    Path(path).write_text('#!/bin/sh\n%secho "$@" >> "%s"\nexit %d\n' % ("", log or "/dev/null", code))
    os.chmod(path, 0o755)
    return str(path)


def lines(p):
    try:
        return Path(p).read_text().splitlines()
    except OSError:
        return []


class AgentBusWireTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ab = load('agent_bus_silent', BIN / 'agent-bus')

    def setUp(self):
        d = tempfile.mkdtemp(prefix='streak-')
        p = mock.patch.dict(os.environ, {'AGENT_BUS_DB': os.path.join(d, 'bus.db')})
        p.start()
        self.addCleanup(p.stop)
        self.calls = []
        m = mock.patch.object(self.ab, 'harness_alarm', lambda *a: self.calls.append(a) or True)
        m.start()
        self.addCleanup(m.stop)

    def test_streak_alarms_on_second_failure_and_resets(self):
        ab = self.ab
        self.assertEqual(ab.fail_streak('x', False, 2), 1)
        self.assertEqual(self.calls, [])  # one failure is not an alarm
        self.assertEqual(ab.fail_streak('x', False, 2), 2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], 'agent-bus:x')
        self.assertEqual(ab.fail_streak('x', True, 2), 0)
        self.assertEqual(ab.fail_streak('x', False, 2), 1)
        self.assertEqual(len(self.calls), 1)

    def test_wake_card_counts_whole_calls(self):
        with mock.patch.object(self.ab, 'polar_bin', lambda: '/usr/bin/false'):
            self.assertFalse(self.ab.polar_wake_card('t', 'c', 'p'))
            self.assertEqual(self.calls, [])
            self.assertFalse(self.ab.polar_wake_card('t', 'c', 'p'))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], 'agent-bus:polar-wake-card')
        with mock.patch.object(self.ab, 'polar_bin', lambda: '/usr/bin/true'):
            self.assertTrue(self.ab.polar_wake_card('t', 'c', 'p'))
        with mock.patch.object(self.ab, 'polar_bin', lambda: '/usr/bin/false'):
            self.ab.polar_wake_card('t', 'c', 'p')
        self.assertEqual(len(self.calls), 1)  # the success reset the count

    def test_polar_notice_counts(self):
        class P:  # a failing `sh -c` run
            returncode, stderr = 1, 'boom'
        with mock.patch.object(self.ab.subprocess, 'run', lambda *a, **k: P()):
            self.ab.polar_notice('t', 'b', 'sid')
            self.assertEqual(self.calls, [])
            self.ab.polar_notice('t', 'b', 'sid')
        self.assertEqual([c[0] for c in self.calls], ['agent-bus:polar-notice'])

    def test_laptop_notice_counts(self):
        with mock.patch.object(self.ab.subprocess, 'run', side_effect=OSError('no polar')):
            self.ab.laptop_polar_notice('t', 'b', 'sid')
            self.ab.laptop_polar_notice('t', 'b', 'sid')
        self.assertEqual([c[0] for c in self.calls], ['agent-bus:laptop-polar-notice'])

    def test_harness_alarm_off_switch(self):
        m = mock.patch.dict(os.environ, {'HARNESS_ALARM': '0'})
        m.start()
        self.addCleanup(m.stop)
        real = load('agent_bus_silent2', BIN / 'agent-bus')
        self.assertFalse(real.harness_alarm('k', 't'))


class PolarNotifyWireTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pn = load('polar_notify_silent', BIN / 'polar-notify')

    def setUp(self):
        self.calls = []
        m = mock.patch.object(self.pn, 'alarm', lambda *a: self.calls.append(a) or True)
        m.start()
        self.addCleanup(m.stop)

    def test_list_failure_alarms_on_third_run(self):
        st = {}
        for want in (1, 2):
            self.assertEqual(self.pn.streak(st, 'list_fails', True, 3, 't'), want)
        self.assertEqual(self.calls, [])
        self.pn.streak(st, 'list_fails', True, 3, 't')
        self.assertEqual([c[0] for c in self.calls], ['polar-notify:list_fails'])
        self.assertEqual(self.pn.streak(st, 'list_fails', False, 3, 't'), 0)
        self.assertEqual(st['list_fails'], 0)

    def test_main_counts_list_failures(self):
        d = tempfile.mkdtemp(prefix='pn-')
        with mock.patch.object(self.pn, 'STATE', os.path.join(d, 'state.json')), \
                mock.patch.object(self.pn, 'polar_list', lambda since: None), \
                mock.patch.object(self.pn, 'bus', lambda *a, **k: (0, 'nothing to deliver')), \
                mock.patch.object(self.pn, 'is_server', lambda: False):
            for _ in range(3):
                self.pn.main()
            self.assertEqual(json.load(open(os.path.join(d, 'state.json')))['list_fails'], 3)
        self.assertEqual([c[0] for c in self.calls], ['polar-notify:list_fails'])

    def test_bus_away_old_item_alarms(self):
        d = tempfile.mkdtemp(prefix='pn-')
        old = '2020-01-01T00:00:00Z'
        card = {'id': 'c1', 'title': 't', 'status': 'chosen', 'decided_at': old, 'requester': 'x'}
        st = {'last': '2019-01-01T00:00:00Z', 'pending': {'c1': {'at': old, 'tries': 3}}}
        json.dump(st, open(os.path.join(d, 'state.json'), 'w'))
        with mock.patch.object(self.pn, 'STATE', os.path.join(d, 'state.json')), \
                mock.patch.object(self.pn, 'polar_list', lambda since: [card]), \
                mock.patch.object(self.pn, 'handle', lambda r: (False, 'bus away, pending c1: x')), \
                mock.patch.object(self.pn, 'claim_mail', lambda r, again=False: (True, '')), \
                mock.patch.object(self.pn, 'rescue_revise', lambda *a, **k: []), \
                mock.patch.object(self.pn, 'bus', lambda *a, **k: (0, 'nothing to deliver')), \
                mock.patch.object(self.pn, 'is_server', lambda: False):
            self.pn.main()
        self.assertIn('polar-notify:bus-away', [c[0] for c in self.calls])

    def test_failed_rescue_counts(self):
        d = tempfile.mkdtemp(prefix='pn-')
        with mock.patch.object(self.pn, 'STATE', os.path.join(d, 'state.json')), \
                mock.patch.object(self.pn, 'polar_list', lambda since: []), \
                mock.patch.object(self.pn, 'rescue_revise', lambda *a, **k: ['rescue c1 failed (1): x']), \
                mock.patch.object(self.pn, 'bus', lambda *a, **k: (0, 'nothing to deliver')), \
                mock.patch.object(self.pn, 'is_server', lambda: False):
            for _ in range(3):
                self.pn.main()
        self.assertEqual([c[0] for c in self.calls], ['polar-notify:rescue_fails'])


if __name__ == "__main__":
    unittest.main()
