"""harness-alarm: channel order and what counts as sent, with fake push, ssh, osascript and a local Slack API."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: F401,E402
import http.server, json, subprocess, tempfile, threading, unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / 'bin/harness-alarm'


def script(path, body):
    Path(path).write_text('#!/bin/sh\n' + body + '\n')
    os.chmod(path, 0o755)
    return str(path)


class AlarmTest(unittest.TestCase):
    def setUp(self):
        d = self.d = tempfile.mkdtemp(prefix='alarm-')
        self.posts = []
        self.slack_ok = True
        test = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                test.posts.append((self.headers.get('Authorization'), body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'ok': test.slack_ok}).encode())

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.token = os.path.join(d, 'bot-token')
        Path(self.token).write_text('xoxb-test\n')
        os.chmod(self.token, 0o600)
        self.env = dict(os.environ, HARNESS_ALARM='1', AGENT_BUS_ROLE='server',
                        HARNESS_ALARM_STATE_DIR=os.path.join(d, 'state'),
                        HARNESS_ALARM_SLACK_TOKEN_FILE=self.token, HARNESS_ALARM_SLACK_CHANNEL='D123',
                        HARNESS_ALARM_SLACK_URL='http://127.0.0.1:%d/api/chat.postMessage' % self.srv.server_port,
                        HARNESS_ALARM_OSASCRIPT=script(os.path.join(d, 'osa'), 'echo osa >> %s/osa.log' % d),
                        HARNESS_ALARM_PUSH_CWD=d)
        self.set_push(0)
        self.env['HARNESS_ALARM_SSH_CMD'] = script(os.path.join(d, 'ssh'), 'exit 255')

    def set_push(self, rc):
        self.env['HARNESS_ALARM_PUSH_CMD'] = script(os.path.join(self.d, 'push'), 'exit %d' % rc)

    def alarm(self, key='k1'):
        p = subprocess.run([sys.executable, str(BIN), '--key', key, '--title', 'T', '--text', 'X'],
                           env=self.env, capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout.strip()

    def osa_calls(self):
        try:
            return len(open(os.path.join(self.d, 'osa.log')).readlines())
        except OSError:
            return 0

    def test_server_push_first(self):
        self.assertEqual(self.alarm(), (0, 'push'))
        self.assertEqual(self.posts, [])

    def test_server_slack_when_push_fails(self):
        self.set_push(1)
        self.assertEqual(self.alarm(), (0, 'slack'))
        auth, body = self.posts[0]
        self.assertEqual(auth, 'Bearer xoxb-test')
        self.assertEqual(body['channel'], 'D123')
        self.assertIn('T', body['text'])

    def test_server_osascript_alone_is_not_sent(self):
        self.set_push(1)
        self.slack_ok = False
        self.assertEqual(self.alarm(), (1, 'failed'))
        self.assertEqual(self.osa_calls(), 1)
        # not in its quiet time: the next call tries every channel again
        self.slack_ok = True
        self.assertEqual(self.alarm(), (0, 'slack'))

    def test_token_file_open_to_others_is_not_used(self):
        self.set_push(1)
        os.chmod(self.token, 0o644)
        self.assertEqual(self.alarm(), (1, 'failed'))
        self.assertEqual(self.posts, [])

    def test_quiet_time_after_a_send(self):
        self.assertEqual(self.alarm(), (0, 'push'))
        self.assertEqual(self.alarm(), (0, 'suppressed'))
        self.assertEqual(self.alarm('k2'), (0, 'push'))

    def test_laptop_ssh_down_osascript_counts(self):
        self.env['AGENT_BUS_ROLE'] = 'client'
        self.assertEqual(self.alarm(), (0, 'osascript'))
        self.assertEqual(self.posts, [])

    def test_laptop_ssh_ok(self):
        self.env['AGENT_BUS_ROLE'] = 'client'
        self.env['HARNESS_ALARM_SSH_CMD'] = script(os.path.join(self.d, 'ssh'), 'exit 0')
        self.assertEqual(self.alarm(), (0, 'ssh'))
        self.assertEqual(self.osa_calls(), 0)

    def test_no_push_command_and_no_slack_setup_skips_both(self):
        env = dict(self.env, AGENT_BUS_ROLE='server')
        del env['HARNESS_ALARM_PUSH_CMD']
        del env['HARNESS_ALARM_SLACK_CHANNEL']
        env['HOME'] = self.d  # no ~/.config/harness-alarm/slack-channel here
        p = subprocess.run([sys.executable, str(BIN), '--key', 'k9', '--title', 'T'], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual((p.returncode, p.stdout.strip()), (1, 'failed'))
        self.assertEqual(self.posts, [])

    def test_laptop_ssh_host_comes_from_the_server_host_setting(self):
        self.env['AGENT_BUS_ROLE'] = 'client'
        self.env['AGENT_BUS_SERVER_HOST'] = 'my-host'
        del self.env['HARNESS_ALARM_SSH_CMD']
        self.env['PATH'] = self.d + os.pathsep + self.env['PATH']
        script(os.path.join(self.d, 'ssh'), 'echo "$@" >> %s/ssh.log\nexit 0' % self.d)
        self.assertEqual(self.alarm(), (0, 'ssh'))
        self.assertIn('my-host', open(os.path.join(self.d, 'ssh.log')).read())

    def test_off(self):
        self.env['HARNESS_ALARM'] = '0'
        self.assertEqual(self.alarm(), (0, 'off'))


if __name__ == '__main__':
    unittest.main()
