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
