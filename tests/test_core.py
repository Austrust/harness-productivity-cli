import datetime as dt
import json
from pathlib import Path
import threading
from types import SimpleNamespace as NS

import pytest
import requests

from productivity import engine
from productivity.common import Problem, UTC, parse_time, readback, same_time, zone
from productivity.state import Journal, State
from productivity.caldav_backend import CalDAV


@pytest.fixture
def state(tmp_path):
    s=State(tmp_path,"test")
    # Test directories inherit the isolated workspace ACL; credential ACL tested separately.
    s.path.mkdir(parents=True)
    return s


CFG={"provider":"caldav","username":"fake@example.invalid","server":"https://caldav.icloud.com","timezone":"Asia/Shanghai"}
TARGET={"id":"https://p01-caldav.icloud.com/user/work/","name":"Work","read_only":False}
CREATE={"calendar":"Work","request_id":"message-001","title":"组会：研发 / a\nb","start":"2026-10-23T15:00:00+08:00","duration":60,"alarms":[0,10],"notes":"line1\nline2"}


class MemoryHTTP:
    def __init__(self): self.data={}; self.puts=0; self.fail_after_put=False; self.auths=[]
    def request(self,method,url,**kwargs):
        self.auths.append(kwargs.get("auth"))
        r=requests.Response(); r.url=url; r.status_code=200; r._content=b""
        if method=="GET":
            if url not in self.data: r.status_code=404
            else: r._content=self.data[url]; r.headers["ETag"]='"1"'
        elif method=="PUT":
            if kwargs["headers"].get("If-None-Match")=="*" and url in self.data: r.status_code=412
            else:
                self.puts+=1; self.data[url]=kwargs["data"]; r.status_code=201
                if self.fail_after_put: raise requests.Timeout("do not expose this secret")
        elif method=="REPORT":
            import xml.etree.ElementTree as E
            root=E.Element("{DAV:}multistatus")
            for href,data in self.data.items():
                response=E.SubElement(root,"{DAV:}response"); E.SubElement(response,"{DAV:}href").text=href
                stat=E.SubElement(response,"{DAV:}propstat"); E.SubElement(stat,"{DAV:}status").text="HTTP/1.1 200 OK"
                prop=E.SubElement(stat,"{DAV:}prop"); E.SubElement(prop,"{urn:ietf:params:xml:ns:caldav}calendar-data").text=data.decode()
                E.SubElement(prop,"{DAV:}getetag").text='"1"'
            r._content=E.tostring(root)
        elif method=="DELETE":
            if url in self.data: del self.data[url]; r.status_code=204
            else: r.status_code=404
        return r


def cal_backend():
    http=MemoryHTTP(); backend=CalDAV(CFG,"synthetic-app-password",session=http)
    backend._calendars=[TARGET]
    return backend,http


@pytest.mark.parametrize("value,all_day",[("2026-10-23",False),("2026-02-30",True),("tomorrow 8",False)])
def test_invalid_dates_rejected(value,all_day):
    with pytest.raises(Problem): parse_time(value,all_day=all_day)


def test_dst_gap_and_fold_rejected():
    for value in ("2026-03-08T02:30:00","2026-11-01T01:30:00"):
        with pytest.raises(Problem): parse_time(value,"America/New_York")
    assert parse_time("2026-11-01T01:30:00-04:00","America/New_York").hour==1


def test_calendar_roundtrip_and_replay(state):
    b,http=cal_backend()
    first=engine.calendar_create(state,CFG,b,CREATE)
    assert first["verification"]["verified"]
    assert first["item"]["title"]==CREATE["title"]
    assert first["item"]["alarms"]==[0,10]
    assert first["item"]["start"]==CREATE["start"]
    second=engine.calendar_create(state,CFG,b,CREATE)
    assert second["replayed"] and http.puts==1
    with pytest.raises(Problem,match="different input"):
        engine.calendar_create(state,CFG,b,{**CREATE,"title":"Changed"})


def test_timeout_after_success_does_not_duplicate(state):
    b,http=cal_backend(); http.fail_after_put=True
    result=engine.calendar_create(state,CFG,b,CREATE)
    assert result["verification"]["verified"]
    engine.calendar_create(state,CFG,b,CREATE)
    assert http.puts==1


