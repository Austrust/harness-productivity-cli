import datetime as dt
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import requests

from productivity import engine
from productivity.cli import main, parser, load_input
from productivity.common import Problem
from productivity.graph_backend import Graph
from productivity.state import State


class GraphHTTP:
    def __init__(self): self.records={}; self.posts=0; self.patches=[]
    def request(self,method,url,**kw):
        r=requests.Response(); r.status_code=200
        path=url.removeprefix("https://graph.microsoft.com/v1.0")
        if path=="/me/calendars": data={"value":[{"id":"CAL1","name":"Work","canEdit":True}]}
        elif method=="POST":
            self.posts+=1; source=kw["json"]
            source={**source,"id":"EVENT1","type":"singleInstance","@odata.etag":'"etag1"'}
            # Mimic Graph's Prefer: outlook.timezone="UTC" projection.
            for key in ("start","end"):
                v=dt.datetime.fromisoformat(source[key]["dateTime"]).replace(tzinfo=__import__('zoneinfo').ZoneInfo(source[key]["timeZone"]))
                source[key]={"dateTime":v.astimezone(dt.timezone.utc).replace(tzinfo=None).isoformat(),"timeZone":"UTC"}
            self.records[source["id"]]=source; data=source; r.status_code=201
        elif method=="PATCH":
            self.patches.append(kw["json"]); self.records["EVENT1"].update(kw["json"]); data=self.records["EVENT1"]
        elif path.endswith("/calendarView") or (path.endswith("/events") and method=="GET"): data={"value":list(self.records.values())}
        elif path.endswith("/events/EVENT1") and "EVENT1" in self.records: data=self.records["EVENT1"]
        else: data={"error":{}}; r.status_code=404
        r._content=json.dumps(data).encode()
        return r


def test_graph_creation_and_extended_property_reconciliation(tmp_path):
    state=State(tmp_path); state.path.mkdir(parents=True)
    cfg={"provider":"graph","account_id":"FAKE-ACCOUNT","timezone":"Asia/Shanghai"}
    http=GraphHTTP(); backend=Graph(state,cfg,token="synthetic-token",session=http)
    p={"calendar":"Work","request_id":"message-10","title":"会议","start":"2026-10-23T15:00:00+08:00","alarms":[10]}
    result=engine.calendar_create(state,cfg,backend,p)
    assert result["verification"]["verified"] and result["item"]["start"]==p["start"]
    replay=engine.calendar_create(state,cfg,backend,p)
    assert replay["replayed"] and http.posts==1
    assert next(iter(http.records.values()))["transactionId"]


def test_graph_title_edit_does_not_rewrite_notes_or_other_fields(tmp_path):
    state=State(tmp_path); state.path.mkdir(parents=True)
    cfg={"provider":"graph","account_id":"FAKE-ACCOUNT","timezone":"Asia/Shanghai"}
    http=GraphHTTP(); b=Graph(state,cfg,token="fake",session=http)
    first=engine.calendar_create(state,cfg,b,{"calendar":"Work","request_id":"one","title":"会议","start":"2026-10-23T15:00:00+08:00","notes":"original body"})
    engine.calendar_update(state,cfg,b,{"calendar":"Work","id":first["item"]["id"],"request_id":"two","title":"新标题"})
    assert http.patches==[{"subject":"新标题"}]


def test_graph_multiple_alarms_fail_before_post(tmp_path):
    s=State(tmp_path); s.path.mkdir(parents=True)
    cfg={"provider":"graph","account_id":"fake","timezone":"Asia/Shanghai"}
    http=GraphHTTP();b=Graph(s,cfg,token="fake",session=http)
    with pytest.raises(Problem): engine.calendar_create(s,cfg,b,{"calendar":"Work","request_id":"a","title":"x","start":"2026-10-23T15:00","alarms":[0,10]})
    assert http.posts==0


def test_cli_capabilities_does_not_require_login(capsys):
    assert main("mailcal",["capabilities"])==0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert main("ios-reminders",["capabilities"])==0
    assert json.loads(capsys.readouterr().out)["tool"]=="ios-reminders"


def test_cli_input_stdin_json_without_shell_interpolation(monkeypatch,capsys):
    args=parser("mailcal").parse_args(["create","--input","-"])
    value={"title":"`$(do-not-run) 中文","calendar":"Work","start":"2026-10-23T15:00","request_id":"wechat-1"}
    monkeypatch.setattr("sys.stdin",io.StringIO(json.dumps(value)))
    assert load_input(args)==value


def test_cli_rejects_wrong_domain_field_without_loading_credentials(tmp_path,capsys):
    path=tmp_path/"bad.json";path.write_text(json.dumps({"title":"x","notify":True}))
    assert main("mailcal",["create","--input",str(path)])==1
    out=json.loads(capsys.readouterr().out)
    assert out["error"]["code"]=="invalid_input"


def test_cli_does_not_leak_provider_exception(monkeypatch,capsys):
    import productivity.cli as cli
    monkeypatch.setattr(cli,"run",lambda *a: (_ for _ in ()).throw(RuntimeError("password=TOP-SECRET")))
    assert main("mailcal",["capabilities"])==1
    output=capsys.readouterr().out
    assert "TOP-SECRET" not in output and json.loads(output)["error"]["code"]=="provider_error"


def test_icloud_login_saves_owner_account_and_never_password(tmp_path,monkeypatch):
    import productivity.reminders_backend as rb
    state=State(tmp_path);state.path.mkdir(parents=True)
    monkeypatch.setattr(rb,"icloud_session",lambda *a,**k:NS(requires_2fa=False,requires_2sa=False))
    rb.icloud_login(state,{"apple_id":"fake@example.invalid","timezone":"Asia/Shanghai"},"SENSITIVE")
    cfg=state.load("reminders")
    assert cfg["apple_id"]=="fake@example.invalid"
    assert "SENSITIVE" not in (state.path/"reminders.json").read_text()


def test_icloud_noninteractive_session_does_not_prompt_or_use_library_keyring(tmp_path,monkeypatch):
    import productivity.reminders_backend as rb
    observed={}
    class Fake:
        def __init__(self,*a,**kw): observed.update(kw); self.session=NS(request=lambda *a,**kw:None)
        def authenticate(self): pass
    monkeypatch.setattr("pyicloud.PyiCloudService",Fake)
    state=State(tmp_path)
    rb.icloud_session(state,{"apple_id":"fake@example.invalid"})
    assert observed["authenticate"] is False
    assert observed["cookie_directory"].startswith(str(state.path))
