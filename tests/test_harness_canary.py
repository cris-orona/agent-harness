"""harness-canary: every step with fakes (polar, agent-bus, tmux, web-app commands, a local web API). No real state."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: F401,E402
import http.server, json, subprocess, tempfile, threading, unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / 'bin/harness-canary'


def script(path, body):
    Path(path).write_text('#!/bin/sh\n' + body + '\n')
    os.chmod(path, 0o755)
    return str(path)


class CanaryTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='canary-')
        d = self.d
        self.alarms = os.path.join(d, 'alarms.log')
        self.inbox = os.path.join(d, 'state', 'inbox')
        os.makedirs(os.path.dirname(self.inbox))
        self.acts = []
        test = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                test.acts.append((self.path, self.headers.get('Authorization'), body))
                if test.pending > 0:  # Polar has not shown the card yet
                    test.pending -= 1
                    self.send_response(409)
                    self.end_headers()
                    self.wfile.write(b'{"ok": false, "error": "no abierta", "status": "pending"}')
                    return
                if test.deliver:  # polar-notify would type the answer into the session
                    open(test.inbox, 'a').write('Polar: %s chose %s\n' % (body['id'], body['option']))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *a):
                pass

        self.deliver = True
        self.pending = 0
        self.srv = http.server.HTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        open(os.path.join(d, 'token'), 'w').write('tok123\n')
        self.env = dict(os.environ,
                        HARNESS_CANARY_DIR=os.path.join(d, 'state'), HARNESS_CANARY_WAIT='3',
                        HARNESS_CANARY_POLAR_API='http://127.0.0.1:%d' % self.srv.server_port,
                        HARNESS_CANARY_TOKEN=os.path.join(d, 'token'), HARNESS_CANARY_MANDO_CWD=d,
                        HARNESS_ALARM_BIN=script(os.path.join(d, 'alarm'), 'echo "$@" >> %s' % self.alarms),
                        POLAR_BIN=script(os.path.join(d, 'polar'),
                                         'echo "$@" >> %s/polar.args\n[ "$1" = ask ] && echo \'{"id": "card-1"}\'; exit 0' % d),
                        AGENT_BUS_BIN=script(os.path.join(d, 'bus'), 'exit 0'),
                        HARNESS_CANARY_TMUX=script(os.path.join(d, 'tmux'), 'exit 0'))
        self.set_mando(store=0, push=0)

    def set_mando(self, store, push):
        self.env['HARNESS_CANARY_MANDO_STORE_CMD'] = script(os.path.join(self.d, 'store'), 'exit %d' % store)
        self.env['HARNESS_CANARY_MANDO_PUSH_CMD'] = script(os.path.join(self.d, 'pushh'), 'exit %d' % push)

    def run_canary(self):
        p = subprocess.run([sys.executable, str(BIN)], env=self.env, capture_output=True, text=True, timeout=60)
        last = json.load(open(os.path.join(self.d, 'state', 'last.json')))
        return p.returncode, last

    def alarm_keys(self):
        try:
            return [l.split()[1] for l in open(self.alarms)]
        except OSError:
            return []

    def test_all_steps_pass(self):
        rc, last = self.run_canary()
        self.assertEqual(rc, 0, last)
        self.assertTrue(last['ok'])
        self.assertEqual(set(last['steps']), {'session', 'card', 'visible', 'answer', 'delivery', 'push'})
        self.assertTrue(all(s['ok'] and 'at' in s and 'ms' in s for s in last['steps'].values()))
        self.assertEqual(self.alarm_keys(), [])
        path, auth, body = self.acts[0]
        self.assertEqual((path, auth), ('/act', 'Bearer tok123'))
        self.assertEqual(body, {'id': 'card-1', 'action': 'choose', 'option': 'yes'})
        args = open(os.path.join(self.d, 'polar.args')).read()
        self.assertIn('ask decision', args)
        self.assertIn('--canary', args)
        self.assertIn('discard card-1', args)  # cleanup

    def test_card_still_pending_is_tried_again_without_alarm(self):
        self.pending = 2
        rc, last = self.run_canary()
        self.assertTrue(last['steps']['answer']['ok'], last['steps']['answer'])
        self.assertEqual(len(self.acts), 3)
        self.assertNotIn('canary:answer', self.alarm_keys())

    def test_card_that_stays_pending_alarms_once(self):
        self.pending = 99
        self.env['HARNESS_CANARY_SHOWN_WAIT'] = '1'
        rc, last = self.run_canary()
        self.assertFalse(last['steps']['answer']['ok'])
        self.assertIn('pending', last['steps']['answer']['error'])
        self.assertEqual(self.alarm_keys().count('canary:answer'), 1)

    def test_delivery_never_arrives(self):
        self.deliver = False
        rc, last = self.run_canary()
        self.assertEqual(rc, 1)
        self.assertFalse(last['steps']['delivery']['ok'])
        self.assertTrue(last['steps']['push']['ok'])  # later steps still run
        self.assertEqual(self.alarm_keys(), ['canary:delivery'])

    def test_push_health_fails(self):
        self.set_mando(store=0, push=1)
        rc, last = self.run_canary()
        self.assertEqual(rc, 1)
        self.assertEqual(self.alarm_keys(), ['canary:push'])

    def test_card_not_visible_skips_answer(self):
        self.set_mando(store=1, push=0)
        rc, last = self.run_canary()
        self.assertEqual(self.alarm_keys(), ['canary:visible'])
        self.assertEqual(self.acts, [])
        self.assertIn('skipped', last['steps']['answer'])

    def test_steps_that_are_not_set_up_are_skipped_not_failed(self):
        for k in ('HARNESS_CANARY_MANDO_STORE_CMD', 'HARNESS_CANARY_MANDO_PUSH_CMD', 'HARNESS_CANARY_POLAR_API'):
            del self.env[k]
        rc, last = self.run_canary()
        self.assertEqual(rc, 0, last)
        for n in ('visible', 'answer', 'delivery', 'push'):
            self.assertTrue(last['steps'][n]['ok'] and 'skipped' in last['steps'][n], n)
        self.assertEqual((self.alarm_keys(), self.acts), ([], []))

    def test_session_failure_alarms_and_skips(self):
        self.env['HARNESS_CANARY_TMUX'] = script(os.path.join(self.d, 'tmux'), 'exit 1')
        rc, last = self.run_canary()
        self.assertEqual(rc, 1)
        self.assertEqual(self.alarm_keys()[0], 'canary:session')
        self.assertEqual(self.acts, [])


if __name__ == '__main__':
    unittest.main()
