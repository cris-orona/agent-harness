"""agent-bus CLI and hook tests (temp DB)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: F401,E402  (role local, temp bus.db)
import sqlite3, importlib.machinery, importlib.util, shlex, io, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "bin/agent-bus"
loader = importlib.machinery.SourceFileLoader('agent_bus', str(PATH))
spec = importlib.util.spec_from_loader('agent_bus', loader)
bus = importlib.util.module_from_spec(spec)
loader.exec_module(bus)

A, B = 'a' * 8 + '-0000-0000-0000-' + '0' * 12, 'b' * 8 + '-0000-0000-0000-' + '0' * 12
NEW = 'c' * 8 + '-0000-0000-0000-' + '0' * 12


class BusBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ['AGENT_BUS_DB'] = os.path.join(self.tmp.name, 'bus.db')
        os.environ['AGENT_BUS_NO_MOVE'] = '1'  # only MoveToServerTest turns moves on
        self.c = bus.connect()
        for s in (A, B):
            with self.c:
                bus.touch(self.c, s)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def hook(self, payload, raw=None):
        env = dict(os.environ, HERDR_PANE_ID='w1:p1')
        r = subprocess.run([sys.executable, str(PATH), 'hook'], input=raw if raw is not None else json.dumps(payload),
                           capture_output=True, text=True, env=env, timeout=5)
        self.assertEqual(r.returncode, 0)
        return r

    def cli(self, *a):
        env = dict(os.environ)
        r = subprocess.run([sys.executable, str(PATH), *a], capture_output=True, text=True, env=env, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout



class BusTest(BusBase):
    def test_claim_and_owners_order(self):
        with self.c:
            bus.claim(self.c, 'pr', 'app-db#1', A, 'also')
            bus.claim(self.c, 'pr', 'app-db#1', B, 'primary')
        o = bus.owners(self.c, 'pr', 'app-db#1')
        self.assertEqual([x['session_id'] for x in o], [B, A])
        self.assertEqual(o[0]['role'], 'primary')

    def test_owners_all_also_gives_the_claims_of_old_sessions(self):
        with self.c:
            bus.claim(self.c, 'thread', 'mail:T1', A)
            self.c.execute("UPDATE sessions SET last_seen='2020-01-01T00:00:00Z', ended_at='2020-01-01T00:00:00Z' "
                           "WHERE session_id=?", (A,))
        self.assertEqual(bus.owners(self.c, 'thread', 'mail:T1'), [])
        self.assertEqual([o['session_id'] for o in bus.owners(self.c, 'thread', 'mail:T1', True)], [A])
        self.assertEqual(json.loads(self.cli('owners', 'thread', 'mail:T1')), [])
        self.assertEqual(json.loads(self.cli('owners', 'thread', 'mail:T1', '--all'))[0]['session_id'], A)

    def test_claim_no_touch_leaves_a_closed_session_closed(self):
        with self.c:
            self.c.execute("UPDATE sessions SET last_seen='2026-01-01T00:00:00Z', ended_at='2026-01-02T00:00:00Z' "
                           "WHERE session_id=?", (A,))
        self.cli('claim', 'thread', 'mail:T1', '--session', A, '--no-touch')
        row = self.c.execute('SELECT last_seen, ended_at FROM sessions WHERE session_id=?', (A,)).fetchone()
        self.assertEqual((row['last_seen'], row['ended_at']), ('2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z'))
        self.assertEqual([o['session_id'] for o in bus.owners(self.c, 'thread', 'mail:T1', True)], [A])
        self.cli('claim', 'thread', 'mail:T2', '--session', A)  # the plain claim still marks the session live
        self.assertIsNone(self.c.execute('SELECT ended_at FROM sessions WHERE session_id=?', (A,)).fetchone()[0])

    def test_new_primary_demotes_old(self):
        with self.c:
            bus.claim(self.c, 'topic', 'x', A)
            bus.claim(self.c, 'topic', 'x', B)
        roles = {x['session_id']: x['role'] for x in bus.owners(self.c, 'topic', 'x')}
        self.assertEqual(roles, {A: 'also', B: 'primary'})

    def test_release_and_notmine(self):
        with self.c:
            bus.claim(self.c, 'thread', 'C1:1.2', A)
        self.cli('notmine', 'thread', 'C1:1.2', '--session', A)
        self.assertEqual(bus.owners(self.c, 'thread', 'C1:1.2'), [])
        ev = json.loads(self.cli('events'))
        self.assertEqual(ev[0]['session_id'], A)
        self.assertEqual(json.loads(ev[0]['data'])['key'], 'C1:1.2')

    def test_stale_and_ended_skipped(self):
        with self.c:
            bus.claim(self.c, 'pr', 'sales#9', A)
            self.c.execute("UPDATE sessions SET last_seen='2020-01-01T00:00:00Z' WHERE session_id=?", (A,))
        self.assertEqual(bus.owners(self.c, 'pr', 'sales#9'), [])
        with self.c:
            bus.touch(self.c, A)
            self.c.execute("UPDATE sessions SET ended_at='2020-01-01T00:00:00Z' WHERE session_id=?", (A,))
        self.assertEqual(bus.owners(self.c, 'pr', 'sales#9'), [])
        with self.c:
            self.c.execute("UPDATE sessions SET ended_at=? WHERE session_id=?", (bus.now(), A))
        self.assertEqual(len(bus.owners(self.c, 'pr', 'sales#9')), 1)

    def test_cli_claim_owners_who(self):
        self.cli('claim', 'pr', 'supply#5', '--session', A)
        self.cli('claim', 'pr', 'supply#5', '--session', B, '--role', 'also')
        o = json.loads(self.cli('owners', 'pr', 'supply#5'))
        self.assertEqual([x['role'] for x in o], ['primary', 'also'])
        self.assertEqual(json.loads(self.cli('who', A))[0]['key'], 'supply#5')
        self.assertIn(A, self.cli('sessions'))

    def test_hook_malformed_exits_zero(self):
        for raw in ('', 'not json', '[]', '{"hook_event_name":"PostToolUse"}', 'null'):
            self.hook(None, raw)

    def test_hook_session_lifecycle(self):
        sid = 'c' * 8 + '-0000-0000-0000-' + '0' * 12
        self.hook({'session_id': sid, 'hook_event_name': 'SessionStart', 'cwd': self.tmp.name})
        r = self.c.execute('SELECT * FROM sessions WHERE session_id=?', (sid,)).fetchone()
        self.assertEqual(r['pane'], 'w1:p1')
        self.hook({'session_id': sid, 'hook_event_name': 'UserPromptSubmit'})
        self.hook({'session_id': sid, 'hook_event_name': 'Stop'})
        self.hook({'session_id': sid, 'hook_event_name': 'SessionEnd'})
        r = self.c.execute('SELECT * FROM sessions WHERE session_id=?', (sid,)).fetchone()
        self.assertTrue(r['ended_at'])

    def test_hook_branch_claim(self):
        repo = os.path.join(self.tmp.name, 'myrepo')
        subprocess.run(['git', 'init', '-q', '-b', 'feat/x', repo], check=True)
        self.hook({'session_id': A, 'hook_event_name': 'SessionStart', 'cwd': repo})
        self.assertEqual(bus.owners(self.c, 'branch', 'myrepo:feat/x')[0]['session_id'], A)

    def bash(self, sid, cmd, resp=''):
        self.hook({'session_id': sid, 'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                   'tool_input': {'command': cmd}, 'tool_response': {'stdout': resp}})

    def test_gh_pr_create(self):
        self.bash(A, 'gh pr create --draft --title x', 'https://github.com/your-org/app-db/pull/2601\n')
        o = bus.owners(self.c, 'pr', 'app-db#2601')
        self.assertEqual((o[0]['session_id'], o[0]['role']), (A, 'primary'))

    def test_gh_pr_create_without_url_does_nothing(self):
        self.bash(A, 'gh pr create', 'error')
        self.assertEqual(self.c.execute('SELECT count(*) FROM claims').fetchone()[0], 0)

    def test_gh_pr_follow(self):
        self.bash(A, 'gh pr checks 77 --repo your-org/sales')
        self.bash(B, 'cd x && gh pr view 77 --repo your-org/sales --json state')
        roles = {x['session_id']: x['role'] for x in bus.owners(self.c, 'pr', 'sales#77')}
        self.assertEqual(roles, {A: 'primary', B: 'also'})

    def test_polar_ask_thread(self):
        self.bash(A, 'polar ask reply --channel C0000000001 --thread 1795032901.251529 --title t --text-file f')
        self.assertEqual(bus.owners(self.c, 'thread', 'C0000000001:1795032901.251529')[0]['session_id'], A)
        self.bash(B, 'polar ask decision --title t')  # no channel: no claim
        self.assertEqual(self.c.execute("SELECT count(*) FROM claims WHERE session_id=?", (B,)).fetchone()[0], 0)

    def test_sync_polar(self):
        rows = [
            {'id': '1', 'kind': 'reply', 'requester': A + ' w4:p2VK', 'target_channel': 'D1', 'target_thread_ts': '1.1'},
            {'id': '2', 'kind': 'reply', 'requester': B + ' w4:p1', 'result_json': json.dumps({'channel': 'C2', 'ts': '2.2'})},
            {'id': '3', 'kind': 'reply', 'requester': A + ' w4:p2VK',
             'permalink': 'https://example.slack.com/archives/D0000000001/p1795039194527849?thread_ts=1795038533.739559&cid=D0000000001'},
            {'id': '4', 'kind': 'decision', 'requester': 'release-desk b-1'},
            {'id': '5', 'kind': 'reply', 'requester': 'claude 02de09d3 (setup)'},
        ]
        text = '\n'.join(json.dumps(r) for r in rows) + '\nnot json\n'
        self.assertEqual(bus.sync_polar(self.c, text), 3)
        self.assertEqual(bus.sync_polar(self.c, text), 0)  # idempotent
        self.assertEqual(bus.owners(self.c, 'thread', 'D1:1.1')[0]['session_id'], A)
        self.assertEqual(bus.owners(self.c, 'thread', 'C2:2.2')[0]['session_id'], B)
        self.assertEqual(bus.owners(self.c, 'thread', 'D0000000001:1795038533.739559')[0]['session_id'], A)


class ParkTest(BusBase):
    def repo(self, dirty=False):
        d = os.path.join(self.tmp.name, 'wt')
        subprocess.run(['git', 'init', '-q', d], check=True)
        if dirty:
            open(os.path.join(d, 'f'), 'w').write('x')
        with self.c:
            self.c.execute('UPDATE sessions SET cwd=?, pane=?, title=? WHERE session_id=?', (d, 'w4:p1', 'Alex tariff work', A))
        return d

    def fake_herdr(self):
        calls = []
        def fake(*a, timeout=15):
            calls.append(a)
            if a[:2] == ('tab', 'create'):
                return json.dumps({'result': {'tab': {'tab_id': 'w4:t9'}}})
            if a[:2] == ('pane', 'list'):
                return json.dumps({'result': {'panes': [{'pane_id': 'w4:p1', 'tab_id': 'w4:t1'}, {'pane_id': 'w4:p9', 'tab_id': 'w4:t9'}]}})
            if a[:2] == ('pane', 'read'):
                return '❯ '
            return ''
        bus.herdr = fake
        bus.schedule_tab_close = lambda tab, delay=15: calls.append(('close', tab))
        bus.time = __import__('types').SimpleNamespace(**{k: getattr(__import__('time'), k) for k in dir(__import__('time')) if not k.startswith('_')})
        bus.time.sleep = lambda s: None
        return calls

    def test_park_refuses_without_wait(self):
        self.repo()
        r = subprocess.run([sys.executable, str(PATH), 'park', '--session', A, '--about', 'x', '--note-file', '-'],
                           input='n', capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 2)
        self.assertIn('at least one', r.stderr)

    def test_park_refuses_dirty_then_allows(self):
        self.repo(dirty=True)
        quiet = os.path.join(self.tmp.name, 'quiet-bin')  # the CLI lists open Polar cards first: an empty list
        os.makedirs(quiet)
        with open(os.path.join(quiet, 'polar'), 'w') as fh:
            fh.write('#!/bin/sh\nexit 0\n')
        os.chmod(os.path.join(quiet, 'polar'), 0o755)
        env = dict(os.environ, PATH=quiet + os.pathsep + os.environ['PATH'])
        r = subprocess.run([sys.executable, str(PATH), 'park', '--session', A, '--wait', 'person:Alex', '--about', 'x', '--note-file', '-'],
                           input='n', capture_output=True, text=True, timeout=5, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn('uncommitted', r.stderr)
        self.assertIsNone(self.c.execute('SELECT parked_at FROM sessions WHERE session_id=?', (A,)).fetchone()[0])
        calls = self.fake_herdr()
        code, msg = bus.park(self.c, A, ['person:Alex'], 'x', 'note', allow_dirty=True, polar_fn=lambda sid: [])
        self.assertEqual(code, 0)
        self.assertIn(('close', 'w4:t1'), calls)

    def park(self, specs, about='tariff reply'):
        self.repo()
        calls = self.fake_herdr()
        code, msg = bus.park(self.c, A, specs, about, 'next: apply tariff')
        self.assertEqual(code, 0, msg)
        return calls

    def msg(self, **kw):
        m = {'ts': str(__import__('time').time() + 5), 'channel': 'D1', 'sender': 'Alex', 'is_dm': True, 'text': 'ok go ahead'}
        m.update(kw)
        return m

    def test_park_stores_waits_and_note(self):
        self.park(['person:Alex', 'time:+2h', 'prod:sales#5'])
        self.assertEqual(self.c.execute('SELECT count(*) FROM waits').fetchone()[0], 3)
        self.assertTrue(self.c.execute('SELECT parked_at FROM sessions WHERE session_id=?', (A,)).fetchone()[0])
        self.assertEqual(self.cli('note', A).strip(), 'next: apply tariff')

    def test_polar_wait_kind_validation(self):
        self.assertEqual(bus.parse_wait('polar:ABCDEF012345'), ('polar', 'abcdef012345'))
        for bad in ('polar:xyz', 'polar:abcdef01234', 'polar:'):
            with self.assertRaises(ValueError):
                bus.parse_wait(bad)

    def test_park_adds_polar_waits(self):
        self.repo()
        self.fake_herdr()
        code, msg = bus.park(self.c, A, ['person:Alex', 'polar:aaaaaaaaaaaa'], 'x', 'n',
                             polar_fn=lambda sid: ['aaaaaaaaaaaa', 'bbbbbbbbbbbb'])
        self.assertEqual(code, 0, msg)
        self.assertIn('bbbbbbbbbbbb', msg)
        keys = [r[0] for r in self.c.execute("SELECT key FROM waits WHERE kind='polar' ORDER BY id")]
        self.assertEqual(keys, ['aaaaaaaaaaaa', 'bbbbbbbbbbbb'])

    # park --auto: the task runner parks a session that sits quiet on an open Polar card
    def auto(self, cards=('aaaaaaaaaaaa',), **kw):
        fn = cards if callable(cards) else (lambda sid: list(cards))
        return bus.park(self.c, A, [], 'tu respuesta en Polar', '', polar_fn=fn, auto=True, **kw)

    def parked(self):
        return self.c.execute('SELECT parked_at FROM sessions WHERE session_id=?', (A,)).fetchone()[0]

    def test_auto_park_needs_no_wait_no_note_and_no_clean_worktree(self):
        self.repo(dirty=True)
        calls = self.fake_herdr()
        seen = self.c.execute('SELECT last_seen FROM sessions WHERE session_id=?', (A,)).fetchone()[0]
        with self.c:
            self.c.execute("UPDATE sessions SET last_seen='2026-01-01T00:00:00Z' WHERE session_id=?", (A,))
        code, msg = self.auto()
        self.assertEqual(code, 0, msg)
        self.assertTrue(self.parked())
        self.assertEqual([tuple(r) for r in self.c.execute('SELECT kind, key, about FROM waits')],
                         [('polar', 'aaaaaaaaaaaa', 'tu respuesta en Polar')])
        self.assertIn(('close', 'w4:t1'), calls)
        # not a turn of the session: last_seen stays, so a stage made before the park stays valid
        self.assertEqual(self.c.execute('SELECT last_seen FROM sessions WHERE session_id=?', (A,)).fetchone()[0],
                         '2026-01-01T00:00:00Z')
        self.assertNotEqual(seen, '2026-01-01T00:00:00Z')
        park = bus.last_park(self.c, A)
        self.assertEqual((park.get('auto'), park.get('note')), (True, ''))

    def test_auto_park_refuses_and_changes_nothing(self):
        self.repo()
        calls = self.fake_herdr()

        def boom(sid):
            raise RuntimeError('card bbbbbbbbbbbb waits for the session, not for the owner')
        for cards, why in (((), 'no open Polar card'), (boom, 'waits for the session')):
            code, msg = self.auto(cards)
            self.assertEqual(code, 3, msg)
            self.assertIn(why, msg)
        with self.c:
            self.c.execute("UPDATE sessions SET pane='w9:p404' WHERE session_id=?", (A,))
        code, msg = self.auto()
        self.assertEqual((code, 'no Herdr tab' in msg), (3, True), msg)
        with self.c:
            self.c.execute("UPDATE sessions SET pane='w4:p1', ended_at='2026-01-01T00:00:00Z' WHERE session_id=?", (A,))
        self.assertEqual(self.auto()[0], 3)
        with self.c:
            self.c.execute("UPDATE sessions SET ended_at=NULL WHERE session_id=?", (A,))
        code, msg = self.auto(pane='w4:p9')              # the caller checked another pane than the bus has
        self.assertEqual((code, 'another pane' in msg), (3, True), msg)
        with self.c:
            self.c.execute("UPDATE sessions SET host='another-machine' WHERE session_id=?", (A,))
        code, msg = self.auto()
        self.assertEqual((code, 'not open on this machine' in msg), (3, True), msg)
        self.assertIsNone(self.parked())
        self.assertEqual(self.c.execute('SELECT count(*) FROM waits').fetchone()[0], 0)
        self.assertFalse([c for c in calls if c[0] == 'close'])

    def test_auto_park_closes_the_tab_with_the_short_delay(self):
        self.repo()
        self.fake_herdr()
        delays = []
        bus.schedule_tab_close = lambda tab, delay=15: delays.append((tab, delay))
        self.assertEqual(self.auto(pane='w4:p1')[0], 0)
        self.assertEqual(delays, [('w4:t1', bus.AUTO_PARK_DELAY)])

    def test_auto_park_twice_is_refused(self):
        self.repo()
        self.fake_herdr()
        self.assertEqual(self.auto()[0], 0)
        self.assertEqual(self.auto()[0], 3)
        self.assertEqual(self.c.execute('SELECT count(*) FROM waits').fetchone()[0], 1)

    def test_a_plain_park_still_needs_a_wait(self):
        self.repo()
        self.fake_herdr()
        code, msg = bus.park(self.c, A, [], 'x', 'n', polar_fn=lambda sid: ['aaaaaaaaaaaa'])
        self.assertEqual(code, 2, msg)

    def test_auto_park_wake_text_names_no_note(self):
        self.repo()
        self.fake_herdr()
        self.assertEqual(self.auto()[0], 0)
        with self.c:
            self.c.execute("UPDATE waits SET met_at='2026-01-01T00:00:00Z', met_by=?", (json.dumps({'summary': 'Go'}),))
        text, summ = bus.wake_text(self.c, self.c.execute('SELECT * FROM waits').fetchall())
        self.assertIn('there is no handoff note', text)
        self.assertNotIn('agent-bus note', text)
        self.assertEqual(summ, 'Go')

    def test_polar_owner_ids_only_cards_that_wait_for_the_owner(self):
        d = os.path.join(self.tmp.name, 'bin')
        os.makedirs(d)
        out = os.path.join(self.tmp.name, 'polar.out')
        with open(os.path.join(d, 'polar'), 'w') as fh:
            fh.write('#!/bin/sh\ncat "%s"\n' % out)
        os.chmod(os.path.join(d, 'polar'), 0o755)
        rows = [{'id': 'aaaaaaaaaaaa', 'kind': 'decision', 'status': 'pending', 'requester': A + ' w4:p1'},
                {'id': 'bbbbbbbbbbbb', 'kind': 'questions', 'status': 'shown', 'requester': A + ' w4:p1'},
                {'id': 'cccccccccccc', 'kind': 'reply', 'status': 'running', 'requester': A + ' w4:p1'},
                {'id': 'ffffffffffff', 'kind': 'report', 'status': 'shown', 'requester': A + ' w4:p1'},
                {'id': '999999999999', 'status': 'shown', 'requester': A + ' w4:p1'},
                {'id': 'dddddddddddd', 'kind': 'reply', 'status': 'pending', 'requester': B + ' w4:p2'}]
        old = os.environ['PATH']
        os.environ['PATH'] = d + os.pathsep + old
        try:
            open(out, 'w').write('\n'.join(json.dumps(r) for r in rows) + '\nnot json\n')
            self.assertEqual(bus.polar_owner_ids(A), ['aaaaaaaaaaaa', 'bbbbbbbbbbbb'])
            # a final report is no ask: with only a report open the session does not park (exit 3)
            open(out, 'w').write(json.dumps(rows[3]) + '\n')
            self.assertEqual(bus.polar_owner_ids(A), [])
            self.repo()
            r = subprocess.run([sys.executable, str(PATH), 'park', '--session', A, '--auto', '--about', 'x'],
                               capture_output=True, text=True, timeout=10)
            self.assertEqual((r.returncode, 'no open Polar card' in r.stderr), (3, True), r.stderr)
            rows.append({'id': 'eeeeeeeeeeee', 'kind': 'report', 'status': 'revise', 'requester': A + ' w4:p1'})
            open(out, 'w').write('\n'.join(json.dumps(r) for r in rows) + '\n')
            with self.assertRaises(RuntimeError):
                bus.polar_owner_ids(A)
            self.assertEqual(bus.polar_owner_ids(B), ['dddddddddddd'])
            # the command line: a card in «Pedir cambios» means no park (exit 3)
            r = subprocess.run([sys.executable, str(PATH), 'park', '--session', A, '--auto', '--about', 'x'],
                               capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 3, r.stderr)
            self.assertIn('waits for the session', r.stderr)
        finally:
            os.environ['PATH'] = old
        self.assertIsNone(self.parked())

    def test_park_polar_failure_warns(self):
        self.repo()
        self.fake_herdr()
        def boom(sid):
            raise RuntimeError('ssh down')
        code, msg = bus.park(self.c, A, ['person:Alex'], 'x', 'n', polar_fn=boom)
        self.assertEqual(code, 0)
        self.assertIn('warning', msg)
        self.assertEqual(self.c.execute('SELECT count(*) FROM waits').fetchone()[0], 1)

    def test_meet_marks_wait_and_wake_resumes_with_summary(self):
        calls = self.park(['polar:aaaaaaaaaaaa'])
        self.assertEqual(bus.meet(self.c, 'polar', 'aaaaaaaaaaaa', 'Polar chosen: go', B), 0)  # other session
        self.assertEqual(bus.meet(self.c, 'polar', 'aaaaaaaaaaaa', 'Polar chosen: go', A), 1)
        self.assertEqual(bus.meet(self.c, 'polar', 'aaaaaaaaaaaa', 'again', A), 0)
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='wait-met'").fetchone()[0], 1)
        bus.wake(self.c)
        run = [a for a in calls if a[:2] == ('pane', 'run')]
        self.assertIn('Polar chosen: go', shlex.split(run[0][3])[3])

    def screen(self, calls, fn):
        orig = bus.herdr
        def fake(*a, timeout=15):
            if a[:2] == ('pane', 'read'):
                return fn(calls)
            if a[:2] == ('agent', 'list'):
                return json.dumps({'result': {'agents': []}})
            return orig(*a, timeout=timeout)
        bus.herdr = fake

    def test_wake_that_does_not_start_claude_keeps_the_waits_met(self):
        # 2026-11-20: a Slack relay opened a tab, Claude never started, and the waits were marked woken anyway.
        calls = self.park(['polar:aaaaaaaaaaaa'])
        bus.meet(self.c, 'polar', 'aaaaaaaaaaaa', 'Polar chosen: go', A)
        self.screen(calls, lambda calls: '➜  work ')
        bus.wake(self.c, ready_timeout=0, poll=0)
        self.assertIn(('tab', 'close', 'w4:t9'), calls)
        self.assertEqual(len(bus.met_waits(self.c, A)), 1)  # a later wake tries again

    def test_command_left_unrun_at_the_prompt_gets_one_more_enter(self):
        calls = self.park(['polar:aaaaaaaaaaaa'])
        bus.meet(self.c, 'polar', 'aaaaaaaaaaaa', 'Polar chosen: go', A)
        bus.ENTER_AGAIN_S = 0
        def read(calls):
            if ('pane', 'send-keys', 'w4:p9', 'Enter') in calls:
                return '❯ '
            ran = [c for c in calls if c[:2] == ('pane', 'run')]
            return ('➜  ' + ran[0][3]) if ran else '➜  '
        self.screen(calls, read)
        try:
            bus.wake(self.c, ready_timeout=5, poll=0)
        finally:
            bus.ENTER_AGAIN_S = 10
        self.assertEqual(calls.count(('pane', 'send-keys', 'w4:p9', 'Enter')), 1)
        self.assertEqual(bus.met_waits(self.c, A), [])

    def screen_agents(self, calls, read, agents):
        """Like screen(), with `agents(n)` as the answer of the n-th `herdr agent list` (a list, or an Exception)."""
        orig, seen = bus.herdr, []
        def fake(*a, timeout=15):
            if a[:2] == ('pane', 'read'):
                return read(calls)
            if a[:2] == ('agent', 'list'):
                seen.append(1)
                got = agents(len(seen))
                if isinstance(got, Exception):
                    raise got
                return json.dumps({'result': {'agents': got}})
            return orig(*a, timeout=timeout)
        bus.herdr = fake
        return seen

    def unrun_then(self, after):
        def read(calls):
            ran = [c for c in calls if c[:2] == ('pane', 'run')]
            return ('➜  ' + ran[0][3] + after) if ran else '➜  '
        return read

    def test_sits_unrun_ignores_wrapping_and_sees_output_after_the_command(self):
        cmd = "claude --resume abc 'a long text that wraps over two lines of the screen'"
        self.assertTrue(bus.sits_unrun('➜  work ' + cmd[:40] + '\n' + cmd[40:] + '  \n', cmd))
        self.assertFalse(bus.sits_unrun('➜  work ' + cmd + '\n Claude Code v2 …', cmd))
        self.assertFalse(bus.sits_unrun('➜  work ', cmd))

    def test_no_second_enter_for_a_claude_that_loads_slowly(self):
        # The command ran and Claude prints its start screen: an Enter now could answer a start question.
        calls = self.park(['polar:aaaaaaaaaaaa'])
        bus.meet(self.c, 'polar', 'aaaaaaaaaaaa', 'Polar chosen: go', A)
        bus.ENTER_AGAIN_S = 0
        self.screen(calls, self.unrun_then('\n\n Claude Code\n loading the session…'))
        try:
            bus.wake(self.c, ready_timeout=0, poll=0)
        finally:
            bus.ENTER_AGAIN_S = 10
        self.assertNotIn(('pane', 'send-keys', 'w4:p9', 'Enter'), calls)
        self.assertEqual(len(bus.met_waits(self.c, A)), 1)

    def test_no_second_enter_while_a_claude_process_runs_in_the_pane(self):
        # The command is still the last thing on the screen (Claude has drawn nothing yet), but its process runs: an
        # Enter now could answer the question that Claude draws next. With only the shell there, the Enter goes.
        for procs, enters in ((['node', 'claude'], 0), (['zsh'], 1)):
            with self.subTest(procs=procs):
                calls = self.park(['polar:aaaaaaaaaaaa'])
                bus.meet(self.c, 'polar', 'aaaaaaaaaaaa', 'Polar chosen: go', A)
                bus.ENTER_AGAIN_S = 0
                self.screen(calls, self.unrun_then(''))
                base = bus.herdr
                bus.herdr = lambda *a, timeout=15: (proc_info(procs) if a[:2] == ('pane', 'process-info')
                                                    else base(*a, timeout=timeout))
                try:
                    bus.wake(self.c, ready_timeout=0, poll=0)
                finally:
                    bus.ENTER_AGAIN_S = 10
                self.assertEqual(calls.count(('pane', 'send-keys', 'w4:p9', 'Enter')), enters)

    def test_shell_prompt_with_the_claude_arrow_is_not_ready(self):
        calls = self.park(['polar:aaaaaaaaaaaa'])
        bus.meet(self.c, 'polar', 'aaaaaaaaaaaa', 'Polar chosen: go', A)
        bus.ENTER_AGAIN_S = 0
        def read(calls):
            ran = [c for c in calls if c[:2] == ('pane', 'run')]
            if ('pane', 'send-keys', 'w4:p9', 'Enter') in calls:
                return '❯ ' + ran[0][3] + '\n ? for shortcuts'
            return ('❯ ' + ran[0][3]) if ran else '❯ '
        self.screen(calls, read)
        try:
            bus.wake(self.c, ready_timeout=5, poll=0)
        finally:
            bus.ENTER_AGAIN_S = 10
        self.assertEqual(calls.count(('pane', 'send-keys', 'w4:p9', 'Enter')), 1)
        self.assertEqual(bus.met_waits(self.c, A), [])

    def test_herdr_with_no_answer_leaves_the_tab_and_keeps_the_waits_met(self):
        calls = self.park(['polar:aaaaaaaaaaaa'])
        bus.meet(self.c, 'polar', 'aaaaaaaaaaaa', 'Polar chosen: go', A)
        seen = self.screen_agents(calls, self.unrun_then('\n loading…'), lambda n: RuntimeError('herdr away'))
        res = bus.wake(self.c, ready_timeout=0, poll=0)
        self.assertEqual(len(seen), 4)  # the check before the tab opens, then 3 reads
        self.assertNotIn(('tab', 'close', 'w4:t9'), calls)
        self.assertIn('not known', res[0])
        self.assertEqual(len(bus.met_waits(self.c, A)), 1)

    def test_agent_that_registers_late_counts_as_started(self):
        calls = self.park(['polar:aaaaaaaaaaaa'])
        bus.meet(self.c, 'polar', 'aaaaaaaaaaaa', 'Polar chosen: go', A)
        agent = [{'pane_id': 'w4:p9', 'agent_session': {'value': A}}]
        # the first `agent list` is the check before the tab opens (the session does not run yet)
        self.screen_agents(calls, self.unrun_then('\n loading…'), lambda n: agent if n >= 3 else [])
        bus.wake(self.c, ready_timeout=0, poll=0)
        self.assertNotIn(('tab', 'close', 'w4:t9'), calls)
        self.assertEqual(bus.met_waits(self.c, A), [])

    def test_session_from_a_claude_folder_resumes_from_work(self):
        calls = self.park(['polar:aaaaaaaaaaaa'])
        odd = os.path.join(os.path.expanduser('~'), '.claude', 'projects', 'x', 'memory')
        with self.c:
            self.c.execute('UPDATE sessions SET cwd=? WHERE session_id=?', (odd, A))
        bus.meet(self.c, 'polar', 'aaaaaaaaaaaa', 'Polar chosen: go', A)
        bus.wake(self.c)
        run = [c for c in calls if c[:2] == ('pane', 'run')][0][3]
        self.assertTrue(run.startswith('cd ') and '/work &&' in run.split(' claude ')[0], run)

    def test_wake_prompt_is_one_line_and_capped(self):
        out = bus.wake_prompt('a\nb ' + 'x' * 3000)
        self.assertNotIn('\n', out)
        self.assertLessEqual(len(out), 1500)
        self.assertTrue(out.endswith('Run agent-bus note for the full handoff.'))

    def test_wake_trust_question_notifies_and_types_nothing(self):
        calls = self.park(['person:Alex'])
        bus.match_slack(self.c, [self.msg(text='approved')])
        base = bus.herdr
        def fake(*a, timeout=15):
            if a[:2] == ('pane', 'read'):
                calls.append(a)
                return TRUST_SCREEN
            return base(*a, timeout=timeout)
        bus.herdr = fake
        polar_calls = []
        from unittest import mock
        with mock.patch.object(bus.subprocess, 'run', lambda cmd, *a, **k: polar_calls.append(cmd) or mock.Mock(returncode=0, stdout='', stderr='')):
            bus.wake(self.c, 2)
        self.assertEqual([c for c in calls if c[:2] == ('pane', 'send-text')], [])
        self.assertEqual([c for c in calls if c[:2] == ('pane', 'send-keys')], [])
        self.assertEqual([c for c in calls if c[0] == 'notification'], [])
        self.assertEqual(len(polar_calls), 1)
        card = polar_calls[0]
        self.assertEqual([os.path.basename(card[0])] + card[1:3], ['polar', 'ask', 'decision'])
        self.assertTrue(card[card.index('--title') + 1].startswith('Confirma la carpeta en la pestaña '))
        self.assertIn('ok:Listo', card)
        self.assertIn('Yes, I trust this folder', card[card.index('--context') + 1])

    def test_wake_sends_no_resumed_notification(self):
        calls = self.park(['person:Alex'])
        bus.match_slack(self.c, [self.msg(text='approved')])
        bus.wake(self.c, 2)
        self.assertEqual([c for c in calls if c[0] == 'notification'], [])

    def test_wake_from_home_resumes_in_work(self):
        calls = self.park(['person:Alex'])
        with self.c:
            self.c.execute('UPDATE sessions SET cwd=? WHERE session_id=?', (os.path.expanduser('~'), A))
        bus.match_slack(self.c, [self.msg(text='approved')])
        bus.wake(self.c, 2)
        run = [c for c in calls if c[:2] == ('pane', 'run')][0][3]
        self.assertTrue(run.startswith(f"cd {os.path.expanduser('~/work')} && claude --resume {A} "))

    def test_meet_add_only_for_parked(self):
        self.assertEqual(bus.meet(self.c, 'polar', 'aaaaaaaaaaaa', 's', A, add=True), 0)  # not parked
        self.park(['person:Alex'])
        self.assertEqual(bus.meet(self.c, 'polar', 'aaaaaaaaaaaa', 's', A, add=True), 1)
        self.assertEqual(self.c.execute("SELECT count(*) FROM waits WHERE kind='polar' AND met_at IS NOT NULL").fetchone()[0], 1)

    def test_meet_cli(self):
        self.park(['polar:aaaaaaaaaaaa'])
        self.assertEqual(self.cli('meet', 'polar', 'aaaaaaaaaaaa', '--session', A, '--summary', 'x').strip(), 'met 1')

    def test_match_slack_thread_exact_and_person(self):
        self.park(['thread:C9:1.5', 'person:Alex'])
        self.assertEqual(bus.match_slack(self.c, [self.msg(channel='C9', thread_ts='1.6', sender='Ana')]), 0)
        hits = []
        self.assertEqual(bus.match_slack(self.c, [self.msg(channel='C9', thread_ts='1.5', sender='Ana')], hits), 1)
        self.assertEqual(hits[0]['kind'], 'thread')
        self.assertEqual(bus.match_slack(self.c, [self.msg(sender='ALEX')]), 1)  # case-insensitive
        self.assertEqual(len(bus.open_waits(self.c)), 0)

    def test_match_slack_thread_from_permalink(self):
        self.park(['thread:D9:1795070915.693709'])
        link = 'https://x.slack.com/archives/D9/p1795096369256619?thread_ts=1795070915.693709'
        self.assertEqual(bus.match_slack(self.c, [self.msg(channel='D9', sender='Ana', permalink=link)]), 1)

    def test_match_slack_met_by_has_permalink_and_capped_text(self):
        self.park(['thread:D9:1795070915.693709', 'person:Alex'])
        link = 'https://x.slack.com/archives/D9/p1795096369256619?thread_ts=1795070915.693709'
        text = 'a' * 700
        n = bus.match_slack(self.c, [self.msg(channel='D9', sender='Alex', permalink=link, text=text)])
        self.assertEqual(n, 2)
        for r in self.c.execute('SELECT met_by FROM waits WHERE met_at IS NOT NULL').fetchall():
            summary = json.loads(r['met_by'])['summary']
            self.assertIn(link, summary)
            self.assertIn('a' * 600, summary)
            self.assertNotIn('a' * 601, summary)

    def test_match_slack_ignores_owner_and_old(self):
        self.park(['person:Alex'])
        self.assertEqual(bus.match_slack(self.c, [self.msg(ts='1.0')]), 0)
        self.assertEqual(len(bus.open_waits(self.c)), 1)

    # -- mail lines (`polar mail inbox`, slice 9)
    TID = '1a1f09ce900b6f9c'

    def mail(self, **kw):
        m = {'source': 'mail', 'ts': str(__import__('time').time() + 5), 'channel': 'mail', 'thread_ts': self.TID,
             'message_id': 'm77', 'sender': 'Ana Pérez', 'sender_id': 'ana@client.example', 'is_dm': True,
             'text': 'Re: REGLAS — va la regla', 'permalink': 'https://mail.google.com/mail/u/0/#all/' + self.TID,
             'to_owner': True, 'auth_ok': True, 'internal': False}
        m.update(kw)
        return m

    def test_mail_person_wait_needs_a_proven_sender(self):
        # «From» is free text: without Google's check of the From domain, neither the address nor the name counts.
        self.park(['person:ana@client.example', 'person:Ana'])
        self.assertEqual(bus.match_slack(self.c, [self.mail(auth_ok=False, internal=True)]), 0)
        self.assertEqual(bus.match_slack(self.c, [self.mail(auth_ok=False)]), 0)
        self.assertEqual(len(bus.open_waits(self.c)), 2)

    def test_mail_summary_has_no_control_characters_and_says_when_the_sender_is_not_verified(self):
        self.park(['thread:mail:' + self.TID])
        hits = []
        bus.match_slack(self.c, [self.mail(auth_ok=False, sender='Bob\x1b[2J\x03\r\nX', text='uno\x1b\x03\rdos')], hits)
        self.assertEqual(hits[0]['message_id'], 'm77')
        by = json.loads(self.c.execute('SELECT met_by FROM waits WHERE met_at IS NOT NULL').fetchone()['met_by'])
        self.assertTrue(all(ch.isprintable() for ch in by['summary']), by['summary'])
        self.assertIn('the sender is not verified', by['summary'])
        self.assertTrue(by['summary'].endswith(': uno dos'), by['summary'])

    def test_slack_hit_has_no_message_id(self):
        self.park(['person:Alex'])
        hits = []
        bus.match_slack(self.c, [self.msg()], hits)
        self.assertEqual(sorted(hits[0]), ['kind', 'session_id', 'ts'])

    def test_mail_thread_wait_is_met_and_keeps_the_message_id(self):
        self.park(['thread:mail:' + self.TID])
        hits = []
        self.assertEqual(bus.match_slack(self.c, [self.mail(thread_ts='other')]), 0)
        self.assertEqual(bus.match_slack(self.c, [self.mail()], hits), 1)
        self.assertEqual(hits[0]['kind'], 'thread')
        by = json.loads(self.c.execute('SELECT met_by FROM waits WHERE met_at IS NOT NULL').fetchone()['met_by'])
        self.assertEqual((by['message_id'], by['channel']), ('m77', 'mail'))
        self.assertIn('Ana Pérez wrote by email at ', by['summary'])
        self.assertIn('mail.google.com', by['summary'])

    def test_mail_thread_wait_with_a_16_digit_id_is_not_rewritten(self):
        self.assertEqual(bus.parse_wait('thread:mail:1795056443203239'), ('thread', 'mail:1795056443203239'))
        self.assertEqual(bus.parse_wait('thread:D1:p1795056443203239'), ('thread', 'D1:1795056443.203239'))

    def test_mail_person_wait_by_address_matches_any_sender(self):
        self.park(['person:Ana@Client.example'])
        self.assertEqual(bus.match_slack(self.c, [self.mail()]), 1)

    def test_mail_person_wait_by_name_needs_an_internal_sender(self):
        # A display name is free text: only a sender of the owner's own domain (Polar checked it) matches by name.
        self.park(['person:Ana', 'person:perez'])
        self.assertEqual(bus.match_slack(self.c, [self.mail()]), 0)
        self.assertEqual(bus.match_slack(self.c, [self.mail(internal=True, sender_id='ana@corp.example')]), 2)

    def test_claim_no_touch_goes_to_the_handoff_successor_and_follow_keeps_the_primary(self):
        C = 'c' * 8 + '-0000-0000-0000-' + '0' * 12
        with self.c:
            bus.touch(self.c, NEW)
            self.c.execute('UPDATE sessions SET handoff_from=? WHERE session_id=?', (A, C))
        self.cli('claim', 'thread', 'mail:T1', '--session', A, '--no-touch')
        self.assertEqual([(o['session_id'], o['role']) for o in bus.owners(self.c, 'thread', 'mail:T1')], [(C, 'primary')])
        self.cli('claim', 'thread', 'mail:T1', '--session', B, '--no-touch', '--role', 'follow')
        roles = {o['session_id']: o['role'] for o in bus.owners(self.c, 'thread', 'mail:T1')}
        self.assertEqual(roles, {C: 'primary', B: 'also'})
        self.cli('claim', 'thread', 'mail:T2', '--session', B, '--no-touch', '--role', 'follow')
        self.assertEqual(bus.owners(self.c, 'thread', 'mail:T2')[0]['role'], 'primary')

    def test_mail_person_wait_does_not_match_a_piece_of_a_word(self):
        self.park(['person:wall'])
        self.assertEqual(bus.match_slack(self.c, [self.mail(internal=True)]), 0)

    def test_slack_person_wait_still_needs_the_exact_sender(self):
        self.park(['person:Ana'])
        self.assertEqual(bus.match_slack(self.c, [self.msg(sender='Ana Pérez')]), 0)

    def test_due_time_and_prod(self):
        self.park(['time:+1h', 'prod:sales#5', 'prod:sales#6'])
        self.c.execute("UPDATE waits SET key='2020-01-01T00:00:00Z' WHERE kind='time'")
        self.c.commit()
        fol = json.dumps({'repo': 'sales', 'pr': 5, 'at': '2026-11-18T10:00:00Z'}) + '\nbad\n'
        self.assertEqual(bus.due(self.c, followups=fol), 2)
        left = bus.open_waits(self.c)
        self.assertEqual([w['key'] for w in left], ['sales#6'])

    def test_due_ci_cached(self):
        self.park(['ci:sales#7'])
        n = []
        def ci(repo, num):
            n.append(1)
            return 'pending'
        bus.due(self.c, ci_fn=ci)
        bus.due(self.c, ci_fn=ci)
        self.assertEqual(len(n), 1)  # cached 5 min
        os.remove(os.path.join(os.path.dirname(bus.db_path()), 'ci-cache.json'))
        self.assertEqual(bus.due(self.c, ci_fn=lambda r, x: 'done'), 1)

    def test_wake_resumes_with_trigger(self):
        calls = self.park(['person:Alex'])
        bus.match_slack(self.c, [self.msg(text='approved')])
        res = bus.wake(self.c, 2)
        self.assertTrue(res[0].startswith('ok'))
        run = [c for c in calls if c[:2] == ('pane', 'run')][0]
        self.assertTrue(run[3].startswith(f'claude --resume {A} '))
        self.assertEqual([c for c in calls if c[:2] == ('pane', 'send-text')], [])
        sent = shlex.split(run[3])[3]
        self.assertIn('Alex wrote in DM', sent)
        self.assertIn('approved', sent)
        self.assertLessEqual(len(sent), 600)
        self.assertIsNone(self.c.execute('SELECT parked_at FROM sessions WHERE session_id=?', (A,)).fetchone()[0])
        self.assertEqual(bus.wake(self.c, 2), [])  # not woken twice

    def test_wake_max_two(self):
        self.repo()
        calls = self.fake_herdr()
        ids = []
        for i in range(3):
            sid = f'{i}' * 8 + '-0000-0000-0000-' + '0' * 12
            ids.append(sid)
            with self.c:
                bus.touch(self.c, sid)
            bus.park(self.c, sid, [f'prod:r#{i}'], 'a', 'n', allow_dirty=True)
        bus.due(self.c, followups='\n'.join(json.dumps({'repo': 'r', 'pr': i}) for i in range(3)))
        self.assertEqual(len(bus.wake(self.c, 2)), 2)
        self.assertEqual(len(bus.wake(self.c, 2)), 1)

    def test_wake_person_with_two_candidates_goes_to_inbox(self):
        self.park(['person:Alex'])
        with self.c:
            bus.touch(self.c, B)
        bus.park(self.c, B, ['person:alex'], 'other', 'n', allow_dirty=True)
        inbox = os.path.join(self.tmp.name, 'inbox.md')
        bus.append_inbox = lambda line: open(inbox, 'a').write(line + '\n')
        bus.match_slack(self.c, [self.msg()])
        self.assertEqual(bus.wake(self.c, 2), [])
        self.assertIn('Alex'.lower(), open(inbox).read())
        self.assertEqual(bus.wake(self.c, 2), [])
        self.assertEqual(open(inbox).read().count('\n'), 2)  # one line per session, not repeated
        self.assertTrue(bus.wake(self.c, 2, only=A)[0].startswith('ok'))

    def test_unpark(self):
        self.park(['time:+1h'])
        self.cli('unpark', A)
        self.assertEqual(bus.open_waits(self.c), [])
        self.assertIsNone(self.c.execute('SELECT parked_at FROM sessions WHERE session_id=?', (A,)).fetchone()[0])


class TextTableTest(unittest.TestCase):
    """AGENT_BUS_LANG picks the few fixed owner-facing texts; the server name comes from AGENT_BUS_SERVER_HOST."""
    def setUp(self):
        self.addCleanup(setattr, bus, 'LANG', bus.LANG)

    def test_default_is_english(self):
        bus.LANG = 'en'
        self.assertIn('Where: its', bus.attach_line('task-w1'))
        self.assertIn('tmux attach -t task-w1', bus.attach_line('task-w1'))

    def test_spanish_text_and_server_name(self):
        bus.LANG = 'es'
        self.assertIn('Dónde: su pestaña', bus.attach_line('task-w1'))
        self.assertIn(bus.SERVER_HOST, bus.attach_line('task-w1'))
        self.assertIn(bus.SERVER_LABEL, bus.txt('took_over_title'))
        self.assertEqual(bus.txt('tz_note'), 'hora de Ciudad de México')

    def test_unknown_language_is_english(self):
        bus.LANG = 'fr'
        self.assertEqual(bus.txt('two_copies_title'), 'Two copies of one session: ')

    def test_every_language_has_every_key(self):
        self.assertEqual(set(bus.TEXTS['en']), set(bus.TEXTS['es']))


if __name__ == '__main__':
    unittest.main()


class MoveToServerTest(BusBase):
    """move_plan / move_to_server: a closed laptop session moves to server only when its work is safe there."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp.name
        g = lambda *a, cwd=None: subprocess.run(['git', *a], cwd=cwd, check=True, capture_output=True)
        self.origin = os.path.join(self.root, 'gh', 'your-org', 'demo.git')
        os.makedirs(os.path.dirname(self.origin))
        g('init', '-q', '--bare', self.origin)
        self.repo = os.path.join(self.root, 'demo')
        g('clone', '-q', self.origin, self.repo)
        for k, v in (('user.email', 't@t'), ('user.name', 't')):
            g('config', k, v, cwd=self.repo)
        Path(self.repo, 'a.txt').write_text('a')
        g('add', '.', cwd=self.repo); g('commit', '-qm', 'a', cwd=self.repo); g('push', '-q', 'origin', 'HEAD:main', cwd=self.repo)
        g('branch', '-q', '--set-upstream-to=origin/main', cwd=self.repo)
        self.g = g
        self.ssh_calls = []
        self.saved = (bus.ssh, bus.copy_transcript, bus.live_agent_sessions)
        bus.ssh = lambda cmd, timeout=40: (self.ssh_calls.append(cmd) or (0, 'READY\n'))
        bus.copy_transcript = lambda sid: True
        bus.live_agent_sessions = lambda: set()
        os.environ.pop('AGENT_BUS_NO_MOVE', None)

    def tearDown(self):
        bus.ssh, bus.copy_transcript, bus.live_agent_sessions = self.saved
        super().tearDown()

    def row(self, cwd, host='local'):
        with self.c:
            self.c.execute("UPDATE sessions SET cwd=?, host=?, ended_at='2026-11-19T00:00:00Z' WHERE session_id=?", (cwd, host, A))
        return self.c.execute("SELECT * FROM sessions WHERE session_id=?", (A,)).fetchone()

    def test_clean_pushed_product_repo_moves(self):
        plan, why = bus.move_plan(self.repo)
        self.assertIsNone(why)
        self.assertEqual(plan['slug'], 'your-org/demo')
        ok, cwd = bus.move_to_server(self.c, A, self.row(self.repo))
        self.assertTrue(ok, cwd)
        r = self.c.execute("SELECT host FROM sessions WHERE session_id=?", (A,)).fetchone()
        self.assertEqual(r['host'], bus.SERVER_HOST)
        self.assertTrue(any('READY' in c for c in self.ssh_calls))

    def test_dirty_or_unpushed_stays(self):
        Path(self.repo, 'b.txt').write_text('b')
        self.assertEqual(bus.move_plan(self.repo), (None, 'uncommitted changes'))
        self.g('add', '.', cwd=self.repo); self.g('commit', '-qm', 'b', cwd=self.repo)
        self.assertEqual(bus.move_plan(self.repo), (None, 'commits not pushed'))
        ok, why = bus.move_to_server(self.c, A, self.row(self.repo))
        self.assertFalse(ok)
        self.assertEqual(self.c.execute("SELECT host FROM sessions WHERE session_id=?", (A,)).fetchone()['host'], 'local')

    def test_personal_repo_stays(self):
        self.g('remote', 'set-url', 'origin', 'git@github.com:someone/agent-harness.git', cwd=self.repo)
        self.assertEqual(bus.move_plan(self.repo), (None, 'not a product repo'))

    def test_home_resumes_in_work(self):
        plan, why = bus.move_plan(os.path.expanduser('~'))
        self.assertEqual(plan['cwd'], os.path.expanduser('~/work'))
        self.assertIsNone(plan['top'])

    def test_running_session_never_moves(self):
        bus.live_agent_sessions = lambda: {A}
        self.assertEqual(bus.move_to_server(self.c, A, self.row(self.repo)), (False, 'the session runs now'))

    def test_switch_off(self):
        os.environ['AGENT_BUS_NO_MOVE'] = '1'
        try:
            self.assertFalse(bus.move_to_server(self.c, A, self.row(self.repo))[0])
        finally:
            os.environ.pop('AGENT_BUS_NO_MOVE')


