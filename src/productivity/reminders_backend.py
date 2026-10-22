from __future__ import annotations

import copy
import datetime as dt
import functools
import re
import sys
import time
import uuid

from .common import Problem, UTC, choose, iso, zone


def auth_call(cfg, stage, action):
    """Expose structured diagnostics, never provider messages/response bodies."""
    try:
        return action()
    except Problem:
        raise
    except Exception as exc:
        import requests
        from pyicloud.exceptions import PyiCloudAcceptTermsException
        chain, pending = [], [exc]
        while pending and len(chain) < 8:
            error = pending.pop(0)
            if any(error is previous for previous in chain):
                continue
            chain.append(error)
            pending.extend(x for x in (error.__cause__, error.__context__, *error.args) if isinstance(x, BaseException))
        details = {"auth_stage":stage, "auth_region":"china_mainland" if cfg.get("china_mainland") else "global",
                   "exception_types":list(dict.fromkeys(type(error).__name__ for error in chain))}
        for error in chain:
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            if type(status) is int and 100 <= status <= 599:
                details["http_status"] = status
                break
            code = getattr(error, "code", None)
            if type(code) is int and 100 <= code <= 599:
                details["http_status"] = code
                break
        if any(isinstance(error, PyiCloudAcceptTermsException) for error in chain):
            code, message = "icloud_action_required", "Open iCloud in a browser and complete the account's required terms/setup, then retry auth"
        elif any(isinstance(error, (requests.Timeout, requests.ConnectionError)) for error in chain):
            code, message = "network_error", "iCloud authentication connection failed; check network, proxy and certificate settings"
        else:
            code, message = "needs_login", "iCloud authentication is incomplete; run auth locally with the Apple account password and correct region"
        raise Problem(code, message, details=details, exit_code=2) from None


def needs_two_factor(api):
    # pyicloud 2.6.5 can have a pending HSA2 challenge before dsInfo.hsaVersion
    # arrives. Its public requires_2fa then returns False despite this MFA flag.
    return bool(getattr(api, "requires_2fa", False) or getattr(api, "_requires_mfa", False))


def icloud_session(state,cfg,*,password=None):
    from pyicloud import PyiCloudService
    # authenticate=False avoids the library's implicit keyring lookup or startup network requests.
    api=auth_call(cfg,"session_setup",lambda: PyiCloudService(cfg["apple_id"],password,cookie_directory=str(state.path/"icloud-session"),
                        china_mainland=cfg.get("china_mainland",False),authenticate=False))
    old=api.session.request
    def request(*args,**kwargs):
        if kwargs.get("timeout") is None: kwargs["timeout"]=(10,30)
        return old(*args,**kwargs)
    api.session.request=request
    auth_call(cfg,"authentication",api.authenticate)
    return api


def complete_two_factor(api, cfg):
    if getattr(api,"requires_2sa",False) and not getattr(api,"requires_2fa",False):
        raise Problem("unsupported_auth", "Legacy two-step authentication is unsupported; use Apple two-factor authentication", exit_code=2)
    if not sys.stdin.isatty():
        raise Problem("interactive_required","Complete 2FA in an interactive terminal",exit_code=2)
    method = auth_call(cfg,"code_delivery",lambda: api.two_factor_delivery_method)
    if method == "security_key":
        raise Problem("unsupported_auth", "This CLI supports verification codes; hardware security-key authentication is not implemented", exit_code=2)
    auth_call(cfg,"code_delivery",api.request_2fa_code)
    print("Approve this sign-in on your Apple device, then enter the code sent for this sign-in.",file=sys.stderr,flush=True)
    print("Enter the 6-digit Apple verification code: ",file=sys.stderr,end="",flush=True)
    try:
        code = input().strip()
    except EOFError:
        raise Problem("interactive_required","Verification input ended; rerun auth in a local interactive terminal",exit_code=2) from None
    if not re.fullmatch(r"[0-9]{6}",code):
        raise Problem("invalid_input","Apple verification code must contain exactly six digits")
    if not auth_call(cfg,"code_verification",lambda: api.validate_2fa_code(code)):
        raise Problem("auth_failed","Apple verification code rejected; rerun auth to request a new challenge",exit_code=2)
    if not api.is_trusted_session and not auth_call(cfg,"session_trust",api.trust_session):
        raise Problem("auth_failed","Apple session could not be trusted",exit_code=2)


