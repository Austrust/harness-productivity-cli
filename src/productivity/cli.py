from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import logging
import os
from pathlib import Path
import re
import sys

from . import __version__
from .common import Problem, iso, parse_time, redact_error, zone
from .state import State


class Parser(argparse.ArgumentParser):
    def error(self,message): raise Problem("invalid_input",message)


def parser(tool):
    p=Parser(prog=tool,description="Server-verified productivity CLI. JSON stdout; auth prompts only on stderr.")
    p.set_defaults(_tool=tool)
    p.add_argument("--version",action="version",version=__version__)
    p.add_argument("--profile",default="default")
    p.add_argument("--state-dir",help="Override per-user state root")
    sub=p.add_subparsers(dest="command",required=True)
    auth=sub.add_parser("auth",help="Interactive owner setup; never run from a remote agent")
    auth.add_argument("--timezone",default="Asia/Shanghai")
    auth.add_argument("--password-env",help="Read password from a named environment variable instead of a prompt")
    if tool=="mailcal":
        auth.add_argument("--provider",choices=["caldav","graph"],required=True)
        auth.add_argument("--server",default="https://caldav.icloud.com")
        auth.add_argument("--username")
        auth.add_argument("--client-id")
        auth.add_argument("--tenant",default="common")
    else:
        auth.add_argument("--apple-id",required=True)
        auth.add_argument("--china-mainland",action="store_true")
    sub.add_parser("status",help="Check saved login without interactive prompts")
    sub.add_parser("logout",help="Remove local login state; does not revoke provider-wide access")
    sub.add_parser("capabilities",help="Machine-readable supported operations")
    sub.add_parser("doctor",help="Offline runtime and dependency checks; no account access")
    sub.add_parser("now",help="Current local date and timezone for resolving relative language")
    sub.add_parser("calendars" if tool=="mailcal" else "lists")
    listing=sub.add_parser("list")
    if tool=="mailcal":
        listing.add_argument("--calendar",required=True)
        listing.add_argument("--from",dest="from_time",required=True)
        listing.add_argument("--to",dest="to_time",required=True)
    else:
        listing.add_argument("--list",required=True)
        listing.add_argument("--include-completed",action="store_true")
    get=sub.add_parser("get"); get.add_argument("--id",required=True)
    if tool=="mailcal": get.add_argument("--calendar",required=True)
    verify=sub.add_parser("verify",help="Read and compare against an expected JSON object")
    verify.add_argument("--id",required=True); verify.add_argument("--input",required=True)
    if tool=="mailcal": verify.add_argument("--calendar",required=True)
    if tool=="ios-reminders": verify.add_argument("--notify",action="store_true",default=None)
    for command in (["create","update","delete"] if tool=="mailcal" else ["create","complete","delete"]):
        q=sub.add_parser(command)
        q.add_argument("--input",help="UTF-8 JSON file, or - for stdin. Recommended for agent calls.")
        q.add_argument("--request-id",help="Stable request ID; reuse exactly on retries")
        if command!="create": q.add_argument("--id")
        if tool=="mailcal": q.add_argument("--calendar")
        if command=="delete": q.add_argument("--confirm",action="store_true",default=None)
        if command in {"create","update"}:
            q.add_argument("--title"); q.add_argument("--notes")
            q.add_argument("--all-day",action="store_true",default=None)
            if tool=="mailcal":
                q.add_argument("--start"); q.add_argument("--end"); q.add_argument("--duration",type=int)
                q.add_argument("--location"); q.add_argument("--alarm",type=int,action="append",dest="alarms")
            else:
                q.add_argument("--list"); q.add_argument("--due")
                q.add_argument("--priority",choices=["none","high","medium","low"])
                q.add_argument("--notify",action="store_true",default=None)
    if tool=="ios-reminders":
        q=sub.add_parser("calibrate-alert",help="Learn a Date alarm schema from one iPhone-created timed reminder (read-only)")
        q.add_argument("--source-id",required=True)
    return p


def load_input(args):
    if hasattr(args,"_input_data"): return dict(args._input_data)
    data={}
    if getattr(args,"input",None):
        try:
            content=sys.stdin.read(1_000_001) if args.input=="-" else Path(args.input).read_text(encoding="utf-8-sig")
            if len(content)>1_000_000: raise ValueError()
            data=json.loads(content)
        except (OSError,ValueError): raise Problem("invalid_input","Input must be a readable UTF-8 JSON object under 1 MB") from None
        if not isinstance(data,dict): raise Problem("invalid_input","Input must be a JSON object")
    base={"request_id","id","confirm"}
    calendar_fields={"title","start","end","duration","all_day","notes","location","alarms","calendar"}
    reminder_fields={"title","all_day","notes","list","due","priority","notify","completed","timezone"}
    allowed=base|(calendar_fields if args._tool=="mailcal" else reminder_fields)
    if args.command in {"delete","complete"}: allowed=base|({"calendar"} if args._tool=="mailcal" else set())
    if args.command=="verify": allowed-=base-{"id"}
    if args.command=="create": allowed-={"id","confirm","completed","timezone"}
    if set(data)-allowed: raise Problem("invalid_input","Unknown JSON fields: "+", ".join(sorted(set(data)-allowed)))
    for key in allowed:
        value=getattr(args,key,None)
        if value is not None: data[key]=value
    args._input_data=dict(data)
    return data