def test_readback_mismatch_not_success(state):
    b,http=cal_backend()
    original=b.get
    b.get=lambda *a: {**original(*a),"title":"Wrong stored title"}
    with pytest.raises(Problem) as error: engine.calendar_create(state,CFG,b,CREATE)
    assert error.value.code=="verification_failed"
    assert error.value.details["resource"]
    assert http.puts==1


def test_unknown_write_replay_never_recreates(state):
    b,http=cal_backend()
    def reject(*a): raise Problem("network_error","Disconnected")
    b.create=reject
    with pytest.raises(Problem) as first: engine.calendar_create(state,CFG,b,CREATE)
    assert first.value.code=="write_unconfirmed"
    b.create=lambda *a: pytest.fail("replay must never mutate")
    with pytest.raises(Problem) as second: engine.calendar_create(state,CFG,b,CREATE)
    assert second.value.details["replayed"] is True


def test_all_day_calendar_end_exclusive(state):
    b,http=cal_backend()
    result=engine.calendar_create(state,CFG,b,{"title":"假期","start":"2026-10-23","all_day":True,"calendar":"Work","request_id":"day-1"})
    assert result["item"]["start"]=="2026-10-23"
    assert result["item"]["end"]=="2026-10-24"
    assert result["item"]["all_day"] is True


def test_calendar_move_preserves_duration(state):
    b,http=cal_backend()
    first=engine.calendar_create(state,CFG,b,CREATE)
    result=engine.calendar_update(state,CFG,b,{"calendar":"Work","id":first["item"]["id"],"start":"2026-10-23T19:00:00+08:00","request_id":"move-1"})
    assert result["item"]["end"]=="2026-10-23T20:00:00+08:00"
    assert result["verification"]["verified"]


def test_delete_confirm_and_verified(state):
    b,http=cal_backend(); first=engine.calendar_create(state,CFG,b,CREATE)
    raw={"calendar":"Work","id":first["item"]["id"],"request_id":"delete-1"}
    with pytest.raises(Problem) as e: engine.delete_verified(state,CFG,b,raw,kind="calendar")
    assert e.value.code=="confirmation_required"
    result=engine.delete_verified(state,CFG,b,{**raw,"confirm":True},kind="calendar")
    assert result["deleted"] and result["verification"]["verified"]


def test_caldav_credentials_stay_on_trusted_hosts():
    b,http=cal_backend()
    assert b.trusted("https://p42-caldav.icloud.com/test")
    for url in ("https://icloud.com.attacker.invalid/","http://p01-caldav.icloud.com/","https://example.invalid/"):
        with pytest.raises(Problem): b.request("GET",url)
    assert not http.auths


def test_calendar_lists_no_silent_first_choice():
    b,_=cal_backend(); b._calendars=[TARGET,{**TARGET,"id":"https://p01-caldav.icloud.com/other/"}]
    with pytest.raises(Problem) as e: b.target("Work")
    assert e.value.code=="ambiguous_target"


def test_journal_concurrency(state):
    Journal(state); results=[]
    def work(): results.append(Journal(state).reserve("scope","request",{"value":1}))
    threads=[threading.Thread(target=work) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(results)==8 and sum(fresh for _,fresh in results)==1
    assert len({rid for rid,_ in results})==1


def test_priority_mapping_not_reversed():
    assert engine.reminder_payload({"title":"x","priority":"high"},"Asia/Shanghai")["priority"]==1
    assert engine.reminder_payload({"title":"x","priority":"low"},"Asia/Shanghai")["priority"]==9


@pytest.mark.skipif(__import__('os').name!='nt',reason="Windows DPAPI")
def test_windows_credential_roundtrip(state):
    state.secret_set("test-secret","中文-fake-secret")
    path=state.path/"test-secret.dpapi"
    assert b"fake-secret" not in path.read_bytes()
    assert state.secret_get("test-secret")=="中文-fake-secret"
    state.secret_delete("test-secret")
    assert state.secret_get("test-secret") is None
