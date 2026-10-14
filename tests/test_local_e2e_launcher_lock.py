"""Injectable mutex contract; these tests never touch a real startup process."""
from pathlib import Path
import importlib.util
import pytest
SPEC=importlib.util.spec_from_file_location("launcher_mutex_test",Path(__file__).resolve().parents[1]/"scripts/local_e2e/launcher_lock.py")
MOD=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)

class API:
    def __init__(self, outcomes):
        self.outcomes=list(outcomes);self.releases=[];self.closed=[];self.waits=[]
    def create(self,name): return 123
    def wait(self,handle,milliseconds):
        self.waits.append(milliseconds)
        return self.outcomes.pop(0),6
    def release(self,handle): self.releases.append(handle)
    def close(self,handle): self.closed.append(handle)

def test_contention_waits_then_acquires_instead_of_a_busy_popup():
    api=API([258,258,0]);lock=MOD.LauncherLock("fixture",api=api)
    assert lock.acquire() is False
    progress=[]
    assert lock.wait_until_free(timeout_s=2,progress=lambda: progress.append("waiting"))
    assert len(progress)==2
    lock.close()
    assert api.releases==[123] and api.closed==[123]

@pytest.mark.parametrize("value",[0,128])
def test_free_or_abandoned_mutex_is_usable(value):
    api=API([value]);lock=MOD.LauncherLock("fixture",api=api)
    assert lock.acquire()
    lock.close();lock.close()
    assert api.releases==[123] and api.closed==[123]

@pytest.mark.parametrize("value",[-1,0xFFFFFFFF])
def test_win32_failure_is_not_mislabeled_as_another_build(value):
    api=API([value]);lock=MOD.LauncherLock("fixture",api=api)
    with pytest.raises(OSError,match="mutex wait failed"): lock.acquire()
    lock.close()
    assert api.releases==[] and api.closed==[123]

def test_wait_timeout_does_not_release_someone_elses_lock():
    api=API([258]);lock=MOD.LauncherLock("fixture",api=api)
    assert not lock.acquire()
    assert not lock.wait_until_free(timeout_s=0)
    lock.close()
    assert api.releases==[] and api.closed==[123]

def test_stale_progress_file_is_not_an_ownership_lock(tmp_path):
    (tmp_path/"current-run.json").write_text("{\"phase\":\"prepare\",\"pid\":999999}")
    api=API([0]);lock=MOD.LauncherLock("fixture",api=api)
    assert lock.acquire()
    lock.close()
