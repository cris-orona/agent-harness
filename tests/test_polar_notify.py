"""polar-notify: the line builders, the hand-off to `agent-bus deliver`, and the main loop (bus stubbed)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: F401,E402  (role local, temp bus.db)
import importlib.machinery
import json, sqlite3, subprocess, tempfile
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / "bin/polar-notify"
loader = importlib.machinery.SourceFileLoader('polar_notify', str(PATH))
spec = importlib.util.spec_from_loader('polar_notify', loader)
notify = importlib.util.module_from_spec(spec)
loader.exec_module(notify)

QUESTIONS = {
    'id': 'abc', 'kind': 'questions', 'status': 'answered', 'title': 'Alertas',
    'questions': [{'key': 'a', 'text': '¿Finanzas recibe alerta?'}, {'key': 'u', 'text': 'x' * 80}],
    'answers': {'a': {'option': 'si', 'text': 'Sí'}, 'u': {'option': None, 'text': '5 mil'}},
    'note': 'ojo con X',
}


class PolarNotifyTest(unittest.TestCase):
    def test_answered_is_done(self):
        self.assertIn('answered', notify.DONE)

    def test_questions_line(self):
        text = notify.line(QUESTIONS)
        self.assertEqual(text, (
            'Polar (info): tus preguntas «Alertas» → 1. ¿Finanzas recibe alerta? = Sí; '
            '2. ' + 'x' * 59 + '… = 5 mil; nota: ojo con X. Run polar status abc for details.'))

    def test_no_note(self):
        text = notify.line({**QUESTIONS, 'note': ''})
        self.assertNotIn('nota', text)

    def test_other_kinds_unchanged(self):
        r = {'id': 'z', 'status': 'chosen', 'title': 'T', 'chosen_option': 'go'}
        self.assertIn('elegiste «go»', notify.line(r))

    def test_revise_line(self):
        self.assertIn('revise', notify.DONE)
        r = {'id': 'z', 'status': 'revise', 'title': 'T', 'instructions': 'otro argumento'}
        self.assertEqual(notify.line(r), (
            'Polar (cambios): the owner asks you to change your card z «T»: otro argumento. '
            'Write a new card with polar ask … --replaces z.'))

    def test_follow_up_tap_is_not_a_change_request(self):
        r = {'id': 'z', 'status': 'revise', 'title': 'T', 'instructions': 'Abrir el PR', 'via': 'next'}
        self.assertEqual(notify.line(r), (
            'Polar (seguimiento): the owner started your suggested follow-up on report z «T»: Abrir el PR. '
            'Do it now. Send one polar report when it is done.'))
        r.pop('via')  # the same words typed in «Pedir cambios» stay a change request
        self.assertTrue(notify.line(r).startswith('Polar (cambios): '))

    def test_several_follow_ups_are_one_relay(self):
        r = {'id': 'z', 'status': 'revise', 'title': 'T', 'via': 'next', 'instructions': '1) Abrir el PR 2) Medir',
             'indexes': [0, 2], 'steps': ['Abrir el PR', 'Medir']}
        self.assertEqual(notify.line(r), (
            'Polar (seguimiento): the owner started 2 of your suggested follow-ups on report z «T»: '
            '1) Abrir el PR 2) Medir. Do all of them now. Send one polar report when they are done.'))
        r['steps'], r['instructions'] = ['Abrir el PR'], 'Abrir el PR'  # one step keeps the one-step words
        self.assertIn('your suggested follow-up on report z', notify.line(r))

    def test_skipped_line(self):
        r = {'id': 'z', 'status': 'skipped', 'title': 'T'}
        self.assertIn('skipped', notify.DONE)
        self.assertEqual(notify.line(r), (
            'Polar (info): your request z «T» was skipped — the owner answered newer cards first. '
            'If it still matters, re-ask with fresh context (no «URGENTE»), or drop it. '
            'Run polar status z for details.'))


UUID = 'a' * 8 + '-0000-0000-0000-' + '0' * 12
CARD = 'abc123def456'


def card(**kw):
    r = {'id': CARD, 'status': 'chosen', 'title': 'T', 'chosen_option': 'go', 'decided_at': '2026-11-19T10:00:00Z',
         'requester': UUID + ' w4:p1'}
    r.update(kw)
    return r


class FakeBus:
    """Replaces notify.bus: records (args, stdin) and answers from `replies` (a function of args) or (0, 'ok')."""

    def __init__(self, replies=None):
        self.calls, self.replies = [], replies

    def __call__(self, *args, stdin=None, timeout=300):
        self.calls.append((args, stdin))
        return self.replies(args) if self.replies else (0, 'ok')

    def named(self, name):
        return [c for c in self.calls if c[0][0] == name]


class NotifyStubBase(unittest.TestCase):
    PATCHED = ('bus', 'on_server', 'polar_list', 'is_server', 'STATE')

    def setUp(self):
        self.saved = {k: getattr(notify, k) for k in self.PATCHED}
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeBus()
        notify.bus = self.fake
        self.server_answers = []
        notify.on_server = lambda cmd, timeout=30: self.server_answers.append(cmd) or None
        notify.STATE = os.path.join(self.tmp.name, 'state.json')

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(notify, k, v)
        self.tmp.cleanup()


class RescueReviseTest(NotifyStubBase):
    """The safety net of «Pedir cambios»: a closed session gets the owner's text once more, but not when it already
    sent its next card."""

    NOW = notify.datetime(2026, 11, 19, 11, 0, 0, tzinfo=notify.timezone.utc)

    def run_rescue(self, reqs, ended='2026-11-19T10:30:00Z', parked=None):
        row = json.dumps({'session_id': UUID, 'ended_at': ended, 'parked_at': parked})
        self.fake.replies = lambda args: (0, row) if args[0] == 'session' else (0, 'ok')
        st = {'rescue_since': '2026-11-19T00:00:00Z'}
        return notify.rescue_revise(reqs, st, now=self.NOW), st

    def test_closed_session_with_no_later_card_gets_the_text_again(self):
        notes, st = self.run_rescue([card(status='revise', instructions='otro', created_at='2026-11-19T09:00:00Z')])
        sent = self.fake.named('deliver')
        self.assertEqual(len(sent), 1)
        self.assertIn('cambios, otra vez', sent[0][1])
        self.assertIn(CARD, st['rescued'])

    def test_a_later_card_from_the_session_is_its_answer(self):
        reqs = [card(status='revise', instructions='otro', created_at='2026-11-19T09:00:00Z'),
                card(id='new000000001', kind='report', status='noted', created_at='2026-11-19T10:20:00Z')]
        notes, st = self.run_rescue(reqs)
        self.assertEqual(self.fake.named('deliver'), [])
        self.assertEqual(self.fake.named('session'), [])  # no bus call for an answered card
        self.assertIn(CARD, st['rescued'])  # and it is not looked at again

    def test_an_earlier_card_another_session_or_a_silent_notice_is_no_answer(self):
        other = 'b' * 8 + UUID[8:]
        reqs = [card(status='revise', instructions='otro', created_at='2026-11-19T09:00:00Z'),
                card(id='old000000001', status='noted', created_at='2026-11-19T09:30:00Z'),
                card(id='oth000000001', status='noted', created_at='2026-11-19T10:20:00Z', requester=other + ' w4:p2'),
                card(id='sil000000001', status='noted', created_at='2026-11-19T10:20:00Z', silent=True)]
        self.run_rescue(reqs)
        self.assertEqual(len(self.fake.named('deliver')), 1)

    def test_a_live_or_parked_session_is_left_alone(self):
        reqs = [card(status='revise', instructions='otro', created_at='2026-11-19T09:00:00Z')]
        self.run_rescue(reqs, ended='')
        self.run_rescue(reqs, parked='2026-11-19T10:30:00Z')
        self.assertEqual(self.fake.named('deliver'), [])


class HandleTest(NotifyStubBase):
    def test_skip_prefixes_are_done_without_a_bus_call(self):
        for who in ('agent-bus-wake', 'release-desk', 'release-desk owner-steps', 'watchdog', 'prod-op 1 release-desk x'):
            done, msg = notify.handle(card(requester=who))
            self.assertTrue(done, who)
        self.assertEqual(self.fake.calls, [])

    def test_te_espera_card_with_nothing_to_tell_is_done_without_a_bus_call(self):
        nada = {'respuesta': {'option': 'nada', 'text': 'Nada por ahora'}}
        for extra in ({'status': 'discarded'}, {'status': 'expired'}, {'status': 'skipped'},
                      {'status': 'answered', 'kind': 'questions', 'answers': nada, 'note': ''}):
            done, msg = notify.handle(card(title='Te espera: Sesión', **extra))
            self.assertTrue(done, extra)
            self.assertIn('quiet', msg)
        self.assertEqual(self.fake.calls, [])

    def test_te_espera_card_with_an_answer_or_a_note_is_delivered(self):
        q = [{'key': 'respuesta', 'text': '¿Qué le respondes?'}]
        sigue = {'respuesta': {'option': 'sigue', 'text': 'Sigue con lo que propones'}}
        nada = {'respuesta': {'option': 'nada', 'text': 'Nada por ahora'}}
        for answers, note in ((sigue, ''), (nada, 'mejor espera a Ana')):
            self.fake.calls.clear()
            done, msg = notify.handle(card(title='Te espera: Sesión', status='answered', kind='questions',
                                           questions=q, answers=answers, note=note))
            self.assertTrue(done, msg)
            self.assertEqual(len(self.fake.calls), 1)

    def test_a_discarded_card_of_the_session_is_still_delivered(self):
        done, msg = notify.handle(card(title='Respuesta a Leo', status='discarded'))
        self.assertTrue(done, msg)
        self.assertEqual(len(self.fake.calls), 1)

    def test_chosen_card_is_delivered_with_ref_and_wait(self):
        r = card()
        done, msg = notify.handle(r)
        self.assertTrue(done, msg)
        self.assertEqual(self.fake.calls, [(('deliver', UUID, '--ref', f'polar:{CARD}:chosen', '--wait', f'polar:{CARD}',
                                             '--from', 'polar-notify', '--place', 'w4:p1', '--text-file', '-'),
                                            notify.card_text(r))])
        self.assertIn('elegiste', self.fake.calls[0][1])

    def test_a_plain_ok_on_a_report_goes_to_a_live_session_only(self):
        r = card(kind='report', chosen_option='ok')
        done, msg = notify.handle(r)
        self.assertTrue(done, msg)
        self.assertEqual(self.fake.calls, [(('deliver', UUID, '--ref', f'polar:{CARD}:chosen', '--if-live',
                                             '--from', 'polar-notify', '--place', 'w4:p1', '--text-file', '-'),
                                            notify.card_text(r))])

    def test_a_discarded_or_expired_report_goes_to_a_live_session_only(self):
        for status in ('discarded', 'expired'):
            self.fake.calls.clear()
            r = card(kind='report', status=status)
            done, msg = notify.handle(r)
            self.assertTrue(done, msg)
            args = self.fake.calls[0][0]
            self.assertIn('--if-live', args, status)
            self.assertNotIn('--wait', args, status)

    def test_every_other_answer_keeps_the_wait(self):
        for extra in ({'kind': 'report', 'chosen_option': 'resume'},
                      {'kind': 'report', 'status': 'revise', 'instructions': 'cambia el título'},
                      {'kind': 'report', 'status': 'revise', 'via': 'next', 'steps': ['a'], 'instructions': 'a'},
                      {'kind': 'decision', 'chosen_option': 'ok'},
                      {'kind': 'decision', 'status': 'discarded'},
                      {'kind': 'questions', 'status': 'expired'},
                      {'chosen_option': 'ok'}):
            self.fake.calls.clear()
            done, msg = notify.handle(card(**extra))
            self.assertTrue(done, extra)
            args = self.fake.calls[0][0]
            self.assertIn('--wait', args, extra)
            self.assertNotIn('--if-live', args, extra)

    def test_bus_busy_code_is_not_done_and_says_bus_away(self):
        self.fake.replies = lambda a: (3, 'offline')
        done, msg = notify.handle(card())
        self.assertFalse(done)
        self.assertTrue(msg.startswith('bus away'), msg)

    def test_other_failure_is_not_done_and_not_bus_away(self):
        self.fake.replies = lambda a: (1, 'bad')
        done, msg = notify.handle(card())
        self.assertFalse(done)
        self.assertFalse(msg.startswith('bus away'), msg)

    def test_prod_op_requester_delivers_to_the_session_with_the_run_hint(self):
        r = card(requester=f'prod-op op42 {UUID} task-x')
        notify.on_server = lambda cmd, timeout=30: subprocess.CompletedProcess([], 0, 'N\n', '')
        done, msg = notify.handle(r)
        self.assertTrue(done, msg)
        args, stdin = self.fake.calls[0]
        self.assertEqual(args[:2], ('deliver', UUID))
        self.assertIn('prod-op run op42', stdin)

    def session_reply(self, row):
        self.fake.replies = lambda a: (0, json.dumps(row)) if a[0] == 'session' else (0, 'ok')

    def test_polars_own_live_task_is_not_delivered(self):
        notify.on_server = lambda cmd, timeout=30: subprocess.CompletedProcess([], 0, 'Y\n', '')
        self.session_reply({'session_id': UUID, 'parked_at': None, 'ended_at': None})
        done, msg = notify.handle(card(requester=f'{UUID} task-abc'))
        self.assertTrue(done)
        self.assertEqual([c[0] for c in self.fake.calls], [('session', UUID)])

    def test_polar_task_of_a_parked_ended_or_handed_off_session_is_delivered(self):
        notify.on_server = lambda cmd, timeout=30: subprocess.CompletedProcess([], 0, 'Y\n', '')
        for row in ({'parked_at': '2026-11-19T10:00:00Z'}, {'ended_at': '2026-11-19T10:00:00Z'}, {'handoff_of': 'x'}):
            self.fake.calls.clear()
            self.session_reply(row)
            done, msg = notify.handle(card(requester=f'{UUID} task-abc'))
            self.assertTrue(done, msg)
            args = self.fake.calls[-1][0]
            self.assertEqual(args[:2], ('deliver', UUID), row)
            self.assertEqual(args[args.index('--place') + 1], 'task-abc')

    def test_no_place_flag_when_the_requester_has_one_token(self):
        done, msg = notify.handle(card(requester=UUID))
        self.assertTrue(done, msg)
        self.assertNotIn('--place', self.fake.calls[0][0])

    def test_resumed_task_w_name_is_delivered_without_a_tmux_check(self):
        notify.on_server = lambda cmd, timeout=30: self.fail('no has-session check expected')
        done, msg = notify.handle(card(requester=f'{UUID} task-wabc'))
        self.assertTrue(done, msg)
        self.assertEqual(self.fake.calls[0][0][:2], ('deliver', UUID))
        self.assertIn('task-wabc', self.fake.calls[0][0])

    def test_dead_polar_task_is_delivered(self):
        notify.on_server = lambda cmd, timeout=30: subprocess.CompletedProcess([], 0, 'N\n', '')
        done, msg = notify.handle(card(requester=f'{UUID} task-abc'))
        self.assertTrue(done, msg)
        self.assertEqual(len(self.fake.named('deliver')), 1)

    def test_card_without_a_session_meets_by_card_id(self):
        for who in ('', 'unknown-sess'):
            self.fake.calls.clear()
            self.fake.replies = lambda a: (0, 'met 1')
            done, msg = notify.handle(card(requester=who))
            self.assertTrue(done, who)
            self.assertEqual(self.fake.calls[0][0][:3], ('meet', 'polar', CARD))
            self.assertEqual(self.fake.calls[0][0][3], '--summary')
            self.assertEqual(len(self.fake.calls), 1)

    def test_card_nobody_waits_for_goes_to_the_router(self):
        self.fake.replies = lambda a: (0, 'met 0')
        done, msg = notify.handle(card(requester=''))
        self.assertTrue(done, msg)
        self.assertEqual([c[0] for c in self.fake.calls][1], ('deliver', 'router', '--text-file', '-'))
        self.assertIn(f'Polar {CARD}', self.fake.calls[1][1])

    def test_meet_failure_is_bus_away(self):
        self.fake.replies = lambda a: (3, 'offline')
        done, msg = notify.handle(card(requester=''))
        self.assertFalse(done)
        self.assertTrue(msg.startswith('bus away'), msg)


class MainBase(NotifyStubBase):
    def setUp(self):
        super().setUp()
        self.server = False
        notify.is_server = lambda: self.server
        self.cards = []
        notify.polar_list = lambda since: self.cards
        self.write_state({'last': '2026-11-19T09:00:00Z'})
        self.out = []

    def write_state(self, st):
        with open(notify.STATE, 'w') as fh:
            json.dump(st, fh)

    def state(self):
        with open(notify.STATE) as fh:
            return json.load(fh)

    def run_main(self):
        from contextlib import redirect_stdout
        import io
        buf = io.StringIO()
        with redirect_stdout(buf):
            notify.main()
        return buf.getvalue()

    def delivers(self):
        return [c[0][1] for c in self.fake.named('deliver')]


class MainTest(MainBase):
    def test_new_cards_are_handled_in_decided_order_and_last_advances(self):
        s2, s3 = 'b' * 8 + '-0000-0000-0000-' + '0' * 12, 'c' * 8 + '-0000-0000-0000-' + '0' * 12
        self.cards = [card(id='late', decided_at='2026-11-19T10:05:00Z', requester=s3 + ' w4:p3'),
                      card(id='early', decided_at='2026-11-19T10:01:00Z', requester=s2 + ' w4:p2'),
                      card(id='old', decided_at='2026-11-19T08:00:00Z', requester=UUID + ' w4:p1'),
                      card(id='open', status='pending', decided_at='2026-11-19T11:00:00Z')]
        self.run_main()
        self.assertEqual(self.delivers(), [s2, s3])
        st = self.state()
        self.assertEqual(st['last'], '2026-11-19T10:05:00Z')
        self.assertEqual(st['pending'], {})

    def test_failed_card_goes_pending_and_is_retried(self):
        self.fake.replies = lambda a: (3, 'offline') if a[0] == 'deliver' else (0, '')
        self.cards = [card()]
        self.run_main()
        st = self.state()
        self.assertEqual(st['pending'][CARD]['tries'], 1)
        self.assertEqual(st['last'], '2026-11-19T10:00:00Z')
        self.run_main()  # not new any more, but pending: tried again
        self.assertEqual(len(self.fake.named('deliver')), 2)
        self.assertEqual(self.state()['pending'][CARD]['tries'], 2)
        self.fake.replies = None
        self.run_main()
        self.assertEqual(self.state()['pending'], {})

    def test_bus_away_never_goes_to_the_router(self):
        self.fake.replies = lambda a: (3, 'offline') if a[0] == 'deliver' else (0, '')
        self.write_state({'last': '2026-11-19T09:00:00Z',
                          'pending': {CARD: {'at': '2026-11-19T10:00:00Z', 'tries': notify.MAX_TRIES + 5}}})
        self.cards = [card()]
        self.run_main()
        self.assertFalse(any(c[0][1] == 'router' for c in self.fake.named('deliver')))
        self.assertIn(CARD, self.state()['pending'])

    def test_after_max_tries_one_router_deliver_and_the_card_leaves(self):
        self.fake.replies = lambda a: (1, 'bad') if a[0] == 'deliver' and a[1] != 'router' else (0, '')
        self.write_state({'last': '2026-11-19T10:00:00Z',
                          'pending': {CARD: {'at': '2026-11-19T10:00:00Z', 'tries': notify.MAX_TRIES - 1}}})
        self.cards = [card()]
        self.run_main()
        routers = [c for c in self.fake.named('deliver') if c[0][1] == 'router']
        self.assertEqual(len(routers), 1)
        self.assertIn(CARD, routers[0][1])
        self.assertEqual(self.state()['pending'], {})

    def test_old_string_pending_value_is_converted(self):
        self.fake.replies = lambda a: (3, 'offline') if a[0] == 'deliver' else (0, '')
        self.write_state({'last': '2026-11-19T10:00:00Z', 'pending': {CARD: '2026-11-19T10:00:00Z'}})
        self.cards = [card()]
        self.run_main()
        self.assertEqual(self.state()['pending'][CARD], {'at': '2026-11-19T10:00:00Z', 'tries': 1})

    def test_card_that_left_the_window_leaves_pending(self):
        self.write_state({'last': '2026-11-19T09:00:00Z', 'pending': {'gone': {'at': 'x', 'tries': 2}}})
        self.cards = [card()]  # a save happens only when a card is handled
        self.run_main()
        self.assertEqual(self.state()['pending'], {})

    def test_pull_always_runs_wake_only_on_the_server(self):
        self.run_main()
        self.assertEqual([c[0][0] for c in self.fake.calls], ['pull'])
        self.assertEqual(self.fake.calls[0][0], ('pull', '--max', '5', '--budget', '40'))
        self.fake.calls.clear()
        self.server = True
        self.run_main()
        self.assertEqual([c[0][0] for c in self.fake.calls], ['pull', 'wake'])
        self.assertEqual(self.fake.calls[1][0], ('wake', '--max', '2'))

    def test_polar_list_failure_still_pulls(self):
        notify.polar_list = lambda since: None
        self.run_main()
        self.assertEqual([c[0][0] for c in self.fake.calls], ['pull'])


class MainReviewTest(MainBase):
    def test_an_exception_in_handle_counts_as_not_done_and_the_loop_goes_on(self):
        s2 = 'b' * 8 + '-0000-0000-0000-' + '0' * 12
        real = notify.handle
        def handle(r):
            if r['id'] == 'bad':
                raise RuntimeError('boom')
            return real(r)
        saved = notify.handle
        notify.handle = handle
        try:
            self.cards = [card(id='bad', decided_at='2026-11-19T10:00:00Z'),
                          card(id='good', decided_at='2026-11-19T10:01:00Z', requester=s2 + ' w4:p2')]
            self.run_main()
        finally:
            notify.handle = saved
        self.assertEqual(self.delivers(), [s2])
        st = self.state()
        self.assertEqual(st['pending']['bad']['tries'], 1)
        self.assertEqual(st['last'], '2026-11-19T10:01:00Z')

    def test_a_retry_card_that_is_no_longer_decided_is_dropped(self):
        self.write_state({'last': '2026-11-19T10:00:00Z', 'pending': {CARD: {'at': 'x', 'tries': 3}}})
        self.cards = [card(status='pending')]
        self.run_main()
        self.assertEqual(self.state()['pending'], {})
        self.assertEqual(self.fake.named('deliver'), [])


class RealBusIntegrationTest(unittest.TestCase):
    """The real `notify.bus` runs agent-bus as a subprocess on a temp DB; nothing outside the temp dir is touched."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ('AGENT_BUS_DB', 'HERDR_BIN', 'AGENT_BUS_NO_MOVE',
                                                       'AGENT_BUS_SERVER_HOST', 'AGENT_BUS_INBOX', 'AGENT_BUS_CLIENT_DIR')}
        os.environ.update(AGENT_BUS_DB=os.path.join(self.tmp.name, 'bus.db'), HERDR_BIN='/usr/bin/false',
                          AGENT_BUS_NO_MOVE='1', AGENT_BUS_SERVER_HOST='nohost.invalid', AGENT_BUS_CLIENT_DIR=os.path.join(self.tmp.name, 'c'),
                          AGENT_BUS_INBOX=os.path.join(self.tmp.name, 'inbox.md'))

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_card_meets_the_wait_of_a_parked_session(self):
        from datetime import datetime, timedelta, timezone
        rc, out = notify.bus('register', '--session', UUID, '--pane', 'w4:p1')
        self.assertEqual(rc, 0, out)
        parked = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        wid = 'a1b2c3d4e5f6'
        db = sqlite3.connect(os.environ['AGENT_BUS_DB'])
        try:
            db.execute('UPDATE sessions SET parked_at=? WHERE session_id=?', (parked, UUID))
            db.execute('INSERT INTO waits(session_id, kind, key, about, created_at) VALUES(?,?,?,?,?)',
                       (UUID, 'polar', wid, 'polar ' + wid, parked))
            db.commit()
        finally:
            db.close()
        done, msg = notify.handle(card(id=wid, requester=UUID + ' w4:p1'))
        self.assertTrue(done, msg)
        db = sqlite3.connect(os.environ['AGENT_BUS_DB'])
        try:
            met = db.execute('SELECT met_at, met_by FROM waits WHERE key=?', (wid,)).fetchone()
            dv = db.execute('SELECT done_at, outcome FROM deliveries').fetchone()
        finally:
            db.close()
        self.assertIsNotNone(met[0])
        self.assertIn('elegiste', met[1])
        # no Herdr here (HERDR_BIN=false): the wake fails, so the row stays queued until a wake works
        self.assertIsNone(dv[0], dv)
        self.assertTrue(dv[1].startswith('queued 1: met polar:' + wid), dv)


    def test_a_plain_ok_on_a_report_does_not_resume_a_closed_session(self):
        from datetime import datetime, timedelta, timezone
        rc, out = notify.bus('register', '--session', UUID, '--pane', 'w4:p1')
        self.assertEqual(rc, 0, out)
        ended = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        db = sqlite3.connect(os.environ['AGENT_BUS_DB'])
        try:
            db.execute('UPDATE sessions SET ended_at=? WHERE session_id=?', (ended, UUID))
            db.commit()
        finally:
            db.close()
        done, msg = notify.handle(card(kind='report', chosen_option='ok'))
        self.assertTrue(done, msg)
        db = sqlite3.connect(os.environ['AGENT_BUS_DB'])
        try:
            waits = db.execute('SELECT COUNT(*) FROM waits').fetchone()[0]
            dv = db.execute('SELECT done_at, outcome FROM deliveries').fetchone()
            parked = db.execute('SELECT parked_at FROM sessions WHERE session_id=?', (UUID,)).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(waits, 0)
        self.assertIsNone(parked)
        self.assertIsNotNone(dv[0], dv)
        self.assertIn('--if-live: no resume', dv[1])


