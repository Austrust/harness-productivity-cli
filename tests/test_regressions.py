import datetime as dt
import io
import json

import pytest
from icalendar import Calendar, Alarm
from productivity.caldav_backend import CalDAV
from productivity.cli import load_input, main, parser
from productivity.common import Problem
from productivity.engine import calendar_create, calendar_update
from productivity.state import State
from test_core import cal_backend, CFG, CREATE, TARGET
from test_graph_cli import GraphHTTP
from productivity.graph_backend import Graph


def test_verify_json_notify_not_overridden_by_default_false(monkeypatch):
    args=parser("ios-reminders").parse_args(["verify","--id","Reminder/FULL","--input","-"])
    monkeypatch.setattr("sys.stdin",io.StringIO('{"notify":true,"title":"x"}'))
    assert load_input(args)["notify"] is True


def test_calendar_duration_edit(tmp_path):
    s=State(tmp_path);s.path.mkdir(parents=True)
    b,_=cal_backend(); created=calendar_create(s,CFG,b,CREATE)
    result=calendar_update(s,CFG,b,{"calendar":"Work","id":created["item"]["id"],"request_id":"duration-1","duration":30})
    assert result["item"]["end"]=="2026-10-23T15:30:00+08:00"


def test_untouched_attendees_and_non_display_alarms_retained(tmp_path):
    s=State(tmp_path);s.path.mkdir(parents=True)
    b,http=cal_backend(); created=calendar_create(s,CFG,b,CREATE)
    href=b.href(TARGET,created["item"]["id"])
    doc=Calendar.from_ical(http.data[href]); event=doc.walk("VEVENT")[0]
    event.add("ATTENDEE","mailto:fake@example.invalid")
    alarm=Alarm();alarm.add("ACTION","AUDIO");alarm.add("TRIGGER",-dt.timedelta(minutes=5));event.add_component(alarm)
    http.data[href]=doc.to_ical()
    calendar_update(s,CFG,b,{"calendar":"Work","id":created["item"]["id"],"request_id":"title-1","title":"New title"})
    final=Calendar.from_ical(http.data[href]).walk("VEVENT")[0]
    assert str(final["ATTENDEE"])=="mailto:fake@example.invalid"
    assert any(str(c.get("ACTION"))=="AUDIO" for c in final.subcomponents)


def test_graph_all_day_preserves_local_date(tmp_path):
    s=State(tmp_path);s.path.mkdir(parents=True)
    cfg={"provider":"graph","account_id":"fake","timezone":"Asia/Shanghai"}
    http=GraphHTTP();b=Graph(s,cfg,token="fake",session=http)
    result=calendar_create(s,cfg,b,{"calendar":"Work","request_id":"day-1","title":"假期","start":"2026-10-23","all_day":True})
    assert result["item"]["start"]=="2026-10-23" and result["item"]["end"]=="2026-10-24"


def test_invalid_requested_time_fails_before_journal_or_write(tmp_path):
    s=State(tmp_path);s.path.mkdir(parents=True)
    b,http=cal_backend()
    with pytest.raises(Problem):calendar_create(s,CFG,b,{**CREATE,"start":"not-a-date"})
    assert http.puts==0 and not (s.path/"requests.sqlite3").exists()


def test_offline_doctor_loads_actual_provider_dependencies(capsys):
    assert main("ios-reminders",["doctor"])==0
    result=json.loads(capsys.readouterr().out)
    assert result["account_accessed"] is False and result["offline_checks_passed"]
