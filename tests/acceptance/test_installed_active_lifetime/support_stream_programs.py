"""Unchanged stream, backpressure and retirement child-program strings only."""

from __future__ import annotations

_STREAM_BOOT = r"""
import json,os,threading,time
from pathlib import Path
import jeff.bootstrap.runtime as runtime
root=Path(os.environ['JEFF_TEST_D6_GATE']);mutex=threading.Lock();active=0
runners=[];stop=threading.Event()
base=runtime.OwnedGitRunner
class ObservedRunner(base):
 def __init__(self,*args,**kwargs):super().__init__(*args,**kwargs);runners.append(self)
 def __call__(self,*args,**kwargs):
  global active
  until=time.monotonic()+10
  while True:
   with mutex:
    if not (root/'seal').exists():active+=1;break
   assert time.monotonic()<until,'Owned fixture Git-admission gate expired'
   time.sleep(.01)
  try:return super().__call__(*args,**kwargs)
  finally:
   with mutex:active-=1
runtime.OwnedGitRunner=ObservedRunner
def observe():
 while not stop.wait(.01):
  with mutex:
   safe=(root/'seal').exists()and active==0 and all(r.closed for r in runners)
  if safe and not (root/'quiescent').exists():
   (root/'quiescent.tmp').write_text(json.dumps({'active_git_calls':0,'helpers_closed':True}))
   (root/'quiescent.tmp').replace(root/'quiescent')
watcher=threading.Thread(target=observe,name='owned-d6-observer');watcher.start()
try:code=runtime.main()
finally:stop.set();watcher.join(1);assert not watcher.is_alive()
raise SystemExit(code)
"""


_BACKPRESSURE_HOOK = r"""
import asyncio,json,os,socket
from pathlib import Path
import jeff.infrastructure.runtime_ipc as ipc
root=Path(os.environ['JEFF_TEST_D6_GATE']);slow=os.environ['JEFF_TEST_D6_SLOW_ID']
original_send=ipc._stream_send
async def send(writer,frame):
 if str(frame.request_id)!=slow:return await original_send(writer,frame)
 writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,1024)
 actual=asyncio.create_task(original_send(writer,frame))
 await asyncio.sleep(0)
 waiting=writer.transport.get_write_buffer_size()>65536 and not actual.done()
 if waiting:(root/'blocked').write_text(json.dumps({'real_write_buffer_above_64k':True}))
 try:await actual
 finally:
  if waiting:(root/'unwound').write_text('actual drain finished')
ipc._stream_send=send
"""


_RETIREMENT_HOOK = r"""
import asyncio,json,os,time
from pathlib import Path
import jeff.infrastructure.runtime_storage as sql
import jeff.infrastructure.runtime_ipc as ipc
root=Path(os.environ['JEFF_TEST_D6_GATE']);window=os.environ['JEFF_TEST_D6_WINDOW']
slow=os.environ['JEFF_TEST_D6_SLOW_ID'];base_store=sql.SQLiteStorage
class ObservedStore(base_store):
 def __init__(self,*args,**kwargs):
  kwargs['retention_clock']=lambda:(time.time_ns()//1000000+
   (32*86400000 if (root/'expire').exists()else 0))
  kwargs['retention_monotonic']=lambda:160.0 if (root/'expire').exists()else 100.0
  super().__init__(*args,**kwargs)
 def _open(self,*args):
  connection,fd=super()._open(*args);connection.execute('PRAGMA cache_size=1')
  return connection,fd
 def hold(self,connection):
  journal=self.owner.store.layout.data_dir/'state.sqlite3-journal'
  header=journal.read_bytes()[:8].hex()if journal.exists()else ''
  facts={'in_transaction':connection.in_transaction,'journal_magic':header,
         'permit_present':self.owner.store.layout.recovery_permit.exists()}
  (root/'retire-ready.tmp').write_text(json.dumps(facts))
  (root/'retire-ready.tmp').replace(root/'retire-ready')
  until=time.monotonic()+8
  while not(root/'release').exists():
   assert time.monotonic()<until,'Owned retirement gate expired'
   time.sleep(.01)
 def _commit_write(self,connection,fd):
  if getattr(self,'retiring',False)and window=='retirement_before_commit':self.hold(connection)
  return super()._commit_write(connection,fd)
 def _retire(self,connection,directory,fd):
  self.retiring=(root/'expire').exists()and not getattr(self,'fired',False)
  result=super()._retire(connection,directory,fd)
  if self.retiring:
   assert result.retired_count>0
   if window=='retirement_after_commit':self.hold(connection)
   self.fired=True
  self.retiring=False
  return result
sql.SQLiteStorage=ObservedStore
original_send=ipc._stream_send
async def send(writer,frame):
 await original_send(writer,frame)
 if str(frame.request_id)==slow and frame.kind=='event'and(root/'buffer').exists():
  (root/'buffered').write_text('real event prefix sent; immutable page still held')
  until=time.monotonic()+8
  while not(root/'release').exists():
   assert time.monotonic()<until,'Owned buffered-page gate expired'
   await asyncio.sleep(.01)
ipc._stream_send=send
"""