class ResumeCommandTest(BusBase):
    def test_running_session_exits_3(self):
        r = subprocess.run([sys.executable, str(PATH), 'resume', A, '--text', 'hola'], capture_output=True, text=True,
                           env=dict(os.environ), timeout=10)
        self.assertEqual(r.returncode, 3)

    def test_closed_session_gets_a_met_wait(self):
        with self.c:
            self.c.execute("UPDATE sessions SET ended_at='2026-11-19T00:00:00Z' WHERE session_id=?", (A,))
        n = bus.meet(self.c, 'task', 'resume-x', 'hola', A, add=True, resume=True)
        self.assertEqual(n, 1)
        s = self.c.execute("SELECT parked_at FROM sessions WHERE session_id=?", (A,)).fetchone()
        self.assertTrue(s['parked_at'])


SESS = 'd' * 8 + '-0000-0000-0000-' + '0' * 12


class RemoteBusTest(unittest.TestCase):
    """Client role against a local `agent-bus dbd` (no ssh, no real server)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.server_db = os.path.join(t, 'server.db')
        self.good = f"env AGENT_BUS_ROLE=server AGENT_BUS_DB={self.server_db} {sys.executable} {PATH} dbd"
        self.env = dict(os.environ, AGENT_BUS_ROLE='client', AGENT_BUS_CLIENT_DIR=t, AGENT_BUS_REMOTE_CMD=self.good,
                        CLAUDE_SESSION_ID=SESS, HERDR_PANE_ID='w1:p1')
        self.env.pop('AGENT_BUS_DB', None)
        self.saved = {k: os.environ.get(k) for k in ('AGENT_BUS_ROLE', 'AGENT_BUS_CLIENT_DIR', 'AGENT_BUS_REMOTE_CMD')}

    def tearDown(self):
        if bus._REMOTE is not None:
            bus._REMOTE.close()
        bus._REMOTE = None
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def run_cli(self, *a, cmd=None, stdin=None):
        env = dict(self.env)
        if cmd is not None:
            env['AGENT_BUS_REMOTE_CMD'] = cmd
        return subprocess.run([sys.executable, str(PATH), *a], input=stdin, capture_output=True, text=True, env=env, timeout=60)

    def outbox(self):
        out = []
        for n in ('client-outbox.sending', 'client-outbox.jsonl'):  # a failed flush moves lines to .sending
            p = os.path.join(self.tmp.name, n)
            if os.path.exists(p):
                with open(p) as fh:
                    out += [x for x in fh.read().splitlines() if x.strip()]
        return out

    def test_rollback_applies_queued_lines_to_the_local_bus(self):
        # server away: a claim and a hook queue. After a rollback the laptop is local again and
        # `agent-bus flush` applies the queued lines to the laptop bus.db.
        r = self.run_cli('claim', 'topic', 'rolled', cmd='false')
        self.assertIn('queued', r.stdout)
        hook = json.dumps({'session_id': SESS, 'hook_event_name': 'Stop'})
        self.run_cli('hook', stdin=hook, cmd='false')
        laptop_db = os.path.join(self.tmp.name, 'laptop.db')
        env = dict(self.env, AGENT_BUS_ROLE='local', AGENT_BUS_DB=laptop_db)
        r = subprocess.run([sys.executable, str(PATH), 'flush'], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('sent 2', r.stdout)
        import sqlite3
        c = sqlite3.connect(laptop_db)
        try:
            self.assertEqual(c.execute("SELECT key FROM claims WHERE session_id=?", (SESS,)).fetchall(), [('rolled',)])
        finally:
            c.close()
        self.assertEqual(self.outbox(), [])

    def test_laptop_hook_runs_on_server_with_laptop_fields_and_its_time(self):
        hook = json.dumps({'session_id': SESS, 'hook_event_name': 'SessionStart', 'cwd': self.tmp.name})
        self.run_cli('hook', stdin=hook, cmd='false')  # queued: server away
        rec = json.loads(self.outbox()[0])
        self.assertEqual(rec['pre']['pane'], 'w1:p1')
        r = self.run_cli('flush')
        self.assertEqual(r.returncode, 0, r.stderr)
        row = self.server_rows("SELECT pane, host, last_seen FROM sessions WHERE session_id=?", SESS)[0]
        self.assertEqual(row, ('w1:p1', 'local', rec['time']))  # a laptop session; the time it fired

    def test_killed_flush_repeats_no_applied_line(self):
        # A flush killed after 2 of 3 lines left .sending and .sending.done=2: only the third line is applied.
        lines = [json.dumps({'kind': 'cli', 'argv': ['claim', 'topic', f't{i}', '--session', SESS]}) for i in range(3)]
        with open(os.path.join(self.tmp.name, 'client-outbox.sending'), 'w') as fh:
            fh.write('\n'.join(lines) + '\n')
        with open(os.path.join(self.tmp.name, 'client-outbox.sending.done'), 'w') as fh:
            fh.write('2')
        r = self.run_cli('flush')
        self.assertIn('sent 1', r.stdout)
        self.assertEqual(self.server_rows("SELECT key FROM claims"), [('t2',)])
        self.assertEqual(self.outbox(), [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'client-outbox.sending.done')))

    def test_busy_bus_keeps_the_line(self):
        os.environ.update(AGENT_BUS_ROLE='client', AGENT_BUS_CLIENT_DIR=self.tmp.name, AGENT_BUS_REMOTE_CMD=self.good)
        bus.outbox_append({'kind': 'cli', 'argv': ['claim', 'topic', 'busy', '--session', SESS]})
        orig = bus.cli
        def locked(argv):
            raise sqlite3.OperationalError('database is locked')
        bus.cli = locked
        try:
            with self.assertRaises(bus.BusOffline):
                bus.flush()
        finally:
            bus.cli = orig
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(bus.flush(), 1)
        self.assertEqual(self.server_rows("SELECT key FROM claims"), [('busy',)])

    def test_dbd_ends_a_transaction_that_waits_too_long(self):
        env = dict(os.environ, AGENT_BUS_ROLE='server', AGENT_BUS_DB=self.server_db, AGENT_BUS_DBD_TXN_LIMIT='1')
        p = subprocess.Popen([sys.executable, str(PATH), 'dbd'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             env=env, text=True)
        try:
            self.assertIn('hello', p.stdout.readline())
            p.stdin.write(json.dumps({'op': 'execute', 'sql': "INSERT INTO events VALUES('t','s','x','{}')",
                                      'params': []}) + '\n')
            p.stdin.flush()
            self.assertIn('rowcount', p.stdout.readline())
            self.assertEqual(p.wait(timeout=5), 0)  # no next line within 1 s: rolled back and ended
        finally:
            if p.poll() is None:
                p.kill()
        c = sqlite3.connect(self.server_db, timeout=0.2)
        try:
            c.execute("INSERT INTO events VALUES('t2','s','y','{}')")  # the lock is free
            c.commit()
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE type='x'").fetchone()[0], 0)
        finally:
            c.close()

    def server_rows(self, sql, *params):
        import sqlite3
        c = sqlite3.connect(self.server_db)
        try:
            return c.execute(sql, params).fetchall()
        finally:
            c.close()

    def hook_line(self, event='SessionStart'):
        return json.dumps({'session_id': SESS, 'hook_event_name': event, 'cwd': '/tmp'})

    def test_claim_then_owners_and_who(self):
        r = self.run_cli('claim', 'topic', 'Algo')
        self.assertEqual(r.returncode, 0, r.stderr)
        o = json.loads(self.run_cli('owners', 'topic', 'Algo').stdout)
        self.assertEqual([x['session_id'] for x in o], [SESS])
        w = self.run_cli('who', SESS)
        self.assertEqual(w.returncode, 0, w.stderr)
        self.assertIn('Algo', w.stdout)

    def test_hook_is_queued_then_flushed(self):
        r = self.run_cli('hook', stdin=self.hook_line())
        self.assertEqual(r.returncode, 0)
        import time
        for _ in range(50):  # the hook may start a background flush
            if not self.outbox():
                break
            time.sleep(0.1)
        f = self.run_cli('flush')
        self.assertEqual(f.returncode, 0, f.stderr)
        self.assertEqual(len(self.server_rows('SELECT 1 FROM sessions WHERE session_id=?', SESS)), 1)

    def test_hook_queues_with_pane_when_server_away(self):
        self.run_cli('hook', stdin=self.hook_line(), cmd='false')
        lines = [json.loads(x) for x in self.outbox()]
        self.assertEqual((lines[0]['kind'], lines[0]['pre']['pane']), ('hook', 'w1:p1'))

    def test_order_is_kept(self):
        self.run_cli('hook', stdin=self.hook_line(), cmd='false')
        q = self.run_cli('claim', 'topic', 'Orden', cmd='false')
        self.assertIn('queued', q.stdout)
        self.assertEqual(len(self.outbox()), 2)
        f = self.run_cli('flush')
        self.assertEqual(f.returncode, 0, f.stderr)
        self.assertEqual(self.outbox(), [])
        self.assertEqual(len(self.server_rows("SELECT 1 FROM claims WHERE key='Orden' AND session_id=?", SESS)), 1)
        self.assertEqual(len(self.server_rows('SELECT 1 FROM sessions WHERE session_id=?', SESS)), 1)

    def test_server_away(self):
        c = self.run_cli('claim', 'topic', 'Offline', cmd='false')
        self.assertEqual(c.returncode, 0)
        self.assertIn('queued', c.stdout)
        o = self.run_cli('owners', 'topic', 'Offline', cmd='false')
        self.assertEqual((o.returncode, o.stdout.strip()), (0, '[]'))
        self.assertEqual(self.run_cli('park', '--wait', 'person:A', '--note-file', '-', stdin='n', cmd='false').returncode, 3)
        self.assertEqual(self.run_cli('waits', cmd='false').returncode, 3)
        # the next call with a good server sends the queued claim first
        ok = self.run_cli('owners', 'topic', 'Offline')
        self.assertEqual([x['session_id'] for x in json.loads(ok.stdout)], [SESS])
        self.assertEqual(self.outbox(), [])

    def test_dbd_refuses_when_not_server(self):
        bad = f"env AGENT_BUS_ROLE=local AGENT_BUS_DB={self.server_db} {sys.executable} {PATH} dbd"
        r = self.run_cli('owners', 'topic', 'x', cmd=bad)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, '[]'))
        self.assertIn('unreachable', r.stderr)
        self.assertEqual(self.run_cli('waits', cmd=bad).returncode, 3)

    def connect_inproc(self):
        os.environ.update(AGENT_BUS_ROLE='client', AGENT_BUS_CLIENT_DIR=self.tmp.name, AGENT_BUS_REMOTE_CMD=self.good)
        bus._REMOTE = None
        return bus.connect()

    def test_remote_row_forms_and_errors(self):
        c = self.connect_inproc()
        with c:
            bus.touch(c, SESS)
        row = c.execute('SELECT session_id, pane FROM sessions WHERE session_id=?', (SESS,)).fetchone()
        self.assertEqual((row['session_id'], row[0], row[1]), (SESS, SESS, None))
        self.assertEqual(set(dict(row)), {'session_id', 'pane'})
        sid, pane = row
        self.assertEqual(sid, SESS)
        self.assertEqual(dict(c.execute('SELECT session_id, 1 FROM sessions')), {SESS: 1})
        import sqlite3
        with self.assertRaises(sqlite3.OperationalError):
            c.execute('SELECT * FROM no_such_table')

    def test_exception_in_with_rolls_back(self):
        c = self.connect_inproc()
        with self.assertRaises(RuntimeError):
            with c:
                bus.touch(c, SESS)
                raise RuntimeError('x')
        self.assertEqual(c.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
        self.assertEqual(self.server_rows('SELECT COUNT(*) FROM sessions')[0][0], 0)


class ServerRoleTest(unittest.TestCase):
    """AGENT_BUS_ROLE=server in this process; every outside effect is replaced."""
    PATCHED = ('end_task_tmux', 'tmux_name', 'git', 'git_status', 'herdr', 'schedule_tab_close')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_env = {k: os.environ.get(k) for k in ('AGENT_BUS_ROLE', 'AGENT_BUS_DB', 'AGENT_BUS_PARKED_DIR')}
        os.environ.update(AGENT_BUS_ROLE='server', AGENT_BUS_DB=os.path.join(self.tmp.name, 'bus.db'),
                          AGENT_BUS_PARKED_DIR=os.path.join(self.tmp.name, 'parked'))
        self.saved = {k: getattr(bus, k) for k in self.PATCHED}
        self.herdr_calls, self.ended, self.dirty = [], [], ''
        bus.herdr = lambda *a, timeout=15: self.herdr_calls.append(a) or ''
        bus.schedule_tab_close = lambda tab, delay=15: self.herdr_calls.append(('close', tab))
        bus.tmux_name = lambda: 'task-wdddddddd'
        bus.end_task_tmux = lambda name, sid, delay=15: self.ended.append(name) or True
        bus.git = lambda cwd, *a: self.dirty if a[:1] == ('status',) else ''
        bus.git_status = lambda cwd: self.dirty
        self.c = bus.connect()

    def tearDown(self):
        self.c.close()
        for k, v in self.saved.items():
            setattr(bus, k, v)
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_hook_records_host_and_tmux(self):
        bus.hook(json.dumps({'session_id': SESS, 'hook_event_name': 'SessionStart', 'cwd': self.tmp.name}))
        r = self.c.execute('SELECT host, tmux FROM sessions WHERE session_id=?', (SESS,)).fetchone()
        self.assertEqual((r['host'], r['tmux']), ('server', 'task-wdddddddd'))

    def test_park_ends_task_tmux_without_herdr(self):
        bus.hook(json.dumps({'session_id': SESS, 'hook_event_name': 'SessionStart', 'cwd': self.tmp.name}))
        code, msg = bus.park(self.c, SESS, ['person:Ana'], 'x', 'note')
        self.assertEqual(code, 0, msg)
        self.assertEqual(self.ended, ['task-wdddddddd'])
        self.assertEqual(self.herdr_calls, [])

    def test_auto_park_ends_the_task_tmux_named_in_the_bus_row(self):
        bus.hook(json.dumps({'session_id': SESS, 'hook_event_name': 'SessionStart', 'cwd': self.tmp.name}))
        bus.tmux_name = lambda: None   # the coordinator runs outside the session's tmux
        self.dirty = ' M f'            # the session could not commit first: no worktree check
        code, msg = bus.park(self.c, SESS, [], 'tu respuesta en Polar', '', polar_fn=lambda sid: ['aaaaaaaaaaaa'], auto=True)
        self.assertEqual(code, 0, msg)
        self.assertEqual(self.ended, ['task-wdddddddd'])
        self.assertEqual(self.herdr_calls, [])
        self.assertTrue(self.c.execute('SELECT parked_at FROM sessions WHERE session_id=?', (SESS,)).fetchone()[0])

    def test_auto_park_never_ends_another_tmux_session(self):
        bus.hook(json.dumps({'session_id': SESS, 'hook_event_name': 'SessionStart', 'cwd': self.tmp.name}))
        with self.c:
            self.c.execute("UPDATE sessions SET tmux='release-desk' WHERE session_id=?", (SESS,))
        code, msg = bus.park(self.c, SESS, [], 'x', '', polar_fn=lambda sid: ['aaaaaaaaaaaa'], auto=True)
        self.assertEqual(code, 3, msg)
        self.assertEqual(self.ended, [])
        self.assertIsNone(self.c.execute('SELECT parked_at FROM sessions WHERE session_id=?', (SESS,)).fetchone()[0])

    def test_park_dirty_worktree_refused(self):
        bus.hook(json.dumps({'session_id': SESS, 'hook_event_name': 'SessionStart', 'cwd': self.tmp.name}))
        self.dirty = ' M file'
        code, msg = bus.park(self.c, SESS, ['person:Ana'], 'x', 'note')
        self.assertEqual(code, 2)
        self.assertIn('uncommitted', msg)
        self.assertEqual(self.ended, [])

    def test_ssh_runs_locally(self):
        calls = []
        orig = bus.subprocess.run
        bus.subprocess.run = lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, 'out', '')
        try:
            self.assertEqual(bus.ssh('echo hi'), (0, 'out'))
        finally:
            bus.subprocess.run = orig
        self.assertEqual(calls, [['/bin/sh', '-c', 'echo hi']])

    def test_wake_laptop_session_returns_false(self):
        with self.c:
            bus.touch(self.c, SESS)
        ok, why = bus.wake_one(self.c, SESS, [])
        self.assertFalse(ok)
        self.assertIn('laptop', why)
        self.assertEqual(self.herdr_calls, [])
        self.assertIsNone(self.c.execute('SELECT ended_at FROM sessions WHERE session_id=?', (SESS,)).fetchone()[0])


NEWSID = 'c' * 8 + '-0000-0000-0000-' + '0' * 12


# ---- deliveries --------------------------------------------------------------------------------------------------
def minutes_ago(n):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=n)).strftime('%Y-%m-%dT%H:%M:%SZ')


class DeliverBase(BusBase):
    """In-process deliveries: every outside effect (type into a pane or tmux, ssh, herdr, inbox, wake) is a stub."""
    PATCHED = ('deliver_tmux', 'deliver_pane', 'ssh', 'herdr', 'append_inbox', 'wake', '_wake_one', 'wake_remote',
               'write_retry')

    def setUp(self):
        super().setUp()
        self.saved = {k: getattr(bus, k) for k in self.PATCHED}
        self.saved_env = os.environ.get('AGENT_BUS_ROLE')
        self.rc, self.rc_tmux = 0, 0
        self.wake_res = ['ok: woke']
        self.saved_cdir = os.environ.get('AGENT_BUS_CLIENT_DIR')
        os.environ['AGENT_BUS_CLIENT_DIR'] = os.path.join(self.tmp.name, 'client')  # the done journal
        self.pane_calls, self.tmux_calls, self.inbox, self.wake_calls, self.ssh_calls = [], [], [], [], []
        self.stashes = []
        self.panes = ['w4:p1']  # panes the fake Herdr lists
        self.agents = [A, B]    # sessions that the fake Herdr shows as running
        self.tmux_alive = 'Y'
        bus.deliver_pane = lambda pane, text, stash=False: self.pane_calls.append((pane, text)) or self.stashes.append(stash) or self.rc
        bus.deliver_tmux = lambda name, text, stash=False: self.tmux_calls.append((name, text)) or self.stashes.append(stash) or self.rc_tmux
        bus.append_inbox = lambda line: self.inbox.append(line)
        bus.herdr = lambda *a, timeout=15: json.dumps({'result': {
            'panes': [{'pane_id': p} for p in self.panes],
            'agents': [{'agent_session': {'value': s}} for s in self.agents]}})
        bus.ssh = lambda cmd, timeout=40: self.ssh_calls.append(cmd) or (0, self.tmux_alive)
        bus.wake = self.fake_wake

    def fake_wake(self, c, n=2, only=None, **kw):
        """Like a real wake: when the result starts with «ok: », the session's met waits reach it (woken_at)."""
        self.wake_calls.append((n, only))
        if self.wake_res and self.wake_res[0].startswith('ok: '):
            with c:
                c.execute('UPDATE waits SET woken_at=? WHERE session_id=? AND met_at IS NOT NULL AND woken_at IS NULL',
                          (bus.now(), only))
        return self.wake_res

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(bus, k, v)
        if self.saved_cdir is None:
            os.environ.pop('AGENT_BUS_CLIENT_DIR', None)
        else:
            os.environ['AGENT_BUS_CLIENT_DIR'] = self.saved_cdir
        if self.saved_env is None:
            os.environ.pop('AGENT_BUS_ROLE', None)
        else:
            os.environ['AGENT_BUS_ROLE'] = self.saved_env
        super().tearDown()

    def set_session(self, sid=A, **cols):
        with self.c:
            for k, v in cols.items():
                self.c.execute(f'UPDATE sessions SET {k}=? WHERE session_id=?', (v, sid))

    def row(self, did):
        return self.c.execute('SELECT * FROM deliveries WHERE id=?', (did,)).fetchone()

    def rows(self):
        return self.c.execute('SELECT * FROM deliveries ORDER BY id').fetchall()

    def add_wait(self, sid, kind, key, met=False, delivery=None, woken=False):
        with self.c:
            cur = self.c.execute('INSERT INTO waits(session_id, kind, key, about, created_at, met_at, delivery_id, woken_at) '
                                 'VALUES(?,?,?,?,?,?,?,?)',
                                 (sid, kind, key, f'{kind} {key}', bus.now(), bus.now() if met else None, delivery,
                                  bus.now() if woken else None))
        return cur.lastrowid

    def outcome_id(self, outcome):
        return int(outcome.split()[1].rstrip(':')) if outcome.startswith('queued') else None


LIMIT_MENU = """  Ran 1 shell command
  ⎿  You've hit your Fable limit · resets Nov 30 at 10am (America/Mexico_City)
▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
  What do you want to do?
  ❯ 1. Stop and wait for limit to reset
    2. Wait here, then continue automatically at Nov 30 at 10am
    3. Switch to usage credits
  Enter to confirm · Esc to cancel
"""
LIMIT_MENU_GONE = """  ⎿  You've hit your Fable limit · resets Nov 30 at 10am (America/Mexico_City)
────────────────────────────────────────
❯ 
────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle)
"""


class LimitMenuTest(DeliverBase):
    """The menu after a model limit has no input box and waits for a key: the bus presses Escape, and no other key."""
    PATCHED = DeliverBase.PATCHED + ('place_screen',)

    def test_the_real_screen_is_the_menu_and_a_quote_or_another_menu_is_not(self):
        self.assertTrue(bus.limit_menu(LIMIT_MENU))
        self.assertTrue(bus.limit_menu(LIMIT_MENU + '[task-59d20:2.1.331*            "x" 17:31 23-Nov-26\n'))  # tmux bar
        self.assertFalse(bus.limit_menu(LIMIT_MENU_GONE))
        self.assertFalse(bus.limit_menu(''))
        self.assertFalse(bus.limit_menu(None))
        quoted = '⏺ The screen said:\n' + LIMIT_MENU + LIMIT_MENU_GONE   # a message quotes it above a live input box
        self.assertFalse(bus.limit_menu(quoted))
        no_cursor = LIMIT_MENU.replace('❯ 1.', '  1.')
        self.assertFalse(bus.limit_menu(no_cursor))
        other = LIMIT_MENU.replace('What do you want to do?', 'Do you want to proceed?')
        self.assertFalse(bus.limit_menu(other))
        prose = '⏺ Options: Stop and wait for limit to reset, or switch.\n  What do you want to do?\n  ❯ 1. Yes\n  Esc to cancel\n'
        self.assertFalse(bus.limit_menu(prose))

    def test_a_delivery_closes_the_menu_on_the_laptop_and_the_next_try_types_the_text(self):
        self.set_session(host='local', pane='w4:p1')
        screens, keys = [LIMIT_MENU], []
        bus.place_screen = lambda host, place: screens[-1]
        herdr = bus.herdr
        bus.herdr = lambda *a, timeout=15: keys.append(a) or '{}' if a[:2] == ('pane', 'send-keys') else herdr(*a)
        self.rc = 5
        out = bus.deliver(self.c, A, 'continue')
        self.assertTrue(out.startswith('queued'), out)
        self.assertIn('Escape closed it', out)
        self.assertEqual(keys, [('pane', 'send-keys', 'w4:p1', 'Escape')])
        self.assertEqual(self.inbox, [])
        screens.append(LIMIT_MENU_GONE)
        self.rc = 0
        bus.pull(self.c)
        self.assertIsNotNone(self.rows()[0]['done_at'])
        self.assertEqual(len(keys), 1)  # one Escape, no more

    def test_a_delivery_closes_the_menu_in_a_server_tmux(self):
        self.set_session(host='server', tmux='task-wx')
        bus.place_screen = lambda host, place: LIMIT_MENU
        self.rc_tmux = 5
        out = bus.deliver(self.c, A, 'continue')
        self.assertIn('Escape closed it', out)
        sent = [c for c in self.ssh_calls if 'send-keys' in c]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].endswith(' Escape') and 'task-wx' in sent[0], sent[0])

    def test_another_screen_with_no_input_box_gets_no_key(self):
        self.set_session(host='local', pane='w4:p1')
        keys = []
        bus.place_screen = lambda host, place: LIMIT_MENU.replace('What do you want to do?', 'Do you trust this folder?')
        herdr = bus.herdr
        bus.herdr = lambda *a, timeout=15: keys.append(a) or '{}' if a[:2] == ('pane', 'send-keys') else herdr(*a)
        self.rc = 5
        out = bus.deliver(self.c, A, 'x')
        self.assertIn('no free input box', out)
        self.assertEqual(keys, [])


