"""An agent builds and verifies a full-stack app — no server anywhere.

The agent gets the apps runtime: it writes backend handlers under
/app/api/, tests them instantly with the `curl` terminal builtin,
writes a frontend, and verifies the whole thing headlessly with
test_app (screenshots come back as real images to vision models and
persist under /app/screenshots/).

Run:  ANTHROPIC_API_KEY=... uv run python examples/webapp.py
Deps: pip install nontainer[agno,apps] anthropic
      playwright install chromium
"""

import tempfile

from agno.agent import Agent

from nontainer import workspace
from nontainer.adapters.agno import WorkspaceTools
from nontainer.apps import enable_apps

from _model import pick_model

TASK = """\
Build a tiny guestbook web app in this workspace:

1. Backend: /app/api/entries.py with get (returns {"entries": [...]}
   from the cache) and post (adds req.require('name') to the list).
2. Test the backend with curl (GET, then POST a couple of names, then
   GET again) before writing any frontend.
3. Frontend: /app/index.html — plain JS, RELATIVE urls — showing the
   entries as a list with an input + button to add one.
4. Verify with test_app: read the list, add an entry through the UI,
   assert the list grew, and take a screenshot.

Report what you verified."""


def main() -> None:
    with workspace("guestbook-demo", store=tempfile.mkdtemp()) as ws:
        runtime = enable_apps(ws)
        agent = Agent(
            model=pick_model(),
            tools=[WorkspaceTools(ws, apps=runtime)],
            tool_call_limit=30,
            markdown=False,
        )
        run = agent.run(TASK)
        print("=== agent ===\n", run.content)

        print("\n=== the app the agent left behind ===")
        print(ws.terminal("find /app -name '*.py' -o -name '*.html' -o -name '*.png'").stdout)

        print("=== guestbook state ===")
        print(ws.cache.get("entries", ws.terminal("curl /api/entries").stdout))

        print("\n=== versioned history (newest first) ===")
        for c in ws.history(limit=12):
            print(f"  {c.id[:10]}  {c.info}")


if __name__ == "__main__":
    main()
