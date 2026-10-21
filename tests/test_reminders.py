import copy
import datetime as dt
from types import SimpleNamespace as NS

import pytest

from pyicloud.common.cloudkit import CKLookupResponse, CKModifyResponse, CKRecord
from pyicloud.services.reminders import RemindersService
from pyicloud.services.reminders.models import RemindersList
from productivity.common import Problem, UTC
from productivity.engine import reminder_create, reminder_complete
from productivity.reminders_backend import IOSReminders
from productivity.state import State

CFG={"apple_id":"synthetic@example.invalid","timezone":"Asia/Shanghai"}


class Raw:
    def __init__(self): self.records={}; self.writes=0; self.reject=False
    def lookup(self,record_names,zone_id):
        return CKLookupResponse.model_validate({"records":[self.records[n] if n in self.records else
            {"recordName":n,"serverErrorCode":"UNKNOWN_ITEM","reason":"Not found"} for n in record_names]})
    def modify(self,operations,zone_id,atomic=False):
        self.writes+=1
        if self.reject:
            return CKModifyResponse.model_validate({"records":[{"recordName":operations[0].record.recordName,"serverErrorCode":"BAD_REQUEST","reason":"Synthetic rejection"}]})
        out=[]
        for op in operations:
            rec=op.record.model_dump(mode="json",by_alias=True,exclude_none=True)
            rid=rec["recordName"]
            if op.operationType=="create" and rid in self.records:
                out.append({"recordName":rid,"serverErrorCode":"CONFLICT","reason":"Already exists"}); continue
            if op.operationType=="delete":
                self.records.pop(rid,None); out.append({**rec,"deleted":True}); continue
            old=copy.deepcopy(self.records.get(rid,{}))
            fields={**old.get("fields",{}),**rec["fields"]}
            fields={k:v for k,v in fields.items() if "value" in v and v["value"] is not None}
            merged={**old,**rec,"fields":fields,"recordChangeTag":str(self.writes)}
            self.records[rid]=merged; out.append(merged)
        return CKModifyResponse.model_validate({"records":out})


@pytest.fixture
def fixture(tmp_path):
    state=State(tmp_path); state.path.mkdir(parents=True)
    svc=RemindersService("https://example.invalid",NS(),{})
    raw=Raw(); svc._raw=raw
    svc.lists=lambda:[RemindersList(id="List/L1",title="Work")]
    svc.list_reminders=lambda *a,**k:NS(reminders=[svc._mapper.record_to_reminder(CKRecord.model_validate(v)) for v in raw.records.values() if v["recordType"]=="Reminder"])
    api=NS(reminders=svc,requires_2fa=False)
    return state,IOSReminders(state,CFG,api=api),raw


def payload(**kw):
    return {"request_id":"wechat-message-01","list":"Work","title":"提交实验报告","due":"2026-10-23T08:00:00+08:00","notes":"含中文说明",**kw}


def test_cloudkit_wire_roundtrip_not_cached(fixture):
    state,b,raw=fixture
    result=reminder_create(state,CFG,b,payload())
    assert result["verification"]["verified"]
    assert result["item"]["due"]=="2026-10-23T08:00:00+08:00"
    assert result["item"]["all_day"] is False
    assert result["item"]["notes"]=="含中文说明"
    again=reminder_create(state,CFG,b,payload())
    assert again["replayed"] and raw.writes==1
    assert b.items(b.target("Work"))[0]["id"]==result["item"]["id"]


def test_all_day_is_model_field_not_midnight_heuristic(fixture):
    state,b,raw=fixture
    result=reminder_create(state,CFG,b,payload(due="2026-10-23",all_day=True))
    assert result["item"]["due"]=="2026-10-23" and result["item"]["all_day"]


def test_notification_requires_setup_before_write(fixture):
    state,b,raw=fixture
    with pytest.raises(Problem) as e: reminder_create(state,CFG,b,payload(notify=True))
    assert e.value.code=="alert_setup_required" and raw.writes==0


def test_business_error_never_reported_success(fixture):
    state,b,raw=fixture; raw.reject=True
    with pytest.raises(Problem) as e: reminder_create(state,CFG,b,payload())
    assert e.value.code=="write_unconfirmed"
    assert raw.writes==1


def test_complete_verified_with_real_mapper(fixture):
    state,b,raw=fixture
    created=reminder_create(state,CFG,b,payload())
    done=reminder_complete(state,CFG,b,{"id":created["item"]["id"],"request_id":"complete-01"})
    assert done["item"]["completed"] and done["verification"]["verified"]


def test_source_alert_calibration_atomic_create_and_readback(fixture):
    state,b,raw=fixture
    created=reminder_create(state,CFG,b,payload())
    rid=created["item"]["id"]
    raw.records[rid]["fields"]["AlarmIDs"]={"type":"STRING_LIST","value":["SOURCE-ALARM"]}
    raw.records["Alarm/SOURCE-ALARM"]={"recordName":"Alarm/SOURCE-ALARM","recordType":"Alarm","fields":{
        "AlarmUID":{"type":"STRING","value":"SOURCE-ALARM"},"Reminder":{"type":"REFERENCE","value":{"recordName":rid,"action":"NONE"}},
        "TriggerID":{"type":"STRING","value":"SOURCE-TRIGGER"}}}
    raw.records["AlarmTrigger/SOURCE-TRIGGER"]={"recordName":"AlarmTrigger/SOURCE-TRIGGER","recordType":"AlarmTrigger","fields":{
        "Type":{"type":"STRING","value":"Date"},"Date":{"type":"TIMESTAMP","value":1792713600000},
        "Alarm":{"type":"REFERENCE","value":{"recordName":"Alarm/SOURCE-ALARM","action":"NONE"}},"Deleted":{"type":"INT64","value":0}}}
    # Compute fixture timestamp from its independent ISO input.
    raw.records["AlarmTrigger/SOURCE-TRIGGER"]["fields"]["Date"]["value"]=int(dt.datetime.fromisoformat(payload()["due"]).timestamp()*1000)
    assert b.calibrate_alert(rid)["calibrated"]
    template=state.load("alert-template")
    assert "SOURCE" not in str(template) and template["trigger_fields"]["Date"]["value"]==0
    result=reminder_create(state,CFG,b,payload(request_id="notify-002",due="2026-10-24T20:00:00+08:00",notify=True))
    assert result["verification"]["notification_record_verified"]
    assert result["verification"]["device_notification"]=="not_observed"
    assert result["item"]["due"]=="2026-10-24T20:00:00+08:00"


def test_partial_ids_are_rejected(fixture):
    _,b,_=fixture
    with pytest.raises(Problem) as e:b.get("ABC123")
    assert e.value.code=="invalid_input"