class DeliverIfLiveTest(DeliverBase):
    def assert_not_delivered(self, out, word):
        self.assertTrue(out.startswith('not delivered:'), out)
        self.assertIn(word, out)
        self.assertEqual(self.pane_calls, [])
        self.assertEqual(self.tmux_calls, [])
        self.assertEqual(self.wake_calls, [])
        self.assertEqual(self.inbox, [])
        self.assertEqual(self.c.execute('SELECT count(*) FROM waits').fetchone()[0], 0)

    def test_live_laptop_session_is_typed(self):
        self.set_session(host='local', pane='w4:p1')
        out = bus.deliver(self.c, A, 'hi', live_only=True)
        self.assertTrue(out.startswith('delivered'), out)
        self.assertEqual(self.pane_calls, [('w4:p1', 'hi')])
        r = self.rows()[0]
        self.assertIsNotNone(r['done_at'])
        self.assertEqual(r['live_only'], 1)

    def test_live_server_session_is_typed_into_tmux(self):
        self.set_session(host='server', tmux='task-wx', pane='w9:p9')
        out = bus.deliver(self.c, A, 'hello', live_only=True)
        self.assertTrue(out.startswith('delivered'), out)
        self.assertEqual(self.tmux_calls, [('task-wx', 'hello')])
        self.assertEqual(self.pane_calls, [])

    def test_parked_session_is_not_delivered_and_not_woken(self):
        self.set_session(host='local', pane='w4:p1', parked_at=minutes_ago(60))
        out = bus.deliver(self.c, A, 'hi', live_only=True)
        self.assert_not_delivered(out, 'parked')
        self.assertIsNotNone(self.rows()[0]['done_at'])

    def test_closed_session_is_not_delivered_and_not_woken(self):
        self.set_session(host='local', pane='w4:p1', ended_at=minutes_ago(60))
        out = bus.deliver(self.c, A, 'hi', live_only=True)
        self.assert_not_delivered(out, 'closed')
        self.assertIsNotNone(self.rows()[0]['done_at'])

    def test_owner_typing_queues_then_a_parked_session_drops_it_on_pull(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 3
        out = bus.deliver(self.c, A, 'hi', live_only=True)
        self.assertTrue(out.startswith('queued'), out)
        self.assertIsNone(self.rows()[0]['done_at'])
        self.set_session(parked_at=minutes_ago(60))
        with self.c:
            self.c.execute('UPDATE deliveries SET lease_at=NULL')
        self.pane_calls.clear()
        res = bus.pull(self.c)
        self.assertTrue(any('not delivered' in x for x in res), res)
        r = self.rows()[0]
        self.assertIsNotNone(r['done_at'])
        self.assertTrue(r['outcome'].startswith('not delivered'), r['outcome'])
        self.assertEqual(self.pane_calls, [])
        self.assertEqual(self.wake_calls, [])
        self.assertEqual(self.inbox, [])

    def test_handoff_successor_live_gets_the_text(self):
        code, msg = bus.handoff(self.c, A, 'x', 'n', new_sid=NEWSID, allow_dirty=True, spawn=False)
        self.assertEqual(code, 0, msg)
        self.set_session(NEWSID, pane='w4:p77', host='local')
        out = bus.deliver(self.c, A, 'for the old id', live_only=True)
        self.assertTrue(out.startswith('delivered'), out)
        self.assertEqual(self.pane_calls, [('w4:p77', 'for the old id')])

    def test_without_if_live_a_parked_session_is_woken(self):
        self.set_session(host='local', pane='w4:p1', parked_at=minutes_ago(60))
        bus.deliver(self.c, A, 'hi')
        self.assertEqual(len(self.wake_calls), 1)
        self.assertEqual(self.rows()[0]['live_only'], 0)


class DeliverSlackWaitTest(DeliverBase):
    REF = 'slack:C123:1700000000.000100'

    def met_wait(self, ts='1700000000.000100', woken=False, cancelled=False, kind='thread'):
        wid = self.add_wait(A, kind, 'C123:1700000000.000001', met=True, woken=woken)
        with self.c:
            self.c.execute('UPDATE waits SET met_by=?, cancelled_at=? WHERE id=?',
                           (json.dumps({'ts': ts, 'channel': 'C123', 'sender': 'U1', 'summary': 's'}),
                            bus.now() if cancelled else None, wid))
        return wid

    def test_parked_session_whose_wait_took_it_is_not_typed(self):
        self.set_session(host='local', pane='w4:p1', parked_at=minutes_ago(60))
        self.met_wait()
        out = bus.deliver(self.c, A, 'hi', ref=self.REF)
        self.assertTrue(out.startswith('not typed: wait'), out)
        self.assertEqual((self.pane_calls, self.wake_calls, self.inbox), ([], [], []))
        self.assertIsNotNone(self.rows()[0]['done_at'])
        self.assertEqual(self.c.execute('SELECT count(*) FROM waits').fetchone()[0], 1)

    def test_live_session_whose_wait_was_woken_is_not_typed(self):
        self.set_session(host='local', pane='w4:p1')
        self.met_wait(woken=True, kind='person')
        out = bus.deliver(self.c, A, 'hi', ref=self.REF)
        self.assertTrue(out.startswith('not typed: wait'), out)
        self.assertEqual((self.pane_calls, self.wake_calls, self.inbox), ([], [], []))

    def test_different_ts_still_delivers(self):
        self.set_session(host='local', pane='w4:p1')
        self.met_wait(ts='1700000000.000999', woken=True)
        out = bus.deliver(self.c, A, 'hi', ref=self.REF)
        self.assertTrue(out.startswith('delivered'), out)
        self.assertEqual(self.pane_calls, [('w4:p1', 'hi')])

    def test_cancelled_wait_does_not_count(self):
        self.set_session(host='local', pane='w4:p1')
        self.met_wait(woken=True, cancelled=True)
        out = bus.deliver(self.c, A, 'hi', ref=self.REF)
        self.assertTrue(out.startswith('delivered'), out)
        self.assertEqual(self.pane_calls, [('w4:p1', 'hi')])

    def test_ref_that_is_not_slack_is_not_affected(self):
        self.set_session(host='local', pane='w4:p1')
        self.met_wait(woken=True)
        out = bus.deliver(self.c, A, 'hi', ref='other:C123:1700000000.000100')
        self.assertTrue(out.startswith('delivered'), out)
        self.assertEqual(self.pane_calls, [('w4:p1', 'hi')])

    def mail_wait(self, message_id='m77', woken=True):
        wid = self.add_wait(A, 'thread', 'mail:1a1f09ce900b6f9c', met=True, woken=woken)
        with self.c:
            self.c.execute('UPDATE waits SET met_by=? WHERE id=?',
                           (json.dumps({'ts': '1700000000.000', 'channel': 'mail', 'message_id': message_id,
                                        'sender': 'Ana', 'summary': 's'}), wid))

    def test_mail_ref_whose_wait_took_it_is_not_typed(self):
        self.set_session(host='local', pane='w4:p1')
        self.mail_wait()
        out = bus.deliver(self.c, A, 'hi', ref='mail:m77')
        self.assertTrue(out.startswith('not typed: wait'), out)
        self.assertEqual(self.pane_calls, [])

    def test_mail_ref_of_another_message_still_delivers(self):
        self.set_session(host='local', pane='w4:p1')
        self.mail_wait(message_id='m78')
        out = bus.deliver(self.c, A, 'hi', ref='mail:m77')
        self.assertTrue(out.startswith('delivered'), out)


MODEL_LIMIT_MENU = """  Continue the task.
  You've reached your Fable limit
  You've used your included Fable usage for this week. Continuing on Fable 5.2 uses usage credits, purchased separately from your plan.
  Learn more: https://support.claude.com/en/articles/12429409-extra-usage-for-paid-claude-plans
  ❯ Switch to Opus 5.5 and continue
    Continue with Fable 5.2
  Enter to confirm · Esc to cancel
"""


MODEL_LIMIT_FABLE_CURSOR = MODEL_LIMIT_MENU.replace('  ❯ Switch to Opus 5.5 and continue\n    Continue with Fable 5.2',
                                                   '    Switch to Opus 5.5 and continue\n  ❯ Continue with Fable 5.2')




class ModelLimitMenuTest(DeliverBase):
    """«You've reached your Fable limit»: the cursor on «Switch to Opus … and continue» gets Enter (owner, 2026-11-24).
    Any other cursor row gets no key and one card."""
    PATCHED = DeliverBase.PATCHED + ('place_screen', 'polar_wake_card')

    def setUp(self):
        super().setUp()
        self.cards = []
        bus.polar_wake_card = lambda title, ctx, plain, body=None: self.cards.append((title, body)) or True

    def test_reader(self):
        self.assertTrue(bus.model_limit_menu(MODEL_LIMIT_MENU))
        self.assertFalse(bus.model_limit_menu(MODEL_LIMIT_MENU + LIMIT_MENU_GONE))  # quoted above a live box
        self.assertFalse(bus.model_limit_menu(MODEL_LIMIT_MENU.replace('Enter to confirm · Esc to cancel', '')))
        self.assertFalse(bus.model_limit_menu(LIMIT_MENU))
        self.assertFalse(bus.limit_menu(MODEL_LIMIT_MENU))
        self.assertFalse(bus.model_limit_menu(None))
        self.assertEqual(bus.model_limit_cursor(MODEL_LIMIT_MENU), 'Switch to Opus 5.5 and continue')
        self.assertEqual(bus.model_limit_cursor(MODEL_LIMIT_FABLE_CURSOR), 'Continue with Fable 5.2')
        self.assertIsNone(bus.model_limit_cursor(MODEL_LIMIT_MENU + LIMIT_MENU_GONE))

    def test_the_stop_menu_with_the_credits_row_of_an_org_is_still_closed_with_escape(self):
        org = LIMIT_MENU.replace('3. Switch to usage credits', '3. Your organization is out of usage credits · ask an admin')
        self.assertTrue(bus.limit_menu(org))
        self.set_session(host='server', tmux='task-wx')
        bus.place_screen = lambda host, place: org
        self.rc_tmux = 5
        self.assertIn('Escape closed it', bus.deliver(self.c, A, 'continue'))

    def test_a_server_delivery_presses_enter_on_switch_to_opus(self):
        self.set_session(host='server', tmux='task-wx', title='Caso Ejemplo')
        bus.place_screen = lambda host, place: MODEL_LIMIT_MENU
        self.rc_tmux = 5
        out = bus.deliver(self.c, A, 'continue')
        self.assertIn('Enter chose «Switch to Opus»', out)
        sent = [c for c in self.ssh_calls if 'send-keys' in c]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].endswith(' Enter') and 'task-wx' in sent[0], sent[0])
        self.assertEqual(self.cards, [])

    def test_the_cursor_on_continue_with_fable_gets_no_key_and_one_card(self):
        self.set_session(host='server', tmux='task-wx', title='Caso Ejemplo')
        bus.place_screen = lambda host, place: MODEL_LIMIT_FABLE_CURSOR
        self.rc_tmux = 5
        out = bus.deliver(self.c, A, 'continue')
        self.assertIn('no key; the owner has a Polar card', out)
        bus.pull(self.c)
        self.assertEqual([c for c in self.ssh_calls if 'send-keys' in c], [])
        self.assertEqual(len(self.cards), 1)
        self.assertIn('tmux attach -t task-wx', self.cards[0][1])
        self.assertTrue(bus.wake_held(self.c, A))

    def test_sweep_switches_a_session_with_no_message_waiting(self):
        bus.place_screen = lambda host, place: MODEL_LIMIT_MENU if place == 'task-wabc12345' else MODEL_LIMIT_FABLE_CURSOR
        def ssh(cmd, timeout=40):
            self.ssh_calls.append(cmd)
            return (0, 'task-wabc12345\nother\n') if 'list-sessions' in cmd else (0, '')
        bus.ssh = ssh
        self.assertEqual(bus.model_limit_sweep(self.c), ['task-wabc12345'])
        sent = [c for c in self.ssh_calls if 'send-keys' in c]
        self.assertEqual(len(sent), 1)
        self.assertIn('task-wabc12345', sent[0])
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='model-switched'").fetchone()[0], 1)


class OpenModelTest(unittest.TestCase):
    """Fable at its weekly limit: a session the harness starts gets --model opus (owner, 2026-11-24)."""

    def setUp(self):
        self.saved = dict(bus._FULL)

    def tearDown(self):
        bus._FULL.clear()
        bus._FULL.update(self.saved)

    def test_fable_full_gives_opus_and_open_keeps_fable(self):
        bus._FULL.clear(); bus._FULL['fable'] = True
        self.assertEqual(bus.flag_of('fable'), '--model opus ')
        self.assertEqual(bus.flag_of('opus'), '--model opus ')
        self.assertEqual(bus.flag_of(''), '')
        bus._FULL['fable'] = False
        self.assertEqual(bus.flag_of('fable'), '--model fable ')


class WakeRemoteLimitTest(DeliverBase):
    """wake_remote: Escape on «Stop and wait», a card (no key) on the model menu, and a clear line when the resume
    closed at once."""
    PATCHED = DeliverBase.PATCHED + ('place_screen', 'open_cloud_tab', 'mark_woken', 'wake_no_box_note', 'time',
                                     'polar_wake_card')

    def setUp(self):
        super().setUp()
        bus.wake_remote = self.saved['wake_remote']
        bus.open_cloud_tab = lambda label, name, poll=2: ('t1', 'p1')
        bus.mark_woken = lambda *a, **k: None
        bus.wake_no_box_note = lambda *a, **k: None
        self.cards = []
        bus.polar_wake_card = lambda title, ctx, plain, body=None: self.cards.append(title) or True
        clock = [1000.0]
        class T:
            @staticmethod
            def time():
                clock[0] += 1
                return clock[0]
            @staticmethod
            def sleep(s):
                clock[0] += s
        bus.time = T
        self.s = {'cwd': None, 'title': 'x'}
        self.w = [{'id': 1, 'about': 'a', 'kind': 'time', 'key': 'k'}]

    def test_stop_menu_on_wake_gets_escape_then_text_goes(self):
        screens = [LIMIT_MENU, LIMIT_MENU_GONE]
        bus.place_screen = lambda host, place: screens.pop(0) if len(screens) > 1 else screens[0]
        ok, line = bus.wake_remote(self.c, A, self.s, self.w, poll=0)
        self.assertTrue(ok, line)
        sent = [c for c in self.ssh_calls if 'send-keys' in c]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].endswith(' Escape'))

    def test_model_menu_on_wake_gets_enter_on_opus_then_text_goes(self):
        screens = [MODEL_LIMIT_MENU, LIMIT_MENU_GONE]
        bus.place_screen = lambda host, place: screens.pop(0) if len(screens) > 1 else screens[0]
        ok, line = bus.wake_remote(self.c, A, self.s, self.w, poll=0)
        self.assertTrue(ok, line)
        sent = [c for c in self.ssh_calls if 'send-keys' in c]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].endswith(' Enter'))
        self.assertEqual(self.cards, [])

    def test_model_menu_on_fable_row_on_wake_is_a_card_and_no_key(self):
        bus.place_screen = lambda host, place: MODEL_LIMIT_FABLE_CURSOR
        ok, line = bus.wake_remote(self.c, A, self.s, self.w, poll=0)
        self.assertFalse(ok)
        self.assertIn('model limit menu; the owner has a card', line)
        self.assertEqual([c for c in self.ssh_calls if 'send-keys' in c], [])
        self.assertEqual(len(self.cards), 1)
        self.assertEqual(self.tmux_calls, [])

    def test_resume_that_closed_says_so(self):
        bus.place_screen = lambda host, place: ''
        calls = []
        def ssh(cmd, timeout=40):
            calls.append(cmd)
            if 'has-session' in cmd:
                return (0, '') if len([c for c in calls if 'has-session' in c]) == 1 else (1, '')
            if cmd.startswith('ls '):
                return (2, '')
            return (0, '')
        bus.ssh = ssh
        self.rc_tmux = 5
        ok, line = bus.wake_remote(self.c, A, self.s, self.w, poll=0)
        self.assertFalse(ok)
        self.assertIn('closed at once', line)
        self.assertIn('no transcript', line)
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='resume-exited'").fetchone()[0], 1)


class GoneSessionTest(DeliverBase):
    """A parked server session with no tmux, no transcript and no process: the row closes once to the router and
    its own wait is cancelled (delivery 1341, 2026-11-24)."""

    def gone_ssh(self, out):
        bus.ssh = lambda cmd, timeout=40: self.ssh_calls.append(cmd) or ((0, out) if 'gone-check' in cmd else (0, ''))

    def setup_row(self):
        self.set_session(host='server', parked_at=minutes_ago(120))
        self.wake_res = []
        out = bus.deliver(self.c, A, 'ok')
        self.assertTrue(out.startswith('queued'), out)
        with self.c:
            self.c.execute('UPDATE deliveries SET created_at=?', (minutes_ago(45),))

    def test_gone_closes_the_row_and_cancels_its_wait(self):
        self.setup_row()
        self.gone_ssh('gone-check\n')
        bus.pull(self.c)
        r = self.rows()[0]
        self.assertIsNotNone(r['done_at'])
        self.assertIn('no session to wake', r['outcome'])
        self.assertEqual(len(self.inbox), 1)
        self.assertEqual(self.c.execute('SELECT count(*) FROM waits WHERE cancelled_at IS NULL AND woken_at IS NULL '
                                        'AND delivery_id IS NOT NULL').fetchone()[0], 0)
        self.assertEqual(self.c.execute('SELECT count(*) FROM waits WHERE cancelled_at IS NOT NULL').fetchone()[0], 1)

    def test_a_transcript_or_no_answer_keeps_the_row(self):
        self.setup_row()
        for out in ('gone-check\nfile\n', 'gone-check\nproc\n', ''):
            self.gone_ssh(out)
            bus.pull(self.c)
            self.assertIsNone(self.rows()[0]['done_at'], out)
        self.assertEqual(self.inbox, [])


class DeliveryAlarmTest(DeliverBase):
    """A message not typed after 30 min: one Polar card per session and oldest message, whatever the reason."""
    PATCHED = DeliverBase.PATCHED + ('place_screen', 'polar_wake_card')

    def setUp(self):
        super().setUp()
        self.cards = []
        bus.polar_wake_card = lambda title, ctx, plain, body=None: self.cards.append((title, ctx, body)) or True
        bus.place_screen = lambda host, place: MODEL_LIMIT_MENU

    def add(self, sid, text, mins):
        with self.c:
            self.c.execute("INSERT INTO deliveries(session_id, host, text, ref, created_at, tries, outcome, place) "
                           "VALUES(?,?,?,?,?,?,?,?)", (sid, 'server', text, text, bus.ago_s(mins * 60), 9,
                                                       'queued: box is not free', 'task-wx'))

    def test_one_card_per_session_then_quiet(self):
        self.set_session(title='Caso Ejemplo')
        self.add(A, 'ok', 45)
        self.add(A, 'otra', 40)
        self.add(B, 'nuevo', 5)  # younger than 30 min: no card
        self.assertEqual(bus.delivery_alarm(self.c), [A])
        self.assertEqual(len(self.cards), 1)
        title, ctx, body = self.cards[0]
        self.assertIn('Caso Ejemplo', title)
        self.assertIn('«ok»', body)
        self.assertIn('Switch to Opus', body)
        self.assertIn('tmux attach -t task-wx', body)
        self.assertEqual(bus.delivery_alarm(self.c), [])  # same oldest message: no second card
        self.assertEqual(len(self.cards), 1)

    def test_a_card_polar_did_not_take_is_tried_again_later(self):
        bus.polar_wake_card = lambda *a, **k: False
        self.add(A, 'ok', 45)
        self.assertEqual(bus.delivery_alarm(self.c), [A])
        self.assertEqual(bus.delivery_alarm(self.c), [])  # before CARD_RETRY_S
        with self.c:
            self.c.execute("UPDATE events SET at=? WHERE type='delivery-alarm'", (bus.ago_s(bus.CARD_RETRY_S + 5),))
        self.assertEqual(bus.delivery_alarm(self.c), [A])

    def test_pull_runs_it_only_on_the_server(self):
        self.add(A, 'ok', 45)
        bus.pull(self.c)  # role local in tests
        self.assertEqual(self.cards, [])




class DeliverTest(DeliverBase):
    def test_live_laptop_session_is_typed_into_its_pane(self):
        self.set_session(host='local', pane='w4:p1')
        out = bus.deliver(self.c, A, 'hi')
        self.assertTrue(out.startswith('delivered'), out)
        self.assertEqual(self.pane_calls, [('w4:p1', 'hi')])
        r = self.rows()[0]
        self.assertIsNotNone(r['done_at'])
        self.assertIsNone(r['lease_at'])
        self.assertEqual(r['tries'], 1)

    def test_live_server_session_is_typed_into_its_tmux(self):
        self.set_session(host='server', tmux='task-wx', pane='w9:p9')
        out = bus.deliver(self.c, A, 'hello')
        self.assertTrue(out.startswith('delivered'), out)
        self.assertEqual(self.tmux_calls, [('task-wx', 'hello')])
        self.assertEqual(self.pane_calls, [])
        self.assertIsNotNone(self.rows()[0]['done_at'])

    def test_text_in_box_is_set_aside_after_five_minutes(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 3
        bus.deliver(self.c, A, 'hi')
        self.assertEqual(self.stashes, [False])  # a fresh message waits for the owner
        with self.c:
            self.c.execute('UPDATE deliveries SET lease_at=NULL, created_at=?', (minutes_ago(6),))
        self.rc = 0
        bus.pull(self.c)
        self.assertEqual(self.stashes[-1], True)
        self.assertIsNotNone(self.rows()[0]['done_at'])

    def test_owner_typing_queues_and_keeps_order(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 3
        out1 = bus.deliver(self.c, A, 'first')
        self.assertTrue(out1.startswith('queued'), out1)
        r1 = self.rows()[0]
        self.assertIsNone(r1['done_at'])
        self.assertIsNone(r1['lease_at'])
        out2 = bus.deliver(self.c, A, 'second')
        self.assertTrue(out2.startswith(f"queued {r1['id'] + 1}: after {r1['id']} "), out2)
        self.assertEqual(self.pane_calls, [('w4:p1', 'first'), ('w4:p1', 'first')])  # the second never typed 'second'
        self.rc = 0
        self.pane_calls.clear()
        res = bus.pull(self.c)
        self.assertEqual(len(res), 2, res)
        self.assertEqual(self.pane_calls, [('w4:p1', 'first'), ('w4:p1', 'second')])
        self.assertTrue(all(r['done_at'] for r in self.rows()))

    def test_left_in_input_box_is_done(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 4
        out = bus.deliver(self.c, A, 'x')
        self.assertIn('left in the input box', out)
        self.assertIsNotNone(self.rows()[0]['done_at'])

    def test_no_input_box_young_row_and_alive_place_stays_queued(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 5
        out = bus.deliver(self.c, A, 'x')
        self.assertTrue(out.startswith('queued'), out)
        self.assertIsNone(self.rows()[0]['done_at'])
        self.assertEqual(self.inbox, [])

    def test_no_input_box_and_place_gone_goes_to_router(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 5
        self.panes = ['w4:p2']
        out = bus.deliver(self.c, A, 'the text')
        self.assertIn('no live input box', out)
        self.assertEqual(len(self.inbox), 1)
        self.assertIn('agent-bus deliver', self.inbox[0])
        self.assertIn('the text', self.inbox[0])
        self.assertIsNotNone(self.rows()[0]['done_at'])

    def test_no_input_box_alive_but_old_row_tells_the_router_and_stays_queued(self):
        # Until 2026-11-23 the row was closed here, and the router had to send the text again. It did not: six
        # messages were lost for 3 hours. Now the row stays queued (StartQuestionDeliverTest has the whole cycle).
        self.set_session(host='local', pane='w4:p1')
        self.rc = 5
        out = bus.deliver(self.c, A, 'old text')
        self.assertTrue(out.startswith('queued'), out)
        with self.c:
            self.c.execute('UPDATE deliveries SET created_at=?', (minutes_ago(31),))
        res = bus.pull(self.c)
        self.assertEqual(len(res), 1)
        self.assertEqual(len(self.inbox), 1)
        self.assertIn('old text', self.inbox[0])
        self.assertIsNone(self.rows()[0]['done_at'])

    def test_no_input_box_server_session_checks_tmux_over_ssh(self):
        # the tmux is gone and the session is not marked ended: it is ended now, then resumed (see the dead-tmux tests)
        self.set_session(host='server', tmux='task-wx')
        self.rc_tmux = 5
        self.tmux_alive = 'N'
        out = bus.deliver(self.c, A, 'x')
        self.assertTrue(any('has-session' in s for s in self.ssh_calls))
        self.assertTrue(out.startswith('met'), out)

    def dead_tmux(self, **cols):
        self.set_session(host='server', tmux='task-wx', **cols)
        self.rc_tmux = 5
        self.tmux_alive = 'N'

    def ended_at(self):
        return self.c.execute('SELECT ended_at FROM sessions WHERE session_id=?', (A,)).fetchone()[0]

    def test_dead_tmux_of_a_live_server_session_marks_it_ended_and_resumes_it(self):
        self.dead_tmux()
        out = bus.deliver(self.c, A, 'the text')
        self.assertTrue(out.startswith('met'), out)
        self.assertIsNotNone(self.ended_at())
        self.assertEqual(len(self.tmux_calls), 1)  # one type try; the second pass takes the closed-session path
        self.assertEqual(self.wake_calls, [(1, A)])
        waits = self.c.execute('SELECT * FROM waits WHERE session_id=?', (A,)).fetchall()
        self.assertEqual(len(waits), 1)
        self.assertIsNotNone(waits[0]['met_at'])
        self.assertEqual(waits[0]['delivery_id'], self.rows()[0]['id'])
        self.assertEqual(self.inbox, [])

    def test_dead_tmux_with_live_only_is_not_delivered_and_not_resumed(self):
        self.dead_tmux()
        out = bus.deliver(self.c, A, 'x', live_only=True)
        self.assertTrue(out.startswith('not delivered:'), out)
        self.assertIn('closed', out)
        self.assertIsNotNone(self.ended_at())
        self.assertEqual(self.wake_calls, [])
        self.assertEqual(self.c.execute('SELECT count(*) FROM waits').fetchone()[0], 0)

    def test_dead_place_that_is_only_a_hint_goes_to_the_router(self):
        self.set_session(host='server', tmux='')
        self.rc_tmux = 5
        self.tmux_alive = 'N'
        out = bus.deliver(self.c, A, 'x', place='task-hint')
        self.assertIn('no live input box', out)
        self.assertEqual(len(self.inbox), 1)
        self.assertIsNone(self.ended_at())
        self.assertEqual(self.wake_calls, [])

    def test_laptop_session_with_a_dead_pane_goes_to_the_router(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 5
        self.panes = ['w4:p2']
        out = bus.deliver(self.c, A, 'x')
        self.assertIn('no live input box', out)
        self.assertEqual(len(self.inbox), 1)
        self.assertIsNone(self.ended_at())
        self.assertEqual(self.wake_calls, [])

    def test_dead_tmux_when_the_write_fails_goes_to_the_router_without_a_second_try(self):
        self.dead_tmux()
        bus.write_retry = lambda c, fn, tries=4: False
        out = bus.deliver(self.c, A, 'x')
        self.assertIn('no live input box', out)
        self.assertEqual(len(self.tmux_calls), 1)  # no recursion
        self.assertEqual(len(self.inbox), 1)
        self.assertIsNone(self.ended_at())
        self.assertEqual(self.wake_calls, [])

    def test_server_session_with_a_live_tmux_and_a_young_row_stays_queued(self):
        self.set_session(host='server', tmux='task-wx')
        self.rc_tmux = 5
        self.tmux_alive = 'Y'
        out = bus.deliver(self.c, A, 'x')
        self.assertTrue(out.startswith('queued'), out)
        self.assertIn('no free input box', out)
        self.assertIsNone(self.ended_at())
        self.assertEqual(self.inbox, [])
        self.assertEqual(self.wake_calls, [])

    def test_same_ref_is_a_duplicate(self):
        self.set_session(host='local', pane='w4:p1')
        o1 = bus.deliver(self.c, A, 'hi', ref='r1')
        o2 = bus.deliver(self.c, A, 'hi', ref='r1')
        self.assertTrue(o1.startswith('delivered'), o1)
        did = self.rows()[0]['id']
        self.assertTrue(o2.startswith(f'duplicate of {did}'), o2)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.pane_calls), 1)

    def test_parked_session_meets_the_given_wait_and_wakes(self):
        self.set_session(parked_at=minutes_ago(5), host='local', pane='w4:p1')
        wid = self.add_wait(A, 'polar', 'a1b2c3d4e5f6')
        out = bus.deliver(self.c, A, 'the answer', wait='polar:a1b2c3d4e5f6')
        self.assertTrue(out.startswith('met polar:a1b2c3d4e5f6 of'), out)
        self.assertIn('ok: woke', out)
        w = self.c.execute('SELECT * FROM waits WHERE id=?', (wid,)).fetchone()
        self.assertIsNotNone(w['met_at'])
        self.assertIn('the answer', w['met_by'])
        self.assertEqual(self.wake_calls, [(1, A)])
        self.assertEqual(self.pane_calls, [])
        self.assertIsNotNone(self.rows()[0]['done_at'])

    def test_session_parked_just_now_waits(self):
        self.set_session(parked_at=bus.now())
        self.add_wait(A, 'polar', 'a1b2c3d4e5f6')
        out = bus.deliver(self.c, A, 'x', wait='polar:a1b2c3d4e5f6')
        self.assertTrue(out.startswith('queued'), out)
        self.assertIn('parked just now', out)
        self.assertIsNone(self.rows()[0]['done_at'])
        self.assertEqual(self.c.execute('SELECT count(*) FROM waits WHERE met_at IS NOT NULL').fetchone()[0], 0)
        self.assertEqual(self.wake_calls, [])

    def test_ended_session_without_wait_is_parked_and_resumed(self):
        self.set_session(ended_at=minutes_ago(10), host='local')
        out = bus.deliver(self.c, A, 'resume me')
        did = self.rows()[0]['id']
        self.assertTrue(out.startswith(f'met task:deliver-{did} of'), out)
        s = self.c.execute('SELECT parked_at FROM sessions WHERE session_id=?', (A,)).fetchone()
        self.assertTrue(s['parked_at'])
        w = self.c.execute("SELECT * FROM waits WHERE session_id=?", (A,)).fetchone()
        self.assertEqual((w['kind'], w['key']), ('task', f'deliver-{did}'))
        self.assertIsNotNone(w['met_at'])
        self.assertEqual(self.wake_calls, [(1, A)])

    def test_handoff_successor_gets_the_message(self):
        code, msg = bus.handoff(self.c, A, 'x', 'n', new_sid=NEWSID, allow_dirty=True, spawn=False)
        self.assertEqual(code, 0, msg)
        self.set_session(NEWSID, pane='w4:p77', host='local')
        out = bus.deliver(self.c, A, 'for the old id')
        self.assertTrue(out.startswith('delivered'), out)
        self.assertEqual(self.pane_calls, [('w4:p77', 'for the old id')])

    def test_unknown_session_goes_to_router(self):
        out = bus.deliver(self.c, 'f' * 8 + '-0000-0000-0000-' + '0' * 12, 'lost')
        self.assertIn('unknown session', out)
        self.assertEqual(len(self.inbox), 1)
        self.assertIn('lost', self.inbox[0])
        self.assertIsNotNone(self.rows()[0]['done_at'])

    def test_session_without_pane_goes_to_router(self):
        self.set_session(host='local', pane=None)
        out = bus.deliver(self.c, A, 'x')
        self.assertIn('no pane', out)
        self.assertEqual(len(self.inbox), 1)

    def test_empty_session_is_a_router_note_without_row(self):
        out = bus.deliver(self.c, '', 'note for the router')
        self.assertEqual(self.inbox, ['note for the router'])
        self.assertIn('router inbox', out)
        self.assertEqual(self.rows(), [])

    def test_bad_wait_queues_nothing(self):
        with self.assertRaises(ValueError):
            bus.deliver(self.c, A, 'x', wait='bad')
        self.assertEqual(self.rows(), [])

    def test_fresh_lease_is_not_tried(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 3
        bus.deliver(self.c, A, 'x')
        self.rc = 0
        self.pane_calls.clear()
        with self.c:
            self.c.execute('UPDATE deliveries SET lease_at=?', (bus.now(),))
        self.assertEqual(bus.pull(self.c), [])
        d = dict(self.rows()[0])
        done, line = bus.delivery_try(self.c, d)
        self.assertFalse(done)
        self.assertIn('another try runs now', line)
        self.assertEqual(self.pane_calls, [])
        self.assertIsNone(self.rows()[0]['done_at'])

    def test_old_row_expires_with_router_note(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 3
        bus.deliver(self.c, A, 'stale text')
        with self.c:
            self.c.execute('UPDATE deliveries SET created_at=?', (minutes_ago(4 * 24 * 60),))
        self.pane_calls.clear()
        res = bus.pull(self.c)
        self.assertEqual(len(res), 1)
        self.assertIn('expired', res[0])
        r = self.rows()[0]
        self.assertEqual((r['outcome'], bool(r['done_at'])), ('expired', True))
        self.assertEqual(len(self.inbox), 1)
        self.assertIn('stale text', self.inbox[0])
        self.assertEqual(self.pane_calls, [])


class DeliverServerRoleTest(DeliverBase):
    def test_laptop_session_waits_for_the_laptop(self):
        self.set_session(host='local', pane='w4:p1')
        os.environ['AGENT_BUS_ROLE'] = 'server'
        out = bus.deliver(self.c, A, 'x')
        self.assertTrue(out.startswith('queued'), out)
        self.assertIn('the laptop types it', out)
        self.assertEqual(self.pane_calls, [])
        r = self.rows()[0]
        self.assertEqual((r['host'], r['done_at']), ('local', None))
        self.assertEqual(bus.pull(self.c), [])  # server pull does not try a laptop row
        self.assertEqual(self.pane_calls, [])
        os.environ['AGENT_BUS_ROLE'] = 'local'  # the laptop comes back
        res = bus.pull(self.c)
        self.assertEqual(len(res), 1)
        self.assertEqual(self.pane_calls, [('w4:p1', 'x')])
        self.assertIsNotNone(self.rows()[0]['done_at'])

    def test_parked_laptop_session_is_left_to_the_laptop(self):
        self.set_session(host='local', parked_at=minutes_ago(5))
        self.add_wait(A, 'polar', 'a1b2c3d4e5f6')
        os.environ['AGENT_BUS_ROLE'] = 'server'
        out = bus.deliver(self.c, A, 'x', wait='polar:a1b2c3d4e5f6')
        self.assertTrue(out.startswith('queued'), out)
        self.assertIn('the laptop resumes it', out)
        self.assertEqual(self.wake_calls, [])
        w = self.c.execute('SELECT met_at FROM waits').fetchone()
        self.assertIsNotNone(w['met_at'])
        self.assertIsNone(self.rows()[0]['done_at'])
        os.environ['AGENT_BUS_ROLE'] = 'local'  # the laptop pulls: it wakes the session, the met wait is not added twice
        res = bus.pull(self.c)
        self.assertEqual(len(res), 1, res)
        self.assertEqual(self.wake_calls, [(1, A)])
        self.assertEqual(self.c.execute('SELECT count(*) FROM waits').fetchone()[0], 1)
        self.assertIsNotNone(self.rows()[0]['done_at'])

    def test_router_note_waits_for_the_laptop_router(self):
        os.environ['AGENT_BUS_ROLE'] = 'server'
        out = bus.deliver(self.c, '', 'x')
        self.assertIn('queued for local', out)
        r = self.rows()[0]
        self.assertEqual((r['session_id'], r['host']), ('', 'local'))
        self.assertEqual(self.inbox, [])
        self.assertEqual(bus.pull(self.c), [])
        os.environ['AGENT_BUS_ROLE'] = 'local'
        res = bus.pull(self.c)
        self.assertEqual(len(res), 1)
        self.assertEqual(self.inbox, ['x'])
        r = self.rows()[0]
        self.assertEqual((bool(r['done_at']), r['outcome']), (True, 'router inbox'))

    def test_server_session_is_typed_by_the_server(self):
        self.set_session(host='server', tmux='task-wx')
        os.environ['AGENT_BUS_ROLE'] = 'server'
        out = bus.deliver(self.c, A, 'x')
        self.assertTrue(out.startswith('delivered'), out)
        self.assertEqual(self.tmux_calls, [('task-wx', 'x')])


class WakeLeaseTest(DeliverBase):
    def parked(self, sid, host='local', met=True):
        self.set_session(sid, parked_at=minutes_ago(5), host=host)
        return self.add_wait(sid, 'task', 't-' + sid[:1], met=met)

    def test_take_and_release(self):
        self.parked(A)
        self.assertTrue(bus.take_wake_lease(self.c, A))
        self.assertFalse(bus.take_wake_lease(self.c, A))
        bus.release_wake_lease(self.c, A)
        self.assertTrue(bus.take_wake_lease(self.c, A))

    def test_no_lease_without_a_met_wait(self):
        self.parked(A, met=False)
        self.assertFalse(bus.take_wake_lease(self.c, A))

    def test_wake_skips_a_session_with_a_fresh_lease(self):
        calls = []
        bus._wake_one = lambda *a, **k: calls.append(a) or (True, 'x')
        wid = self.parked(A)
        with self.c:
            self.c.execute('UPDATE waits SET waking_at=? WHERE id=?', (bus.now(), wid))
        self.saved_wake = None
        bus.wake = self.saved['wake']
        self.assertEqual(bus.wake(self.c), [])
        self.assertEqual(calls, [])

    def test_server_wake_skips_laptop_session_without_counting_it(self):
        woken = []
        bus.wake = self.saved['wake']
        bus._wake_one = lambda c, sid, s, ws, *a, **k: woken.append(sid) or (True, 'x')
        self.parked(A, host='local')
        self.parked(B, host='server')
        with self.c:  # the laptop session waited first
            self.c.execute('UPDATE waits SET met_at=? WHERE session_id=?', (minutes_ago(9), A))
        os.environ['AGENT_BUS_ROLE'] = 'server'
        res = bus.wake(self.c, max_n=1)
        self.assertEqual(woken, [B])
        self.assertEqual(res, ['ok: x'])
        self.assertIsNone(self.c.execute('SELECT waking_at FROM waits WHERE session_id=?', (A,)).fetchone()[0])

    def test_server_wake_with_only_may_wake_a_laptop_session_check(self):
        # `only` skips the laptop filter in wake(); wake_one still refuses a laptop session in server role.
        bus.wake = self.saved['wake']
        self.parked(A, host='local')
        os.environ['AGENT_BUS_ROLE'] = 'server'
        res = bus.wake(self.c, 1, A)
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0].startswith('failed:'), res)
        self.assertIn('laptop', res[0])

    def test_failed_wake_releases_the_lease(self):
        bus._wake_one = lambda *a, **k: (False, 'boom')
        wid = self.parked(A)
        ws = self.c.execute('SELECT * FROM waits WHERE id=?', (wid,)).fetchall()
        ok, msg = bus.wake_one(self.c, A, ws)
        self.assertEqual((ok, msg), (False, 'boom'))
        self.assertIsNone(self.c.execute('SELECT waking_at FROM waits WHERE id=?', (wid,)).fetchone()[0])

    def test_ok_wake_keeps_the_lease_until_woken(self):
        bus._wake_one = lambda *a, **k: (True, 'fine')
        wid = self.parked(A)
        ws = self.c.execute('SELECT * FROM waits WHERE id=?', (wid,)).fetchall()
        self.assertEqual(bus.wake_one(self.c, A, ws), (True, 'fine'))
        self.assertIsNotNone(self.c.execute('SELECT waking_at FROM waits WHERE id=?', (wid,)).fetchone()[0])
        ok, msg = bus.wake_one(self.c, A, ws)
        self.assertFalse(ok)
        self.assertIn('another wake runs', msg)


class FakeDeliverMixin:
    """A fake herdr-deliver: appends its argv and stdin to files; exits with FAKE_RC (default 0)."""

    def make_fake(self, d):
        self.fake_out = os.path.join(d, 'fake.out')
        self.fake_in = os.path.join(d, 'fake.in')
        script = os.path.join(d, 'fake-deliver')
        with open(script, 'w') as fh:
            fh.write(f'#!/bin/sh\necho "$@" >> {shlex.quote(self.fake_out)}\ncat >> {shlex.quote(self.fake_in)}\n'
                     'exit "${FAKE_RC:-0}"\n')
        os.chmod(script, 0o755)
        return script

    def fake_calls(self):
        try:
            with open(self.fake_out) as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def fake_stdin(self):
        try:
            with open(self.fake_in) as fh:
                return fh.read()
        except OSError:
            return ''


class DeliverCliTest(BusBase, FakeDeliverMixin):
    def setUp(self):
        super().setUp()
        self.script = self.make_fake(self.tmp.name)
        self.env = dict(os.environ, AGENT_BUS_ROLE='local', HERDR_BIN='/usr/bin/false', AGENT_BUS_NO_MOVE='1',
                        AGENT_BUS_CLIENT_DIR=os.path.join(self.tmp.name, 'client'),
                        AGENT_BUS_SERVER_HOST='nohost.invalid', HERDR_DELIVER_BIN=self.script,
                        AGENT_BUS_INBOX=os.path.join(self.tmp.name, 'inbox.md'))

    def run_cli(self, *a, stdin=None):
        return subprocess.run([sys.executable, str(PATH), *a], input=stdin, capture_output=True, text=True,
                              env=self.env, timeout=30)

    def test_register_deliver_duplicate_and_errors(self):
        S = 'e' * 8 + '-0000-0000-0000-' + '0' * 12
        self.assertEqual(self.run_cli('register', '--session', S, '--pane', 'w4:p1').returncode, 0)
        r = self.run_cli('deliver', S, '--text-file', '-', '--ref', 'r1', stdin='hi')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith('delivered'), r.stdout)
        self.assertEqual(self.fake_calls(), ['w4:p1 -'])
        self.assertEqual(self.fake_stdin(), 'hi')
        r = self.run_cli('deliver', S, '--text-file', '-', '--ref', 'r1', stdin='hi')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith('duplicate of'), r.stdout)
        self.assertEqual(self.fake_calls(), ['w4:p1 -'])
        self.assertNotEqual(self.run_cli('deliver', S).returncode, 0)
        n = self.c.execute('SELECT count(*) FROM deliveries').fetchone()[0]
        r = self.run_cli('deliver', S, '--text', 'x', '--wait', 'bad')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.c.execute('SELECT count(*) FROM deliveries').fetchone()[0], n)
        r = self.run_cli('deliveries', '--open')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith('id'), r.stdout)
        r = self.run_cli('pull')
        self.assertEqual(r.stdout.strip(), 'nothing to deliver')

    def test_if_live_closed_session_is_not_delivered(self):
        S = 'e' * 8 + '-0000-0000-0000-' + '0' * 12
        self.run_cli('register', '--session', S, '--pane', 'w4:p1')
        with self.c:
            self.c.execute('UPDATE sessions SET ended_at=? WHERE session_id=?', (minutes_ago(60), S))
        r = self.run_cli('deliver', S, '--text', 'x', '--if-live')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith('not delivered:'), r.stdout)
        self.assertEqual(self.fake_calls(), [])
        self.assertFalse(os.path.exists(self.env['AGENT_BUS_INBOX']))

    def test_deliver_to_router_writes_the_inbox(self):
        r = self.run_cli('deliver', 'router', '--text-file', '-', stdin='for the router\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.env['AGENT_BUS_INBOX']) as fh:
            self.assertEqual(fh.read(), 'for the router\n')

    def test_owner_typing_exit_code_queues(self):
        S = 'e' * 8 + '-0000-0000-0000-' + '0' * 12
        self.run_cli('register', '--session', S, '--pane', 'w4:p1')
        self.env['FAKE_RC'] = '3'
        r = self.run_cli('deliver', S, '--text', 'x')
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith('queued'), r.stdout)
        self.env['FAKE_RC'] = '0'
        r = self.run_cli('pull')
        self.assertIn('delivered', r.stdout)


