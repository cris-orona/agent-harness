"""herdr-deliver: mock the shell; no herdr, no ssh."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolate  # noqa: F401,E402  (role local, temp bus.db)
import importlib.machinery
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

PATH = Path(__file__).resolve().parents[1] / 'bin/herdr-deliver'
loader = importlib.machinery.SourceFileLoader('herdr_deliver', str(PATH))
spec = importlib.util.spec_from_loader('herdr_deliver', loader)
hd = importlib.util.module_from_spec(spec)
loader.exec_module(hd)

R = '─' * 20


def screen(box, dim=''):
    return f"output\n{R}\n❯ {box}{dim}\n{R}\n  ? for shortcuts\n"


class Fake:
    """Replace hd.sh. `reads` is a list of screens; the last one repeats. `on_enter` maps the Enter count to a screen."""

    def __init__(self, reads):
        self.reads, self.calls, self.enters = list(reads), [], 0
        self.after_enter = None

    def __call__(self, cmd, timeout=40):
        self.calls.append(cmd)
        joined = ' '.join(cmd)
        if 'send-keys' in joined and joined.rstrip("'\"").endswith('Enter'):
            self.enters += 1
            if self.after_enter:
                self.reads = [self.after_enter(self.enters)]
            return 0, ''
        if 'read' in cmd or 'capture-pane' in joined:
            return 0, self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        return 0, ''


class DeliverTest(unittest.TestCase):
    def run_it(self, fake, argv):
        with mock.patch.object(hd, 'sh', fake), mock.patch.object(hd.time, 'sleep', lambda s: None), \
             mock.patch.object(hd.time, 'monotonic', iter(range(0, 10000)).__next__):
            return hd.main(argv)

    def test_success_first_try(self):
        f = Fake([screen(''), screen('hello world'), screen('hello world')])
        f.after_enter = lambda n: screen('')
        self.assertEqual(self.run_it(f, ['w1:p1', 'hello world']), 0)
        self.assertEqual(f.enters, 1)
        self.assertIn([hd.HERDR, 'pane', 'send-text', 'w1:p1', 'hello world'], f.calls)

    def test_dim_suggestion_counts_as_empty(self):
        f = Fake([screen('hello'), ])
        f.reads = [screen(''), screen('hello')]
        f.after_enter = lambda n: screen('', dim='\x1b[2msuggested next\x1b[0m')
        self.assertEqual(self.run_it(f, ['w1:p1', 'hello']), 0)

    def test_enter_retry_when_text_remains(self):
        f = Fake([screen(''), screen('hello there')])
        f.after_enter = lambda n: screen('hello there') if n < 2 else screen('')
        self.assertEqual(self.run_it(f, ['w1:p1', 'hello there']), 0)
        self.assertEqual(f.enters, 2)

    def test_owner_typing_returns_3_and_types_nothing(self):
        f = Fake([screen('half a sentence')])
        self.assertEqual(self.run_it(f, ['w1:p1', 'hello']), 3)
        self.assertEqual([c for c in f.calls if 'send-text' in c or 'send-keys' in c], [])

    def test_gives_up_with_4_after_three_tries(self):
        f = Fake([screen(''), screen('stuck text')])
        f.after_enter = lambda n: screen('stuck text')
        self.assertEqual(self.run_it(f, ['w1:p1', 'stuck text']), 4)
        self.assertEqual(f.enters, 3)
        self.assertFalse(any('clear' in ' '.join(c) for c in f.calls))

    def test_pasted_text_marker_counts_as_shown(self):
        self.assertTrue(hd.shows_text('[Pasted text #1 +20 lines]', 'a very long message'))

    def test_text_from_stdin(self):
        f = Fake([screen(''), screen('from stdin')])
        f.after_enter = lambda n: screen('')
        with mock.patch.object(hd.sys, 'stdin', mock.Mock(read=lambda: 'from stdin\n')):
            self.assertEqual(self.run_it(f, ['w1:p1', '-']), 0)
        self.assertIn([hd.HERDR, 'pane', 'send-text', 'w1:p1', 'from stdin'], f.calls)

    def test_tmux_builds_quoted_commands(self):
        f = Fake([screen(''), screen("it's ok")])
        f.after_enter = lambda n: screen('')
        self.assertEqual(self.run_it(f, ['--tmux', 'task-w1', "it's ok"]), 0)
        cmds = [c[-1] for c in f.calls if c[0] == 'ssh']
        self.assertTrue(all(c.startswith('tmux ') for c in cmds))
        self.assertIn("tmux send-keys -t '=task-w1:' -l -- 'it'\"'\"'s ok'", cmds)
        self.assertIn("tmux send-keys -t '=task-w1:' Enter", cmds)
        self.assertIn("tmux capture-pane -p -e -t '=task-w1:'", cmds)
        self.assertEqual(f.calls[0][:5], ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8'])

    def test_tmux_runs_locally_on_the_bus_server(self):
        f = Fake([screen(''), screen("hola")])
        f.after_enter = lambda n: screen('')
        with mock.patch.dict(hd.os.environ, {'AGENT_BUS_ROLE': 'server'}):
            self.assertEqual(self.run_it(f, ['--tmux', 'task-w1', 'hola']), 0)
        self.assertTrue(f.calls)
        self.assertTrue(all(c[:2] == ['/bin/sh', '-c'] for c in f.calls))
        self.assertIn("tmux send-keys -t '=task-w1:' Enter", [c[-1] for c in f.calls])
        self.assertFalse(any(c[0] == 'ssh' for c in f.calls))

    def test_usage_error(self):
        self.assertEqual(hd.main(['only-one']), 2)


if __name__ == '__main__':
    unittest.main()


class Box:
    """A simulated Claude Code box: send-text appends, C-u clears, Enter submits."""

    def __init__(self, text, change_on_read=None):
        self.text, self.sent, self.calls, self.reads = text, [], [], 0
        self.change_on_read = change_on_read  # (n, new text): the owner types on read n

    def __call__(self, cmd, timeout=40):
        self.calls.append(cmd)
        if cmd[1:3] == ['pane', 'read']:
            self.reads += 1
            if self.change_on_read and self.reads == self.change_on_read[0]:
                self.text = self.change_on_read[1]
            return 0, screen(self.text)
        if cmd[1:3] == ['pane', 'send-text']:
            self.text += cmd[4]
        elif cmd[1:3] == ['pane', 'send-keys']:
            k = cmd[4]
            if k == 'C-u':
                self.text = ''
            elif k == 'Enter' and self.text:
                self.sent.append(self.text)
                self.text = ''
        return 0, ''


class StashTest(DeliverTest):
    def test_still_draft_is_set_aside_and_typed_back(self):
        b = Box('2')
        self.assertEqual(self.run_it(b, ['--stash', 'w1:p1', 'hello']), 0)
        self.assertEqual(b.sent, ['hello'])
        self.assertEqual(b.text, '2')  # the draft is back, not submitted

    def test_without_stash_a_draft_still_blocks(self):
        b = Box('2')
        self.assertEqual(self.run_it(b, ['w1:p1', 'hello']), 3)
        self.assertEqual((b.sent, b.text), ([], '2'))

    def test_owner_typing_now_is_not_touched(self):
        b = Box('half a', change_on_read=(2, 'half a sentence'))
        self.assertEqual(self.run_it(b, ['--stash', 'w1:p1', 'hello']), 3)
        self.assertEqual((b.sent, b.text), ([], 'half a sentence'))

    def test_long_draft_is_not_moved(self):
        b = Box('x' * (hd.STASH_MAX + 1))
        self.assertEqual(self.run_it(b, ['--stash', 'w1:p1', 'hello']), 3)
        self.assertEqual(b.sent, [])
