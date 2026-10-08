"""Agent code that waits for a delegate inside ``run_python``.

A ``run_python`` call holds the parent's workspace lock for as long as
it runs, and the delegate's answer lands on a worker thread of its own.
Landing used to read the parent through that lock, so code that asked a
delegate and waited for its answer hung until the call timed out. These
run that code on each rung, through a live host object that asks the
parent's :class:`~nontainer.sessions.Sessions`, the way a harness's
code-level ``ask`` will.

Each case runs in a Python process of its own: a deadlock would leave
the delegate pool's threads stuck, and the interpreter waits for them
at exit, so a case run in this process would hang the test run rather
than fail it.
"""

import json
import subprocess
import sys
import textwrap

import pytest

#: How long a case may take before it counts as hung.
PATIENCE = 60.0

CASE = textwrap.dedent(
    """
    import json, sys
    from nontainer import Profile, PythonConfig, Store
    from nontainer.sessions import Sessions
    from nontainer.wsgit import register_wsgit

    root, rung, how = sys.argv[1:4]


    class Delegating:
        sessions = None

        def ask_and_wait(self, task: str) -> str:
            return str(self.sessions.ask(task, wait=True))

        def ask_then_wait(self, task: str) -> str:
            job = self.sessions.ask(task)
            self.sessions.wait()
            return str(self.sessions.result(job.name))


    class Writing:
        def __init__(self, store):
            self.store = store

        def run(self, session, task, *, budget=None):
            child = self.store.open(session)
            try:
                child.files.write("/workspace/answer.txt", task)
            finally:
                child.close()
            return f"did {task}"


    def profile(objects):
        if rung == "dud":
            from nontainer.executor_dud import DudExecutor

            return Profile(
                python=PythonConfig(host_objects=objects),
                executor_factory=lambda: DudExecutor(backend="subprocess"),
            )
        return Profile(python=PythonConfig(isolation=rung, host_objects=objects))


    if __name__ == "__main__":
        store = Store(root)
        delegating = Delegating()
        parent = store.open("analyst", profile=profile({"delegate": delegating}))
        register_wsgit(parent)
        parent.files.write("/workspace/report.md", "# Rates\\n")
        parent.index.commit("seed")
        sessions = Sessions(parent, Writing(store))
        delegating.sessions = sessions
        out = parent.run_python(f"print(delegate.{how}('count the rows'))")
        (job,) = sessions.list()
        print(json.dumps({
            "stdout": out.stdout,
            "error": out.error,
            "status": job.status,
            "seed": list(job.changed["seed"]),
        }))
        sessions.close()
        parent.close()
        store.close()
    """
)


@pytest.mark.parametrize("rung", ["none", "process", "dud"])
@pytest.mark.parametrize("how", ["ask_and_wait", "ask_then_wait"])
def test_code_waiting_for_a_delegate_gets_its_answer(tmp_path, rung, how):
    if rung == "dud":
        if sys.version_info < (3, 11):
            pytest.skip("dud needs Python 3.11+")
        pytest.importorskip("dud")
    script = tmp_path / "case.py"
    script.write_text(CASE)
    try:
        done = subprocess.run(
            [sys.executable, str(script), str(tmp_path / "store"), rung, how],
            capture_output=True,
            text=True,
            timeout=PATIENCE,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the call hung waiting for the delegate")
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert not seen["error"], seen["error"]
    assert "did count the rows" in seen["stdout"]
    assert seen["status"] == "answered"
    assert seen["seed"] == ["/workspace/answer.txt"]