class DeliverClientRoleTest(FakeDeliverMixin, unittest.TestCase):
    # borrow the fixture of RemoteBusTest without inheriting (and re-running) its tests
    setUp, tearDown, run_cli, server_rows = (RemoteBusTest.setUp, RemoteBusTest.tearDown, RemoteBusTest.run_cli,
                                             RemoteBusTest.server_rows)

    def test_client_delivers_and_the_server_db_has_the_row(self):
        script = self.make_fake(self.tmp.name)
        self.env.update(AGENT_BUS_CLIENT_DIR=os.path.join(self.tmp.name, 'client'), HERDR_DELIVER_BIN=script, HERDR_BIN='/usr/bin/false', AGENT_BUS_NO_MOVE='1',
                        AGENT_BUS_SERVER_HOST='nohost.invalid', AGENT_BUS_INBOX=os.path.join(self.tmp.name, 'inbox.md'))
        r = self.run_cli('register', '--session', SESS, '--pane', 'w4:p1')
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_cli('deliver', SESS, '--text-file', '-', '--ref', 'c1', stdin='from client')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith('delivered'), r.stdout)
        self.assertEqual(self.fake_calls(), ['w4:p1 -'])
        self.assertEqual(self.fake_stdin(), 'from client')
        rows = self.server_rows('SELECT session_id, host, done_at IS NOT NULL, tries FROM deliveries')
        self.assertEqual(rows, [(SESS, 'local', 1, 1)])