class MailClaimTest(MainBase):
    """Slice 9: the session whose email card was sent owns the mail thread (`thread mail:<gmail thread id>`)."""

    def mail_card(self, **kw):
        return card(**{'kind': 'email', 'status': 'sent', 'thread_id': '1a1f09ce900b6f9c', 'message_id': 'm9', **kw})

    def claims(self):
        return [c[0] for c in self.fake.named('claim')]

    def test_sent_email_card_claims_the_thread_for_its_session(self):
        self.cards = [self.mail_card()]
        self.run_main()
        self.assertEqual(self.claims(), [('claim', 'thread', 'mail:1a1f09ce900b6f9c', '--session', UUID, '--no-touch')])
        self.assertEqual(self.delivers(), [UUID])

    def test_other_cards_claim_nothing(self):
        self.cards = [card(kind='email', status='discarded', thread_id='T'), card(id='r1', kind='reply', status='sent'),
                      self.mail_card(id='nosid', requester='watchdog'), self.mail_card(id='nothread', thread_id=None)]
        self.run_main()
        self.assertEqual(self.claims(), [])

    def test_a_retried_card_does_not_claim_again(self):
        # A later sender is the primary owner by then: a retry of the older card must not take the thread back.
        self.fake.replies = lambda args: (1, 'no') if args[0] == 'deliver' else (0, 'ok')
        self.cards = [self.mail_card()]
        self.run_main()
        self.run_main()
        self.assertEqual(len(self.claims()), 1)
        self.assertEqual(self.state()['pending'][CARD]['tries'], 2)

    def test_claim_the_bus_did_not_take_is_tried_again_as_follow(self):
        away = {'on': True}
        self.fake.replies = lambda args: (3, 'bus away') if args[0] == 'claim' and away['on'] else (0, 'ok')
        self.cards = [self.mail_card()]
        self.run_main()
        self.assertEqual(self.state()['mail_claims'], {CARD: 1})
        self.run_main()
        self.assertEqual(self.state()['mail_claims'], {CARD: 2})
        away['on'] = False
        self.run_main()
        self.run_main()
        self.assertEqual(self.state()['mail_claims'], {})
        self.assertEqual(len(self.claims()), 3)  # first try, one failed retry, the retry that worked; then no more
        self.assertEqual(self.claims()[-1][-2:], ('--role', 'follow'))  # it must not take the thread from a later sender
        self.assertEqual(len(self.delivers()), 1)

    def test_prod_op_style_requester_is_resolved(self):
        self.assertEqual(notify.mail_claim(self.mail_card(requester='prod-op op1 ' + UUID + ' task-x')),
                         ('mail:1a1f09ce900b6f9c', UUID))

    def test_backfill_claims_sent_email_cards_oldest_first(self):
        other = 'b' * 8 + '-0000-0000-0000-' + '0' * 12
        seen = []
        notify.polar_list = lambda since: seen.append(since) or [
            self.mail_card(id='late', decided_at='2026-11-19T12:00:00Z', requester=other + ' task-2'),
            self.mail_card(id='early', decided_at='2026-11-19T10:00:00Z'),
            card(id='open', kind='email', status='shown', thread_id='T')]
        from contextlib import redirect_stdout
        import io
        with redirect_stdout(io.StringIO()):
            self.assertEqual(notify.claim_mail_backfill(7), 2)
        self.assertEqual([c[4] for c in self.claims()], [UUID, other])  # the last sender ends as primary
        self.assertEqual(self.delivers(), [])
        self.assertEqual(len(seen), 1)
