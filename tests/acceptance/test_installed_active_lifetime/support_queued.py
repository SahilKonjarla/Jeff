"""Unchanged queued/normal-stop child program and graceful load helper."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from jeff.application.dispatch import DispatchReply, EvidenceQuery, ProjectCall
from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_ipc import RuntimeProjectClient
from jeff.infrastructure.runtime_process import RuntimeController

_QUEUED_BOOT = r"""
import json,os,threading,time
from pathlib import Path
import jeff.bootstrap.runtime as runtime
from jeff.application.dispatch import ProjectCall
root=Path(os.environ['JEFF_TEST_D6_GATE']);window=os.environ['JEFF_TEST_D6_WINDOW']
runners=[]
def mark(name,value):
 path=root/(name+'.'+str(threading.get_ident())+'.tmp')
 path.write_text(json.dumps(value));path.replace(root/name)
base_runner=runtime.OwnedGitRunner
class ObservedRunner(base_runner):
 def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);runners.append(self)
runtime.OwnedGitRunner=ObservedRunner
base_storage=runtime.SQLiteStorage
class HeldStorage(base_storage):
 def hold(self,request_id,state):
  if not (root/'arm').exists():return
  mark('claimed.'+str(request_id),{'request_id':str(request_id),'state':state})
  count=7 if window in ('queued','stop_drain','normal_stop')else 4 if window=='running'else 1
  while (root/'arm').exists():
   claims=sorted(root.glob('claimed.*'));admitted=list(root.glob('admitted.*'))
   claims=[p for p in claims if not p.name.endswith('.tmp')]
   admitted=[p for p in admitted if not p.name.endswith('.tmp')]
   expected=4 if window in ('queued','running','stop_drain','normal_stop')else 1
   if len(claims)==expected and len(admitted)==count and all(r.closed for r in runners):
    facts=[json.loads(p.read_text())for p in claims]
    mark('ready',{'claimed':[row['request_id']for row in facts],
                  'states':[row['state']for row in facts],
                  'admitted':count,'helpers_closed':True})
   time.sleep(.01)
 def claim(self,claim,**kwargs):
  result=super().claim(claim,**kwargs)
  if window in ('queued','stop_drain','normal_stop'):
   self.hold(claim.request_id,result.receipt.state)
  return result
 def finish(self,transition,**kwargs):
  result=super().finish(transition,**kwargs)
  if ((window=='running' and transition.state=='running') or
      (window=='terminal_duplicate' and transition.state=='succeeded')):
   self.hold(transition.request_id,result.state)
  return result
runtime.SQLiteStorage=HeldStorage
base_dispatch=runtime.RuntimeDispatcher
class ObservedDispatcher(base_dispatch):
 def begin_drain(self):
  super().begin_drain();mark('draining',{'admission_sealed':True})
 def submit(self,call,deadline):
  result=super().submit(call,deadline)
  if isinstance(call,ProjectCall) and (root/'arm').exists():
   mark('admitted.'+str(call.request_id),{'request_id':str(call.request_id)})
  return result
runtime.RuntimeDispatcher=ObservedDispatcher
raise SystemExit(runtime.main())
"""


def _normal_stop_under_load(
    layout: RuntimeLayout, git_repo: Path, gate: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinct graceful lifecycle: same production owner/dispatch, no fault signal."""
    gate.mkdir(mode=0o700)
    original = subprocess.Popen

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert command[3:5] == ["-m", "jeff.bootstrap.runtime"]
        kwargs["env"] = {
            **kwargs["env"],
            "JEFF_TEST_D6_GATE": str(gate),
            "JEFF_TEST_D6_WINDOW": "normal_stop",
        }
        return original([*command[:3], "-c", _QUEUED_BOOT, *command[5:]], **kwargs)

    control, client = RuntimeController(layout), RuntimeProjectClient(layout)
    calls = [
        ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
        for _ in range(7)
    ]
    outcomes: dict[str, DispatchReply | Exception] = {}
    callers: list[threading.Thread] = []
    stops: list[object] = []
    stopper: threading.Thread | None = None

    def invoke(call: ProjectCall) -> None:
        try:
            outcomes[str(call.request_id)] = client.execute(call)
        except Exception as error:
            outcomes[str(call.request_id)] = error

    def request_stop() -> None:
        try:
            stops.append(control.stop())
        except Exception as error:
            stops.append(error)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            started = control.start()
        assert isinstance(started, ControlSuccess)
        captured = control._child
        assert captured is not None
        (gate / "arm").touch(mode=0o600)
        for call in calls:
            thread = threading.Thread(target=invoke, args=(call,))
            callers.append(thread)
            thread.start()
        until = time.monotonic() + 10
        while not (gate / "ready").exists():
            assert time.monotonic() < until and captured.poll() is None
            time.sleep(0.01)
        claimed = set(json.loads((gate / "ready").read_text())["claimed"])
        assert len(claimed) == 4
        stopper = threading.Thread(target=request_stop)
        stopper.start()
        until = time.monotonic() + 5
        while not (gate / "draining").exists():
            assert time.monotonic() < until and captured.poll() is None
            time.sleep(0.01)
        (gate / "arm").unlink()
        for thread in callers:
            thread.join(15)
            assert not thread.is_alive()
        stopper.join(15)
        assert not stopper.is_alive() and len(stops) == 1 and isinstance(stops[0], ControlSuccess)
        assert captured.wait(timeout=5) == 0
        assert len(outcomes) == 7
        for request_id, reply in outcomes.items():
            assert isinstance(reply, DispatchReply)
            if request_id in claimed:
                assert reply.result.ok
            else:
                assert not reply.result.ok and reply.accepted is not True
        assert isinstance(control.start(), ControlSuccess)
        for call in calls:
            trace = client.inspect_evidence(
                EvidenceQuery(operation="trace", directory=git_repo, request_id=call.request_id)
            )
            assert trace.error is None
            if str(call.request_id) in claimed:
                assert trace.receipt is not None and trace.receipt.state == "succeeded"
            else:
                assert trace.receipt is None and trace.missing_is_not_nonexecution_proof
    finally:
        (gate / "arm").unlink(missing_ok=True)
        for thread in callers:
            thread.join(15)
            assert not thread.is_alive()
        if stopper is not None:
            stopper.join(15)
            assert not stopper.is_alive()
        assert isinstance(control.stop(), ControlSuccess)