class DeliverReviewTest(DeliverBase):
    """Changes after the review: wake result, claimed waits, rc 6/7, done journal, --place, wake text, park, meet."""

    def live_with_wait(self, key='a1b2c3d4e5f6', **cols):
        self.set_session(host='local', pane='w4:p1', **cols)
        return self.add_wait(A, 'polar', key)

    # 1. the row is done only when the wake says "ok: "
    def test_failed_wake_keeps_the_row_queued(self):
        self.set_session(parked_at=minutes_ago(5), host='local')
        self.add_wait(A, 'polar', 'a1b2c3d4e5f6')
        self.wake_res = ['failed: herdr tab create failed']
        out = bus.deliver(self.c, A, 'x', wait='polar:a1b2c3d4e5f6')
        self.assertTrue(out.startswith('queued'), out)
        self.assertIn('met polar:a1b2c3d4e5f6', out)
        self.assertIn('failed: herdr tab create failed', out)
        self.assertIsNone(self.rows()[0]['done_at'])
        self.wake_res = []
        res = bus.pull(self.c)  # nothing woke: the same row, no second wait
        self.assertIn('nothing to wake', res[0])
        self.assertEqual(self.c.execute('SELECT count(*) FROM waits').fetchone()[0], 1)
        self.wake_res = ['ok: woke']
        self.pane_calls.clear()
        res = bus.pull(self.c)
        self.assertTrue(res and 'ok: woke' in res[0], res)
        self.assertEqual(self.c.execute('SELECT count(*) FROM waits').fetchone()[0], 1)  # no second wait
        self.assertIsNotNone(self.rows()[0]['done_at'])

    def test_a_woken_own_wait_closes_the_row_without_typing(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 3
        bus.deliver(self.c, A, 'x', wait='polar:a1b2c3d4e5f6')
        did = self.rows()[0]['id']
        self.add_wait(A, 'polar', 'a1b2c3d4e5f6', met=True, delivery=did, woken=True)  # a resume carried its text
        self.rc = 0
        self.pane_calls.clear()
        res = bus.pull(self.c)
        self.assertIn('with its resume text', res[0])
        self.assertEqual(self.pane_calls, [])
        self.assertIsNotNone(self.rows()[0]['done_at'])

    def test_own_wait_is_found_by_delivery_id(self):
        a = self.add_wait(A, 'task', 'k', met=True, delivery=7)
        b = self.add_wait(A, 'task', 'k', met=True, delivery=7)
        self.add_wait(A, 'task', 'k', met=True, delivery=8)
        self.assertEqual(bus.own_wait(self.c, 7)['id'], b)
        self.assertIsNone(bus.own_wait(self.c, 9))

    def test_meet_with_delivery_sets_delivery_id_on_marked_and_inserted_waits(self):
        self.set_session(parked_at=minutes_ago(5))
        w = self.add_wait(A, 'task', 'k')
        bus.meet(self.c, 'task', 'k', 's', A, add=True, delivery=5)
        self.assertEqual(self.c.execute('SELECT delivery_id FROM waits WHERE id=?', (w,)).fetchone()[0], 5)
        bus.meet(self.c, 'task', 'new', 's', A, add=True, delivery=6)
        self.assertEqual(bus.own_wait(self.c, 6)['key'], 'new')

    def test_done_row_wakes_its_own_waits_in_the_same_write(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 3
        bus.deliver(self.c, A, 'x')
        did = self.rows()[0]['id']
        wid = self.add_wait(A, 'task', 'k', met=True, delivery=did)
        with self.c:
            self.c.execute('UPDATE waits SET waking_at=? WHERE id=?', (bus.now(), wid))
        self.rc = 0
        bus.pull(self.c)
        w = self.c.execute('SELECT * FROM waits WHERE id=?', (wid,)).fetchone()
        self.assertIsNotNone(w['woken_at'])
        self.assertIsNone(w['waking_at'])

    # 2. live session: type first, then cancel the open waits of the same kind:key
    def test_live_delivery_cancels_the_open_wait_of_the_same_key(self):
        wid = self.live_with_wait()
        other = self.add_wait(A, 'polar', 'ffffffffffff')
        met = self.add_wait(A, 'polar', 'a1b2c3d4e5f6', met=True)
        for rc in (0, 4, 7):
            self.c.execute('UPDATE waits SET cancelled_at=NULL')
            self.c.execute('DELETE FROM deliveries')
            self.c.commit()
            self.rc = rc
            bus.deliver(self.c, A, 'the answer', wait='polar:a1b2c3d4e5f6', ref=f'r{rc}')
            q = lambda i: self.c.execute('SELECT cancelled_at FROM waits WHERE id=?', (i,)).fetchone()[0]
            self.assertIsNotNone(q(wid), rc)
            self.assertIsNone(q(other), rc)  # another key stays open
            self.assertIsNone(q(met), rc)    # a met wait is not touched

    def test_failed_type_leaves_the_open_wait(self):
        for rc in (3, 5, 6):
            self.c.execute('DELETE FROM deliveries')
            self.c.execute('DELETE FROM waits')
            self.c.commit()
            wid = self.live_with_wait()
            self.rc = rc
            out = bus.deliver(self.c, A, 'x', wait='polar:a1b2c3d4e5f6', ref=f'r{rc}')
            self.assertTrue(out.startswith('queued'), (rc, out))
            w = self.c.execute('SELECT * FROM waits WHERE id=?', (wid,)).fetchone()
            self.assertEqual((w['cancelled_at'], w['woken_at']), (None, None), rc)

    def test_rc6_queues_and_rc7_goes_to_router(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 6
        out = bus.deliver(self.c, A, 'x')
        self.assertTrue(out.startswith('queued') and 'herdr-deliver did not run' in out, out)
        self.assertIsNone(self.rows()[0]['done_at'])
        self.rc = 7
        bus.pull(self.c)
        r = self.rows()[0]
        self.assertIsNotNone(r['done_at'])
        self.assertIn('timed out', r['outcome'])
        self.assertEqual(len(self.inbox), 1)
        self.assertIn('the text may be there', self.inbox[0])

    def test_run_deliver_maps_oserror_and_timeout(self):
        from unittest import mock
        with mock.patch.object(bus.subprocess, 'run', side_effect=OSError('no such file')):
            self.assertEqual(self.saved['deliver_pane']('w4:p1', 'x'), 6)
            self.assertEqual(self.saved['deliver_tmux']('task-x', 'x'), 6)
        with mock.patch.object(bus.subprocess, 'run', side_effect=subprocess.TimeoutExpired('x', 1)):
            self.assertEqual(self.saved['deliver_pane']('w4:p1', 'x'), 7)
            self.assertEqual(self.saved['deliver_tmux']('task-x', 'x'), 7)
        with mock.patch.object(bus.subprocess, 'run', return_value=subprocess.CompletedProcess([], 5)):
            self.assertEqual(self.saved['deliver_pane']('w4:p1', 'x'), 5)

    # 4. done journal
    def test_failed_final_write_goes_to_the_journal_and_pull_applies_it(self):
        self.set_session(host='local', pane='w4:p1')
        bus.write_retry = lambda c, fn, tries=4: False
        out = bus.deliver(self.c, A, 'once')
        self.assertTrue(out.startswith('delivered'), out)
        self.assertIsNone(self.rows()[0]['done_at'])
        path = os.path.join(os.environ['AGENT_BUS_CLIENT_DIR'], 'deliveries-done.jsonl')
        with open(path) as fh:
            recs = [json.loads(l) for l in fh]
        self.assertEqual([(r['id'], r['outcome'].startswith('delivered')) for r in recs], [(1, True)])
        self.assertIn('at', recs[0])
        bus.write_retry = self.saved['write_retry']
        with self.c:
            self.c.execute('UPDATE deliveries SET lease_at=NULL')  # even with the lease gone it is not typed again
        self.assertEqual(bus.pull(self.c), [])
        self.assertEqual(len(self.pane_calls), 1)
        self.assertIsNotNone(self.rows()[0]['done_at'])
        self.assertFalse(os.path.exists(path))

    def test_failed_write_of_a_queued_row_writes_no_journal(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 3
        bus.write_retry = lambda c, fn, tries=4: False
        bus.deliver(self.c, A, 'x')
        self.assertFalse(os.path.exists(os.path.join(os.environ['AGENT_BUS_CLIENT_DIR'], 'deliveries-done.jsonl')))

    # 5. --place
    def test_place_is_used_only_when_the_bus_has_none(self):
        self.set_session(host='local', pane=None)
        out = bus.deliver(self.c, A, 'x', place='w4:p9')
        self.assertTrue(out.startswith('delivered'), out)
        self.assertEqual(self.pane_calls, [('w4:p9', 'x')])
        self.set_session(pane='w4:p1')
        bus.deliver(self.c, A, 'y', place='w4:p9')
        self.assertEqual(self.pane_calls[-1], ('w4:p1', 'y'))

    def test_place_must_fit_the_machine(self):
        self.set_session(host='local', pane=None)
        out = bus.deliver(self.c, A, 'x', place='task-x')
        self.assertIn('no pane', out)
        self.assertEqual(self.pane_calls, [])
        self.set_session(host='server', tmux=None)
        out = bus.deliver(self.c, A, 'y', place='task-wabc')
        self.assertTrue(out.startswith('delivered'), out)
        self.assertEqual(self.tmux_calls, [('task-wabc', 'y')])
        self.assertIn('no tmux', bus.deliver(self.c, A, 'z', place='bad name;rm'))
        self.assertEqual(bus.place_hint('server', 'w4:p1'), '')
        self.assertEqual(bus.place_hint('local', 'w4:p1'), 'w4:p1')
        self.assertEqual(bus.place_hint('local', 'task-x'), '')

    def test_cli_place_flag(self):
        env = dict(os.environ, AGENT_BUS_ROLE='local', HERDR_BIN='/usr/bin/false', AGENT_BUS_NO_MOVE='1',
                   AGENT_BUS_INBOX=os.path.join(self.tmp.name, 'inbox.md'), HERDR_DELIVER_BIN='/usr/bin/false')
        subprocess.run([sys.executable, str(PATH), 'register', '--session', A, '--host', 'server'], env=env, timeout=30)
        p = subprocess.run([sys.executable, str(PATH), 'deliver', A, '--text', 'x', '--place', 'task-wabc'],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.c.execute('SELECT place FROM deliveries').fetchone()[0], 'task-wabc')

    # 6. wake text, mark_woken, lease
    def test_wake_text_one_wait_and_many(self):
        a = self.add_wait(A, 'polar', 'aaaaaaaaaaaa', met=True)
        b = self.add_wait(A, 'task', 'two', met=True)
        with self.c:
            self.c.execute("UPDATE waits SET met_by=? WHERE id=?", (json.dumps({'summary': 'one'}), a))
            self.c.execute("UPDATE waits SET met_by=? WHERE id=?", (json.dumps({'summary': 'two'}), b))
        rows = self.c.execute('SELECT * FROM waits ORDER BY id').fetchall()
        one, summ = bus.wake_text(self.c, rows[:1])
        self.assertTrue(one.startswith('Resumed by agent-bus: your wait «polar aaaaaaaaaaa'.replace('aaaaaaaaaaa', 'aaaaaaaaaaaa')), one)
        self.assertEqual(summ, 'one')
        many, _ = bus.wake_text(self.c, rows)
        self.assertTrue(many.startswith('Resumed by agent-bus: 2 of your waits were met — «polar aaaaaaaaaaaa» polar aaaaaaaaaaaa: one | '), many)
        self.assertIn('task two: two', many)

    def test_met_waits_and_mark_woken_only_mark_the_given_ids(self):
        self.set_session(parked_at=minutes_ago(5))
        a = self.add_wait(A, 'task', 'one', met=True)
        b = self.add_wait(A, 'task', 'two', met=True)
        rows = self.c.execute('SELECT * FROM waits WHERE id=?', (a,)).fetchall()
        self.assertEqual([w['id'] for w in bus.met_waits(self.c, A)], [a, b])
        bus.mark_woken(self.c, A, rows, {'pane': 'w4:p2'}, {})
        self.assertEqual([w['id'] for w in bus.met_waits(self.c, A)], [b])  # b stays met
        s = self.c.execute('SELECT parked_at, pane FROM sessions WHERE session_id=?', (A,)).fetchone()
        self.assertEqual((s['parked_at'], s['pane']), (None, 'w4:p2'))

    def test_mark_woken_cancels_the_open_waits_of_the_old_park(self):
        self.set_session(parked_at=minutes_ago(5))
        a = self.add_wait(A, 'task', 'one', met=True)
        o = self.add_wait(A, 'person', 'ana')  # open: ends with the resume
        rows = self.c.execute('SELECT * FROM waits WHERE id=?', (a,)).fetchall()
        bus.mark_woken(self.c, A, rows, {'pane': 'w4:p2'}, {})
        self.assertIsNotNone(self.c.execute('SELECT cancelled_at FROM waits WHERE id=?', (o,)).fetchone()[0])
        self.assertIsNone(self.c.execute('SELECT cancelled_at FROM waits WHERE id=?', (a,)).fetchone()[0])

    def test_mark_woken_raises_when_the_write_fails(self):
        bus.write_retry = lambda c, fn, tries=4: False
        with self.assertRaises(bus.BusOffline) as cm:
            bus.mark_woken(self.c, A, [], {'pane': 'x'}, {})
        self.assertTrue(cm.exception.after_start)

    def test_lease_is_kept_only_when_the_session_started(self):
        self.assertEqual(bus.WAKE_LEASE, 900)
        self.set_session(parked_at=minutes_ago(5), host='local')
        wid = self.add_wait(A, 'task', 'k', met=True)
        ws = self.c.execute('SELECT * FROM waits').fetchall()
        lease = lambda: self.c.execute('SELECT waking_at FROM waits WHERE id=?', (wid,)).fetchone()[0]

        def after_start(*a, **k):
            e = bus.BusOffline('write failed')
            e.after_start = True
            raise e
        bus._wake_one = after_start
        with self.assertRaises(bus.BusOffline):
            bus.wake_one(self.c, A, ws)
        self.assertIsNotNone(lease())
        with self.c:
            self.c.execute('UPDATE waits SET waking_at=NULL')

        def plain(*a, **k):
            raise RuntimeError('before the start')
        bus._wake_one = plain
        with self.assertRaises(RuntimeError):
            bus.wake_one(self.c, A, ws)
        self.assertIsNone(lease())
        bus._wake_one = lambda *a, **k: (False, 'no')
        self.assertEqual(bus.wake_one(self.c, A, ws), (False, 'no'))
        self.assertIsNone(lease())

    def test_laptop_wake_reads_the_met_waits_again(self):
        # A wait met while the wake starts goes into the resume text, and mark_woken marks both.
        self.set_session(parked_at=minutes_ago(5), host='server', tmux='task-wa', cwd=self.tmp.name)
        a = self.add_wait(A, 'task', 'one', met=True)
        ws = self.c.execute('SELECT * FROM waits').fetchall()  # the caller's list has only `a`
        b = self.add_wait(A, 'task', 'two', met=True)
        saved_tab = bus.open_cloud_tab
        bus.open_cloud_tab = lambda *a, **k: ('w4:t9', 'w4:p9')
        try:
            ok, msg = bus.wake_remote(self.c, A, self.c.execute('SELECT * FROM sessions WHERE session_id=?', (A,)).fetchone(),
                                      ws, ready_timeout=0, poll=0)
        finally:
            bus.open_cloud_tab = saved_tab
        self.assertTrue(ok, msg)
        self.assertEqual(len(self.tmux_calls), 1)
        self.assertIn('2 of your waits were met', self.tmux_calls[0][1])
        self.assertEqual(bus.met_waits(self.c, A), [])  # both marked woken

    # 8. meet resume parks an ended session even with an open matching wait
    def test_meet_resume_parks_an_ended_session_with_an_open_wait(self):
        self.set_session(ended_at=minutes_ago(10))
        wid = self.add_wait(A, 'task', 'k')
        self.assertEqual(bus.meet(self.c, 'task', 'k', 's', A, add=True, resume=True), 1)
        self.assertTrue(self.c.execute('SELECT parked_at FROM sessions WHERE session_id=?', (A,)).fetchone()[0])
        self.assertIsNotNone(self.c.execute('SELECT met_at FROM waits WHERE id=?', (wid,)).fetchone()[0])

    # 9. the same-person note goes through router_note
    def test_two_sessions_for_one_person_note_is_a_router_row_in_server_role(self):
        bus.wake = self.saved['wake']
        for sid in (A, B):
            self.set_session(sid, parked_at=minutes_ago(5), host='server')
            self.add_wait(sid, 'person', 'ana')
        with self.c:
            self.c.execute("UPDATE waits SET met_at=?, met_by=? WHERE session_id=?",
                           (bus.now(), json.dumps({'summary': 'hola'}), A))
        os.environ['AGENT_BUS_ROLE'] = 'server'
        res = bus.wake(self.c, 2)
        self.assertEqual(res, [])
        self.assertEqual(self.inbox, [])
        r = self.rows()[0]
        self.assertEqual((r['session_id'], r['host']), ('', 'local'))
        self.assertIn('ana', r['text'])


class DeliverRound3Test(DeliverBase):
    def test_park_settle_does_not_hold_back_an_ended_session(self):
        self.set_session(parked_at=bus.now(), ended_at=bus.now(), host='local')
        out = bus.deliver(self.c, A, 'x')
        self.assertNotIn('parked just now', out)
        self.assertTrue(out.startswith('met task:deliver-'), out)
        self.assertEqual(self.wake_calls, [(1, A)])

    def test_pull_laptop_fallback_for_server_rows_is_ten_minutes(self):
        self.set_session(host='server', tmux='task-wx')
        self.rc_tmux = 3
        bus.deliver(self.c, A, 'x')
        self.rc_tmux = 0
        os.environ['AGENT_BUS_ROLE'] = 'client'
        with self.c:
            self.c.execute('UPDATE deliveries SET created_at=?', (minutes_ago(5),))
        self.assertEqual(bus.pull(self.c), [])
        with self.c:
            self.c.execute('UPDATE deliveries SET created_at=?', (minutes_ago(11),))
        self.assertEqual(len(bus.pull(self.c)), 1)
        self.assertIsNotNone(self.rows()[0]['done_at'])

    def test_wake_waits_for_the_settle_time_unless_the_session_ended(self):
        bus.wake = self.saved['wake']
        woken = []
        bus._wake_one = lambda c, sid, s, ws, *a, **k: woken.append(sid) or (True, 'x')
        old = os.environ.get('AGENT_BUS_WAKE_SETTLE')
        os.environ['AGENT_BUS_WAKE_SETTLE'] = '30'
        try:
            self.set_session(A, parked_at=minutes_ago(0), host='local')
            self.add_wait(A, 'task', 'k', met=True)
            self.assertEqual(bus.wake(self.c), [])
            self.set_session(B, parked_at=minutes_ago(0), ended_at=bus.now(), host='local')
            self.add_wait(B, 'task', 'k', met=True)
            self.assertEqual(bus.wake(self.c), ['ok: x'])
            self.assertEqual(woken, [B])
        finally:
            if old is None:
                os.environ.pop('AGENT_BUS_WAKE_SETTLE', None)
            else:
                os.environ['AGENT_BUS_WAKE_SETTLE'] = old

    def test_wait_summary_keeps_1500_chars_for_polar_and_task(self):
        for kind, n in (('polar', 1500), ('task', 1500), ('thread', 300), ('person', 300)):
            w = {'kind': kind, 'met_by': json.dumps({'summary': 'x' * 2000})}
            self.assertEqual(len(bus.wait_summary(w)), n, kind)

    def wake_remote_run(self, rcs, existed=False):
        self.set_session(parked_at=minutes_ago(5), host='server', tmux='task-wa', cwd=self.tmp.name)
        self.add_wait(A, 'task', 'one', met=True)
        ws = self.c.execute('SELECT * FROM waits').fetchall()
        rcs = list(rcs)
        sleeps, marked = [], []
        bus.deliver_tmux = lambda name, text, stash=False: self.tmux_calls.append((name, text)) or rcs.pop(0)
        saved = (bus.open_cloud_tab, bus.mark_woken, bus.time.sleep, bus.ssh)
        self.tabs = []
        bus.open_cloud_tab = lambda *a, **k: self.tabs.append(a) or ('w4:t9', 'w4:p9')
        bus.mark_woken = lambda c, sid, rows, sets, ev, t0=None: marked.append([r['id'] for r in rows])
        bus.time.sleep = lambda n: sleeps.append(n)
        bus.ssh = lambda cmd, timeout=40: (0 if existed else 1, '') if 'has-session' in cmd else (0, '')
        try:
            s = self.c.execute('SELECT * FROM sessions WHERE session_id=?', (A,)).fetchone()
            res = bus.wake_remote(self.c, A, s, ws, ready_timeout=0, poll=2)
        finally:
            bus.open_cloud_tab, bus.mark_woken, bus.time.sleep, bus.ssh = saved
        return res, sleeps, marked

    def test_wake_remote_retries_a_busy_box_then_marks_the_wait_woken(self):
        (ok, msg), sleeps, marked = self.wake_remote_run([5, 6, 0])
        self.assertTrue(ok, msg)
        self.assertEqual(len(self.tmux_calls), 3)
        self.assertEqual(sleeps.count(30), 2)
        self.assertEqual(len(marked[0]), 1)

    def test_wake_remote_that_never_typed_records_nothing(self):
        (ok, msg), sleeps, marked = self.wake_remote_run([5, 5, 5])
        self.assertFalse(ok, msg)  # the lease goes back; the session still counts as parked
        self.assertEqual(len(self.tmux_calls), 3)  # one try and two retries at most
        self.assertEqual(marked, [])
        self.assertEqual(self.tabs, [])  # no ☁️ tab before the text is in

    def test_wake_remote_into_an_existing_tmux_tries_once(self):
        (ok, msg), sleeps, marked = self.wake_remote_run([5], existed=True)
        self.assertFalse(ok, msg)
        self.assertEqual(len(self.tmux_calls), 1)
        self.assertEqual(sleeps.count(30), 0)

    def test_wake_remote_types_into_an_existing_tmux(self):
        (ok, msg), sleeps, marked = self.wake_remote_run([0], existed=True)
        self.assertTrue(ok, msg)
        self.assertEqual(len(self.tmux_calls), 1)
        self.assertEqual(len(marked[0]), 1)

    def test_wake_remote_rc7_counts_as_typed(self):
        (ok, msg), sleeps, marked = self.wake_remote_run([7])
        self.assertEqual(len(self.tmux_calls), 1)
        self.assertEqual(len(marked[0]), 1)

    def test_laptop_wake_types_into_a_session_that_runs_already(self):
        self.set_session(parked_at=minutes_ago(5), host='local')
        self.add_wait(A, 'task', 'k', met=True)
        ws = self.c.execute('SELECT * FROM waits').fetchall()
        saved = bus.live_agent_panes
        bus.live_agent_panes = lambda: {A: 'w4:p5'}
        try:
            s = self.c.execute('SELECT * FROM sessions WHERE session_id=?', (A,)).fetchone()
            ok, msg = self.saved['_wake_one'](self.c, A, s, ws)
            self.assertTrue(ok, msg)
            self.assertEqual(len(self.pane_calls), 1)
            self.assertEqual(self.pane_calls[0][0], 'w4:p5')
            self.assertIn('your wait', self.pane_calls[0][1])
            row = self.c.execute('SELECT parked_at, pane FROM sessions WHERE session_id=?', (A,)).fetchone()
            self.assertEqual((row['parked_at'], row['pane']), (None, 'w4:p5'))
            self.assertEqual(bus.met_waits(self.c, A), [])
            self.set_session(parked_at=minutes_ago(5))
            self.add_wait(A, 'task', 'k2', met=True)
            self.rc = 3  # the box is not free: not ok, still parked
            ok, msg = self.saved['_wake_one'](self.c, A, s, self.c.execute('SELECT * FROM waits WHERE key="k2"').fetchall())
            self.assertFalse(ok)
            self.assertTrue(self.c.execute('SELECT parked_at FROM sessions WHERE session_id=?', (A,)).fetchone()[0])
        finally:
            bus.live_agent_panes = saved


class DeliverRound4Test(DeliverBase):
    """The final review: own waits that were cancelled or belong to another session, the resume text, t0, the cap."""

    def test_a_cancelled_own_wait_sends_the_row_to_the_router(self):
        self.set_session(parked_at=minutes_ago(5), host='local')
        self.wake_res = []  # nothing woke: the row stays queued
        did = self.outcome_id(bus.deliver(self.c, A, 'x', wait='polar:abcdef012345'))
        self.assertIsNotNone(did)
        with self.c:  # the owner runs agent-bus unpark
            self.c.execute('UPDATE waits SET cancelled_at=? WHERE delivery_id=?', (bus.now(), did))
        bus.pull(self.c)
        self.assertEqual(len(self.inbox), 1)
        self.assertIn('cancelled', self.row(did)['outcome'])
        self.assertIsNotNone(self.row(did)['done_at'])
        self.assertEqual(len(self.wake_calls), 1)  # no second resume for it

    def test_an_own_wait_of_another_session_is_met_again_on_this_one(self):
        self.set_session(parked_at=minutes_ago(5), host='local')
        self.wake_res = []
        did = self.outcome_id(bus.deliver(self.c, A, 'x', wait='polar:abcdef012345'))
        with self.c:  # met on the old id before a handoff
            self.c.execute("UPDATE waits SET session_id='old' WHERE delivery_id=?", (did,))
        self.wake_res = ['ok: woke']
        bus.pull(self.c)
        self.assertIsNotNone(self.row(did)['done_at'])
        mine = self.c.execute('SELECT * FROM waits WHERE delivery_id=? AND session_id=?', (did, A)).fetchall()
        self.assertEqual(len(mine), 1)
        self.assertIsNotNone(mine[0]['woken_at'])

    def test_resume_text_names_the_waits_that_end(self):
        a = self.add_wait(A, 'polar', 'x', met=True)
        self.add_wait(A, 'prod', 'repo#12')
        text, _ = bus.wake_text(self.c, self.c.execute('SELECT * FROM waits WHERE id=?', (a,)).fetchall())
        self.assertIn('prod:repo#12', text)
        self.assertIn('declare again', text)
        with self.c:
            self.c.execute("UPDATE waits SET cancelled_at=? WHERE kind='prod'", (bus.now(),))
        text, _ = bus.wake_text(self.c, self.c.execute('SELECT * FROM waits WHERE id=?', (a,)).fetchall())
        self.assertNotIn('declare again', text)

    def test_mark_woken_keeps_a_park_made_after_the_wake_started(self):
        a = self.add_wait(A, 'task', 'one', met=True)
        old = self.add_wait(A, 'person', 'ana')
        with self.c:
            self.c.execute('UPDATE waits SET created_at=? WHERE id IN (?,?)', (minutes_ago(10), a, old))
        t0 = minutes_ago(1)
        new = self.add_wait(A, 'time', 'later')  # the session parked again while the resume ran
        self.set_session(parked_at=bus.now())
        bus.mark_woken(self.c, A, self.c.execute('SELECT * FROM waits WHERE id=?', (a,)).fetchall(), {'pane': 'w4:p2'},
                       {}, t0)
        cancelled = lambda i: self.c.execute('SELECT cancelled_at FROM waits WHERE id=?', (i,)).fetchone()[0]
        self.assertIsNotNone(cancelled(old))
        self.assertIsNone(cancelled(new))
        row = self.c.execute('SELECT parked_at, pane FROM sessions WHERE session_id=?', (A,)).fetchone()
        self.assertIsNotNone(row['parked_at'])
        self.assertEqual(row['pane'], 'w4:p2')

    def test_fitted_wake_text_leaves_out_waits_that_do_not_fit(self):
        ids = [self.add_wait(A, 'polar', k, met=True) for k in ('x', 'y')]
        with self.c:
            self.c.execute('UPDATE waits SET met_by=?', (json.dumps({'summary': 's' * 1500}),))
        rows = self.c.execute('SELECT * FROM waits ORDER BY id').fetchall()
        text, _, used = bus.fitted_wake_text(self.c, rows, 3000)
        self.assertEqual([r['id'] for r in used], ids[:1])
        self.assertLessEqual(len(text), 3000)
        _, _, used = bus.fitted_wake_text(self.c, rows, 10000)
        self.assertEqual([r['id'] for r in used], ids)


class RemoteLinkTest(FakeDeliverMixin, unittest.TestCase):
    setUp, tearDown, run_cli, server_rows = (RemoteBusTest.setUp, RemoteBusTest.tearDown, RemoteBusTest.run_cli,
                                             RemoteBusTest.server_rows)

    def connect(self):
        os.environ.update(AGENT_BUS_ROLE='client', AGENT_BUS_CLIENT_DIR=self.tmp.name, AGENT_BUS_REMOTE_CMD=self.good)
        bus._REMOTE = None
        c = bus.connect()
        self.assertIsInstance(c, bus.RemoteConnection)
        return c

    def test_a_closed_link_is_opened_again_between_transactions(self):
        c = self.connect()
        self.assertEqual(c.execute('SELECT 1').fetchone()[0], 1)
        old = c.p
        c._kill()
        self.assertTrue(c.closed)
        self.assertEqual(c.execute('SELECT 1').fetchone()[0], 1)  # same object, new link
        self.assertIsNot(c.p, old)
        self.assertFalse(c.closed)

    def test_a_request_outside_a_transaction_is_sent_again_when_the_link_breaks_during_it(self):
        c = self.connect()
        c.p.stdin.close()  # the next write fails inside _send, the link is not marked closed yet
        self.assertFalse(c.closed)
        self.assertEqual(c.execute('SELECT 2').fetchone()[0], 2)

    def test_inside_a_transaction_a_lost_link_fails(self):
        c = self.connect()
        c.execute("INSERT INTO events VALUES('t','s','x','{}')")
        self.assertTrue(c._txn)
        c._kill()
        with self.assertRaises(bus.BusOffline):
            c.execute('SELECT 1')
        c.rollback()  # sends nothing on a closed link
        self.assertFalse(c._txn)
        self.assertEqual(c.execute('SELECT count(*) FROM events WHERE type=?', ('x',)).fetchone()[0], 0)

    def test_a_failed_first_write_still_sends_the_rollback(self):
        c = self.connect()
        c.execute("INSERT INTO deliveries(session_id, ref) VALUES('s', 'r')")
        c.commit()
        with self.assertRaises(Exception):
            c.execute("INSERT INTO deliveries(session_id, ref) VALUES('s', 'r')")  # sqlite3 sent BEGIN first
        self.assertTrue(c._txn)
        c.rollback()
        self.assertFalse(c._txn)
        c.execute("INSERT INTO deliveries(session_id, ref) VALUES('s', 'r2')")
        c.commit()
        self.assertEqual(c.execute("SELECT count(*) FROM deliveries WHERE session_id='s'").fetchone()[0], 2)

    def test_commit_without_a_write_sends_nothing(self):
        c = self.connect()
        c.execute('SELECT 1')
        self.assertFalse(c._txn)
        c._kill()
        c.commit()  # a no-op: no link needed
        c.execute("INSERT INTO events VALUES('t','s','y','{}')")
        c.commit()
        self.assertFalse(c._txn)
        self.assertEqual(self.server_rows("SELECT count(*) FROM events WHERE type='y'"), [(1,)])


# ---- slice 3b: router host, sessions --json, resume budget ---------------------------------------------------------
class RouterHostTest(DeliverBase):
    def setUp(self):
        super().setUp()
        self.saved_rh = (os.environ.get('AGENT_BUS_ROUTER_HOST'), os.environ.get('AGENT_BUS_RESUME_BUDGET'),
                         bus.ROUTER_HOST_FILE, bus.wake_one)

    def tearDown(self):
        rh, budget, bus.ROUTER_HOST_FILE, bus.wake_one = self.saved_rh
        for k, v in (('AGENT_BUS_ROUTER_HOST', rh), ('AGENT_BUS_RESUME_BUDGET', budget)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def host_file(self, text):
        p = os.path.join(self.tmp.name, 'router-host')
        if text is not None:
            Path(p).write_text(text)
        bus.ROUTER_HOST_FILE = p
        os.environ.pop('AGENT_BUS_ROUTER_HOST', None)

    def test_router_host_from_env(self):
        os.environ['AGENT_BUS_ROUTER_HOST'] = 'server'
        self.assertEqual(bus.router_host(), 'server')
        os.environ['AGENT_BUS_ROUTER_HOST'] = 'bogus'
        self.assertEqual(bus.router_host(), 'local')

    def test_router_host_from_file(self):
        self.host_file('server\n')
        self.assertEqual(bus.router_host(), 'server')
        self.host_file('nonsense')
        self.assertEqual(bus.router_host(), 'local')

    def test_router_host_without_env_or_file_is_local(self):
        self.host_file(None)
        self.assertEqual(bus.router_host(), 'local')

    def test_env_wins_over_file(self):
        self.host_file('server')
        os.environ['AGENT_BUS_ROUTER_HOST'] = 'local'
        self.assertEqual(bus.router_host(), 'local')

    def test_router_host_cli(self):
        os.environ['AGENT_BUS_ROUTER_HOST'] = 'server'
        self.assertEqual(self.cli('router-host').strip(), 'server')

    def test_router_row_moves_with_the_router(self):
        os.environ['AGENT_BUS_ROUTER_HOST'] = 'local'
        os.environ['AGENT_BUS_ROLE'] = 'server'
        out = bus.deliver(self.c, '', 'note')
        self.assertIn('queued for local', out)
        self.assertEqual(self.inbox, [])
        os.environ['AGENT_BUS_ROUTER_HOST'] = 'server'  # the cutover
        res = bus.pull(self.c)
        self.assertEqual(len(res), 1, res)
        self.assertEqual(self.inbox, ['note'])
        self.assertIsNotNone(self.rows()[0]['done_at'])

    def test_machine_without_the_router_does_not_pull_its_row(self):
        os.environ['AGENT_BUS_ROUTER_HOST'] = 'local'
        os.environ['AGENT_BUS_ROLE'] = 'server'
        bus.deliver(self.c, '', 'note')
        os.environ['AGENT_BUS_ROUTER_HOST'] = 'server'
        os.environ['AGENT_BUS_ROLE'] = 'client'  # the laptop: the router is not here
        self.assertEqual(bus.pull(self.c), [])
        self.assertEqual(self.inbox, [])
        self.assertIsNone(self.rows()[0]['done_at'])

    def test_sessions_json(self):
        self.set_session(A, host=None, pane='w1:p1', tmux='t', cwd='/x')
        self.set_session(B, host='server', parked_at=minutes_ago(5))
        c3 = 'c' * 8 + '-0000-0000-0000-' + '0' * 12
        d4 = 'd' * 8 + '-0000-0000-0000-' + '0' * 12
        with self.c:
            bus.touch(self.c, c3)
            bus.touch(self.c, d4)
        self.set_session(c3, ended_at=bus.now())
        self.set_session(d4, last_seen=bus.ago(10))
        rows = {r['session_id']: r for r in map(json.loads, self.cli('sessions', '--json').splitlines())}
        self.assertEqual(set(rows), {A, B, c3})  # d4 is older than the default 3 days
        self.assertEqual((rows[A]['state'], rows[A]['host']), ('live', 'local'))
        self.assertEqual((rows[A]['pane'], rows[A]['tmux'], rows[A]['cwd']), ('w1:p1', 't', '/x'))
        self.assertEqual((rows[B]['state'], rows[B]['host']), ('parked', 'server'))
        self.assertEqual(rows[c3]['state'], 'closed')
        for k in ('title', 'repo', 'branch', 'last_seen', 'handoff_from', 'parked_at', 'ended_at'):
            self.assertIn(k, rows[A])
        rows = {json.loads(l)['session_id'] for l in self.cli('sessions', '--json', '--days', '30').splitlines()}
        self.assertIn(d4, rows)
        self.set_session(d4, last_seen=minutes_ago(2 * 24 * 60))
        rows = {json.loads(l)['session_id'] for l in self.cli('sessions', '--json', '--days', '1').splitlines()}
        self.assertNotIn(d4, rows)

    # -- resume budget
    def parked_wait(self, sid=A):
        self.set_session(sid, parked_at=minutes_ago(5), host='local')
        return self.add_wait(sid, 'task', 't-' + sid[:1], met=True)

    def wake_event(self, at, live=None):
        data = {} if live is None else {'live': live}
        with self.c:
            self.c.execute('INSERT INTO events VALUES(?,?,?,?)', (at, B, 'wake', json.dumps(data)))

    def run_wake(self):
        calls = []
        bus.wake_one = lambda c, sid, ws, **k: calls.append(sid) or (True, 'woke')
        return self.saved['wake'](self.c, 2), calls

    def test_budget_reached_gives_a_later_line(self):
        os.environ['AGENT_BUS_RESUME_BUDGET'] = '2'
        self.parked_wait()
        self.wake_event(minutes_ago(2))
        self.wake_event(minutes_ago(8))
        res, calls = self.run_wake()
        self.assertEqual(calls, [])
        self.assertEqual(len(res), 1, res)
        self.assertTrue(res[0].startswith('later:'), res)
        self.assertIsNone(self.c.execute('SELECT woken_at FROM waits').fetchone()[0])

    def test_live_wake_events_do_not_count(self):
        os.environ['AGENT_BUS_RESUME_BUDGET'] = '2'
        self.parked_wait()
        self.wake_event(minutes_ago(2), live=True)
        self.wake_event(minutes_ago(3), live=True)
        res, calls = self.run_wake()
        self.assertEqual((calls, res), ([A], ['ok: woke']))

    def test_old_wake_events_do_not_count(self):
        os.environ['AGENT_BUS_RESUME_BUDGET'] = '2'
        self.parked_wait()
        self.wake_event(minutes_ago(11))
        self.wake_event(minutes_ago(30))
        res, calls = self.run_wake()
        self.assertEqual((calls, res), ([A], ['ok: woke']))

    def test_one_below_the_budget_wakes(self):
        os.environ['AGENT_BUS_RESUME_BUDGET'] = '2'
        self.parked_wait()
        self.wake_event(minutes_ago(1))
        self.wake_event(minutes_ago(2), live=True)
        self.assertEqual(bus.recent_resumes(self.c), 1)
        res, calls = self.run_wake()
        self.assertEqual(calls, [A])

    def test_budget_zero_is_no_limit(self):
        os.environ['AGENT_BUS_RESUME_BUDGET'] = '0'
        self.parked_wait()
        for i in range(6):
            self.wake_event(minutes_ago(1))
        res, calls = self.run_wake()
        self.assertEqual(calls, [A])

    def test_ignore_budget_wakes_over_the_budget(self):
        os.environ['AGENT_BUS_RESUME_BUDGET'] = '1'
        self.parked_wait()
        self.wake_event(minutes_ago(2))
        calls = []
        bus.wake_one = lambda c, sid, ws, **k: calls.append(sid) or (True, 'x')
        res = self.saved['wake'](self.c, 1, A, ignore_budget=True)
        self.assertEqual((calls, res), ([A], ['ok: x']))
        calls.clear()
        res = self.saved['wake'](self.c, 1, A)
        self.assertEqual(calls, [])
        self.assertTrue(res[0].startswith('later:'), res)

    def test_wake_session_cli_passes_ignore_budget(self):
        # Code path: cli() `wake --session` calls wake(..., ignore_budget=bool(wsid)); the CLI run itself is not done.
        self.assertIn('ignore_budget=bool(wsid)', Path(PATH).read_text())

    # -- slack_wait_took
    REF = 'slack:C123:1700000000.000100'

    def met_by(self, wid, ts='1700000000.000100'):
        with self.c:
            self.c.execute('UPDATE waits SET met_by=? WHERE id=?',
                           (json.dumps({'ts': ts, 'channel': 'C123', 'sender': 'U1', 'summary': 's'}), wid))

    def test_handoff_chain_wait_took_the_message(self):
        self.set_session(B, host='local', pane='w4:p1', handoff_from=A)
        self.set_session(A, ended_at=bus.now())
        wid = self.add_wait(A, 'thread', 'C123:1700000000.000001', met=True, woken=True)
        self.met_by(wid)
        self.assertEqual(bus.handoff_chain(self.c, B), [B, A])
        out = bus.deliver(self.c, B, 'hi', ref=self.REF)
        self.assertTrue(out.startswith('not typed: wait'), out)
        self.assertEqual(self.pane_calls, [])

    def test_deferred_person_wait_on_parked_session_does_not_skip(self):
        self.set_session(A, host='local', pane='w4:p1', parked_at=minutes_ago(60))
        wid = self.add_wait(A, 'person', 'Alex', met=True)
        self.met_by(wid)
        with self.c:
            self.c.execute('UPDATE waits SET deferred_at=? WHERE id=?', (bus.now(), wid))
        out = bus.deliver(self.c, A, 'hi', ref=self.REF)
        self.assertFalse(out.startswith('not typed'), out)
        self.assertEqual(self.wake_calls, [(1, A)])

    def test_met_not_woken_wait_on_live_session_does_not_skip(self):
        self.set_session(A, host='local', pane='w4:p1')
        wid = self.add_wait(A, 'thread', 'C123:1700000000.000001', met=True)
        self.met_by(wid)
        out = bus.deliver(self.c, A, 'hi', ref=self.REF)
        self.assertTrue(out.startswith('delivered'), out)
        self.assertEqual(self.pane_calls, [('w4:p1', 'hi')])


# ---- slice 5: stage, move_staged, server wake of staged laptop sessions -------------------------------------------
import types
from unittest import mock


class StageBase(BusBase):
    """A real bare origin + clean clone, a fake HOME with a transcript, and stubbed ssh / copy / live sessions."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp.name
        g = lambda *a, cwd=None: subprocess.run(['git', *a], cwd=cwd, check=True, capture_output=True)
        self.origin = os.path.join(self.root, 'gh', 'your-org', 'demo.git')
        os.makedirs(os.path.dirname(self.origin))
        g('init', '-q', '--bare', self.origin)
        self.repo = os.path.join(self.root, 'demo')
        g('clone', '-q', self.origin, self.repo)
        for k, v in (('user.email', 't@t'), ('user.name', 't')):
            g('config', k, v, cwd=self.repo)
        Path(self.repo, 'a.txt').write_text('a')
        g('add', '.', cwd=self.repo); g('commit', '-qm', 'a', cwd=self.repo); g('push', '-q', 'origin', 'HEAD:main', cwd=self.repo)
        g('branch', '-q', '--set-upstream-to=origin/main', cwd=self.repo)
        self.g = g
        self.home = os.path.join(self.root, 'home')
        os.makedirs(os.path.join(self.home, '.claude', 'projects', 'p'))
        self.transcript = os.path.join(self.home, '.claude', 'projects', 'p', f'{A}.jsonl')
        Path(self.transcript).write_text('{"type":"user"}\n')
        self.ssh_calls, self.ssh_rc = [], (0, 'READY\n')
        self.copy_ok = True
        self.live = [set()]
        self.sleeps = []
        saved = (bus.ssh, bus.copy_transcript, bus.live_agent_sessions, bus.time.sleep, os.environ.get('HOME'),
                 os.environ.get('AGENT_BUS_ROLE'))

        def restore():
            bus.ssh, bus.copy_transcript, bus.live_agent_sessions, bus.time.sleep = saved[:4]
            for k, v in (('HOME', saved[4]), ('AGENT_BUS_ROLE', saved[5])):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        bus.ssh = lambda cmd, timeout=40: self.ssh_calls.append(cmd) or self.ssh_rc
        bus.copy_transcript = lambda sid: self.copy_ok
        bus.live_agent_sessions = lambda: self.live.pop(0) if len(self.live) > 1 else self.live[0]
        bus.time.sleep = lambda s: self.sleeps.append(s)
        os.environ['HOME'] = self.home
        with self.c:
            self.c.execute("UPDATE sessions SET cwd=?, host='local', last_seen='2026-01-01T00:00:00Z' WHERE session_id=?",
                           (self.repo, A))

    def srow(self, sid=A):
        return self.c.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()

    def stage(self, sid=A):
        return json.loads(self.srow(sid)['stage'])

    def events(self, typ, sid=A):
        return self.c.execute("SELECT * FROM events WHERE session_id=? AND type=?", (sid, typ)).fetchall()


class StageSessionTest(StageBase):
    def test_ok(self):
        before = self.srow()['last_seen']
        ok, cwd = bus.stage_session(self.c, A)
        self.assertTrue(ok, cwd)
        st = self.stage()
        self.assertTrue(st['ok'])
        self.assertEqual(st['cwd'], cwd)
        self.assertEqual(st['path'], self.transcript)
        self.assertEqual(st['size'], os.path.getsize(self.transcript))
        self.assertGreaterEqual(st['seen'], before)
        self.assertEqual(len(st['repos']), 1)
        r = st['repos'][0]
        self.assertEqual(set(r), {'main', 'top', 'slug', 'branch', 'sha'})
        self.assertEqual(r['slug'], 'your-org/demo')
        self.assertEqual(len(self.events('stage')), 1)

    def test_dirty_repo(self):
        Path(self.repo, 'b.txt').write_text('b')
        ok, why = bus.stage_session(self.c, A)
        self.assertEqual((ok, why), (False, 'uncommitted changes'))
        st = self.stage()
        self.assertFalse(st['ok'])
        self.assertEqual(st['why'], 'uncommitted changes')

    def test_browser_use(self):
        Path(self.transcript).write_text('{"type":"assistant","x":"tool_use","name":"mcp__claude-in-chrome__navigate"}\n')
        ok, why = bus.stage_session(self.c, A)
        self.assertFalse(ok)
        self.assertIn('browser', why)
        self.assertFalse(self.stage()['ok'])

    def test_copy_failure(self):
        self.copy_ok = False
        self.assertEqual(bus.stage_session(self.c, A), (False, 'the transcript copy failed'))
        self.assertFalse(self.stage()['ok'])

    def test_no_transcript(self):
        os.remove(self.transcript)
        self.assertEqual(bus.stage_session(self.c, A), (False, 'no transcript'))

    def test_wait_close_then_stages(self):
        self.live = [{A}, {A}, set()]
        ok, cwd = bus.stage_session(self.c, A, wait_close=60, poll=3)
        self.assertTrue(ok, cwd)
        self.assertEqual(self.sleeps, [3, 3])
        self.assertTrue(self.stage()['ok'])

    def test_still_running_writes_nothing(self):
        self.live = [{A}]
        self.assertEqual(bus.stage_session(self.c, A, wait_close=0), (False, 'the session still runs'))
        self.assertIsNone(self.srow()['stage'])
        self.assertEqual(self.events('stage'), [])

    def test_refused_in_server_role(self):
        os.environ['AGENT_BUS_ROLE'] = 'server'
        ok, why = bus.stage_session(self.c, A)
        self.assertFalse(ok)
        self.assertIsNone(self.srow()['stage'])

    def test_refused_for_server_session(self):
        with self.c:
            self.c.execute("UPDATE sessions SET host='server' WHERE session_id=?", (A,))
        self.assertEqual(bus.stage_session(self.c, A), (False, 'not a laptop session'))
        self.assertIsNone(self.srow()['stage'])

    def test_host_changes_before_the_copy(self):
        copies = []
        bus.copy_transcript = lambda sid: copies.append(sid) or True
        real = bus.transcript_file

        def tf(sid):
            with self.c:
                self.c.execute("UPDATE sessions SET host='server' WHERE session_id=?", (sid,))
            return real(sid)
        with mock.patch.object(bus, 'transcript_file', tf):
            self.assertEqual(bus.stage_session(self.c, A), (False, 'not a laptop session'))
        self.assertEqual(copies, [])
        self.assertIsNone(self.srow()['stage'])
        self.assertEqual(self.events('stage'), [])

    def test_stage_lock_is_taken(self):
        locks = []
        real_lock, real_unlock = bus._lock, bus._unlock
        with mock.patch.object(bus, '_lock', lambda name, wait=None: locks.append(name) or real_lock(name, wait)), \
                mock.patch.object(bus, '_unlock', real_unlock), \
                mock.patch.dict(os.environ, {'AGENT_BUS_CLIENT_DIR': self.root}):
            bus.stage_session(self.c, A)
        self.assertEqual(locks, [f'stage-{A}.lock'])

    def test_cli_stage(self):
        out = self.cli('stage', 'nonexistent-session')
        self.assertTrue(out.startswith('laptop only: '), out)

    def test_move_to_server_still_uses_checks(self):
        os.environ.pop('AGENT_BUS_NO_MOVE', None)
        with self.c:
            self.c.execute("UPDATE sessions SET ended_at='2026-11-19T00:00:00Z' WHERE session_id=?", (A,))
        ok, cwd = bus.move_to_server(self.c, A, self.srow())
        self.assertTrue(ok, cwd)
        self.assertEqual(self.srow()['host'], bus.SERVER_HOST)
        self.assertTrue(any('READY' in c for c in self.ssh_calls))


class StagedOkTest(BusBase):
    def row(self, stage, last_seen):
        with self.c:
            self.c.execute("UPDATE sessions SET stage=?, last_seen=? WHERE session_id=?",
                           (json.dumps(stage) if stage is not None else None, last_seen, A))
        return self.c.execute("SELECT * FROM sessions WHERE session_id=?", (A,)).fetchone()

    def test_cases(self):
        self.assertFalse(bus.staged_ok(self.row(None, '2026-01-01T00:00:00Z')))
        self.assertFalse(bus.staged_ok(self.row({'ok': False, 'seen': '2026-02-01T00:00:00Z'}, '2026-01-01T00:00:00Z')))
        self.assertFalse(bus.staged_ok(self.row({'ok': True, 'seen': '2026-01-01T00:00:00Z'}, '2026-01-02T00:00:00Z')))
        self.assertTrue(bus.staged_ok(self.row({'ok': True, 'seen': '2026-01-02T00:00:00Z'}, '2026-01-02T00:00:00Z')))
        self.assertTrue(bus.staged_ok(self.row({'ok': True, 'seen': '2026-03-02T00:00:00Z'}, '2026-01-02T00:00:00Z')))

    def test_bad_json(self):
        with self.c:
            self.c.execute("UPDATE sessions SET stage='{not json' WHERE session_id=?", (A,))
        s = self.c.execute("SELECT * FROM sessions WHERE session_id=?", (A,)).fetchone()
        self.assertEqual(bus.stage_of(s), {})
        self.assertFalse(bus.staged_ok(s))


class MoveStagedTest(StageBase):
    def setUp(self):
        super().setUp()
        os.environ['AGENT_BUS_ROLE'] = 'server'
        self.ok_stage = {'ok': True, 'seen': '9999', 'cwd': self.repo, 'path': self.transcript,
                         'size': os.path.getsize(self.transcript),
                         'repos': [{'main': 'main', 'top': self.repo, 'slug': 'your-org/demo', 'branch': 'main', 'sha': 'abc'}]}

    def put(self, st):
        with self.c:
            self.c.execute("UPDATE sessions SET stage=? WHERE session_id=?", (json.dumps(st), A))
        return self.srow()

    def failed(self, why_part):
        ok, why = bus.move_staged(self.c, A, self.srow())
        self.assertFalse(ok)
        self.assertIn(why_part, why)
        st = self.stage()
        self.assertFalse(st['ok'])
        self.assertIn(why_part, st['why'])
        self.assertIn('failed_at', st)
        self.assertEqual(len(self.events('stage-failed')), 1)
        self.assertEqual(self.srow()['host'], 'local')

    def test_ok(self):
        s = self.put(self.ok_stage)
        self.assertEqual(bus.move_staged(self.c, A, s), (True, self.repo))
        r = self.srow()
        self.assertEqual((r['host'], r['cwd']), (bus.SERVER_LABEL, self.repo))
        ev = self.events('moved')
        self.assertEqual(len(ev), 1)
        self.assertTrue(json.loads(ev[0]['data'])['staged'])
        self.assertTrue(any('READY' in c for c in self.ssh_calls))

    def test_activity_after_the_stage_blocks_the_move(self):
        s = self.put(dict(self.ok_stage, seen='2026-01-01T00:00:00Z'))
        with self.c:
            self.c.execute("UPDATE sessions SET last_seen='2026-02-01T00:00:00Z' WHERE session_id=?", (A,))
        self.assertEqual(bus.move_staged(self.c, A, self.srow()), (False, 'it is not a laptop session any more'))
        self.assertEqual(self.srow()['host'], 'local')
        self.assertEqual(self.events('moved'), [])

    def test_size_mismatch(self):
        self.put(dict(self.ok_stage, size=self.ok_stage['size'] + 5))
        self.failed('differs')

    def test_missing_file(self):
        self.put(dict(self.ok_stage, path=os.path.join(self.root, 'nope.jsonl')))
        self.failed('no transcript on server')

    def test_prepare_failure_retries_then_gives_up(self):
        self.put(self.ok_stage)
        self.ssh_rc = (1, 'boom')
        for n in (1, 2):
            ok, why = bus.move_staged(self.c, A, self.srow())
            self.assertFalse(ok)
            self.assertIn('could not prepare', why)
            st = self.stage()
            self.assertTrue(st['ok'])
            self.assertEqual(st['tries'], n)
            self.assertIn('could not prepare', st['last_error'])
            self.assertGreater(st['retry_after'], bus.now())
            self.assertEqual(self.srow()['host'], 'local')
        ok, why = bus.move_staged(self.c, A, self.srow())
        self.assertFalse(ok)
        st = self.stage()
        self.assertFalse(st['ok'])
        self.assertEqual(st['tries'], bus.STAGE_TRIES)
        self.assertIn('could not prepare', st['why'])
        self.assertIn('failed_at', st)
        self.assertEqual(len(self.events('stage-failed')), 3)
        self.assertEqual(self.srow()['host'], 'local')

    def test_staged_ok_false_during_retry_after(self):
        self.put(self.ok_stage)
        self.assertTrue(bus.staged_ok(self.srow()))
        self.ssh_rc = (1, 'boom')
        bus.move_staged(self.c, A, self.srow())
        self.assertFalse(bus.staged_ok(self.srow()))
        st = self.stage()
        st['retry_after'] = '2000-01-01T00:00:00Z'
        self.put(st)
        self.assertTrue(bus.staged_ok(self.srow()))

    def test_prepare_repos_renews_before_each_repo(self):
        calls = []
        r = {'main': 'main', 'top': self.repo, 'slug': 's', 'branch': 'main', 'sha': 'x'}
        bus.ssh = lambda cmd, timeout=40: calls.append('ssh') or (0, 'READY')
        self.assertEqual(bus.prepare_repos([r, r], before_each=lambda: calls.append('renew')), (True, None))
        self.assertEqual(calls, ['ssh', 'renew', 'ssh', 'renew', 'ssh'])

    def test_move_staged_renews_the_wake_lease(self):
        self.put(self.ok_stage)
        with self.c:
            self.c.execute("INSERT INTO waits(session_id, kind, key, about, created_at, met_at, waking_at) "
                           "VALUES(?,?,?,?,?,?,?)", (A, 'task', 't', 'x', bus.now(), bus.now(), '2000-01-01T00:00:00Z'))
        seen = []
        bus.ssh = lambda cmd, timeout=40: seen.append(self.c.execute('SELECT waking_at FROM waits').fetchone()[0]) \
            or (0, 'READY')
        self.assertTrue(bus.move_staged(self.c, A, self.srow())[0])
        self.assertGreater(seen[-1], '2001')

    def test_host_changed_meanwhile(self):
        self.put(self.ok_stage)
        with self.c:
            self.c.execute("UPDATE sessions SET host='server' WHERE session_id=?", (A,))
        s = self.c.execute("SELECT * FROM sessions WHERE session_id=?", (A,)).fetchone()
        self.assertEqual(bus.move_staged(self.c, A, s), (False, 'it is not a laptop session any more'))
        self.assertEqual(self.events('moved'), [])

    def test_staged_session_is_not_staged_ok_after_failure(self):
        self.put(dict(self.ok_stage, size=1))
        bus.move_staged(self.c, A, self.srow())
        self.assertFalse(bus.staged_ok(self.srow()))


class SpawnStageTest(BusBase):
    def setUp(self):
        super().setUp()
        self.role = os.environ.get('AGENT_BUS_ROLE')
        self.addCleanup(lambda: os.environ.pop('AGENT_BUS_ROLE', None) if self.role is None
                        else os.environ.__setitem__('AGENT_BUS_ROLE', self.role))

    def test_no_move_switch(self):
        with mock.patch.object(subprocess, 'Popen') as p:
            bus.spawn_stage(A)
        p.assert_not_called()

    def test_server_role(self):
        os.environ.pop('AGENT_BUS_NO_MOVE')
        os.environ['AGENT_BUS_ROLE'] = 'server'
        with mock.patch.object(subprocess, 'Popen') as p:
            bus.spawn_stage(A)
        p.assert_not_called()

    def test_popen_argv(self):
        os.environ.pop('AGENT_BUS_NO_MOVE')
        with mock.patch.object(subprocess, 'Popen') as p:
            bus.spawn_stage(A, wait_close=30)
            bus.spawn_stage('')
        self.assertEqual(p.call_count, 1)
        argv = p.call_args[0][0]
        self.assertEqual(argv[argv.index('stage') + 1], A)
        self.assertIn('--wait-close', argv)
        self.assertEqual(argv[argv.index('--wait-close') + 1], '30')
        self.assertIn('stage', argv)
        self.assertIn(A, argv)


class ParkStageTest(BusBase):
    def test_park_spawns_stage_for_local_session(self):
        calls = []
        with mock.patch.object(bus, 'spawn_stage', lambda sid, **k: calls.append(sid)):
            with self.c:
                self.c.execute("UPDATE sessions SET host='local', cwd=? WHERE session_id=?", (self.tmp.name, A))
            code, msg = bus.park(self.c, A, ['time:+3h'], 'x', 'n', allow_dirty=True)
        self.assertEqual(code, 0, msg)
        self.assertEqual(calls, [A])

    def test_park_does_not_stage_a_server_session(self):
        calls = []
        with mock.patch.object(bus, 'spawn_stage', lambda sid, **k: calls.append(sid)):
            with self.c:
                self.c.execute("UPDATE sessions SET host='server', cwd=? WHERE session_id=?", (self.tmp.name, A))
            code, msg = bus.park(self.c, A, ['time:+3h'], 'x', 'n', allow_dirty=True)
        self.assertEqual(code, 0, msg)
        self.assertEqual(calls, [])


class ClientHookStageTest(BusBase):
    def run_hook(self, payload, args=()):
        calls = []
        with mock.patch.object(bus, 'spawn_stage', lambda sid, **k: calls.append((sid, k))), \
                mock.patch.object(bus, 'outbox_append', lambda rec: None), \
                mock.patch.object(bus, 'kick_flush', lambda: None), \
                mock.patch.object(bus, 'laptop_fields', lambda p: {'cwd': '/x'}), \
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))):
            bus.client_hook(list(args))
        return calls

    def test_session_end_stages(self):
        self.assertEqual(self.run_hook({'hook_event_name': 'SessionEnd', 'session_id': A}), [(A, {'wait_close': 30})])

    def test_stop_does_not(self):
        self.assertEqual(self.run_hook({'hook_event_name': 'Stop', 'session_id': A}), [])

    def test_replayed_server_line_does_not(self):
        self.assertEqual(self.run_hook({'hook_event_name': 'SessionEnd', 'session_id': A}, ['--host', 'server']), [])


class StagedWakeTest(DeliverBase):
    STAGE = json.dumps({'ok': True, 'seen': '9999', 'cwd': '/w', 'path': '/p', 'size': 1, 'repos': []})

    def setUp(self):
        super().setUp()
        bus.wake = self.saved['wake']
        self.woken = []
        bus._wake_one = lambda c, sid, s, ws, *a, **k: self.woken.append(sid) or (True, 'x')
        self.polar, self.polar_rc = [], 0
        self.addCleanup(setattr, subprocess, 'run', subprocess.run)
        real_run = subprocess.run

        def fake_run(cmd, *a, **k):
            if isinstance(cmd, list) and cmd[:2] == ['/bin/sh', '-c'] and 'polar' in cmd[2]:
                self.polar.append((cmd[2], k.get('input')))
                return types.SimpleNamespace(returncode=self.polar_rc, stdout='{"id":1}', stderr='')
            return real_run(cmd, *a, **k)
        subprocess.run = fake_run

    def parked(self, sid, host='local', stage=None, met_min=1):
        self.set_session(sid, parked_at=minutes_ago(5), host=host, stage=stage, last_seen=minutes_ago(10))
        wid = self.add_wait(sid, 'task', 't-' + sid[:1], met=True)
        with self.c:
            self.c.execute('UPDATE waits SET met_at=? WHERE id=?', (minutes_ago(met_min), wid))
        return wid

    def test_server_wakes_staged_session_only(self):
        self.parked(A, stage=self.STAGE)
        self.parked(B, stage=None)
        os.environ['AGENT_BUS_ROLE'] = 'server'
        res = bus.wake(self.c, max_n=5)
        self.assertEqual(self.woken, [A])
        self.assertEqual(res, ['ok: x'])

    def test_activity_after_stage_blocks_wake(self):
        self.parked(A, stage=json.dumps(dict(json.loads(self.STAGE), seen='2000-01-01T00:00:00Z')))
        os.environ['AGENT_BUS_ROLE'] = 'server'
        self.assertEqual(bus.wake(self.c), [])
        self.assertEqual(self.woken, [])

    def test_tell_once_after_wait(self):
        self.parked(A, stage=None, met_min=20)
        self.set_session(A, title='Mi tarea')
        os.environ['AGENT_BUS_ROLE'] = 'server'
        res = bus.wake(self.c)
        self.assertEqual(self.woken, [])
        self.assertEqual(res, [f'told: {A[:8]} waits for the laptop'])
        self.assertEqual(len(self.polar), 1)
        self.assertIn('Mi tarea', self.polar[0][0])
        self.assertEqual(bus.wake(self.c), [])
        self.assertEqual(len(self.polar), 1)
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='laptop-wait'").fetchone()[0], 1)

    def test_no_card_before_the_wait_is_old(self):
        self.parked(A, stage=None, met_min=2)
        os.environ['AGENT_BUS_ROLE'] = 'server'
        self.assertEqual(bus.wake(self.c), [])
        self.assertEqual(self.polar, [])

    def test_stage_not_ok_gives_its_reason(self):
        self.parked(A, stage=json.dumps({'ok': False, 'why': 'it used the browser or the desktop', 'seen': '9999'}), met_min=20)
        os.environ['AGENT_BUS_ROLE'] = 'server'
        bus.wake(self.c)
        self.assertIn('it used the browser', self.polar[0][1])

    def test_failed_card_is_retried_only_after_the_retry_time(self):
        self.parked(A, stage=None, met_min=20)
        self.polar_rc = 1
        os.environ['AGENT_BUS_ROLE'] = 'server'
        self.assertEqual(bus.wake(self.c), [])  # the card failed: no "told"
        self.assertEqual(len(self.polar), 1)
        self.assertEqual(bus.wake(self.c), [])  # too soon
        self.assertEqual(len(self.polar), 1)
        old = minutes_ago(bus.LAPTOP_WAIT_RETRY // 60 + 2)
        with self.c:
            self.c.execute("UPDATE events SET at=? WHERE type='laptop-wait'", (old,))
        self.polar_rc = 0
        self.assertEqual(len(bus.wake(self.c)), 1)
        self.assertEqual(len(self.polar), 2)
        self.assertEqual(bus.wake(self.c), [])
        self.assertEqual(len(self.polar), 2)

    def test_one_card_per_wake(self):
        self.parked(A, stage=None, met_min=20)
        self.parked(B, stage=None, met_min=20)
        os.environ['AGENT_BUS_ROLE'] = 'server'
        self.assertEqual(len(bus.wake(self.c)), 1)
        self.assertEqual(len(self.polar), 1)
        self.assertEqual(len(bus.wake(self.c)), 1)
        self.assertEqual(len(self.polar), 2)

    def test_title_fallback(self):
        self.parked(A, stage=None, met_min=20)
        os.environ['AGENT_BUS_ROLE'] = 'server'
        bus.wake(self.c)
        self.assertIn('sesión sin título', self.polar[0][0])

    def test_wake_select_falls_back_without_stage_column(self):
        self.parked(A, stage=None)
        os.environ['AGENT_BUS_ROLE'] = 'server'
        real = self.c
        sqls = []

        class Proxy:
            first = True

            def execute(self_, sql, *a):
                sqls.append(sql)
                if self_.first:
                    self_.first = False
                    raise sqlite3.OperationalError('no such column: s.stage')
                return real.execute(sql, *a)

            def __getattr__(self_, k):
                return getattr(real, k)

            def __enter__(self_):
                return real.__enter__()

            def __exit__(self_, *a):
                return real.__exit__(*a)
        res = bus.wake(Proxy(), max_n=1)
        self.assertIn('s.stage', sqls[0])
        self.assertIn('NULL AS stage', sqls[1])
        self.assertEqual(self.woken, [])  # no stage: a laptop session stays with the laptop
        self.assertEqual(res, [])

    def test_laptop_role_unchanged(self):
        self.parked(A, stage=None)
        res = bus.wake(self.c)
        self.assertEqual(self.woken, [A])
        self.assertEqual(res, ['ok: x'])
        self.assertEqual(self.polar, [])

    def test_wake_one_gate(self):
        os.environ['AGENT_BUS_ROLE'] = 'server'
        wid = self.parked(A, stage=None)
        ws = self.c.execute('SELECT * FROM waits WHERE id=?', (wid,)).fetchall()
        ok, msg = bus.wake_one(self.c, A, ws)
        self.assertFalse(ok)
        self.assertIn('the laptop wakes it', msg)
        self.set_session(A, stage=self.STAGE)
        self.assertEqual(bus.wake_one(self.c, A, ws), (True, 'x'))
        self.assertEqual(self.woken, [A])


class WakeOneServerBranchTest(DeliverBase):
    def setUp(self):
        super().setUp()
        self.saved_mod = (bus.move_staged, bus.wake_remote)
        self.addCleanup(lambda: (setattr(bus, 'move_staged', self.saved_mod[0]), setattr(bus, 'wake_remote', self.saved_mod[1])))
        self.rem = []
        bus.wake_remote = lambda c, sid, s, ws, rt=60, poll=2, note=None: self.rem.append((sid, s['host'], note)) or (True, 'woke')
        os.environ['AGENT_BUS_ROLE'] = 'server'
        self.set_session(A, host='local', parked_at=minutes_ago(5))
        self.s = self.c.execute('SELECT * FROM sessions WHERE session_id=?', (A,)).fetchone()

    def test_moved_then_wake_remote_with_note(self):
        def mv(c, sid, s):
            self.set_session(sid, host='server')
            return True, '/w'
        bus.move_staged = mv
        self.assertEqual(bus._wake_one(self.c, A, self.s, []), (True, 'woke'))
        self.assertEqual(self.rem, [(A, 'server', bus.MOVE_NOTE)])

    def test_move_failure(self):
        bus.move_staged = lambda c, sid, s: (False, 'nope')
        ok, msg = bus._wake_one(self.c, A, self.s, [])
        self.assertFalse(ok)
        self.assertIn('nope', msg)
        self.assertEqual(self.rem, [])


# ---- slice 5b: laptop away, idle stage, takeover ---------------------------------------------------------------
class LaptopAwayTest(BusBase):
    def test_away_cases(self):
        self.assertFalse(bus.laptop_away(self.c))  # no row: old laptop code
        bus.laptop_heartbeat(self.c)
        self.assertFalse(bus.laptop_away(self.c))
        with self.c:
            self.c.execute("UPDATE machines SET seen=? WHERE name='laptop'", (bus.ago_s(bus.LAPTOP_AWAY_S + 30),))
        self.assertTrue(bus.laptop_away(self.c))
        with self.c:
            self.c.execute("UPDATE machines SET seen=? WHERE name='laptop'", (bus.ago_s(bus.LAPTOP_AWAY_S - 30),))
        self.assertFalse(bus.laptop_away(self.c))

    def test_heartbeat_keeps_one_row(self):
        bus.laptop_heartbeat(self.c)
        bus.laptop_heartbeat(self.c)
        self.assertEqual(self.c.execute("SELECT count(*) FROM machines").fetchone()[0], 1)


class StageIdleTest(BusBase):
    def setUp(self):
        super().setUp()
        self.spawned = []
        p = mock.patch.object(bus, 'spawn_stage', lambda sid, **k: self.spawned.append((sid, k)))
        p.start()
        self.addCleanup(p.stop)
        self.old = bus.ago_s(bus.IDLE_STAGE_S + 60)

    def sess(self, sid, **cols):
        cols = dict({'host': 'local', 'last_seen': self.old}, **cols)
        with self.c:
            bus.touch(self.c, sid)
            for k, v in cols.items():
                self.c.execute(f"UPDATE sessions SET {k}=? WHERE session_id=?", (v, sid))

    def ag(self, sid, status='idle', agent='claude'):
        return {'agent': agent, 'agent_status': status, 'agent_session': {'value': sid}}

    def test_picks_idle_quiet_session_with_live_true(self):
        self.sess(A)
        self.assertEqual(bus.stage_idle(self.c, agents=[self.ag(A)]), [A])
        self.assertEqual(self.spawned, [(A, {'wait_close': 0, 'live': True})])

    def test_done_status_also(self):
        self.sess(A)
        self.assertEqual(bus.stage_idle(self.c, agents=[self.ag(A, 'done')]), [A])

    def test_skips(self):
        for sid, cols, ag in ((A, {}, self.ag(A, 'working')), (B, {}, self.ag(B, agent='codex'))):
            self.sess(sid, **cols)
            self.assertEqual(bus.stage_idle(self.c, agents=[ag]), [], sid)
        self.sess(A, parked_at=self.old)
        self.assertEqual(bus.stage_idle(self.c, agents=[self.ag(A)]), [])
        self.sess(A, parked_at=None, ended_at=self.old)
        self.assertEqual(bus.stage_idle(self.c, agents=[self.ag(A)]), [])
        self.sess(A, ended_at=None, host='server')
        self.assertEqual(bus.stage_idle(self.c, agents=[self.ag(A)]), [])
        self.sess(A, host='local', last_seen=bus.now())  # not quiet yet
        self.assertEqual(bus.stage_idle(self.c, agents=[self.ag(A)]), [])
        self.assertEqual(bus.stage_idle(self.c, agents=[self.ag('c' * 8 + '-x'), {'agent': 'claude', 'agent_status': 'idle'}]), [])
        self.assertEqual(self.spawned, [])

    def test_no_stage_when_activity_is_already_staged(self):
        self.sess(A, stage=json.dumps({'ok': True, 'seen': self.old}))
        self.assertEqual(bus.stage_idle(self.c, agents=[self.ag(A)]), [])
        self.sess(A, stage=json.dumps({'ok': True, 'seen': '2000-01-01T00:00:00Z'}))
        self.assertEqual(bus.stage_idle(self.c, agents=[self.ag(A)]), [A])

    def test_cap(self):
        ids = [f'{i:08d}-0000-0000-0000-' + '0' * 12 for i in range(4)]
        for sid in ids:
            self.sess(sid)
        out = bus.stage_idle(self.c, agents=[self.ag(s) for s in ids])
        self.assertEqual(out, ids[:bus.IDLE_STAGE_MAX])
        self.assertEqual(len(self.spawned), bus.IDLE_STAGE_MAX)


class StageLiveTest(StageBase):
    def test_live_session_stages_without_waiting(self):
        self.live = [{A}]
        ok, cwd = bus.stage_session(self.c, A, live=True)
        self.assertTrue(ok, cwd)
        self.assertEqual(self.sleeps, [])
        self.assertTrue(self.stage()['ok'])

    def test_not_live_still_refuses(self):
        self.live = [{A}]
        self.assertEqual(bus.stage_session(self.c, A), (False, 'the session still runs'))

    def test_live_lock_does_not_wait(self):
        waits = []
        real = bus._lock
        with mock.patch.object(bus, '_lock', lambda name, wait=None: waits.append(wait) or real(name, wait)), \
                mock.patch.dict(os.environ, {'AGENT_BUS_CLIENT_DIR': self.root}):
            bus.stage_session(self.c, A, wait_close=30, live=True)
            bus.stage_session(self.c, A, wait_close=30)
        self.assertEqual(waits, [0, 230])

    def test_live_lock_taken_returns_at_once(self):
        with mock.patch.object(bus, '_lock', lambda name, wait=None: None):
            self.assertEqual(bus.stage_session(self.c, A, live=True), (False, 'another stage of this session runs'))

    def test_cli_passes_live(self):
        seen = []
        with mock.patch.object(bus, 'stage_session', lambda c, sid, wc=0, **k: seen.append((sid, wc, k)) or (True, '/w')), \
                mock.patch.object(bus, 'connect', lambda: self.c):
            bus.cli(['stage', A, '--wait-close', '5', '--live'])
            bus.cli(['stage', A])
        self.assertEqual(seen, [(A, 5.0, {'live': True}), (A, 0.0, {'live': False})])


class SpawnStageLiveTest(BusBase):
    def test_live_adds_flag(self):
        os.environ.pop('AGENT_BUS_NO_MOVE')
        self.addCleanup(os.environ.__setitem__, 'AGENT_BUS_NO_MOVE', '1')
        with mock.patch.object(subprocess, 'Popen') as p:
            bus.spawn_stage(A, wait_close=0, live=True)
            bus.spawn_stage(A, wait_close=0)
        self.assertIn('--live', p.call_args_list[0][0][0])
        self.assertNotIn('--live', p.call_args_list[1][0][0])


class ReapTakenOverTest(BusBase):
    SIZE = 10

    def setUp(self):
        super().setUp()
        self.closed, self.notices, self.tsize = [], [], self.SIZE
        for name, f in (('herdr', lambda *a, **k: self.closed.append(a) or ''),
                        ('tab_of_pane', lambda pane: 't-' + pane),
                        ('laptop_polar_notice', lambda t, b, sid: self.notices.append((t, sid)) or True),
                        ('transcript_file', lambda sid: self.tfile())):
            p = mock.patch.object(bus, name, f)
            p.start()
            self.addCleanup(p.stop)
        self.f = os.path.join(self.tmp.name, 't.jsonl')

    def tfile(self):
        if self.tsize is None:
            return None
        with open(self.f, 'w') as fh:
            fh.write('x' * self.tsize)
        return self.f

    def taken(self, sid=A, pane='w1:p1', host='server', size=SIZE):
        with self.c:
            self.c.execute("UPDATE sessions SET host=?, stage=? WHERE session_id=?",
                           (host, json.dumps({'ok': True, 'size': size}), sid))
            self.c.execute("INSERT INTO events VALUES(?,?,?,?)",
                           (bus.ago_s(60), sid, 'taken-over', json.dumps({'pane': pane})))

    def ag(self, sid=A, pane='w1:p1', status='idle'):
        return {'agent': 'claude', 'agent_status': status, 'pane_id': pane, 'terminal_title_stripped': 'Mi tarea',
                'agent_session': {'value': sid}}

    def events(self, typ):
        return self.c.execute("SELECT * FROM events WHERE type=?", (typ,)).fetchall()

    def test_match_closes(self):
        self.taken()
        self.assertEqual(bus.reap_taken_over(self.c, agents=[self.ag()]), [A])
        self.assertEqual(self.closed, [('tab', 'close', 't-w1:p1')])
        ev = self.events('laptop-copy-closed')
        self.assertEqual(json.loads(ev[0]['data']), {'pane': 'w1:p1', 'tab': 't-w1:p1'})
        self.assertEqual(self.notices, [])

    def test_done_status_closes(self):
        self.taken()
        self.assertEqual(bus.reap_taken_over(self.c, agents=[self.ag(status='done')]), [A])

    def test_closes_once(self):
        self.taken()
        bus.reap_taken_over(self.c, agents=[self.ag()])
        self.assertEqual(bus.reap_taken_over(self.c, agents=[self.ag()]), [])
        self.assertEqual(len(self.closed), 1)
        self.assertEqual(len(self.events('laptop-copy-closed')), 1)

    def test_other_pane_is_skipped(self):
        self.taken(pane='w1:p1')
        self.assertEqual(bus.reap_taken_over(self.c, agents=[self.ag(pane='w9:p9')]), [])
        self.assertEqual((self.closed, self.notices, self.events('two-copies')), ([], [], []))

    def test_not_taken_over_or_not_server_is_skipped(self):
        self.taken(A, host='server')
        with self.c:
            self.c.execute("DELETE FROM events")
        self.assertEqual(bus.reap_taken_over(self.c, agents=[self.ag()]), [])  # server session, no event
        self.taken(B, host='local')
        self.assertEqual(bus.reap_taken_over(self.c, agents=[self.ag(B)]), [])  # event but host local
        self.assertEqual(bus.reap_taken_over(self.c, agents=[self.ag('z' * 8)]), [])
        self.assertEqual((self.closed, self.notices), ([], []))

    def test_working_copy_gives_two_copies_and_one_notice(self):
        self.taken()
        self.assertEqual(bus.reap_taken_over(self.c, agents=[self.ag(status='working')]), [])
        self.assertEqual(self.closed, [])
        self.assertEqual(len(self.events('two-copies')), 1)
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.notices[0][1], A)
        bus.reap_taken_over(self.c, agents=[self.ag(status='working')])
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(len(self.events('two-copies')), 1)

    def test_size_differs_gives_two_copies(self):
        self.taken()
        self.tsize = self.SIZE + 5
        self.assertEqual(bus.reap_taken_over(self.c, agents=[self.ag()]), [])
        self.assertEqual(self.closed, [])
        self.assertEqual(len(self.notices), 1)
        bus.reap_taken_over(self.c, agents=[self.ag()])
        self.assertEqual(len(self.notices), 1)

    def test_missing_transcript_gives_two_copies(self):
        self.taken()
        self.tsize = None
        bus.reap_taken_over(self.c, agents=[self.ag()])
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.closed, [])

    def test_no_tab_found_still_logs_event(self):
        self.taken()
        with mock.patch.object(bus, 'tab_of_pane', lambda pane: None):
            self.assertEqual(bus.reap_taken_over(self.c, agents=[self.ag()]), [A])
        self.assertEqual(self.closed, [])
        self.assertEqual(len(self.events('laptop-copy-closed')), 1)

    def test_panes_filter(self):
        self.taken()
        self.assertEqual(bus.reap_taken_over(self.c, {A: 'w2:p2'}, agents=[self.ag()]), [])
        self.assertEqual(bus.reap_taken_over(self.c, {A: 'w1:p1'}, agents=[self.ag()]), [A])

    def test_lists_herdr_agents_by_default(self):
        self.taken()
        with mock.patch.object(bus, 'herdr_json', lambda *a: {'agents': [self.ag()]}):
            self.assertEqual(bus.reap_taken_over(self.c), [A])


class TakeOverTest(BusBase):
    D = {'id': 7}

    def s(self):
        return self.c.execute("SELECT * FROM sessions WHERE session_id=?", (A,)).fetchone()

    def set(self, **cols):
        with self.c:
            for k, v in cols.items():
                self.c.execute(f"UPDATE sessions SET {k}=? WHERE session_id=?", (v, A))

    def test_takes_a_live_laptop_session_once(self):
        self.set(host='local', pane='w1:p1')
        self.assertTrue(bus.take_over(self.c, A, self.s(), self.D))
        self.assertIsNotNone(self.s()['ended_at'])
        ev = self.c.execute("SELECT data FROM events WHERE type='taken-over'").fetchall()
        self.assertEqual(len(ev), 1)
        self.assertEqual(json.loads(ev[0]['data']), {'delivery': 7, 'pane': 'w1:p1'})
        self.assertFalse(bus.take_over(self.c, A, self.s(), self.D))  # already ended
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='taken-over'").fetchone()[0], 1)

    def test_null_host_counts_as_local(self):
        self.set(host=None)
        self.assertTrue(bus.take_over(self.c, A, self.s(), self.D))

    def test_refuses(self):
        s = self.s()
        for cols in ({'host': 'server'}, {'host': 'local', 'parked_at': bus.now()}, {'parked_at': None, 'ended_at': bus.now()}):
            self.set(**cols)
            self.assertFalse(bus.take_over(self.c, A, s, self.D), cols)
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='taken-over'").fetchone()[0], 0)


class TakeoverDeliveryTest(DeliverBase):
    STAGE = json.dumps({'ok': True, 'seen': '9999', 'cwd': '/w', 'path': '/p', 'size': 1, 'repos': []})

    def setUp(self):
        super().setUp()
        self.told = []
        p = mock.patch.object(bus, 'tell_taken_over', lambda c, sid: self.told.append(sid) or True)
        p.start()
        self.addCleanup(p.stop)
        os.environ['AGENT_BUS_ROLE'] = 'server'
        self.set_session(A, host='local', pane='w4:p1', stage=self.STAGE, last_seen=minutes_ago(10))
        self.moves = True  # the fake wake moves the session like a real wake with move_staged

        def wake(c, n=2, only=None, **kw):
            res = self.fake_wake(c, n, only, **kw)
            if self.moves:
                self.set_session(only, host='server')
            return res
        bus.wake = wake
        self.away()

    def away(self, on=True):
        with self.c:
            self.c.execute("DELETE FROM machines")
            self.c.execute("INSERT INTO machines(name, seen) VALUES('laptop', ?)",
                           (minutes_ago(30 if on else 0),))

    def queue(self, age_s=300, **kw):
        did, _ = bus.delivery_add(self.c, A, 'hello', **kw)
        with self.c:
            self.c.execute("UPDATE deliveries SET created_at=? WHERE id=?", (bus.ago_s(age_s), did))
        return bus.delivery_try(self.c, dict(self.row(did)))

    def assertStays(self, out):
        self.assertFalse(out[0], out)
        self.assertIn('runs on the laptop', out[1])
        self.assertIsNone(self.c.execute("SELECT ended_at FROM sessions WHERE session_id=?", (A,)).fetchone()[0])
        self.assertEqual(self.told, [])
        self.assertEqual(self.wake_calls, [])

    def test_takes_over_and_wakes(self):
        out = self.queue()
        s = self.c.execute("SELECT * FROM sessions WHERE session_id=?", (A,)).fetchone()
        self.assertIsNotNone(s['ended_at'])
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='taken-over'").fetchone()[0], 1)
        w = self.c.execute("SELECT * FROM waits WHERE session_id=?", (A,)).fetchall()
        self.assertEqual(len(w), 1)
        self.assertIsNotNone(w[0]['met_at'])
        self.assertEqual(self.wake_calls, [(1, A)])
        self.assertEqual(self.told, [A])
        self.assertEqual(self.pane_calls, [])
        self.assertTrue(out[0] or 'met' in out[1], out)

    def test_no_card_when_the_move_failed(self):
        self.moves = False
        self.queue()
        self.assertEqual(len(self.wake_calls), 1)
        self.assertEqual(self.told, [])
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='taken-over'").fetchone()[0], 1)

    def test_pull_takes_over_when_away(self):
        did, _ = bus.delivery_add(self.c, A, 'hello')
        with self.c:
            self.c.execute("UPDATE deliveries SET created_at=? WHERE id=?", (bus.ago_s(300), did))
        out = bus.pull(self.c)
        self.assertEqual(len(out), 1, out)
        self.assertEqual(self.wake_calls, [(1, A)])
        self.assertEqual(self.told, [A])
        self.assertIsNotNone(self.c.execute("SELECT ended_at FROM sessions WHERE session_id=?", (A,)).fetchone()[0])

    def test_pull_leaves_the_local_row_when_laptop_present(self):
        self.away(False)
        did, _ = bus.delivery_add(self.c, A, 'hello')
        with self.c:
            self.c.execute("UPDATE deliveries SET created_at=? WHERE id=?", (bus.ago_s(300), did))
        self.assertEqual(bus.pull(self.c), [])
        self.assertEqual((self.wake_calls, self.told), ([], []))

    def test_pull_leaves_a_new_local_row_when_away(self):
        did, _ = bus.delivery_add(self.c, A, 'hello')
        self.assertEqual(bus.pull(self.c), [])
        self.assertEqual(self.wake_calls, [])

    def test_laptop_not_away(self):
        self.away(False)
        self.assertStays(self.queue())

    def test_no_machines_row(self):
        with self.c:
            self.c.execute("DELETE FROM machines")
        self.assertStays(self.queue())

    def test_not_staged(self):
        self.set_session(A, stage=None)
        self.assertStays(self.queue())

    def test_activity_after_stage(self):
        self.set_session(A, stage=json.dumps(dict(json.loads(self.STAGE), seen='2000-01-01T00:00:00Z')))
        self.assertStays(self.queue())

    def test_row_too_new(self):
        self.assertStays(self.queue(age_s=bus.TAKEOVER_AFTER_S - 60))

    def test_live_only_row(self):
        self.assertStays(self.queue(live_only=True))

    def test_laptop_role_does_not_take_over(self):
        os.environ['AGENT_BUS_ROLE'] = 'local'
        with mock.patch.object(bus, 'runs_here', lambda h: h != 'server'):
            out = self.queue()
        self.assertEqual(self.told, [])
        self.assertEqual(self.wake_calls, [])
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='taken-over'").fetchone()[0], 0)

    def test_parked_session_is_not_taken_over(self):
        self.set_session(A, parked_at=minutes_ago(5))
        self.queue()
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='taken-over'").fetchone()[0], 0)


class CanWakeTest(BusBase):
    def test_server_role(self):
        os.environ['AGENT_BUS_ROLE'] = 'server'
        self.addCleanup(os.environ.__setitem__, 'AGENT_BUS_ROLE', 'local')
        staged = json.dumps({'ok': True, 'seen': '9999'})
        with self.c:
            self.c.execute("UPDATE sessions SET stage=? WHERE session_id=?", (staged, A))
        a = self.c.execute("SELECT * FROM sessions WHERE session_id=?", (A,)).fetchone()
        b = self.c.execute("SELECT * FROM sessions WHERE session_id=?", (B,)).fetchone()
        self.assertTrue(bus.can_wake('local', a))
        self.assertFalse(bus.can_wake('local', b))
        self.assertFalse(bus.can_wake('local'))
        self.assertTrue(bus.can_wake('server', b))
        self.assertTrue(bus.can_wake('server'))
        os.environ['AGENT_BUS_ROLE'] = 'local'
        self.assertTrue(bus.can_wake('local', b))


class PullClientRoleStepsTest(BusBase):
    def setUp(self):
        super().setUp()
        os.environ['AGENT_BUS_ROLE'] = 'client'
        self.addCleanup(os.environ.__setitem__, 'AGENT_BUS_ROLE', 'local')
        self.calls = []
        for name, f in (('laptop_heartbeat', lambda c: self.calls.append('heartbeat')),
                        ('reap_taken_over', lambda c, *a, **k: self.calls.append('reap') or [A]),
                        ('stage_idle', lambda c, *a, **k: self.calls.append('idle') or []),
                        ('apply_done_journal', lambda c: set())):
            p = mock.patch.object(bus, name, f)
            p.start()
            self.addCleanup(p.stop)

    def test_client_pull_runs_the_three_steps_in_order(self):
        out = bus.pull(self.c)
        self.assertEqual(self.calls, ['heartbeat', 'reap', 'idle'])
        self.assertEqual(out, [f'closed laptop copy of {A[:8]}: server took it over'])

    def test_a_failing_step_does_not_stop_pull(self):
        with mock.patch.object(bus, 'stage_idle', side_effect=RuntimeError('x')):
            self.assertEqual(len(bus.pull(self.c)), 1)

    def test_other_roles_skip_the_steps(self):
        for role in ('server', 'local'):
            os.environ['AGENT_BUS_ROLE'] = role
            self.calls.clear()
            bus.pull(self.c)
            self.assertEqual(self.calls, [], role)


class LaptopHookOfTakenOverSessionTest(BusBase):
    def setUp(self):
        super().setUp()
        with self.c:
            self.c.execute("UPDATE sessions SET host='server', last_seen='2026-01-01T00:00:00Z', ended_at=NULL "
                           "WHERE session_id=?", (A,))

    def s(self):
        return self.c.execute("SELECT * FROM sessions WHERE session_id=?", (A,)).fetchone()

    def fire(self, ev, **kw):
        bus.hook(json.dumps({'hook_event_name': ev, 'session_id': A, 'cwd': self.tmp.name}), c=self.c, **kw)

    def test_laptop_end_and_stop_are_ignored(self):
        for ev in ('SessionEnd', 'Stop', 'UserPromptSubmit', 'PostToolUse'):
            for pre in ({}, {'cwd': '/x', 'pane': 'w1:p1'}):
                self.fire(ev, pre=pre, at='2026-06-01T00:00:00Z')
                s = self.s()
                self.assertIsNone(s['ended_at'], ev)
                self.assertEqual(s['last_seen'], '2026-01-01T00:00:00Z', ev)
                self.assertEqual(s['host'], 'server')

    def test_laptop_session_start_without_takeover_applies(self):
        self.fire('SessionStart', pre={'cwd': '/x', 'pane': 'w1:p1'}, at='2026-06-01T00:00:00Z')
        self.assertEqual(self.s()['last_seen'], '2026-06-01T00:00:00Z')

    def test_laptop_session_start_after_takeover_is_ignored(self):
        with self.c:
            bus.event(self.c, A, 'taken-over', {})
        self.fire('SessionStart', pre={'cwd': '/x', 'pane': 'w1:p1'}, at='2026-06-01T00:00:00Z')
        self.assertEqual(self.s()['last_seen'], '2026-01-01T00:00:00Z')
        self.assertEqual(self.s()['host'], 'server')

    def test_own_hook_pre_none_still_works(self):
        self.fire('SessionEnd', at='2026-06-01T00:00:00Z')
        self.assertEqual(self.s()['ended_at'], '2026-06-01T00:00:00Z')

    def test_laptop_hook_of_a_local_session_still_ends_it(self):
        with self.c:
            self.c.execute("UPDATE sessions SET host='local' WHERE session_id=?", (A,))
        self.fire('SessionEnd', pre={}, at='2026-06-01T00:00:00Z')
        self.assertEqual(self.s()['ended_at'], '2026-06-01T00:00:00Z')


class PolarNoticeTest(BusBase):
    def test_notice_used_by_tell_functions(self):
        sent = []
        with mock.patch.object(bus, 'polar_notice', lambda t, b, sid, silent=False: sent.append((t, b, sid, silent)) or True):
            with self.c:
                self.c.execute("UPDATE sessions SET title='Mi tarea' WHERE session_id=?", (A,))
            self.assertTrue(bus.tell_taken_over(self.c, A))
        self.assertEqual(len(sent), 1)
        self.assertIn('Mi tarea', sent[0][0])
        self.assertEqual(sent[0][2], A)
        self.assertTrue(sent[0][3])   # «Sigue en server» is information only
        ev = self.c.execute("SELECT data FROM events WHERE type='taken-over-told'").fetchone()
        self.assertEqual(json.loads(ev[0]), {'sent': True})

    def test_notice_command_has_silent_only_when_asked(self):
        cmds = []
        run = lambda argv, **kw: cmds.append(argv[-1]) or subprocess.CompletedProcess(argv, 0, '', '')
        with mock.patch.object(bus.subprocess, 'run', run):
            self.assertTrue(bus.polar_notice('t', 'b', A))
            self.assertTrue(bus.polar_notice('t', 'b', A, silent=True))
        self.assertNotIn(bus.POLAR_SILENT, cmds[0])
        self.assertTrue(cmds[1].endswith(' ' + bus.POLAR_SILENT))

    def test_notice_failure(self):
        with mock.patch.object(bus.subprocess, 'run', side_effect=OSError):
            self.assertFalse(bus.polar_notice('t', 'b', A))


class WakeHeartbeatTest(DeliverBase):
    def setUp(self):
        super().setUp()
        bus.wake = self.saved['wake']
        bus._wake_one = lambda c, sid, s, ws, *a, **k: (True, 'x')
        self.beats = []
        p = mock.patch.object(bus, 'laptop_heartbeat', lambda c: self.beats.append(1))
        p.start()
        self.addCleanup(p.stop)
        self.set_session(A, parked_at=minutes_ago(5), host='local', last_seen=minutes_ago(10))
        wid = self.add_wait(A, 'task', 't', met=True)

    def test_client_role_beats_per_session(self):
        os.environ['AGENT_BUS_ROLE'] = 'client'
        bus.wake(self.c)
        self.assertEqual(len(self.beats), 1)

    def test_other_roles_do_not(self):
        os.environ['AGENT_BUS_ROLE'] = 'local'
        bus.wake(self.c)
        self.assertEqual(self.beats, [])


class TrustFolderTest(unittest.TestCase):
    """A server resume pre-accepts Claude folder trust, only inside ~/work (owner, 2026-11-20)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.repo = os.path.join(self.home, 'work', 'portal')
        os.makedirs(self.repo)
        self.cfg = os.path.join(self.home, '.claude.json')

    def tearDown(self):
        self.tmp.cleanup()

    def read(self):
        with open(self.cfg) as fh:
            return json.load(fh)

    def test_trusts_a_work_repo_and_keeps_other_keys(self):
        with open(self.cfg, 'w') as fh:
            json.dump({'keep': 1, 'projects': {'/other': {'x': 1}}}, fh)
        self.assertEqual(bus.trust_folder(self.repo, self.home), 'trusted')
        d = self.read()
        self.assertTrue(d['projects'][self.repo]['hasTrustDialogAccepted'])
        self.assertEqual((d['keep'], d['projects']['/other']), (1, {'x': 1}))
        self.assertEqual(bus.trust_folder(self.repo, self.home), 'already')

    def test_refuses_outside_work(self):
        self.assertEqual(bus.trust_folder(self.home, self.home), 'refused')
        self.assertEqual(bus.trust_folder('/tmp', self.home), 'refused')
        self.assertEqual(bus.trust_folder(os.path.join(self.home, 'work-evil'), self.home), 'refused')
        self.assertFalse(os.path.exists(self.cfg))

    def test_refuses_a_symlink_out_of_work(self):
        out = os.path.join(self.home, 'secret')
        os.makedirs(out)
        link = os.path.join(self.home, 'work', 'link')
        os.symlink(out, link)
        self.assertEqual(bus.trust_folder(link, self.home), 'refused')

    def test_unreadable_config_is_not_rewritten(self):
        with open(self.cfg, 'w') as fh:
            fh.write('{broken')
        self.assertEqual(bus.trust_folder(self.repo, self.home), 'failed')
        with open(self.cfg) as fh:
            self.assertEqual(fh.read(), '{broken')

    def test_wake_remote_trusts_before_new_session(self):
        cmds = []
        saved = bus.ssh
        bus.ssh = lambda cmd, timeout=40: cmds.append(cmd) or ((1, '') if 'has-session' in cmd else (1, ''))
        try:
            s = {'cwd': self.repo}
            ok, msg = bus.wake_remote(None, 'a' * 36, s, [], ready_timeout=0, poll=0)
        finally:
            bus.ssh = saved
        self.assertFalse(ok)  # new-session failed in this fake: stops there
        trust = [i for i, c in enumerate(cmds) if ' trust ' in c]
        new = [i for i, c in enumerate(cmds) if 'new-session' in c]
        self.assertTrue(trust and new and trust[0] < new[0], cmds)


class InitiativeTest(BusBase):
    def test_back_to_active_clears_the_wait(self):
        """An initiative that moves again must not keep a wait from a card that is long closed."""
        bus.initiative_set(self.c, 'demo-wait', None, title='Demo', state='waiting', waits_on='Alex: card x')
        bus.initiative_set(self.c, 'demo-wait', A, state='active', text='It moved again.')
        row = self.c.execute("SELECT state, waits_on FROM initiatives WHERE slug='demo-wait'").fetchone()
        self.assertEqual((row['state'], row['waits_on']), ('active', ''))
        bus.initiative_set(self.c, 'demo-wait', A, text='No state change.', waits_on='the answer from Sam')
        row = self.c.execute("SELECT waits_on FROM initiatives WHERE slug='demo-wait'").fetchone()
        self.assertEqual(row['waits_on'], 'the answer from Sam')  # an explicit --waits is kept

    def test_set_refuses_an_empty_title(self):
        with self.c:
            pass
        code, _ = bus.initiative_set(self.c, 'demo-vacio', None, title='Demo')
        self.assertEqual(code, 0)
        code, msg = bus.initiative_set(self.c, 'demo-vacio', None, title='  ')
        self.assertEqual(code, 2, msg)
        row = self.c.execute("SELECT title FROM initiatives WHERE slug='demo-vacio'").fetchone()
        self.assertEqual(row['title'], 'Demo')

    def test_set_join_note_and_list(self):
        self.cli('initiative', 'set', 'demand', '--title', 'Demand planning', '--pmo', 'task-1')
        self.cli('initiative', 'join', 'demand', '--session', A)
        self.cli('initiative', 'note', 'demand', '--session', B, '--text', 'Campaña enviada. Falta medir.',
                 '--state', 'waiting', '--waits', 'replies from vendors', '--next', 'leer respuestas el lunes')
        rows = json.loads(self.cli('initiatives', '--json'))
        self.assertEqual(len(rows), 1)
        d = rows[0]
        self.assertEqual((d['title'], d['pmo'], d['state'], d['waits_on'], d['next_step']),
                         ('Demand planning', 'task-1', 'waiting', 'replies from vendors', 'leer respuestas el lunes'))
        self.assertEqual(sorted(x['session_id'] for x in d['live']), [A, B])
        self.assertEqual(d['last_note']['text'], 'Campaña enviada. Falta medir.')
        roles = {x['session_id']: x['role'] for x in bus.owners(self.c, 'initiative', 'demand')}
        self.assertEqual(roles, {A: 'also', B: 'also'})  # sharing an initiative demotes nobody

    def test_ended_and_parked_sessions_are_not_live(self):
        self.cli('initiative', 'set', 'demand', '--title', 'Demanda')
        self.cli('initiative', 'join', 'demand', '--session', A)
        self.cli('initiative', 'join', 'demand', '--session', B)
        with self.c:
            self.c.execute('UPDATE sessions SET ended_at=? WHERE session_id=?', (bus.now(), A))
            self.c.execute('UPDATE sessions SET parked_at=?, ended_at=? WHERE session_id=?', (bus.now(), bus.now(), B))
        d = bus.initiative_list(self.c)[0]
        self.assertEqual(d['live'], [])
        self.assertEqual([x['session_id'] for x in d['parked']], [B])

    def test_done_initiatives_leave_the_default_list(self):
        self.cli('initiative', 'set', 'ci', '--title', 'CI rápido', '--state', 'done')
        self.assertEqual(json.loads(self.cli('initiatives', '--json')), [])
        self.assertEqual(len(json.loads(self.cli('initiatives', '--json', '--all'))), 1)

    def test_bad_input_is_refused(self):
        def run(*a):
            return subprocess.run([sys.executable, str(PATH), *a], capture_output=True, text=True, timeout=5)
        self.assertEqual(run('initiative', 'set', 'Bad Slug', '--title', 'x').returncode, 2)
        self.assertEqual(run('initiative', 'set', 'nuevo').returncode, 2)  # a new one needs a title
        self.assertEqual(run('initiative', 'join', 'nope', '--session', A).returncode, 2)  # join does not create
        self.assertEqual(run('initiative', 'note', 'nope', '--session', A, '--text', 'x').returncode, 2)
        self.cli('initiative', 'set', 'demand', '--title', 'Demanda')
        self.assertEqual(run('initiative', 'set', 'demand', '--state', 'weird').returncode, 2)
        self.assertEqual(run('initiative', 'note', 'demand', '--session', A, '--text', '  ').returncode, 2)
        self.assertNotEqual(run('initiative', 'note', 'demand', '--session', A).returncode, 0)
        self.assertEqual(self.c.execute('SELECT count(*) FROM initiative_notes').fetchone()[0], 0)

    def test_park_wants_a_fresh_initiative_note(self):
        d = os.path.join(self.tmp.name, 'wt')
        subprocess.run(['git', 'init', '-q', d], check=True)
        with self.c:
            self.c.execute('UPDATE sessions SET cwd=?, pane=? WHERE session_id=?', (d, '', A))
        self.cli('initiative', 'set', 'demand', '--title', 'Demanda')
        self.cli('initiative', 'join', 'demand', '--session', A)
        code, msg = bus.park(self.c, A, ['person:Alex'], 'x', 'note')
        self.assertEqual(code, 2)
        self.assertIn('agent-bus initiative note demand', msg)
        self.assertIsNone(self.c.execute('SELECT parked_at FROM sessions WHERE session_id=?', (A,)).fetchone()[0])
        # a note from another session does not count; an old one does not count either
        self.cli('initiative', 'note', 'demand', '--session', B, '--text', 'otra sesión')
        with self.c:
            self.c.execute('INSERT INTO initiative_notes(slug, at, session_id, text) VALUES(?,?,?,?)',
                           ('demand', bus.ago(2), A, 'vieja'))
        self.assertEqual(bus.initiative_note_due(self.c, A), ['demand'])
        self.cli('initiative', 'note', 'demand', '--session', A, '--text', 'Hecho X. Falta Y. Sigue Alex.')
        self.assertEqual(bus.initiative_note_due(self.c, A), [])
        # a done initiative asks for nothing
        self.cli('initiative', 'set', 'ci', '--title', 'CI', '--state', 'done')
        with self.c:
            bus.claim(self.c, 'initiative', 'ci', A, 'also')
        self.assertEqual(bus.initiative_note_due(self.c, A), [])

    def test_handoff_keeps_the_initiative(self):
        d = os.path.join(self.tmp.name, 'wt')
        subprocess.run(['git', 'init', '-q', d], check=True)
        with self.c:
            self.c.execute('UPDATE sessions SET cwd=? WHERE session_id=?', (d, A))
        self.cli('initiative', 'set', 'demand', '--title', 'Demanda')
        self.cli('initiative', 'join', 'demand', '--session', A)
        code, msg = bus.handoff(self.c, A, 'x', 'note', new_sid='n' * 8, spawn=False)
        self.assertEqual(code, 0, msg)
        self.assertEqual([x['session_id'] for x in bus.initiative_list(self.c)[0]['live']], ['n' * 8])


class FreshWakeTest(BusBase):
    """A session parked longer than AGENT_BUS_FRESH_AFTER_S continues as a new session from its note."""
    repo = ParkTest.repo
    fake_herdr = ParkTest.fake_herdr
    msg = ParkTest.msg
    PATCHED = ('herdr', 'schedule_tab_close', 'polar_trust_card', 'ssh', 'open_cloud_tab', 'time')
    ENV = ('AGENT_BUS_FRESH_AFTER_S', 'AGENT_TASK_MODEL')

    def setUp(self):
        super().setUp()
        self.saved = {k: getattr(bus, k) for k in self.PATCHED}
        self.saved_fresh = bus.FRESH_AFTER_S
        self.saved_env = {k: os.environ.pop(k, None) for k in self.ENV}
        bus.FRESH_AFTER_S = 3600
        bus.ssh = lambda cmd, timeout=40: (1 if 'has-session' in cmd else 0, '')

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(bus, k, v)
        bus.FRESH_AFTER_S = self.saved_fresh
        for k, v in self.saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        super().tearDown()

    def parked(self, minutes, note='next: apply tariff', host=None):
        self.repo()
        self.calls = self.fake_herdr()
        with self.c:
            bus.claim(self.c, 'pr', 'app-db#1', A)
        code, msg = bus.park(self.c, A, ['person:Alex', 'polar:aaaaaaaaaaaa', 'time:+9h'], 'tariff reply', note or 'x')
        self.assertEqual(code, 0, msg)
        with self.c:
            self.c.execute('UPDATE sessions SET parked_at=?, host=COALESCE(?, host) WHERE session_id=?',
                           (minutes_ago(minutes), host, A))
            if not note:
                self.c.execute("UPDATE events SET data=json_set(data, '$.note', '') WHERE type='park'")
        bus.match_slack(self.c, [self.msg(text='approved')])

    def runs(self):
        return [c[3] for c in self.calls if c[:2] == ('pane', 'run')]

    def test_two_hours_starts_a_new_session_from_the_note(self):
        self.parked(120)
        res = bus.wake(self.c, 2)
        self.assertTrue(res[0].startswith('ok'), res)
        run = self.runs()[0]
        self.assertNotIn('--resume', run)
        self.assertIn('--session-id', run)
        self.assertIn('--model opus ', run)
        new = shlex.split(run)[shlex.split(run).index('--session-id') + 1]
        sent = shlex.split(run)[-1]
        self.assertIn('Alex wrote in DM', sent)
        self.assertIn('approved', sent)
        self.assertIn('You were parked for 2 h, so this is a new session by design. Your handoff note has the state.', sent)
        self.assertIn(f'agent-bus note {A}', sent)
        self.assertEqual({r['session_id'] for r in bus.owners(self.c, 'pr', 'app-db#1')}, {new})
        left = {(r['kind'], r['session_id']) for r in self.c.execute('SELECT * FROM waits WHERE met_at IS NULL '
                                                                     'AND cancelled_at IS NULL')}
        self.assertEqual(left, set())  # the other open waits of the park are cancelled, as after a resume
        old = self.c.execute('SELECT ended_at, parked_at FROM sessions WHERE session_id=?', (A,)).fetchone()
        self.assertIsNotNone(old['ended_at'])
        self.assertIsNone(old['parked_at'])
        ev = self.c.execute("SELECT data FROM events WHERE type='fresh_wake'").fetchall()
        self.assertEqual(len(ev), 1)
        d = json.loads(ev[0]['data'])
        self.assertEqual((d['old'], d['new']), (A, new))
        self.assertGreaterEqual(d['parked_s'], 7200)
        self.assertEqual(self.c.execute('SELECT handoff_from FROM sessions WHERE session_id=?', (new,)).fetchone()[0], A)
        self.assertEqual(self.c.execute("SELECT count(*) FROM waits WHERE woken_at IS NOT NULL").fetchone()[0], 1)
        self.assertEqual(self.cli('note', new).strip(), 'next: apply tariff')
        self.calls.clear()
        self.assertEqual(bus.wake(self.c, 2), [])  # no second session for one wake
        self.assertEqual(self.runs(), [])

    def test_ten_minutes_resumes(self):
        self.parked(10)
        bus.wake(self.c, 2)
        run = self.runs()[0]
        self.assertTrue(run.startswith(f'claude --resume {A} '))
        self.assertNotIn('--model', run)
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='fresh_wake'").fetchone()[0], 0)

    def test_two_hours_with_an_empty_note_resumes(self):
        self.parked(120, note='')
        bus.wake(self.c, 2)
        self.assertTrue(self.runs()[0].startswith(f'claude --resume {A} '))

    def test_switch_off_resumes(self):
        self.parked(120)
        bus.FRESH_AFTER_S = 0
        bus.wake(self.c, 2)
        run = self.runs()[0]
        self.assertTrue(run.startswith(f'claude --resume {A} '))
        self.assertNotIn('--model', run)

    def test_env_off_at_load(self):
        env = dict(os.environ, AGENT_BUS_FRESH_AFTER_S='0')
        out = subprocess.run([sys.executable, '-c', 'import importlib.machinery as m, sys; '
                              'mod = m.SourceFileLoader("b", sys.argv[1]).load_module(); print(mod.FRESH_AFTER_S)', str(PATH)],
                             capture_output=True, text=True, env=env).stdout
        self.assertEqual(out.strip(), '0')

    def test_failed_start_undoes_the_move_and_the_next_wake_tries_again(self):
        self.parked(120)
        bus.herdr = lambda *a, timeout=15: json.dumps({'result': {}})  # tab create gives no tab
        res = bus.wake(self.c, 2)
        self.assertTrue(res[0].startswith('failed'), res)
        self.assertEqual(bus.owners(self.c, 'pr', 'app-db#1')[0]['session_id'], A)
        row = self.c.execute('SELECT ended_at, parked_at FROM sessions WHERE session_id=?', (A,)).fetchone()
        self.assertIsNone(row['ended_at'])
        self.assertIsNotNone(row['parked_at'])
        self.assertEqual(self.c.execute("SELECT count(*) FROM waits WHERE woken_at IS NOT NULL").fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT count(*) FROM sessions").fetchone()[0], 2)  # A and B only
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='fresh_wake'").fetchone()[0], 0)

    def fake_ssh(self, live=lambda cmd: False):
        """ssh: has-session answers rc 0 when `live(cmd)`; capture-pane shows a ready screen (no 60 s poll)."""
        def fn(cmd, timeout=40):
            self.cmds.append(cmd)
            if 'has-session' in cmd:
                return (0, '') if live(cmd) else (1, '')
            return 0, ('❯ ' if 'capture-pane' in cmd else '')
        return fn

    def remote(self, minutes, note='next: apply tariff'):
        self.parked(minutes, note, host='server')
        with self.c:
            self.c.execute("UPDATE sessions SET tmux='task-waaaaaaaa', cwd='/Users/x/w' WHERE session_id=?", (A,))
        self.cmds = []
        bus.ssh = self.fake_ssh()
        bus.open_cloud_tab = lambda *a, **k: ('w4:t9', 'w4:p9')
        self.tmux = []
        self.saved_dt = bus.deliver_tmux
        bus.deliver_tmux = lambda name, text, stash=False: self.tmux.append((name, text)) or 0

    def test_remote_two_hours_starts_a_new_tmux_session(self):
        self.remote(120)
        try:
            res = bus.wake(self.c, 2)
        finally:
            bus.deliver_tmux = self.saved_dt
        self.assertTrue(res[0].startswith('ok'), res)
        new_cmd = [c for c in self.cmds if 'new-session' in c]
        self.assertEqual(len(new_cmd), 1)
        self.assertIn('--session-id', new_cmd[0])
        self.assertIn('--model opus ', new_cmd[0])
        self.assertNotIn('--resume', new_cmd[0])
        self.assertIn('You were parked for 2 h', new_cmd[0])
        self.assertIn('Alex wrote in DM', new_cmd[0])
        self.assertEqual(self.tmux, [])
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='fresh_wake'").fetchone()[0], 1)
        self.cmds.clear()
        self.assertEqual(bus.wake(self.c, 2), [])
        self.assertEqual([c for c in self.cmds if 'new-session' in c], [])

    def test_remote_ten_minutes_resumes(self):
        self.remote(10)
        try:
            bus.wake(self.c, 2)
        finally:
            bus.deliver_tmux = self.saved_dt
        new_cmd = [c for c in self.cmds if 'new-session' in c][0]
        self.assertIn(f'--resume {A}', new_cmd)
        self.assertNotIn('--model', new_cmd)

    def test_remote_empty_note_resumes(self):
        self.remote(120, note='')
        try:
            bus.wake(self.c, 2)
        finally:
            bus.deliver_tmux = self.saved_dt
        self.assertIn(f'--resume {A}', [c for c in self.cmds if 'new-session' in c][0])

    def test_remote_running_tmux_keeps_the_old_path(self):
        self.remote(120)
        bus.ssh = self.fake_ssh(lambda cmd: True)  # has-session: it runs
        try:
            bus.wake(self.c, 2)
        finally:
            bus.deliver_tmux = self.saved_dt
        self.assertEqual([c for c in self.cmds if 'new-session' in c], [])
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='fresh_wake'").fetchone()[0], 0)

    def test_task_model(self):
        self.assertEqual(bus.task_model('plain'), 'opus')
        self.assertEqual(bus.task_model(''), 'opus')
        for m in ('[difícil]', '[dificil]', '[fable]', '[DIFÍCIL]', 'x [Fable] y'):
            self.assertEqual(bus.task_model('task ' + m), 'fable', m)
        os.environ['AGENT_TASK_MODEL'] = 'sonnet'
        self.assertEqual(bus.task_model('[fable]'), 'sonnet')
        self.assertEqual(bus.model_flag('x'), '--model sonnet ')
        os.environ['AGENT_TASK_MODEL'] = ''
        self.assertEqual(bus.task_model('x'), '')
        self.assertEqual(bus.model_flag('x'), '')

    # -- durable fresh start (findings of 2026-11-20) --------------------------------------------------------------
    def agents(self, sids):
        """Herdr `agent list` answers with these session ids (None: the default fake, no answer)."""
        inner = bus.herdr

        def fake(*a, timeout=15):
            if a[:2] == ('agent', 'list') and sids is not None:
                self.calls.append(a)
                return json.dumps({'result': {'agents': [{'agent_session': {'value': s}, 'pane_id': 'w4:p9'}
                                                         for s in sids]}})
            return inner(*a, timeout=timeout)
        bus.herdr = fake

    def expire_lease(self):
        with self.c:
            self.c.execute('UPDATE waits SET waking_at=? WHERE waking_at IS NOT NULL', (minutes_ago(30),))

    def pending(self):
        rows = self.c.execute("SELECT data FROM events WHERE session_id=? AND type='fresh-pending'", (A,)).fetchall()
        return json.loads(rows[-1]['data'])['new'] if rows else None

    def lease_held(self):
        return self.c.execute('SELECT count(*) FROM waits WHERE waking_at IS NOT NULL').fetchone()[0] > 0

    def assert_not_moved(self, new):
        """Before a confirmed start nothing of the old session moved: claims, waits, the park."""
        self.assertEqual(bus.owners(self.c, 'pr', 'app-db#1')[0]['session_id'], A)
        row = self.c.execute('SELECT ended_at, parked_at FROM sessions WHERE session_id=?', (A,)).fetchone()
        self.assertIsNone(row['ended_at'])
        self.assertIsNotNone(row['parked_at'])
        self.assertEqual(self.c.execute('SELECT count(*) FROM waits WHERE woken_at IS NOT NULL OR cancelled_at IS NOT NULL '
                                        'OR session_id<>?', (A,)).fetchone()[0], 0)
        n = self.c.execute('SELECT handoff_from, ended_at FROM sessions WHERE session_id=?', (new,)).fetchone()
        self.assertIsNone(n['handoff_from'])  # successor() still gives the old session
        self.assertIsNotNone(n['ended_at'])
        self.assertEqual(bus.successor(self.c, A), A)
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='fresh_wake'").fetchone()[0], 0)

    def assert_moved(self, new):
        self.assertEqual({r['session_id'] for r in bus.owners(self.c, 'pr', 'app-db#1')}, {new})
        self.assertEqual(self.c.execute('SELECT handoff_from FROM sessions WHERE session_id=?', (new,)).fetchone()[0], A)
        self.assertIsNotNone(self.c.execute('SELECT ended_at FROM sessions WHERE session_id=?', (A,)).fetchone()[0])
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='fresh_wake'").fetchone()[0], 1)
        self.assertFalse(self.lease_held())
        self.assertEqual(self.c.execute("SELECT count(*) FROM sessions").fetchone()[0], 3)  # A, B, new: one new session

    def new_of(self, run):
        parts = shlex.split(run)
        return parts[parts.index('--session-id') + 1]

    def test_process_dies_before_the_start_next_wake_starts_the_same_id(self):
        self.parked(120)
        real = bus.handoff_spawn
        bus.handoff_spawn = lambda *a, **k: (_ for _ in ()).throw(SystemExit(-9))  # killed here
        try:
            with self.assertRaises(SystemExit):
                bus.wake(self.c, 2)
        finally:
            bus.handoff_spawn = real
        new = self.pending()
        self.assertIsNotNone(new)
        self.assert_not_moved(new)
        self.assertTrue(self.lease_held())
        self.assertEqual(bus.wake(self.c, 2), [])  # the lease holds: no start before WAKE_LEASE
        self.expire_lease()
        self.agents([])  # Herdr answers: the new id does not run
        res = bus.wake(self.c, 2)
        self.assertTrue(res[0].startswith('ok'), res)
        self.assertEqual([self.new_of(r) for r in self.runs()], [new])  # the SAME id
        self.assert_moved(new)

    def test_unknown_start_then_the_new_id_runs_finishes_without_a_second_start(self):
        self.parked(120)
        real = bus.handoff_spawn
        bus.handoff_spawn = lambda *a, **k: (False, 'not known if claude started in tab w4:t9 (herdr did not answer)')
        try:
            res = bus.wake(self.c, 2)
        finally:
            bus.handoff_spawn = real
        self.assertTrue(res[0].startswith('failed'), res)
        new = self.pending()
        self.assert_not_moved(new)
        self.assertTrue(self.lease_held())  # kept: a slow start is not started twice
        self.assertEqual(bus.wake(self.c, 2), [])
        self.expire_lease()
        self.agents([new])
        self.calls.clear()
        res = bus.wake(self.c, 2)
        self.assertTrue(res[0].startswith('ok'), res)
        self.assertEqual(self.runs(), [])  # no second claude
        self.assert_moved(new)
        self.assertEqual(self.c.execute('SELECT pane FROM sessions WHERE session_id=?', (new,)).fetchone()[0], 'w4:p9')
        self.assertEqual(self.cli('note', new).strip(), 'next: apply tariff')

    def test_unknown_start_then_not_running_starts_the_same_id(self):
        self.parked(120)
        real = bus.handoff_spawn
        bus.handoff_spawn = lambda *a, **k: (False, 'not known if claude started in tab w4:t9 (herdr did not answer)')
        try:
            bus.wake(self.c, 2)
        finally:
            bus.handoff_spawn = real
        new = self.pending()
        self.expire_lease()
        self.agents(None)  # Herdr does not answer: not known, nothing starts, nothing moves
        res = bus.wake(self.c, 2)
        self.assertTrue(res[0].startswith('failed'), res)
        self.assertIn('not known', res[0])
        self.assertEqual(self.runs(), [])
        self.assert_not_moved(new)
        self.agents([])
        res = bus.wake(self.c, 2)
        self.assertTrue(res[0].startswith('ok'), res)
        self.assertEqual([self.new_of(r) for r in self.runs()], [new])
        self.assert_moved(new)

    def test_exception_after_a_real_start_keeps_the_pending_id(self):
        self.parked(120)
        real = bus.handoff_spawn

        def spawn(*a, **k):
            real(*a, **k)
            raise RuntimeError('bus write after the start')
        bus.handoff_spawn = spawn
        try:
            res = bus.wake(self.c, 2)
        finally:
            bus.handoff_spawn = real
        self.assertTrue(res[0].startswith('failed'), res)
        new = self.pending()
        self.assertEqual([self.new_of(r) for r in self.runs()], [new])
        self.assertTrue(self.lease_held())
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='fresh_wake'").fetchone()[0], 0)
        self.expire_lease()
        self.calls.clear()
        res = bus.wake(self.c, 2)  # its pane is in the row: it ran
        self.assertTrue(res[0].startswith('ok'), res)
        self.assertEqual(self.runs(), [])
        self.assert_moved(new)

    def test_undo_that_raises_keeps_the_pending_id_and_the_next_wake_uses_it(self):
        self.parked(120)
        inner = bus.herdr
        bus.herdr = lambda *a, timeout=15: json.dumps({'result': {}}) if a[:2] == ('tab', 'create') else inner(*a, timeout=timeout)
        with self.c:
            self.c.execute("CREATE TRIGGER no_del BEFORE DELETE ON sessions BEGIN SELECT RAISE(ABORT, 'locked'); END")
        res = bus.wake(self.c, 2)
        self.assertTrue(res[0].startswith('failed'), res)
        new = self.pending()
        self.assertIsNotNone(new)  # the undo failed: the pending id stays
        self.assert_not_moved(new)
        self.assertFalse(self.lease_held())  # the start certainly failed: the next wake may try now
        with self.c:
            self.c.execute('DROP TRIGGER no_del')
        bus.herdr = inner
        self.agents([])
        res = bus.wake(self.c, 2)
        self.assertTrue(res[0].startswith('ok'), res)
        self.assertEqual([self.new_of(r) for r in self.runs()], [new])
        self.assert_moved(new)

    def test_failed_final_write_keeps_the_lease_and_the_next_wake_finishes(self):
        self.parked(120)
        real = bus.write_retry
        bus.write_retry = lambda c, fn, tries=4: False
        try:
            with self.assertRaises(bus.BusOffline) as cm:
                bus.wake(self.c, 2)
        finally:
            bus.write_retry = real
        self.assertTrue(cm.exception.after_start)
        new = self.pending()
        self.assertEqual(len(self.runs()), 1)
        self.assert_not_moved(new)
        self.assertTrue(self.lease_held())
        self.expire_lease()
        self.calls.clear()
        res = bus.wake(self.c, 2)
        self.assertTrue(res[0].startswith('ok'), res)
        self.assertEqual(self.runs(), [])
        self.assert_moved(new)

    def test_old_open_waits_are_cancelled_not_moved(self):
        self.parked(120)
        res = bus.wake(self.c, 2)
        self.assertTrue(res[0].startswith('ok'), res)
        new = self.new_of(self.runs()[0])
        rows = self.c.execute('SELECT kind, session_id, cancelled_at FROM waits WHERE met_at IS NULL').fetchall()
        self.assertEqual({(r['kind'], r['session_id']) for r in rows}, {('polar', A), ('time', A)})
        self.assertTrue(all(r['cancelled_at'] for r in rows))
        self.assertEqual(self.c.execute('SELECT count(*) FROM waits WHERE session_id=?', (new,)).fetchone()[0], 0)

    def set_pane(self, pane):
        with self.c:
            self.c.execute('UPDATE sessions SET pane=? WHERE session_id=?', (pane, A))

    def test_fresh_wake_does_not_close_a_reused_old_pane(self):
        self.parked(120)
        self.set_pane('w4:p5')  # stale: Herdr now shows this pane in another tab
        self.calls.clear()  # the park closed the tab of its own time
        inner = bus.herdr

        def fake(*a, timeout=15):
            if a[:2] == ('pane', 'list'):
                self.calls.append(a)
                return json.dumps({'result': {'panes': [{'pane_id': 'w4:p5', 'tab_id': 'w4:t5'},
                                                        {'pane_id': 'w4:p9', 'tab_id': 'w4:t9'}]}})
            return inner(*a, timeout=timeout)
        bus.herdr = fake
        res = bus.wake(self.c, 2)
        self.assertTrue(res[0].startswith('ok'), res)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual([c for c in self.calls if c[0] == 'close'], [])
        # a normal handoff (close_old default) still closes the old tab
        self.calls.clear()
        with self.c:
            bus.touch(self.c, NEW)
        ok, msg = bus.handoff_spawn(self.c, A, NEW, delay=0, poll=0)
        self.assertTrue(ok, msg)
        self.assertIn(('close', 'w4:t5'), self.calls)

    def test_fresh_finish_keeps_the_new_sessions_own_note(self):
        self.parked(120)
        real = bus.handoff_spawn
        bus.handoff_spawn = lambda *a, **k: (False, 'not known if claude started in tab w4:t9 (herdr did not answer)')
        try:
            bus.wake(self.c, 2)
        finally:
            bus.handoff_spawn = real
        new = self.pending()
        with self.c:  # the new session registered and parked with its own note
            bus.event(self.c, new, 'park', {'note': 'own note', 'waits': [], 'about': 'x'})
        parks = lambda: self.c.execute("SELECT count(*) FROM events WHERE session_id=? AND type='park'", (new,)).fetchone()[0]
        self.assertEqual(parks(), 1)
        bus.fresh_finish(self.c, A, new, [], 7200, 'found running')
        self.assertEqual(parks(), 1)
        self.assertEqual(bus.last_park(self.c, new)['note'], 'own note')
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='fresh_wake'").fetchone()[0], 1)

    def test_fresh_finish_writes_the_old_note_when_the_new_session_has_none(self):
        self.parked(120)
        real = bus.handoff_spawn
        bus.handoff_spawn = lambda *a, **k: (False, 'not known if claude started in tab w4:t9 (herdr did not answer)')
        try:
            bus.wake(self.c, 2)
        finally:
            bus.handoff_spawn = real
        new = self.pending()
        self.assertEqual(bus.last_park(self.c, new), {})
        bus.fresh_finish(self.c, A, new, [], 7200, 'found running')
        self.assertEqual(bus.last_park(self.c, new)['note'], 'next: apply tariff')
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE session_id=? AND type='park'",
                                        (new,)).fetchone()[0], 1)

    def test_server_does_not_restart_a_pending_laptop_row(self):
        self.parked(120)
        real = bus.handoff_spawn
        bus.handoff_spawn = lambda *a, **k: (False, 'not known if claude started in tab w4:t9 (herdr did not answer)')
        try:
            bus.wake(self.c, 2)
        finally:
            bus.handoff_spawn = real
        new = self.pending()
        self.assertTrue(self.lease_held())
        self.expire_lease()
        self.cmds = []
        bus.ssh = self.fake_ssh()
        with self.c:  # the server may wake it: staged
            self.c.execute('UPDATE sessions SET stage=? WHERE session_id=?', (json.dumps({'ok': True, 'seen': '9999'}), A))
        self.calls.clear()
        saved_role = os.environ.get('AGENT_BUS_ROLE')
        os.environ['AGENT_BUS_ROLE'] = 'server'
        try:
            res = bus.wake(self.c, 2)
        finally:
            os.environ.pop('AGENT_BUS_ROLE', None)
            if saved_role is not None:
                os.environ['AGENT_BUS_ROLE'] = saved_role
        self.assertTrue(res[0].startswith('failed'), res)
        self.assertIn('not known', res[0])
        self.assertEqual([c for c in self.cmds if 'new-session' in c or '--session-id' in c], [])
        self.assertEqual([c for c in self.calls if c[:2] == ('tab', 'create')], [])
        self.assertEqual(self.runs(), [])
        self.assert_not_moved(new)

    def test_bad_fresh_pending_event_does_not_break_wake(self):
        self.parked(120)
        bus.FRESH_AFTER_S = 0
        with self.c:
            bus.event(self.c, A, 'fresh-pending', {})
            self.c.execute("UPDATE events SET data='{not json' WHERE type='fresh-pending'")
        self.assertIsNone(bus.fresh_pending(self.c, A))
        with self.c:
            self.c.execute("UPDATE events SET data='[1]' WHERE type='fresh-pending'")
        self.assertIsNone(bus.fresh_pending(self.c, A))
        res = bus.wake(self.c, 2)
        self.assertTrue(res[0].startswith('ok'), res)
        self.assertTrue(self.runs()[0].startswith(f'claude --resume {A} '))

    def test_no_pane_in_new_tab_closes_the_tab(self):
        self.parked(120)
        inner = bus.herdr

        def fake(*a, timeout=15):
            if a[:2] == ('pane', 'list'):
                self.calls.append(a)
                return json.dumps({'result': {'panes': [{'pane_id': 'w4:p1', 'tab_id': 'w4:t1'}]}})
            return inner(*a, timeout=timeout)
        bus.herdr = fake
        with self.c:
            bus.touch(self.c, NEW)
        ok, msg = bus.handoff_spawn(self.c, A, NEW, delay=0, poll=0)
        self.assertEqual((ok, msg), (False, 'no pane in new tab'))
        self.assertIn(('tab', 'close', 'w4:t9'), self.calls)
        self.assertEqual(self.runs(), [])

    def test_server_staged_laptop_session_takes_the_fresh_path(self):
        self.remote(120)
        bus.deliver_tmux = self.saved_dt
        with self.c:
            self.c.execute("UPDATE sessions SET host='local', tmux=NULL, stage=? WHERE session_id=?",
                           (json.dumps({'ok': True, 'seen': '9999'}), A))
        moves = []

        def move_staged(c, sid, s):
            moves.append(sid)
            with c:
                c.execute("UPDATE sessions SET host=?, cwd='/Users/x/w' WHERE session_id=?", (bus.SERVER_LABEL, sid))
            return True, '/Users/x/w'
        saved_ms, saved_role = bus.move_staged, os.environ.get('AGENT_BUS_ROLE')
        bus.move_staged = move_staged
        os.environ['AGENT_BUS_ROLE'] = 'server'
        try:
            res = bus.wake(self.c, 2)
        finally:
            bus.move_staged = saved_ms
            os.environ.pop('AGENT_BUS_ROLE', None)
            if saved_role is not None:
                os.environ['AGENT_BUS_ROLE'] = saved_role
        self.assertTrue(res[0].startswith('ok'), res)
        self.assertEqual(moves, [A])
        new_cmd = [c for c in self.cmds if 'new-session' in c]
        self.assertEqual(len(new_cmd), 1)
        self.assertIn('--session-id', new_cmd[0])
        self.assertNotIn('--resume', new_cmd[0])
        self.assertIn('You now run on server', new_cmd[0])
        self.assertIn('You were parked for 2 h', new_cmd[0])
        self.assertTrue(any('agent-bus" trust' in c and '/Users/x/w' in c for c in self.cmds))
        new = self.pending()
        self.assertEqual(self.c.execute('SELECT host, cwd FROM sessions WHERE session_id=?', (new,)).fetchone()[:],
                         (bus.SERVER_LABEL, '/Users/x/w'))
        self.assert_moved(new)

    def test_remote_old_session_live_in_task_id8_takes_the_old_path(self):
        self.remote(120)
        live = f"'=task-{A[:8]}'"
        bus.ssh = self.fake_ssh(lambda cmd: live in cmd)
        try:
            bus.wake(self.c, 2)
        finally:
            bus.deliver_tmux = self.saved_dt
        self.assertTrue(any(live in c for c in self.cmds))
        self.assertFalse(any('--session-id' in c for c in self.cmds))
        self.assertIsNone(self.pending())
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='fresh_wake'").fetchone()[0], 0)

    def test_remote_old_session_live_in_its_row_tmux_takes_the_old_path(self):
        self.remote(120)
        with self.c:
            self.c.execute("UPDATE sessions SET tmux='owner-x' WHERE session_id=?", (A,))
        bus.ssh = self.fake_ssh(lambda cmd: "'=owner-x'" in cmd)
        try:
            bus.wake(self.c, 2)
        finally:
            bus.deliver_tmux = self.saved_dt
        self.assertFalse(any('--session-id' in c for c in self.cmds))
        self.assertIsNone(self.pending())

    def test_recorded_model_survives_a_fresh_wake(self):
        self.parked(120)
        self.cli('register', '--session', A, '--model', 'fable')
        res = bus.wake(self.c, 2)
        self.assertTrue(res[0].startswith('ok'), res)
        run = self.runs()[0]
        self.assertIn('--model fable ', run)
        self.assertEqual(bus.recorded_model(self.c, self.new_of(run)), 'fable')
        os.environ['AGENT_TASK_MODEL'] = 'sonnet'  # the env still wins over the record
        self.assertEqual(bus.successor_model(self.c, A, 'x'), ('sonnet', 'fable'))

    def test_no_record_keeps_the_rule(self):
        self.parked(120)
        bus.wake(self.c, 2)
        run = self.runs()[0]
        self.assertIn('--model opus ', run)
        self.assertEqual(bus.recorded_model(self.c, self.new_of(run)), 'opus')

    def test_register_model_records_once(self):
        self.cli('register', '--session', A, '--model', 'fable')
        self.cli('register', '--session', A, '--model', 'fable')
        self.cli('register', '--session', A)
        self.assertEqual(self.c.execute("SELECT count(*) FROM events WHERE type='model'").fetchone()[0], 1)
        self.assertEqual(bus.recorded_model(self.c, A), 'fable')





# ---- Claude Code start questions («Resume this conversation?») ----------------
# The texts and the option labels below are from the Claude Code 2.1.330 and 2.1.331 code. No real screen was
# captured: the question needs server flags, an expired prompt cache and a cost share that a test cannot set. So
# these screens are built from the code's texts, and the layout (indent, numbers, the «❯» cursor) is an assumption.
COLD_HEAD = [' Resume this conversation?', '',
             ' This conversation has been inactive for 57m and is 412k tokens long. Resuming it will use about 9% of',
             ' your 5-hour usage limit.']
COLD_330 = [('Resume', ''),
            ('Start a new conversation', 'Claude can look this one up if you refer to it later'),
            ('Resume from summary', 'Claude writes a summary now, and you continue from it')]
COLD_331 = COLD_330[:2]
OLD_HEAD = [' This session is 3h 2m old and 412k tokens.', '',
            ' Resuming the full session uses a lot of your usage limits. Resuming from a summary uses less.']
OLD_OPTS = [('Resume from summary (instant, recommended)', ''), ('Resume full session as-is', ''),
            ("Don't ask again", '')]
INPUT_BOX = '─' * 40 + '\n❯ \n' + '─' * 40 + '\n  ? for shortcuts'


PERMISSION_MENU = '\n'.join(['', '', '─' * 40, ' Bash command', '', '   rm -rf build', '   Remove the build folder', '',
                             ' Do you want to proceed?', ' ❯ 1. Yes', "   2. Yes, and don't ask again for rm commands",
                             '   3. No, and tell Claude what to do differently (esc)'])


class QuestionScreen:
    """A start question as the bus reads it: read() gives the screen."""

    def __init__(self, head, options, cursor=0, numbered=True, glyph='❯', below=''):
        self.head, self.options, self.cursor, self.numbered = head, options, cursor, numbered
        self.glyph, self.below = glyph, below

    def read(self):
        rows = []
        for i, (label, desc) in enumerate(self.options):
            rows.append(f" {self.glyph if i == self.cursor else ' '} {f'{i + 1}. ' if self.numbered else ''}{label}")
            if desc:
                rows.append(f'      {desc}')
        return '\n'.join(self.head + [''] + rows + ['', ' Enter to confirm · Esc to cancel']) + self.below


class Seq:
    """Screens in order: each read() gives the next one, and the last one stays."""

    def __init__(self, *screens):
        self.screens = list(screens)

    def read(self):
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]


class QuestionHelpers:
    """The bus presses no key at a question. `keys` collects every send-keys call that a fake Herdr or ssh gets."""

    @property
    def keys(self):
        return self.__dict__.setdefault('_keys', [])

    def events(self, typ):
        return self.c.execute('SELECT * FROM events WHERE type=?', (typ,)).fetchall()

    def age(self, typ, minutes):
        """Event times have a resolution of 1 s and the compare is strict: set the time by hand."""
        with self.c:
            self.c.execute('UPDATE events SET at=? WHERE type=?', (minutes_ago(minutes), typ))

    def add_event(self, typ, minutes, sid=A):
        with self.c:
            self.c.execute('INSERT INTO events VALUES(?,?,?,?)', (minutes_ago(minutes), sid, typ, '{}'))

    def screen_on_pane(self, scr, procs=None):
        """`procs`: the foreground processes of the pane for `herdr pane process-info`. None: this Herdr does not
        know that command, and pane_claude gives None."""
        base = bus.herdr
        def fake(*a, timeout=15):
            if a[:2] == ('pane', 'read'):
                return scr.read()
            if a[:2] == ('pane', 'send-keys'):
                self.keys.append(a)
                return ''
            if a[:2] == ('pane', 'process-info') and procs is not None:
                return proc_info(procs)
            return base(*a, timeout=timeout)
        bus.herdr = fake

    def remote_wake(self, scr, ready_timeout=5, existed=False):
        self.set_session(parked_at=minutes_ago(5), host='server', tmux='task-wa', cwd=self.tmp.name)
        if not self.c.execute('SELECT 1 FROM waits').fetchone():
            self.add_wait(A, 'task', 'one', met=True)
        ws = self.c.execute('SELECT * FROM waits').fetchall()
        marked = []
        started = [existed]
        def ssh(cmd, timeout=40):
            if 'has-session' in cmd:
                return (0 if started[0] else 1), ''
            if 'new-session' in cmd:
                started[0] = True
            if 'capture-pane' in cmd:
                return 0, scr.read()
            if 'send-keys' in cmd:
                self.keys.append(cmd)
            return 0, ''
        saved = (bus.open_cloud_tab, bus.mark_woken, bus.time.sleep, bus.ssh)
        self.tabs = getattr(self, 'tabs', [])
        bus.open_cloud_tab = lambda *a, **k: self.tabs.append(a) or ('w4:t9', 'w4:p9')
        bus.mark_woken = lambda c, sid, rows, sets, ev, t0=None: marked.append([r['id'] for r in rows])
        bus.time.sleep = lambda n: None
        bus.ssh = ssh
        try:
            s = self.c.execute('SELECT * FROM sessions WHERE session_id=?', (A,)).fetchone()
            res = bus.wake_remote(self.c, A, s, ws, ready_timeout=ready_timeout, poll=2)
        finally:
            bus.open_cloud_tab, bus.mark_woken, bus.time.sleep, bus.ssh = saved
        return res, marked

def proc_info(names):
    """The answer of `herdr pane process-info` for these foreground processes (its shape on 2026-11-23: the name of
    a Claude Code process is its version)."""
    return json.dumps({'result': {'process_info': {
        'foreground_process_group_id': 70, 'pane_id': 'w4:p1', 'shell_pid': 60,
        'foreground_processes': [{'argv': [n, '--resume', 'x'], 'argv0': n, 'cmdline': n + ' --resume x', 'cwd': '/x',
                                  'name': '2.1.330' if n == 'claude' else n, 'pid': 71 + i}
                                 for i, n in enumerate(names)]}}})


def polar_stub(calls):
    from unittest import mock
    return mock.patch.object(bus.subprocess, 'run',
                             lambda cmd, *a, **k: calls.append(cmd) or mock.Mock(returncode=0, stdout='', stderr=''))


class StartQuestionTest(BusBase, QuestionHelpers):
    repo, fake_herdr, park, msg = ParkTest.repo, ParkTest.fake_herdr, ParkTest.park, ParkTest.msg

    def setUp(self):
        super().setUp()
        saved = (bus.herdr, bus.schedule_tab_close, bus.time)
        self.addCleanup(lambda: [setattr(bus, k, v) for k, v in zip(('herdr', 'schedule_tab_close', 'time'), saved)])

    def test_both_questions_are_seen_and_are_not_ready(self):
        for head, opts in ((COLD_HEAD, COLD_330), (COLD_HEAD, COLD_331), (OLD_HEAD, OLD_OPTS)):
            out = QuestionScreen(head, opts).read()
            self.assertTrue(bus.start_question(out), out)
            self.assertEqual(bus.question_kind(out), 'question')
            self.assertFalse(bus.claude_ready(out), out)  # the cursor «❯» alone made it «ready» on 2026-11-23
        self.assertIsNone(bus.start_question(INPUT_BOX))
        self.assertIsNone(bus.question_kind(INPUT_BOX))
        self.assertTrue(bus.claude_ready(INPUT_BOX))
        self.assertTrue(bus.claude_ready('❯ '))

    def test_a_question_whose_long_line_wraps_is_seen(self):
        head = [' Resuming the full session uses a lot of', ' your usage limits. Resuming from a summary uses less.']
        self.assertTrue(bus.start_question(QuestionScreen(head, OLD_OPTS).read()))

    def test_a_quoted_question_is_not_an_open_question(self):
        quoted = QuestionScreen(COLD_HEAD, COLD_330).read()
        self.assertIsNone(bus.start_question(quoted + '\n\n' + INPUT_BOX))  # a resumed session that shows old text
        self.assertTrue(bus.claude_ready(quoted + '\n\n' + INPUT_BOX))
        self.assertIsNone(bus.start_question('The screen said «Resume this conversation?» and then waited.'))
        self.assertIsNone(bus.start_question('Resume'))

    def test_a_quoted_question_above_another_menu_is_not_an_open_question(self):
        for head, opts in ((COLD_HEAD, COLD_330), (OLD_HEAD, OLD_OPTS)):
            out = QuestionScreen(head, opts, below=PERMISSION_MENU).read()
            self.assertIsNone(bus.start_question(out), out)  # the cursor of the menu under it is not its cursor
            self.assertIsNone(bus.question_kind(out))
        self.assertIsNone(bus.start_question(QuestionScreen(COLD_HEAD, COLD_330, below='\n\n❯ ').read()))  # a prompt

    def test_an_open_question_with_more_text_under_its_rows_is_still_a_question(self):
        # This screen was never read from a real one. A question that the bus does not see reads as a started Claude
        # (the cursor of its rows is «❯»): the waits count as woken and the owner gets no card.
        for below in ('\n one\n two', '\n one\n two\n three\n four\n five\n six',
                      '\n Your unsent message will still be there after you answer.\n\n status ❯ main'):
            out = QuestionScreen(COLD_HEAD, COLD_330, below=below).read()
            with self.subTest(below=below):
                self.assertEqual(bus.question_kind(out), 'question')
                self.assertFalse(bus.claude_ready(out))

    def test_rows_have_the_labels_and_the_cursor(self):
        rows = bus.question_rows(QuestionScreen(COLD_HEAD, COLD_330, cursor=1).read())
        self.assertEqual(rows, [('Resume', False), ('Start a new conversation', True), ('Resume from summary', False)])
        rows = bus.question_rows(QuestionScreen(OLD_HEAD, OLD_OPTS, numbered=False).read())
        self.assertEqual(rows, [('Resume from summary', True), ('Resume full session as-is', False),
                                ("Don't ask again", False)])

    def test_a_cursor_that_is_not_seen_is_still_a_question(self):
        scr = QuestionScreen(COLD_HEAD, COLD_330, glyph='>')
        self.assertTrue(bus.start_question(scr.read()))  # the owner gets a card

    def screens_in_pane(self, *screens, cmd=None):
        """Run start_in_pane against these screens in order. Returns (result, send-keys calls)."""
        import types
        seq, keys = Seq(*screens), []
        def fake(*a, timeout=15):
            if a[:2] == ('pane', 'read'):
                return seq.read()
            if a[:2] == ('pane', 'send-keys'):
                keys.append(a)
            return ''
        bus.herdr = fake
        bus.time = types.SimpleNamespace(time=bus.time.time, sleep=lambda s: None)
        got = bus.start_in_pane('w1:p1', cmd or f'claude --resume {A} x', f'--resume {A}', ready_timeout=5, poll=0)
        return got, keys

    def test_start_in_pane_returns_question_on_two_looks_and_presses_no_key(self):
        got, keys = self.screens_in_pane('$ ', QuestionScreen(COLD_HEAD, COLD_330).read())
        self.assertEqual(got, 'question')
        self.assertEqual(keys, [])

    def test_start_in_pane_returns_trust_on_two_looks_and_presses_no_key(self):
        got, keys = self.screens_in_pane('$ ', TRUST_SCREEN)
        self.assertEqual(got, 'trust')
        self.assertEqual(keys, [])

    def test_start_in_pane_is_ready_when_the_question_is_gone_on_the_second_look(self):
        got, keys = self.screens_in_pane('$ ', QuestionScreen(COLD_HEAD, COLD_330).read(), INPUT_BOX)
        self.assertEqual(got, 'ready')
        self.assertEqual(keys, [])

    def test_the_echo_of_the_command_is_not_a_question(self):
        text = QuestionScreen(COLD_HEAD, COLD_330).read()  # a wake text that quotes the question
        cmd = f"claude --resume {A} '{text}'"
        got, keys = self.screens_in_pane('$ ', '$ ' + cmd, '$ ' + cmd, INPUT_BOX, cmd=cmd)
        self.assertEqual(got, 'ready')
        self.assertEqual(keys, [])  # no arrow key and no Enter into the shell

    def test_a_multi_line_echo_that_holds_the_trust_rows_is_not_the_trust_question(self):
        cmd = f"claude --resume {A} '{TRUST_SCREEN}'"  # the wake text quotes the whole screen
        got, keys = self.screens_in_pane('$ ', '$ ' + cmd, '$ ' + cmd, INPUT_BOX, cmd=cmd)
        self.assertEqual(got, 'ready')
        self.assertEqual(keys, [])

    def wake_at(self, scr):
        calls = self.park(['person:Alex'])
        bus.match_slack(self.c, [self.msg(text='approved')])
        base = bus.herdr
        def fake(*a, timeout=15):
            if a[:2] == ('pane', 'read'):
                return scr.read()
            if a[:2] == ('pane', 'send-keys'):
                calls.append(a)
                self.keys.append(a)
                return ''
            return base(*a, timeout=timeout)
        bus.herdr = fake
        polar = []
        with polar_stub(polar):
            res = bus.wake(self.c, 2)
        return calls, polar, res

    def woken(self):
        return self.c.execute('SELECT woken_at FROM waits WHERE session_id=?', (A,)).fetchone()[0]

    def test_wake_at_a_start_question_sends_one_card_and_presses_no_key(self):
        scr = QuestionScreen(COLD_HEAD, COLD_330)
        calls, polar, res = self.wake_at(scr)
        self.assertEqual(self.keys, [])
        self.assertEqual([c for c in calls if c[:2] == ('pane', 'send-text')], [])  # the text is the prompt argument
        self.assertEqual(len(polar), 1)
        card = polar[0]
        self.assertEqual([os.path.basename(card[0])] + card[1:3], ['polar', 'ask', 'decision'])
        self.assertTrue(card[card.index('--title') + 1].startswith('Contesta la pregunta de Claude en la pestaña '))
        self.assertLessEqual(len(card[card.index('--context') + 1]), 280)
        self.assertIsNotNone(self.woken(), res)  # it runs and keeps the wake text; the owner's answer starts it
        with polar_stub(polar):
            self.assertEqual(bus.tell_start_question(self.c, A, 'la pestaña 9', 'tariff reply'), 'had')
        self.assertEqual(len(polar), 1)  # one card per session and question

    def test_wake_with_a_cursor_that_is_not_seen_sends_a_card(self):
        calls, polar, res = self.wake_at(QuestionScreen(COLD_HEAD, COLD_330, glyph='>'))
        self.assertEqual(self.keys, [])
        self.assertEqual(len(polar), 1)

    def test_wake_at_a_start_question_records_its_card_after_the_wake_and_a_delivery_sends_no_second(self):
        calls, polar, res = self.wake_at(QuestionScreen(COLD_HEAD, COLD_330))
        card, wake = self.events('start-question-card'), self.events('wake')
        self.assertEqual((len(card), len(wake)), (1, 1))
        self.assertGreaterEqual(card[0]['at'], wake[0]['at'])  # not earlier: the wake does not end its own card
        with polar_stub(polar):
            got = bus.question_on_place(self.c, A, 'local', 'w4:p1', 'tariff reply')
        self.assertEqual(got, ('question', 'had'))
        self.assertEqual(len(polar), 1)

    def test_wake_at_the_trust_question_records_its_card_after_the_wake_and_a_delivery_sends_no_second(self):
        calls, polar, res = self.wake_at(StillScreen(TRUST_SCREEN))
        self.assertEqual(len(polar), 1)
        self.assertTrue(polar[0][polar[0].index('--title') + 1].startswith('Confirma la carpeta en la pestaña '))
        self.assertEqual(self.keys, [])
        card, wake = self.events('trust-question-card'), self.events('wake')
        self.assertEqual((len(card), len(wake)), (1, 1))
        self.assertGreaterEqual(card[0]['at'], wake[0]['at'])
        with polar_stub(polar):
            got = bus.question_on_place(self.c, A, 'local', 'w4:p1', 'tariff reply')
        self.assertEqual(got, ('trust', 'had'))
        self.assertEqual(len(polar), 1)

    def test_a_refused_card_does_not_stop_the_wake(self):
        from unittest import mock
        with mock.patch.object(bus.subprocess, 'run', side_effect=OSError('no polar')):
            bus.polar_question_card('la pestaña 3', 'x')  # never raises

    def test_a_session_that_quotes_the_trust_question_is_ready(self):
        scr = QuestionScreen([], [])
        scr.read = lambda: TRUST_SCREEN + '\n' + INPUT_BOX  # its last messages show the text; the box is there
        calls, polar, res = self.wake_at(scr)
        self.assertEqual(polar, [])
        self.assertIsNotNone(self.woken(), res)


class StartQuestionDeliverTest(DeliverBase, QuestionHelpers):
    def queued_at_a_question(self, scr, text='the text', title='tariff work'):
        self.set_session(host='local', pane='w4:p1', title=title)
        self.rc = 5
        self.screen_on_pane(scr)
        return bus.deliver(self.c, A, text)

    def test_no_box_at_a_start_question_is_one_card_and_no_key(self):
        polar = []
        with polar_stub(polar):
            out = self.queued_at_a_question(QuestionScreen(OLD_HEAD, OLD_OPTS))
            bus.pull(self.c, 5, 60)
        self.assertIn('a start question holds it; the owner has a Polar card for it', out)
        self.assertIn('next try soon', out)
        self.assertEqual(self.keys, [])
        self.assertEqual(len(polar), 1)
        self.assertIn('tariff work', polar[0][polar[0].index('--title') + 1])
        self.assertIsNone(self.rows()[0]['done_at'])
        self.assertEqual(self.inbox, [])
        self.assertEqual(len(self.events('start-question-card')), 1)  # one event, not one on every pull

    def test_a_second_pull_sends_no_second_card(self):
        polar = []
        with polar_stub(polar):
            self.queued_at_a_question(QuestionScreen(COLD_HEAD, COLD_330))
            bus.pull(self.c, 5, 60)
            bus.pull(self.c, 5, 60)
        self.assertEqual(len(polar), 1)

    def test_a_second_question_after_a_typed_delivery_gets_a_new_card(self):
        polar = []
        with polar_stub(polar):
            self.queued_at_a_question(QuestionScreen(COLD_HEAD, COLD_330), 'one')
            self.age('start-question-card', 10)
            self.rc = 0
            bus.pull(self.c, 5, 60)  # the box was free: 'one' is typed
            self.assertIsNotNone(self.rows()[0]['done_at'])
            self.rc = 5
            out = bus.deliver(self.c, A, 'two')  # a new message meets the question again
        self.assertIn('the owner has a Polar card for it', out)
        self.assertEqual(len(polar), 2)
        self.assertEqual(self.keys, [])

    def test_a_second_question_after_a_wake_gets_a_new_card(self):
        polar = []
        with polar_stub(polar):
            self.queued_at_a_question(QuestionScreen(COLD_HEAD, COLD_330))
            self.age('start-question-card', 10)
            self.add_event('wake', 5)  # the session ran again after card 1
            bus.pull(self.c, 5, 60)
        self.assertEqual(len(polar), 2)

    def test_the_same_question_with_nothing_in_between_keeps_one_card(self):
        polar = []
        with polar_stub(polar):
            self.queued_at_a_question(QuestionScreen(COLD_HEAD, COLD_330))
            self.age('start-question-card', 10)
            bus.pull(self.c, 5, 60)
        self.assertEqual(len(polar), 1)

    def test_a_delivery_with_its_resume_text_does_not_end_the_question(self):
        polar = []
        with polar_stub(polar):  # without it, each run sent a real card to the owner
            self.queued_at_a_question(QuestionScreen(COLD_HEAD, COLD_330))
        self.assertEqual(len(polar), 1)
        with self.c:
            self.c.execute('UPDATE deliveries SET done_at=?, outcome=?', (bus.now(), f'delivered to {A[:8]} with its resume text (task:x)'))
        self.assertFalse(bus.passed_since(self.c, A, minutes_ago(10)))
        with self.c:
            self.c.execute('UPDATE deliveries SET outcome=?', (f'left in the input box of {A[:8]} in w4:p1 (Enter did not submit it)',))
        self.assertTrue(bus.passed_since(self.c, A, minutes_ago(10)))

    def test_a_card_that_polar_refuses_is_said_in_the_outcome_and_the_router_knows_once(self):
        polar = []
        with polar_rec(polar, [2, 2]):  # the first card (two tries, the second in plain words) is refused
            out = self.queued_at_a_question(QuestionScreen(COLD_HEAD, COLD_330))
            self.assertIn('no Polar card went out for it', out)
            self.assertEqual(len(polar), 2)
            with self.c:
                self.c.execute('UPDATE deliveries SET created_at=?', (minutes_ago(45),))
            bus.pull(self.c, 5, 60)
            bus.pull(self.c, 5, 60)
            self.assertEqual(len(polar), 2)  # too soon to try the card again
            self.assertEqual(len(self.inbox), 1)
            self.assertIn('a start question holds it; no Polar card went out for it', self.inbox[0])
            self.assertEqual(len(self.events('delivery-no-box')), 1)
            self.assertIsNone(self.rows()[0]['done_at'])
            self.age('start-question-card', 11)  # CARD_RETRY_S has passed
            bus.pull(self.c, 5, 60)
        self.assertEqual(len(polar), 3)  # tried again, and Polar took it
        self.assertEqual(len(self.inbox), 1)
        self.assertEqual(len(self.events('start-question-card')), 2)
        self.assertEqual(self.keys, [])

    def test_an_old_row_at_a_start_question_stays_queued_and_the_router_knows_once(self):
        polar = []
        with polar_stub(polar):
            self.queued_at_a_question(QuestionScreen(COLD_HEAD, COLD_331))
            with self.c:
                self.c.execute('UPDATE deliveries SET created_at=?', (minutes_ago(45),))
            out = bus.pull(self.c, 5, 60)
            bus.pull(self.c, 5, 60)
        self.assertIn('the router knows', out[0])
        self.assertIsNone(self.rows()[0]['done_at'])
        self.assertEqual(len(polar), 1)
        self.assertEqual(len(self.inbox), 1)
        self.assertIn('a start question holds it; the owner has a Polar card for it', self.inbox[0])
        self.assertIn('the text', self.inbox[0])
        self.assertEqual(len(self.events('delivery-no-box')), 1)
        self.assertEqual(self.keys, [])

    def test_an_old_row_at_a_question_with_no_claude_in_herdr_is_not_closed(self):
        from unittest import mock
        self.agents = []  # Herdr does not list the session: it may not have started yet
        with polar_stub([]):
            self.queued_at_a_question(QuestionScreen(COLD_HEAD, COLD_330))
            with self.c:
                self.c.execute('UPDATE deliveries SET created_at=?', (minutes_ago(45),))
            with mock.patch.object(bus.time, 'sleep', lambda n: None):
                bus.pull(self.c, 5, 60)
        self.assertIsNone(self.rows()[0]['done_at'])
        self.assertEqual(len(self.inbox), 1)
        self.assertNotIn('no Claude runs there', self.inbox[0])

    def test_an_old_row_in_a_live_place_stays_queued_and_the_router_knows_once(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 5
        bus.deliver(self.c, A, 'the text')
        did = self.rows()[0]['id']
        with self.c:
            self.c.execute('UPDATE deliveries SET created_at=? WHERE id=?', (minutes_ago(45), did))
        out = bus.pull(self.c, 5, 60)
        bus.pull(self.c, 5, 60)
        self.assertIn('the router knows', out[0])
        self.assertEqual(len(self.inbox), 1)
        self.assertIn('stays queued', self.inbox[0])
        self.assertIn('the text', self.inbox[0])
        self.assertIsNone(self.row(did)['done_at'])
        self.rc = 0  # the menu went away
        bus.pull(self.c, 5, 60)
        self.assertIsNotNone(self.row(did)['done_at'])
        self.assertEqual(self.pane_calls[-1], ('w4:p1', 'the text'))
        self.assertEqual(len(self.inbox), 1)

    def test_a_new_row_with_no_known_question_keeps_the_old_words(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 5
        out = bus.deliver(self.c, A, 'the text')
        self.assertIn('(a menu or a question?); next try soon', out)

    def test_an_old_row_in_a_live_tmux_stays_queued(self):
        self.set_session(host='server', tmux='task-wa')
        self.rc_tmux = 5
        bus.deliver(self.c, A, 'the text')
        with self.c:
            self.c.execute('UPDATE deliveries SET created_at=?', (minutes_ago(45),))
        bus.pull(self.c, 5, 60)
        self.assertIsNone(self.rows()[0]['done_at'])
        self.assertEqual(len(self.inbox), 1)

    def test_a_quoted_question_above_a_permission_menu_gets_no_key_and_no_card(self):
        polar = []
        with polar_stub(polar):
            out = self.queued_at_a_question(QuestionScreen(COLD_HEAD, COLD_330, below=PERMISSION_MENU))
            bus.pull(self.c, 5, 60)
        self.assertEqual(self.keys, [])  # an Enter here would confirm the permission
        self.assertEqual(polar, [])      # no start question is open: a card would send the owner to the wrong thing
        self.assertIn('(a menu or a question?); next try soon', out)
        self.assertEqual(self.events('start-question-card'), [])
        self.assertIsNone(self.rows()[0]['done_at'])

    def test_an_old_row_for_a_pane_where_no_claude_runs_goes_to_the_router(self):
        from unittest import mock
        self.set_session(host='local', pane='w4:p1')
        self.rc, self.agents = 5, []  # the pane lives, but Claude ended and left a shell
        bus.deliver(self.c, A, 'the text')
        did = self.rows()[0]['id']
        self.assertIsNone(self.row(did)['done_at'])  # a new row: Claude may still start
        with self.c:
            self.c.execute('UPDATE deliveries SET created_at=? WHERE id=?', (minutes_ago(45), did))
        with mock.patch.object(bus.time, 'sleep', lambda n: None):
            bus.pull(self.c, 5, 60)
        self.assertIsNotNone(self.row(did)['done_at'])
        self.assertEqual(len(self.inbox), 1)
        self.assertIn('no Claude runs there', self.inbox[0])
        self.assertIn('the text', self.inbox[0])

    def test_an_old_if_live_row_goes_to_the_router(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 5
        bus.deliver(self.c, A, 'the text', live_only=True)
        did = self.rows()[0]['id']
        self.assertIsNone(self.row(did)['done_at'])
        with self.c:
            self.c.execute('UPDATE deliveries SET created_at=? WHERE id=?', (minutes_ago(45), did))
        bus.pull(self.c, 5, 60)
        self.assertIsNotNone(self.row(did)['done_at'])  # not kept for days and not dropped in silence
        self.assertEqual(len(self.inbox), 1)
        self.assertIn('--if-live', self.inbox[0])
        self.assertIn('the text', self.inbox[0])

    def test_an_old_if_live_row_at_a_start_question_goes_to_the_router(self):
        self.set_session(host='local', pane='w4:p1')
        self.rc = 5
        self.screen_on_pane(QuestionScreen(COLD_HEAD, COLD_331))
        with polar_stub([]):
            bus.deliver(self.c, A, 'the text', live_only=True)
            with self.c:
                self.c.execute('UPDATE deliveries SET created_at=?', (minutes_ago(45),))
            bus.pull(self.c, 5, 60)
        self.assertIsNotNone(self.rows()[0]['done_at'])
        self.assertEqual(len(self.inbox), 1)

    def test_a_pull_takes_a_new_message_before_the_stuck_rows(self):
        sids = [f'{i:08d}-0000-4000-8000-000000000000' for i in range(1, 7)]
        self.panes = [f'w4:p{i}' for i in range(1, 7)]
        for i, sid in enumerate(sids, 1):
            with self.c:
                bus.touch(self.c, sid)
            self.set_session(sid, host='local', pane=f'w4:p{i}')
        tried = []
        bus.deliver_pane = lambda pane, text, stash=False: tried.append(text) or (0 if text == 'new' else 5)
        ids = [bus.delivery_add(self.c, sid, f'stuck {i}')[0] for i, sid in enumerate(sids[:5], 1)]
        bus.delivery_add(self.c, sids[0], 'second of 1')
        new = bus.delivery_add(self.c, sids[5], 'new')[0]
        with self.c:
            for n, did in zip((30, 29, 28, 27, 26), ids):
                self.c.execute('UPDATE deliveries SET tries=? WHERE id=?', (n, did))
        bus.pull(self.c, 5, 60)  # five slots, six sessions: the stuck rows held all five (2026-11-23: four of five)
        self.assertEqual(tried, ['new', 'stuck 5', 'stuck 4', 'stuck 3', 'stuck 2'])  # then the least tried first
        self.assertIsNotNone(self.row(new)['done_at'])
        bus.pull(self.c, 9, 60)
        self.assertIn('stuck 1', tried)
        self.assertNotIn('second of 1', tried)  # the rows of one session keep their order

    def test_a_session_with_a_row_under_lease_is_skipped_whole_in_a_pull(self):
        self.panes = ['w4:p1', 'w4:p2']
        self.set_session(A, host='local', pane='w4:p1')
        self.set_session(B, host='local', pane='w4:p2')
        a1, a2 = bus.delivery_add(self.c, A, 'a1')[0], bus.delivery_add(self.c, A, 'a2')[0]
        b1 = bus.delivery_add(self.c, B, 'b1')[0]
        with self.c:
            self.c.execute('UPDATE deliveries SET lease_at=? WHERE id=?', (bus.now(), a1))  # a try runs on it now
        bus.pull(self.c, 5, 60)
        self.assertEqual(self.pane_calls, [('w4:p2', 'b1')])  # nothing is typed for A
        self.assertEqual(self.row(a2)['tries'] or 0, 0)
        self.assertIsNone(self.row(a2)['done_at'])
        self.assertIsNotNone(self.row(b1)['done_at'])
        with self.c:
            self.c.execute('UPDATE deliveries SET lease_at=NULL WHERE id=?', (a1,))
        bus.pull(self.c, 5, 60)
        self.assertEqual(self.pane_calls[1:], [('w4:p1', 'a1'), ('w4:p1', 'a2')])  # id order

    def test_a_bus_that_goes_away_after_the_text_is_typed_does_not_type_it_twice(self):
        from unittest import mock
        self.set_session(host='local', pane='w4:p1')
        self.screen_on_pane(QuestionScreen(COLD_HEAD, COLD_331, numbered=False))
        with mock.patch.object(bus, 'event', side_effect=bus.BusOffline('away')):
            out = bus.deliver(self.c, A, 'the text')
        self.assertTrue(out.startswith('delivered'), out)
        self.assertIsNotNone(self.rows()[0]['done_at'])
        bus.pull(self.c, 5, 60)
        self.assertEqual(len(self.pane_calls), 1)

    def test_a_question_after_a_delivery_is_counted(self):
        self.set_session(host='local', pane='w4:p1')
        self.screen_on_pane(QuestionScreen(COLD_HEAD, COLD_331, numbered=False))
        out = bus.deliver(self.c, A, 'the text')
        self.assertTrue(out.startswith('delivered'), out)
        self.assertEqual(len(self.events('start-question-after-deliver')), 1)

    def test_a_delivery_with_no_question_counts_nothing(self):
        self.set_session(host='local', pane='w4:p1')
        out = bus.deliver(self.c, A, 'the text')
        self.assertTrue(out.startswith('delivered'), out)
        self.assertEqual(self.events('start-question-after-deliver'), [])

    def test_wake_remote_at_a_start_question_types_nothing_and_opens_one_tab_for_the_owner(self):
        scr = QuestionScreen(COLD_HEAD, COLD_330)
        polar = []
        with polar_stub(polar):
            (ok, msg), marked = self.remote_wake(scr, existed=True)
            (ok2, msg2), marked2 = self.remote_wake(scr, existed=True)  # the next wake: no second card, no second tab
        self.assertFalse(ok, msg)
        self.assertFalse(ok2, msg2)
        want = f'{A} runs in task-w{A[:8]} on server at a start question; the owner has a card'
        self.assertEqual((msg, msg2), (want, want))
        self.assertEqual(self.keys, [])
        self.assertEqual(self.tmux_calls, [])
        self.assertEqual(marked + marked2, [])
        self.assertEqual(len(polar), 1)
        self.assertEqual(len(self.tabs), 1)

    def test_wake_remote_with_a_card_that_polar_refuses_says_so_and_the_router_knows_once(self):
        self.add_wait(A, 'task', 'one', met=True)
        with self.c:
            self.c.execute('UPDATE waits SET met_at=?', (minutes_ago(45),))
        polar = []
        with polar_rec(polar, [2, 2]):
            (ok, msg), _ = self.remote_wake(QuestionScreen(COLD_HEAD, COLD_330))
            (ok2, msg2), _ = self.remote_wake(QuestionScreen(COLD_HEAD, COLD_330))
        self.assertFalse(ok, msg)
        self.assertTrue(msg.endswith('at a start question; no card went out'), msg)
        self.assertTrue(msg2.endswith('no card went out'), msg2)  # too soon to try the card again
        self.assertEqual(len(polar), 2)
        self.assertEqual(len(self.inbox), 1)
        self.assertEqual(len(self.events('wake-no-box')), 1)
        self.assertIn('no input box', self.inbox[0])

    def test_wake_remote_types_the_text_when_the_question_is_gone_on_the_second_look(self):
        scr = Seq(QuestionScreen(COLD_HEAD, COLD_330).read(), INPUT_BOX)
        polar = []
        with polar_stub(polar):
            (ok, msg), marked = self.remote_wake(scr)
        self.assertTrue(ok, msg)  # a quoted question before the box: a resumed session
        self.assertEqual(len(self.tmux_calls), 1)
        self.assertEqual(len(marked[0]), 1)
        self.assertEqual((polar, self.keys), ([], []))

    def test_wake_remote_after_a_wake_event_gives_the_next_question_a_new_card(self):
        scr = QuestionScreen(COLD_HEAD, COLD_330)
        polar = []
        with polar_stub(polar):
            self.remote_wake(scr, existed=True)
            self.age('start-question-card', 10)
            self.add_event('wake', 5)  # the session ran again after card 1
            self.remote_wake(scr, existed=True)
        self.assertEqual(len(polar), 2)


# ---- the folder trust question -------------------------------------------------
# A real screen: tmux capture-pane of a resumed server session on 2026-11-23. Three sessions sat at it for 3 hours
# and the bus tried to type into each one 440 times. The cursor «❯» starts on «No, exit».
TRUST_SCREEN = '\n'.join([
    '',
    '─' * 80,
    ' Accessing workspace:',
    '',
    ' /Users/me/career-ops',
    '',
    ' Quick safety check: Is this a project you created or one you trust? (Like your',
    ' own code, a well-known open source project, or work from your team). If not,',
    " take a moment to review what's in this folder first.",
    '',
    " Claude Code'll be able to read, edit, and execute files here.",
    '',
    ' Security guide',
    '',
    ' ❯ No, exit',
    '   Yes, I trust this folder',
    '',
    ' Enter to confirm · Esc to cancel',
    '', '', ''])


class StillScreen:
    """A screen that does not change."""

    def __init__(self, text):
        self.text = text

    def read(self):
        return self.text


def polar_rec(calls, rcs=()):
    """Like polar_stub, and it keeps each card's body. rcs: the exit codes of the first calls (then 0)."""
    from unittest import mock
    rcs = list(rcs)
    def run(cmd, *a, **k):
        calls.append((cmd, k.get('input')))
        return mock.Mock(returncode=rcs.pop(0) if rcs else 0, stdout='', stderr='refused')
    return mock.patch.object(bus.subprocess, 'run', run)


class TrustQuestionTest(DeliverBase, QuestionHelpers):
    def opt(self, card, name):
        return card[0][card[0].index(name) + 1]

    def test_the_real_screen_is_the_trust_question_and_is_not_ready(self):
        self.assertTrue(bus.trust_question(TRUST_SCREEN))
        self.assertFalse(bus.claude_ready(TRUST_SCREEN))  # «❯ No, exit» made it «ready»
        self.assertIsNone(bus.start_question(TRUST_SCREEN))
        self.assertEqual(bus.trust_folder_on_screen(TRUST_SCREEN), '/Users/me/career-ops')
        self.assertEqual(bus.trust_folder_on_screen(INPUT_BOX), '')

    def test_a_quoted_trust_question_above_the_input_box_is_not_open(self):
        out = TRUST_SCREEN + '\n' + INPUT_BOX
        self.assertFalse(bus.trust_question(out))
        self.assertTrue(bus.claude_ready(out))

    def test_the_real_screen_is_a_trust_question(self):
        self.assertTrue(bus.trust_question(TRUST_SCREEN))
        self.assertEqual(bus.question_kind(TRUST_SCREEN), 'trust')

    def test_the_other_row_order_with_numbers_is_a_trust_question(self):
        out = TRUST_SCREEN.replace(' ❯ No, exit\n   Yes, I trust this folder',
                                   ' ❯ 1. Yes, I trust this folder\n   2. No, exit')
        self.assertNotEqual(out, TRUST_SCREEN)
        self.assertTrue(bus.trust_question(out))

    def test_the_two_rows_quoted_above_a_permission_menu_are_not_a_trust_question(self):
        self.assertFalse(bus.trust_question(TRUST_SCREEN + '\n' + PERMISSION_MENU))
        self.assertFalse(bus.trust_question(TRUST_SCREEN.rstrip('\n') + '\n\n' + PERMISSION_MENU))

    def test_the_real_screen_followed_by_a_shell_prompt_is_not_a_trust_question(self):
        self.assertFalse(bus.trust_question(TRUST_SCREEN.rstrip('\n') + '\n❯ '))  # a dead frame: Claude ended

    def test_rows_with_no_cursor_or_only_the_sentence_are_not_a_trust_question(self):
        self.assertFalse(bus.trust_question(TRUST_SCREEN.replace('❯', ' ')))
        self.assertFalse(bus.trust_question(' Quick safety check: Do you trust this folder?'))
        self.assertFalse(bus.trust_question('We talked about «trust this folder» before.'))

    def test_wake_remote_at_the_trust_question_presses_no_key_and_the_owner_has_one_card(self):
        scr = StillScreen(TRUST_SCREEN)
        polar = []
        with polar_rec(polar):
            (ok, msg), marked = self.remote_wake(scr)
            (ok2, msg2), marked2 = self.remote_wake(scr)  # the next wake: no second card, no second tab
        self.assertFalse(ok, msg)
        self.assertFalse(ok2, msg2)
        want = f'{A} runs in task-w{A[:8]} on server at the folder trust question; the owner has a card'
        self.assertEqual((msg, msg2), (want, want))
        self.assertEqual(self.keys, [])       # to trust a folder is the owner's decision
        self.assertEqual(self.tmux_calls, [])  # no text is typed into the question
        self.assertEqual(marked + marked2, [])
        self.assertEqual(len(polar), 1)
        self.assertEqual([os.path.basename(polar[0][0][0])] + polar[0][0][1:3], ['polar', 'ask', 'decision'])
        self.assertEqual(self.opt(polar[0], '--title'), 'Confirma la carpeta de una sesión en server')
        self.assertLessEqual(len(self.opt(polar[0], '--context')), 280)
        body = polar[0][1]
        self.assertIn('`/Users/me/career-ops`', body)
        self.assertIn('tmux attach -t task-w' + A[:8], body)
        self.assertIn('agent-bus unpark ' + A, body)
        self.assertEqual(len(self.tabs), 1)

    def test_a_context_that_polar_refuses_goes_again_in_plain_words(self):
        polar = []
        with polar_rec(polar, [2]):
            self.assertEqual(bus.tell_trust_question(self.c, A, 'fix pricing_rule', '/x/y', 'task-wa'), 'new')
        ctx = [self.opt(c, '--context') for c in polar]
        self.assertEqual(len(ctx), 2)
        self.assertIn('pricing_rule', ctx[0])
        self.assertNotIn('pricing_rule', ctx[1])

    def test_a_card_that_polar_did_not_take_is_tried_again_after_ten_minutes(self):
        polar = []
        with polar_rec(polar, [2, 2]):
            self.assertEqual(bus.tell_trust_question(self.c, A, 'x', '/x/y', 'task-wa'), '')
        with polar_rec(polar):
            self.assertEqual(bus.tell_trust_question(self.c, A, 'x', '/x/y', 'task-wa'), '')  # too soon
            with self.c:
                self.c.execute("UPDATE events SET at=? WHERE type='trust-question-card'", (minutes_ago(11),))
            self.assertEqual(bus.tell_trust_question(self.c, A, 'x', '/x/y', 'task-wa'), 'new')
            self.assertEqual(bus.tell_trust_question(self.c, A, 'x', '/x/y', 'task-wa'), 'had')  # sent: one in 6 hours
        self.assertEqual(len(polar), 3)

    def test_no_box_at_the_trust_question_is_one_card_and_no_key(self):
        self.set_session(host='local', pane='w4:p1', title='tariff work')
        self.rc = 5
        self.screen_on_pane(StillScreen(TRUST_SCREEN))
        polar = []
        with polar_rec(polar):
            out = bus.deliver(self.c, A, 'the text')
            with self.c:
                self.c.execute('UPDATE deliveries SET created_at=?', (minutes_ago(45),))
            bus.pull(self.c, 5, 60)
        self.assertIn('the folder trust question holds it; the owner has a Polar card for it', out)
        self.assertEqual(self.keys, [])
        self.assertEqual(len(polar), 1)
        self.assertEqual(self.opt(polar[0], '--title'), 'Confirma la carpeta de la sesión «tariff work»')
        self.assertIn('`/Users/me/career-ops`', polar[0][1])
        self.assertNotIn('tmux attach', polar[0][1])
        self.assertIsNone(self.rows()[0]['done_at'])
        self.assertEqual(len(self.inbox), 1)  # the row is old: the router knows it once, and says the owner has the card
        self.assertIn('the folder trust question holds it; the owner has a Polar card for it', self.inbox[0])

    def test_wake_remote_at_an_unknown_screen_tells_the_router_once(self):
        scr = StillScreen(' A new notice of Claude Code\n\n   Press a key to go on')
        self.rc_tmux = 5
        (ok, msg), marked = self.remote_wake(scr, ready_timeout=0)
        self.assertFalse(ok, msg)
        self.assertEqual(self.inbox, [])  # the wait was met just now: a slow start is not news
        with self.c:
            self.c.execute('UPDATE waits SET met_at=?', (minutes_ago(45),))
        self.remote_wake(scr, ready_timeout=0)
        self.remote_wake(scr, ready_timeout=0)
        self.assertEqual(len(self.inbox), 1)
        self.assertIn('Press a key to go on', self.inbox[0])
        self.assertIn('tmux attach -t task-w' + A[:8], self.inbox[0])
        self.assertEqual(self.keys, [])


# ---- a frame is not a process: the review of 2026-11-23 -------------------------
class QuestionReviewTest(DeliverBase, QuestionHelpers):
    """A question on the screen of a laptop pane counts only while a `claude` process runs there. And a session that
    waits for the owner at a question does not take the wake tries of the other sessions."""
    SHELL_PROMPTS = ('➜  career-ops git:(main) ', 'cris@laptop career-ops % ', '$ ')
    FRAMES = {'trust': TRUST_SCREEN, 'cold': QuestionScreen(COLD_HEAD, COLD_330).read(),
              'old': QuestionScreen(OLD_HEAD, OLD_OPTS).read()}

    def test_pane_claude_reads_the_foreground_processes(self):
        for names, want in ((['railway', 'node', 'mcp-server', 'claude'], True), (['/Users/x/.local/bin/claude'], True),
                            (['zsh'], False), (['-zsh'], False), (['/bin/bash', 'zsh'], False),
                            (['ssh'], None), (['claude-helper'], None), (['zsh', 'vim'], None), (['node'], None)):
            bus.herdr = lambda *a, timeout=15, n=names: proc_info(n)
            self.assertIs(bus.pane_claude('w4:p1'), want, names)
        bus.herdr = lambda *a, timeout=15: json.dumps({'result': {'process_info': {'foreground_processes': [
            {'argv': ['node', '/opt/lib/node_modules/@anthropic-ai/claude-code/cli.js'], 'argv0': 'node'}]}}})
        self.assertIs(bus.pane_claude('w4:p1'), True)  # a Claude Code that runs on node
        bus.herdr = lambda *a, timeout=15: json.dumps({'result': {'process_info': {'foreground_processes': [
            {'argv': ['node', '/Users/x/.claude/hooks/notify.js'], 'argv0': 'node'}]}}})
        self.assertIsNone(bus.pane_claude('w4:p1'))  # a hook script of the owner is not Claude Code
        bus.herdr = lambda *a, timeout=15: json.dumps({'result': {'process_info': {'foreground_processes': [
            {'argv': ['claude', '--resume', 'x'], 'name': '2.1.330'}]}}})
        self.assertIs(bus.pane_claude('w4:p1'), True)  # no argv0: the first word of argv
        for raw in ('', 'not json', '{"error":{"code":"pane_not_found","message":"pane not found"}}', proc_info([]),
                    '{"result":{"process_info":{"foreground_processes":"zsh"}}}', '{"result":{"panes":[]}}'):
            bus.herdr = lambda *a, timeout=15, r=raw: r
            self.assertIsNone(bus.pane_claude('w4:p1'), raw)
        def broken(*a, timeout=15):
            raise OSError('no herdr')
        bus.herdr = broken
        self.assertIsNone(bus.pane_claude('w4:p1'))

    def old_row_on(self, screen, procs):
        """One row for a laptop pane with this screen and these processes, where Herdr lists no agent of the session:
        a first try, then two pulls of the row at 45 min. Returns (first outcome, Polar calls)."""
        base = bus.herdr
        with self.c:
            self.c.execute('DELETE FROM deliveries')
            self.c.execute('DELETE FROM events')
        del self.inbox[:], self.keys[:]
        self.set_session(host='local', pane='w4:p1', title='tariff work')
        self.rc, self.agents = 5, []
        self.screen_on_pane(StillScreen(screen), procs)
        polar = []
        try:
            with polar_stub(polar), mock.patch.object(bus.time, 'sleep', lambda n: None):
                first = bus.deliver(self.c, A, 'the text')
                with self.c:
                    self.c.execute('UPDATE deliveries SET created_at=?', (minutes_ago(45),))
                bus.pull(self.c, 5, 60)
                bus.pull(self.c, 5, 60)
        finally:
            bus.herdr = base
        return first, polar

    def test_a_frame_that_claude_left_above_a_shell_prompt_gets_no_card_and_its_row_goes_to_the_router(self):
        for name, frame in self.FRAMES.items():
            for prompt in self.SHELL_PROMPTS:
                screen = frame.rstrip('\n') + '\n' + prompt
                with self.subTest(frame=name, prompt=prompt):
                    self.assertTrue(bus.question_kind(screen))  # the text alone cannot tell: it reads as open
                    first, polar = self.old_row_on(screen, ['zsh'])
                    self.assertIn('(a menu or a question?); next try soon', first)
                    self.assertEqual(polar, [])  # no card every 6 hours for a question that nobody can answer
                    self.assertEqual(self.events('trust-question-card') + self.events('start-question-card'), [])
                    self.assertIsNotNone(self.rows()[0]['done_at'])  # not tried for 3 days
                    self.assertEqual(len(self.inbox), 1)
                    self.assertIn('no Claude runs there', self.inbox[0])
                    self.assertIn('the text', self.inbox[0])
                    self.assertEqual(self.keys, [])

    def test_an_open_question_of_a_claude_that_herdr_does_not_list_keeps_its_card_and_its_row(self):
        for name, frame in self.FRAMES.items():
            with self.subTest(frame=name):
                first, polar = self.old_row_on(frame, ['node', 'claude'])
                self.assertIn('holds it; the owner has a Polar card for it', first)
                self.assertEqual(len(polar), 1)
                self.assertIsNone(self.rows()[0]['done_at'])
                self.assertEqual(len(self.inbox), 1)
                self.assertNotIn('no Claude runs there', self.inbox[0])
                self.assertEqual(self.keys, [])

    def test_a_herdr_that_does_not_tell_the_processes_keeps_the_card(self):
        first, polar = self.old_row_on(TRUST_SCREEN.rstrip('\n') + '\n$ ', None)  # not known: as before the check
        self.assertEqual(len(polar), 1)
        self.assertIsNone(self.rows()[0]['done_at'])

    def test_a_screen_the_bus_does_not_know_keeps_its_row_while_a_claude_process_runs(self):
        notice = ' A new notice of Claude Code\n\n   Press a key to go on'
        first, polar = self.old_row_on(notice, ['node', 'claude'])
        self.assertEqual(polar, [])
        self.assertIsNone(self.rows()[0]['done_at'])  # the text is typed when the box is free
        self.assertEqual(len(self.inbox), 1)
        self.assertIn('stays queued', self.inbox[0])
        first, polar = self.old_row_on(notice, ['zsh'])  # only a shell: no box will come
        self.assertIsNotNone(self.rows()[0]['done_at'])
        self.assertIn('no Claude runs there', self.inbox[0])

    def live_wake(self, rc, scr, procs):
        self.set_session(parked_at=minutes_ago(5), host='local', title='tariff work')
        if not self.c.execute('SELECT 1 FROM waits').fetchone():
            self.add_wait(A, 'task', 'k', met=True)
        ws = self.c.execute('SELECT * FROM waits').fetchall()
        self.rc = rc
        base = bus.herdr
        self.screen_on_pane(scr, procs)
        try:
            with mock.patch.object(bus, 'live_agent_panes', lambda: {A: 'w4:p5'}):
                s = self.c.execute('SELECT * FROM sessions WHERE session_id=?', (A,)).fetchone()
                return self.saved['_wake_one'](self.c, A, s, ws)
        finally:
            bus.herdr = base

    def test_a_wake_into_a_running_laptop_session_at_a_question_sends_its_one_card(self):
        polar = []
        with polar_stub(polar):
            ok, msg = self.live_wake(5, QuestionScreen(COLD_HEAD, COLD_330), ['node', 'claude'])
            ok2, msg2 = self.live_wake(5, QuestionScreen(COLD_HEAD, COLD_330), ['node', 'claude'])
        self.assertFalse(ok, msg)
        self.assertIn('a start question holds it, the owner has a card', msg)
        self.assertEqual(msg, msg2)
        self.assertEqual(len(polar), 1)
        self.assertIn('task k', polar[0][polar[0].index('--context') + 1])
        self.assertEqual(self.keys, [])
        self.assertEqual(len(bus.met_waits(self.c, A)), 1)  # not woken: the next wake types the text
        self.assertTrue(self.c.execute('SELECT parked_at FROM sessions WHERE session_id=?', (A,)).fetchone()[0])

    def test_a_wake_into_a_running_laptop_session_with_a_busy_box_sends_no_card(self):
        polar = []
        with polar_stub(polar):
            ok, msg = self.live_wake(3, QuestionScreen(COLD_HEAD, COLD_330), ['node', 'claude'])  # the owner types
            self.assertFalse(ok, msg)
            self.assertNotIn('holds it', msg)
            ok, msg = self.live_wake(5, StillScreen(PERMISSION_MENU), ['node', 'claude'])  # a menu: no known question
            self.assertFalse(ok, msg)
            self.assertNotIn('holds it', msg)
        self.assertEqual(polar, [])

    def wake_order(self, held, n=2, pick=0, fail=()):
        """Three parked sessions with a met wait, the oldest first: A, B, NEW. `held`: {session: event type}.
        `pick`: which held session chance gives the try. `fail`: other sessions whose wake fails.
        Returns the sessions that the real wake tried, in order."""
        with self.c:
            bus.touch(self.c, NEW)
        for i, sid in enumerate((A, B, NEW)):
            self.set_session(sid, parked_at=minutes_ago(50), host='server', tmux='task-w' + sid[:8])
            wid = self.add_wait(sid, 'task', 'k' + sid[:1], met=True)
            with self.c:
                self.c.execute('UPDATE waits SET met_at=? WHERE id=?', (minutes_ago(40 - 10 * i), wid))
        for sid, typ in held.items():
            self.add_event(typ, 5, sid)
        tried = []
        def wake_one(c, sid, ws, **kw):
            tried.append(sid)
            return (sid not in held and sid not in fail), 'x'
        with mock.patch.object(bus, 'wake_one', wake_one), \
                mock.patch.object(bus.random, 'choice', lambda seq: seq[pick]):
            self.saved['wake'](self.c, n)
        return tried

    def test_sessions_held_at_a_question_do_not_take_the_wake_tries_of_the_others(self):
        tried = self.wake_order({A: 'trust-question-card', B: 'start-question-card'})
        self.assertEqual(tried, [NEW, A])  # before: [A, B] on every run, and NEW never woke

    def test_chance_picks_the_held_session_that_gets_the_try(self):
        held = {A: 'trust-question-card', B: 'start-question-card'}
        self.assertEqual(self.wake_order(held, pick=1), [NEW, B])  # so a third held session is not left out for hours
        self.assertEqual(self.wake_order(held, n=1, pick=1), [NEW, B])

    def test_a_held_session_gets_its_try_when_the_others_take_every_try(self):
        # The owner's answer writes no event: only a try can tell that the question is gone. Two sessions that fail
        # for another reason must not hold that try back for hours.
        self.assertEqual(self.wake_order({A: 'start-question-card'}, fail=(B, NEW)), [B, NEW, A])
        self.assertEqual(self.wake_order({A: 'start-question-card'}, n=1, fail=(B, NEW)), [B, A])

    def test_one_held_session_alone_is_tried_as_before(self):
        self.set_session(A, parked_at=minutes_ago(50), host='server', tmux='task-w' + A[:8])
        self.add_wait(A, 'task', 'k', met=True)
        self.add_event('trust-question-card', 5, A)
        tried = []
        with mock.patch.object(bus, 'wake_one', lambda c, sid, ws, **kw: (tried.append(sid), (False, 'x'))[1]):
            self.saved['wake'](self.c, 2)
        self.assertEqual(tried, [A])

    def test_a_session_at_a_screen_with_no_box_goes_last_too(self):
        self.assertEqual(self.wake_order({A: 'wake-no-box'}), [B, NEW, A])

    def test_with_no_held_session_the_oldest_met_wait_goes_first(self):
        self.assertEqual(self.wake_order({}), [A, B])

    def test_a_held_session_that_got_a_text_since_is_first_again(self):
        self.add_event('wake', 2, A)  # it ran after the card: the question of the card is gone
        self.assertEqual(self.wake_order({A: 'trust-question-card', B: 'start-question-card'}), [A, NEW, B])