def icloud_login(state,cfg,password):
    state.initialize()
    api=icloud_session(state,cfg,password=password)
    verified_code = False
    if needs_two_factor(api):
        complete_two_factor(api,cfg)
        verified_code = True
    elif getattr(api,"requires_2sa",False):
        raise Problem("unsupported_auth","Legacy two-step authentication is unsupported; use Apple two-factor authentication",exit_code=2)
    # A separate server validation is required even if authenticate() returned.
    # Do not call this before handling a pending challenge: the library clears
    # challenge state when its partial session does not yet have a webauth cookie.
    status = auth_call(cfg,"session_validation",api.get_auth_status)
    if not verified_code and status.get("authenticated") and (status.get("requires_2fa") or not status.get("trusted_session")):
        complete_two_factor(api,cfg)
        verified_code = True
        status = auth_call(cfg,"session_validation",api.get_auth_status)
    if not status.get("authenticated") or not status.get("trusted_session") or status.get("requires_2fa") or status.get("requires_2sa") or needs_two_factor(api):
        raise Problem("needs_login","Apple did not confirm a fully authenticated trusted session; rerun auth locally",
                      details={"auth_stage":"session_validation", "auth_region":"china_mainland" if cfg.get("china_mainland") else "global"},exit_code=2)
    state.save("reminders",cfg)
    return {"authenticated":True,"session_verified":True,"two_factor_completed":verified_code,
            "apple_id":cfg["apple_id"],"password_stored":False}