def password(args):
    if args.password_env:
        value=os.environ.get(args.password_env)
        if not value: raise Problem("invalid_input","Password environment variable is empty")
        return value
    if not sys.stdin.isatty(): raise Problem("interactive_required","Run auth in an interactive owner terminal",exit_code=2)
    return getpass.getpass("Password (local prompt; hidden): ",stream=sys.stderr)


def calendar_backend(state,cfg):
    if cfg["provider"]=="graph":
        from .graph_backend import Graph
        return Graph(state,cfg)
    from .caldav_backend import CalDAV
    pw=state.secret_get("caldav-password")
    if pw is None: raise Problem("needs_login","Run mailcal auth first",exit_code=2)
    return CalDAV(cfg,pw)


def run(tool,args):
    from . import engine
    state=State(args.state_dir,args.profile)
    if args.command=="doctor":
        import platform
        import importlib
        for name in ("requests","icalendar","pyicloud","msal"):
            try: importlib.import_module(name)
            except Exception as exc:
                raise Problem("runtime_dependency_missing","A runtime dependency could not load",
                              details={"dependency":name,"exception_type":type(exc).__name__}) from None
        from pyicloud.common.cloudkit import CKModifyOperation, CKRecord
        from pyicloud.services.reminders._protocol import _encode_crdt_document
        from .caldav_backend import CalDAV
        from .graph_backend import Graph
        from .reminders_backend import IOSReminders
        return {"offline_checks_passed":True,"platform":platform.system(),"architecture":platform.machine(),
                "python":platform.python_version(),"default_timezone":str(zone("Asia/Shanghai")),
                "dependencies_loaded":["requests","icalendar","pyicloud","msal","tzdata"],"account_accessed":False}
    if args.command=="capabilities":
        return {"tool":tool,"version":__version__,"providers":["caldav","graph"] if tool=="mailcal" else ["icloud-cloudkit"],
            "operations":["auth","status","logout","doctor","now","calendars","list","get","create","update","delete","verify"] if tool=="mailcal" else ["auth","status","logout","doctor","now","lists","list","get","create","complete","delete","verify","calibrate-alert"],
            "writes_require_request_id":True,"notification_delivery_observable":False,
            "recurring_edits_supported":False,"reminder_notify_requires_calibration":tool=="ios-reminders"}
    if args.command=="now":
        try: cfg=state.load("calendar" if tool=="mailcal" else "reminders")
        except Problem: cfg={"timezone":"Asia/Shanghai"}
        return {"now":dt.datetime.now(zone(cfg["timezone"])).isoformat(),"timezone":cfg["timezone"]}
    if args.command=="auth":
        zone(args.timezone)
        if tool=="mailcal":
            if args.provider=="graph":
                if not args.client_id or not re.fullmatch(r"[0-9a-fA-F-]{36}",args.client_id): raise Problem("invalid_input","Graph requires your Microsoft public-client app's --client-id")
                if not re.fullmatch(r"[a-zA-Z0-9.-]+",args.tenant): raise Problem("invalid_input","Invalid Microsoft tenant")
                if not sys.stdin.isatty(): raise Problem("interactive_required","Run Graph auth in an interactive terminal",exit_code=2)
                from .graph_backend import graph_login
                return graph_login(state,{"provider":"graph","client_id":args.client_id,"tenant":args.tenant,"timezone":args.timezone})
            if not args.username: raise Problem("invalid_input","CalDAV requires --username")
            from .caldav_backend import CalDAV
            cfg={"provider":"caldav","server":args.server,"username":args.username,"timezone":args.timezone}
            pw=password(args)
            backend=CalDAV(cfg,pw); calendars=backend.calendars()
            state.secret_set("caldav-password",pw); state.save("calendar",cfg)
            return {"authenticated":True,"provider":"caldav","calendars":calendars}
        from .reminders_backend import icloud_login
        return icloud_login(state,{"apple_id":args.apple_id,"timezone":args.timezone,"china_mainland":args.china_mainland},password(args))
    if args.command=="logout":
        # Deliberately local-only, and leave the idempotency journal intact.
        import shutil
        if tool=="mailcal":
            for s in ("caldav-password","graph-token-cache"): state.secret_delete(s)
            (state.path/"calendar.json").unlink(missing_ok=True)
        else:
            target=state.path/"icloud-session"
            if target.exists():
                if target.is_symlink() or not target.resolve().is_relative_to(state.path):
                    raise Problem("local_storage_error","Refused to remove a session directory outside the profile")
                shutil.rmtree(target)
            (state.path/"reminders.json").unlink(missing_ok=True)
            (state.path/"alert-template.json").unlink(missing_ok=True)
        return {"logged_out_locally":True,"provider_access_revoked":False}
    cfg=state.load("calendar" if tool=="mailcal" else "reminders")
    if tool=="mailcal":
        backend=calendar_backend(state,cfg)
        if args.command in {"status","calendars"}:
            cals=backend.calendars()
            return {"authenticated":True,"provider":cfg["provider"],"calendars":cals}
        if args.command=="list":
            target=backend.target(args.calendar)
            start=parse_time(args.from_time,cfg["timezone"]); end=parse_time(args.to_time,cfg["timezone"])
            if end<=start or end-start>dt.timedelta(days=366): raise Problem("invalid_input","Use a positive query range of at most 366 days")
            return {"events":backend.events(target,start,end),"source":"live_server"}
        if args.command in {"get","verify"}:
            item=backend.get(backend.target(args.calendar),args.id)
            if args.command=="get": return {"item":item}
            expected=load_input(args); expected.pop("id",None); expected.pop("calendar",None)
            from .common import verify_fields
            v=verify_fields(expected,item)
            if not v["verified"]: raise Problem("verification_failed","Server values differ",details={"item":item,"verification":v},exit_code=3)
            return {"item":item,"verification":v}
        raw=load_input(args)
        if args.command=="create": return engine.calendar_create(state,cfg,backend,raw)
        if args.command=="update": return engine.calendar_update(state,cfg,backend,raw)
        return engine.delete_verified(state,cfg,backend,raw,kind="calendar")
    from .reminders_backend import IOSReminders
    backend=IOSReminders(state,cfg)
    if args.command in {"status","lists"}: return {"authenticated":True,"lists":backend.lists()}
    if args.command=="calibrate-alert": return backend.calibrate_alert(args.source_id)
    if args.command=="list": return {"reminders":backend.items(backend.target(args.list),include_completed=args.include_completed),"source":"live_server"}
    if args.command in {"get","verify"}:
        item=backend.get(args.id)
        if args.command=="get": return {"item":item}
        from .common import verify_fields
        expected=load_input(args); notify=expected.pop("notify",False); expected.pop("id",None)
        v=verify_fields(expected,item)
        if notify: v["notification_record_verified"]=backend.verify_alert(args.id); v["verified"] &= v["notification_record_verified"]
        if not v["verified"]: raise Problem("verification_failed","Reminder readback differs",details={"item":item,"verification":v},exit_code=3)
        return {"item":item,"verification":v}
    raw=load_input(args)
    if args.command=="create": return engine.reminder_create(state,cfg,backend,raw)
    if args.command=="complete": return engine.reminder_complete(state,cfg,backend,raw)
    return engine.delete_verified(state,cfg,backend,raw,kind="reminder")


