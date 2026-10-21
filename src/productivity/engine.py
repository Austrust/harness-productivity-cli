from __future__ import annotations

import datetime as dt
import uuid

from .common import Problem, fingerprint, iso, parse_time, readback, same_time, zone
from .state import Journal


def calendar_payload(raw,tz):
    title=raw.get("title")
    if not isinstance(title,str) or not title.strip(): raise Problem("invalid_input","A non-empty title is required")
    all_day=raw.get("all_day",False)
    if type(all_day) is not bool: raise Problem("invalid_input","all_day must be a boolean")
    start=parse_time(raw.get("start"),tz,all_day=all_day)
    if raw.get("end") is not None:
        end=parse_time(raw["end"],tz,all_day=all_day)
    else:
        minutes=raw.get("duration",60)
        if type(minutes) is not int or minutes<=0: raise Problem("invalid_input","Duration must be a positive integer")
        end=start+dt.timedelta(days=1) if all_day else start+dt.timedelta(minutes=minutes)
    if end<=start: raise Problem("invalid_input","End must be after start (all-day end is exclusive)")
    alarms=raw.get("alarms",[])
    if not isinstance(alarms,list) or any(type(a) is not int or a<0 or a>525600 for a in alarms):
        raise Problem("invalid_input","alarms must be an array of nonnegative minutes before start")
    for key in ("notes","location"):
        if not isinstance(raw.get(key,""),str): raise Problem("invalid_input",key+" must be a string")
    return {"title":title,"start":iso(start),"end":iso(end),"all_day":all_day,
            "notes":raw.get("notes",""),"location":raw.get("location",""),"alarms":sorted(set(alarms))}


def reminder_payload(raw,tz):
    title=raw.get("title")
    if not isinstance(title,str) or not title.strip(): raise Problem("invalid_input","A non-empty title is required")
    for key in ("all_day","notify"):
        if type(raw.get(key,False)) is not bool: raise Problem("invalid_input",key+" must be boolean")
    all_day=raw.get("all_day",False)
    due=parse_time(raw["due"],tz,all_day=all_day) if raw.get("due") is not None else None
    if all_day and due is None: raise Problem("invalid_input","All-day reminders require a due date")
    if raw.get("notify") and (all_day or due is None): raise Problem("invalid_input","notify requires a due datetime")
    priority=raw.get("priority",0)
    names={"none":0,"high":1,"medium":5,"low":9}
    if isinstance(priority,str): priority=names.get(priority,-1)
    if type(priority) is not int or priority not in (0,1,5,9): raise Problem("invalid_input","Priority must be none/high/medium/low or 0/1/5/9")
    if not isinstance(raw.get("notes",""),str): raise Problem("invalid_input","notes must be a string")
    return {"title":title,"due":iso(due),"all_day":all_day,"notes":raw.get("notes",""),
            "priority":priority,"notify":raw.get("notify",False)}


def scope(kind,cfg,target):
    return kind+":"+fingerprint({"provider":cfg.get("provider","cloudkit"),"account":cfg.get("account_id") or cfg.get("username") or cfg.get("apple_id"),
                                  "server":cfg.get("server"),"china":cfg.get("china_mainland"),"target":target})


def verified_write(write,read,expected,*,request_id,resource,replay=False,extra_verify=None):
    write_problem=None
    if not replay:
        try: write()
        except Exception as exc: write_problem=exc
        if isinstance(write_problem,Problem) and write_problem.code in {"invalid_input","needs_login","read_only","unsupported_event","unsupported_reminder","alert_setup_required"}:
            raise write_problem
    try:
        item,verification=readback(read,expected)
    except Problem as exc:
        # Preserve both the reservation and stable locator even after a provider timeout.
        exc.details.update({"request_id":request_id,"resource":resource,"replayed":replay})
        if isinstance(write_problem,Problem): exc.details["write_error_code"]=write_problem.code
        raise
    if extra_verify:
        try: alarm_ok=extra_verify()
        except Exception: alarm_ok=False
        verification["notification_record_verified"]=alarm_ok
        if not alarm_ok:
            raise Problem("verification_failed","Reminder data exists but its notification record was not verified",
                details={"item":item,"verification":verification,"request_id":request_id,"resource":resource},exit_code=3)
    return {"item":item,"verification":verification,"request_id":request_id,"resource":resource,"replayed":replay}


def calendar_create(state,cfg,backend,raw):
    p=calendar_payload(raw,cfg["timezone"])
    target=backend.target(raw.get("calendar"))
    if target.get("read_only"): raise Problem("read_only","Calendar is read-only")
    if cfg["provider"]=="graph" and len(p["alarms"])>1:
        raise Problem("invalid_input","Outlook supports one event notification")
    key=raw.get("request_id")
    sc=scope("calendar-create",cfg,target["id"])
    rid,fresh=Journal(state).reserve(sc,key,p)
    resource="request:"+rid if cfg["provider"]=="graph" else rid
    expected=dict(p,calendar=target["id"])
    return verified_write(lambda:backend.create(target,resource,p),lambda:backend.get(target,resource),expected,
                          request_id=key,resource=resource,replay=not fresh)


