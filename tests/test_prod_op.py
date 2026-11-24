"""prod-op: the script kind, the plugin loader (a toy plugin) and the owner-facing text, with a fake Polar and a temp HOME."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

TOOL = Path(__file__).resolve().parents[1] / 'bin/prod-op'

FAKE_POLAR = r'''#!/usr/bin/env python3
import json, os, sys, uuid
path = os.path.join(os.environ["HOME"], "polar.json")
cards = json.load(open(path)) if os.path.exists(path) else {}
a = sys.argv[1:]
if a[:2] == ["ask", "decision"]:
    cid = uuid.uuid4().hex[:12]
    cards[cid] = {"id": cid, "kind": "decision", "status": "shown", "title": a[a.index("--title") + 1],
                  "context": a[a.index("--context") + 1], "chosen_option": None,
                  "options": [a[i + 1] for i, x in enumerate(a) if x == "--option"]}
    json.dump(cards, open(path, "w")); print(json.dumps({"id": cid}))
elif a[:1] == ["status"]:
    print(json.dumps(cards[a[1]]))
else:
    sys.exit(3)
'''

TOY_PLUGIN = '''
OP = None


def build(a, op_dir):
    if a.word == "bad":
        raise OP.Refuse("the toy refuses the word bad")
    spec = {"kind": "toy", "word": a.word}
    return spec, f"toy {a.word}", f"dry: would say {a.word}\\n", f"Toy says {a.word}"


def apply(spec, op_dir, polar_id):
    return 0, f"toy said {spec['word']} for {polar_id}\\n"


def register(op):
    global OP
    OP = op
    op.register_kind("toy", build, apply, lambda k: k.add_argument("--word", required=True))
'''


class ProdOpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / 'bin').mkdir()
        (self.home / 'bin/polar').write_text(FAKE_POLAR)
        (self.home / 'bin/polar').chmod(0o755)
        self.plugins = self.home / 'plugins'
        self.plugins.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *args, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith(('PROD_OP_', 'AGENT_BUS_LANG', 'POLAR_'))}
        e.update(HOME=str(self.home), POLAR_BIN=str(self.home / 'bin/polar'),
                 PROD_OP_SCRIPT_ROOT=str(self.home / 'scripts'))
        e.pop('TMUX', None)
        e.update(env)
        return subprocess.run(['python3', str(TOOL), *args], env=e, capture_output=True, text=True)

    def cards(self):
        return json.loads((self.home / 'polar.json').read_text())

    def choose(self, option):
        cards = self.cards()
        for c in cards.values():
            c['status'], c['chosen_option'] = 'chosen', option
        (self.home / 'polar.json').write_text(json.dumps(cards))

    def toy(self):
        (self.plugins / 'toy.py').write_text(TOY_PLUGIN)

    def test_without_plugins_only_script_exists(self):
        p = self.run_tool('request', 'toy', '--why', 'x')
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('invalid choice', p.stderr)

    def test_plugin_kind_runs_only_after_go_and_once(self):
        self.toy()
        env = {'PROD_OP_PLUGINS': str(self.plugins)}
        p = self.run_tool('request', 'toy', '--word', 'hi', '--why', 'say hi', **env)
        self.assertEqual(p.returncode, 0, p.stderr)
        op_id = p.stdout.split()[1].rstrip(':')
        card = next(iter(self.cards().values()))
        self.assertTrue(card['title'].startswith('Production: toy hi'))
        self.assertIn('Exact change: Toy says hi', card['context'])
        self.assertEqual(card['options'], ['go:Run in production', 'no:Do not run'])
        p = self.run_tool('run', op_id, **env)
        self.assertEqual(p.returncode, 2)
        self.assertIn('no Go yet', p.stderr)
        self.choose('go')
        p = self.run_tool('run', op_id, **env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('toy said hi for', p.stdout)
        p = self.run_tool('run', op_id, **env)
        self.assertEqual(p.returncode, 2)
        self.assertIn('runs once', p.stderr)

    def test_plugin_refusal_leaves_no_op(self):
        self.toy()
        p = self.run_tool('request', 'toy', '--word', 'bad', '--why', 'x', PROD_OP_PLUGINS=str(self.plugins))
        self.assertEqual(p.returncode, 2)
        self.assertIn('refuses the word bad', p.stderr)
        self.assertFalse((self.home / '.local/state/prod-op/ops').exists() and
                         any((self.home / '.local/state/prod-op/ops').iterdir()))

    def test_a_plugin_cannot_replace_a_kind(self):
        (self.plugins / 'dup.py').write_text(
            'def register(op):\n    op.register_kind("script", None, None)\n')
        p = self.run_tool('list', PROD_OP_PLUGINS=str(self.plugins))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('already registered', p.stderr)

    def test_spanish_card_text(self):
        self.toy()
        p = self.run_tool('request', 'toy', '--word', 'hi', '--why', 'di hola',
                          PROD_OP_PLUGINS=str(self.plugins), AGENT_BUS_LANG='es')
        self.assertEqual(p.returncode, 0, p.stderr)
        card = next(iter(self.cards().values()))
        self.assertTrue(card['title'].startswith('Producción: toy hi'))
        self.assertEqual(card['options'], ['go:Correr en producción', 'no:No correr'])

    def test_script_kind_uses_the_pinned_copy(self):
        src = self.home / 'scripts/seed-x'
        src.mkdir(parents=True)
        (src / 'run.sh').write_text('echo "mode=$1"; [ "$1" = apply ] && touch "$HOME/applied"; exit 0\n')
        p = self.run_tool('request', 'script', '--name', 'seed-x', '--why', 'seed')
        self.assertEqual(p.returncode, 0, p.stderr)
        op_id = p.stdout.split()[1].rstrip(':')
        (src / 'run.sh').write_text('touch "$HOME/evil"\n')
        self.choose('go')
        p = self.run_tool('run', op_id)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((self.home / 'applied').exists())
        self.assertFalse((self.home / 'evil').exists())

    def test_extra_secret_pattern_is_hidden(self):
        self.toy()
        (self.plugins / 'toy.py').write_text(TOY_PLUGIN.replace('toy said', 'toy said tok_abc123 and'))
        env = {'PROD_OP_PLUGINS': str(self.plugins), 'PROD_OP_SECRET_EXTRA': r'tok_[a-z0-9]+'}
        p = self.run_tool('request', 'toy', '--word', 'hi', '--why', 'x', **env)
        op_id = p.stdout.split()[1].rstrip(':')
        self.choose('go')
        p = self.run_tool('run', op_id, **env)
        self.assertIn('[secret]', p.stdout)
        self.assertNotIn('tok_abc123', p.stdout)


if __name__ == '__main__':
    unittest.main()