def main(tool,argv=None):
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,"reconfigure"): stream.reconfigure(encoding="utf-8",errors="replace")
    logging.disable(logging.CRITICAL)
    try:
        args=parser(tool).parse_args(argv)
        if args.command in {"create","update","complete","delete","verify"}: load_input(args)
        if args.command in {"now","capabilities","doctor"}:
            result=run(tool,args)
        else:
            with State(args.state_dir,args.profile).lock():
                result=run(tool,args)
        print(json.dumps({"ok":True,"tool":tool,"version":__version__,**result},ensure_ascii=False,default=iso))
        return 0
    except KeyboardInterrupt:
        exc=Problem("interrupted","Command interrupted; reuse the same request ID to reconcile uncertain writes",exit_code=130)
    except Exception as error:
        exc=redact_error(error)
    print(json.dumps({"ok":False,"tool":tool,"version":__version__,"error":{"code":exc.code,"message":str(exc)},**exc.details},ensure_ascii=False,default=str))
    return exc.exit_code


def calendar_main(): return main("mailcal")
def reminders_main(): return main("ios-reminders")


def portable_main():
    name=Path(sys.executable).stem.lower() if getattr(sys,"frozen",False) else Path(sys.argv[0]).stem.lower()
    if name=="ios-reminders": return reminders_main()
    return calendar_main()