class IOSReminders:
    def __init__(self,state,cfg,*,api=None):
        self.state,self.cfg,self.tz=state,cfg,cfg.get("timezone","Asia/Shanghai")
        self.api=api or icloud_session(state,cfg)
        if needs_two_factor(self.api) or getattr(self.api,"requires_2sa",False):
            raise Problem("needs_login","Apple session expired; run ios-reminders auth interactively",exit_code=2)
        self.service=self.api.reminders

    def lists(self):
        return [{"id":x.id,"name":x.title} for x in self.service.lists() if not getattr(x,"is_group",False)]

    def target(self,name): return choose(self.lists(),name)

    def raw_lookup(self,ids):
        from pyicloud.common.cloudkit import CKRecord
        from pyicloud.services.reminders._constants import _REMINDERS_ZONE_REQ
        response=self.service._raw.lookup(record_names=ids,zone_id=_REMINDERS_ZONE_REQ)
        records={}
        for rec in response.records:
            if isinstance(rec,CKRecord): records[rec.recordName]=rec
            elif getattr(rec,"serverErrorCode","") in {"UNKNOWN_ITEM","NOT_FOUND"}:
                raise Problem("not_found","Reminder or related record not found")
            else: raise Problem("server_error","CloudKit rejected a record lookup")
        if set(ids)!=set(records): raise Problem("not_found","Reminder or related record missing from lookup")
        return records

    def get_model(self,rid):
        if not rid.startswith("Reminder/"): raise Problem("invalid_input","Use a full Reminder/... ID, never a partial prefix")
        rec=self.raw_lookup([rid])[rid]
        r=self.service._mapper.record_to_reminder(rec)
        if r.deleted: raise Problem("not_found","Reminder is deleted")
        return r

    def serialize(self,r):
        due=r.due_date
        if due is not None:
            if due.tzinfo is None: due=due.replace(tzinfo=UTC)
            due=due.astimezone(zone(self.tz))
        return {"id":r.id,"list":r.list_id,"title":r.title,"notes":r.desc or "",
            "completed":bool(r.completed),"all_day":bool(r.all_day),
            "due":due.date().isoformat() if due and r.all_day else iso(due),"priority":r.priority,
            "timezone":r.time_zone,"alarm_ids":list(r.alarm_ids),"recurring":bool(r.recurrence_rule_ids),
            "record_change_tag":r.record_change_tag}

    def get(self,rid): return self.serialize(self.get_model(rid))

    def items(self,target,*,include_completed=False):
        result=self.service.list_reminders(target["id"],include_completed=include_completed,results_limit=200)
        return [self.serialize(r) for r in result.reminders if not r.deleted and (include_completed or not r.completed)]

    @staticmethod
    def fields(record):
        result={}
        for k,f in record.fields.items():
            d=f.model_dump(mode="json",by_alias=True,exclude_none=True)
            value=f.value
            if isinstance(value,dt.datetime): d["value"]=int(value.timestamp()*1000)
            result[k]=d
        return result

    def calibrate_alert(self,rid):
        """Learn the actual Date trigger schema from a phone-created reminder, rather than guessing it."""
        r=self.get_model(rid)
        if r.all_day or not r.due_date or not r.alarm_ids:
            raise Problem("invalid_input","Choose a timed phone-created reminder with one date notification")
        alarms=self.raw_lookup(["Alarm/"+i.removeprefix("Alarm/") for i in r.alarm_ids])
        candidates=[]
        for alarm in alarms.values():
            af=self.fields(alarm)
            trigger_id=af.get("TriggerID",{}).get("value")
            if not trigger_id: continue
            tid="AlarmTrigger/"+str(trigger_id).removeprefix("AlarmTrigger/")
            tf=self.fields(self.raw_lookup([tid])[tid])
            if tf.get("Type",{}).get("value")!="Date": continue
            expected=int(r.due_date.timestamp()*1000)
            keys=[k for k,v in tf.items() if v.get("type")=="TIMESTAMP" and isinstance(v.get("value"),(int,float)) and abs(v["value"]-expected)<1000]
            if len(keys)!=1: continue
            date_field=keys[0]
            # Preserve only a schema whose fields can safely be regenerated.
            for k,v in tf.items():
                if k in {"Alarm",date_field,"Type","Deleted","Imported","TimeZone"}: continue
                raise Problem("unsupported_alert_template","Date trigger has an unknown field; refusing to clone it")
            from .common import fingerprint
            candidates.append({"version":1,"account":fingerprint({"apple_id":self.cfg["apple_id"],"china":self.cfg.get("china_mainland",False)}),
                               "date_field":date_field,"trigger_fields":{k:v for k,v in tf.items() if k!="Alarm"}})
        if len(candidates)!=1:
            raise Problem("unsupported_alert_template","Need exactly one Date trigger matching the reminder due time")
        template=candidates[0]
        # Do not save the user's source date, title, IDs, cookies or notes.
        template["trigger_fields"][template["date_field"]]["value"]=0
        self.state.save("alert-template",template)
        return {"calibrated":True,"type":"Date","date_field":template["date_field"],
                "device_notification":"not_observed","scope":"server_schema_readback"}

    def alert_template(self):
        try:
            from .common import fingerprint
            template=self.state.load("alert-template")
            account=fingerprint({"apple_id":self.cfg["apple_id"],"china":self.cfg.get("china_mainland",False)})
            if template.get("account")!=account: raise Problem("alert_setup_required","Notification calibration belongs to another account")
            if template.get("version")!=1: raise Problem("alert_setup_required","Unsupported notification calibration version")
            return template
        except Problem:
            raise Problem("alert_setup_required","Before --notify, run calibrate-alert using a timed reminder created on your iPhone",exit_code=2) from None

    def create(self,target,rid,p):
        from pyicloud.common.cloudkit import CKModifyOperation
        from pyicloud.services.reminders._constants import _REMINDERS_ZONE_REQ
        from pyicloud.services.reminders._protocol import _encode_crdt_document, _generate_resolution_token_map
        from pyicloud.services.reminders._support import _assert_modify_success
        from .common import parse_time
        due=parse_time(p["due"],self.tz,all_day=p["all_day"]) if p.get("due") else None
        if type(due) is dt.date: due=dt.datetime.combine(due,dt.time(),tzinfo=zone(self.tz))
        now_ms=int(time.time()*1000)
        changed=["titleDocument","notesDocument","list","allDay","completed","priority","dueDate","timeZone","lastModifiedDate","creationDate"]
        fields={"TitleDocument":{"type":"STRING","value":_encode_crdt_document(p["title"])},
            "NotesDocument":{"type":"STRING","value":_encode_crdt_document(p.get("notes", ""))},
            "List":{"type":"REFERENCE","value":{"recordName":target["id"],"action":"VALIDATE"}},
            "AllDay":{"type":"INT64","value":int(p["all_day"])},"Completed":{"type":"INT64","value":0},
            "Deleted":{"type":"INT64","value":0},"Imported":{"type":"INT64","value":0},
            "Flagged":{"type":"INT64","value":0},"Priority":{"type":"INT64","value":p["priority"]},
            "CreationDate":{"type":"TIMESTAMP","value":now_ms},"LastModifiedDate":{"type":"TIMESTAMP","value":now_ms}}
        if due is not None:
            fields["DueDate"]={"type":"TIMESTAMP","value":int(due.timestamp()*1000)}
            fields["TimeZone"]={"type":"STRING","value":self.tz}
        ops=[]
        write=self.service._writes._write_record
        if p.get("notify"):
            if not due or p["all_day"]: raise Problem("invalid_input","--notify requires a timed --due")
            template=self.alert_template()
            alarm=str(uuid.uuid5(uuid.NAMESPACE_URL,rid+"/alarm")).upper()
            trigger=str(uuid.uuid5(uuid.NAMESPACE_URL,rid+"/trigger")).upper()
            fields["AlarmIDs"]={"type":"STRING_LIST","value":[alarm]}; changed.append("alarmIDs")
            tf=copy.deepcopy(template["trigger_fields"])
            tf[template["date_field"]]["value"]=int(due.timestamp()*1000)
            if "TimeZone" in tf: tf["TimeZone"]["value"]=self.tz
            tf["Alarm"]={"type":"REFERENCE","value":{"recordName":"Alarm/"+alarm,"action":"VALIDATE"}}
            af={"AlarmUID":{"type":"STRING","value":alarm},"TriggerID":{"type":"STRING","value":trigger},
                "Reminder":{"type":"REFERENCE","value":{"recordName":rid,"action":"VALIDATE"}},
                "Deleted":{"type":"INT64","value":0},"Imported":{"type":"INT64","value":0},
                "DueDateResolutionTokenAsNonce":{"type":"DOUBLE","value":100_000_000_000+time.time()-978307200}}
            ops.extend([CKModifyOperation(operationType="create",record=write(record_name="Alarm/"+alarm,record_type="Alarm",fields=af,parent_record_name=rid)),
                CKModifyOperation(operationType="create",record=write(record_name="AlarmTrigger/"+trigger,record_type="AlarmTrigger",fields=tf,parent_record_name="Alarm/"+alarm))])
        fields["ResolutionTokenMap"]={"type":"STRING","value":_generate_resolution_token_map(changed)}
        ops.insert(0,CKModifyOperation(operationType="create",record=write(record_name=rid,record_type="Reminder",fields=fields,parent_record_name=target["id"])))
        response=self.service._raw.modify(operations=ops,zone_id=_REMINDERS_ZONE_REQ,atomic=True)
        _assert_modify_success(response,"Create reminder")
        ack={getattr(r,"recordName",None) for r in response.records}
        if not {op.record.recordName for op in ops}.issubset(ack):
            raise Problem("write_unconfirmed","CloudKit did not acknowledge every created record",exit_code=4)

    def verify_alert(self,rid):
        r=self.get_model(rid)
        template=self.alert_template()
        if not r.alarm_ids: return False
        alarms=self.raw_lookup(["Alarm/"+i.removeprefix("Alarm/") for i in r.alarm_ids])
        due_ms=int(r.due_date.timestamp()*1000) if r.due_date else None
        for a in alarms.values():
            af=self.fields(a)
            if af.get("Reminder",{}).get("value",{}).get("recordName")!=rid: continue
            tid=af.get("TriggerID",{}).get("value")
            if not tid: continue
            tf=self.fields(self.raw_lookup(["AlarmTrigger/"+str(tid).removeprefix("AlarmTrigger/")])["AlarmTrigger/"+str(tid).removeprefix("AlarmTrigger/")])
            if tf.get("Type",{}).get("value")=="Date" and tf.get(template["date_field"],{}).get("value")==due_ms:
                if tf.get("Alarm",{}).get("value",{}).get("recordName")==a.recordName: return True
        return False

    def complete(self,rid):
        r=self.get_model(rid)
        if r.recurrence_rule_ids: raise Problem("unsupported_reminder","Recurring reminders require the native app")
        r.completed=True
        self.service.update(r)

    def delete(self,rid):
        r=self.get_model(rid)
        if r.recurrence_rule_ids: raise Problem("unsupported_reminder","Recurring reminders require the native app")
        self.service.delete(r)