def calendar_update(state,cfg,backend,raw):
    target=backend.target(raw.get("calendar"))
    uid=raw.get("id")
    if not uid: raise Problem("invalid_input","A full event ID is required")
    keys={k:v for k,v in raw.items() if k in {"title","start","end","all_day","notes","location","alarms","duration"}}
    if not keys: raise Problem("invalid_input","No update fields supplied")
    journal=Journal(state)
    # Save the absolute intended result, rather than recomputing relative times on retries.
    old=backend.get(target,uid)
    if old.get("recurring"): raise Problem("unsupported_event","Recurring events cannot be edited")
    if "start" in keys and "end" not in keys:
        # Explicitly preserve duration when moving a single event.
        old_start=parse_time(old["start"],cfg["timezone"],all_day=old["all_day"])
        old_end=parse_time(old["end"],cfg["timezone"],all_day=old["all_day"])
        new_start=parse_time(keys["start"],cfg["timezone"],all_day=keys.get("all_day",old["all_day"]))
        keys["end"]=iso(new_start+(old_end-old_start))
    if "duration" in keys and "end" not in raw:
        if keys.get("all_day",old["all_day"]): raise Problem("invalid_input","Use explicit date end for all-day updates")
        minutes=keys["duration"]
        if type(minutes) is not int or minutes<=0: raise Problem("invalid_input","Duration must be positive minutes")
        keys["end"]=iso(parse_time(keys.get("start",old["start"]),cfg["timezone"])+dt.timedelta(minutes=minutes))
    p=calendar_payload({**old,**keys},cfg["timezone"])
    if cfg["provider"]=="graph" and len(p["alarms"])>1: raise Problem("invalid_input","Outlook supports one notification")
    _,fresh=journal.reserve(scope("calendar-update",cfg,target["id"]),raw.get("request_id"),{"id":uid,"changes":keys},resource=uid)
    # On replay only validate explicitly requested fields (unmodified fields may have changed elsewhere).
    expected={k:v for k,v in p.items() if fresh or k in keys}
    return verified_write(lambda:backend.update(target,old,p,changes=keys),lambda:backend.get(target,uid),expected,
                          request_id=raw.get("request_id"),resource=uid,replay=not fresh)


def reminder_create(state,cfg,backend,raw):
    p=reminder_payload(raw,cfg["timezone"])
    target=backend.target(raw.get("list"))
    if p["notify"]: backend.alert_template()  # fail before reserving or writing if notification setup is missing
    rid,fresh=Journal(state).reserve(scope("reminder-create",cfg,target["id"]),raw.get("request_id"),p)
    resource="Reminder/"+rid
    expected={k:v for k,v in p.items() if k!="notify"}
    expected.update(list=target["id"],completed=False)
    return verified_write(lambda:backend.create(target,resource,p),lambda:backend.get(resource),expected,
        request_id=raw.get("request_id"),resource=resource,replay=not fresh,
        extra_verify=(lambda:backend.verify_alert(resource)) if p["notify"] else None)


def delete_verified(state,cfg,backend,raw,*,kind):
    uid=raw.get("id")
    if not uid: raise Problem("invalid_input","Full ID required")
    if raw.get("confirm") is not True: raise Problem("confirmation_required","Delete requires --confirm")
    target=backend.target(raw.get("calendar")) if kind=="calendar" else None
    _,fresh=Journal(state).reserve(scope(kind+"-delete",cfg,target["id"] if target else "reminders"),raw.get("request_id"),{"id":uid},resource=uid)
    get=(lambda:backend.get(target,uid)) if target else (lambda:backend.get(uid))
    if fresh:
        try:
            old=get()
            backend.delete(target,old) if target else backend.delete(uid)
        except Problem as exc:
            if exc.code!="not_found": raise
    for _ in range(3):
        try: get()
        except Problem as exc:
            if exc.code=="not_found":
                return {"id":uid,"deleted":True,"verification":{"verified":True,"scope":"server_readback"},"replayed":not fresh}
            raise
    raise Problem("write_unconfirmed","Deletion was not confirmed; reuse the same request ID",details={"resource":uid},exit_code=4)


def reminder_complete(state,cfg,backend,raw):
    uid=raw.get("id")
    if not uid: raise Problem("invalid_input","Full reminder ID required")
    _,fresh=Journal(state).reserve(scope("reminder-complete",cfg,"reminders"),raw.get("request_id"),{"id":uid},resource=uid)
    return verified_write(lambda:backend.complete(uid),lambda:backend.get(uid),{"completed":True},
        request_id=raw.get("request_id"),resource=uid,replay=not fresh)
