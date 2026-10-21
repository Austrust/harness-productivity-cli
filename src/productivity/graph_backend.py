from __future__ import annotations

import datetime as dt
from urllib.parse import quote, urlparse

import requests

from .common import Problem, UTC, choose, iso, parse_time, zone

BASE = "https://graph.microsoft.com/v1.0"
EXT = "String {b0270c22-2bcb-4ecd-a675-58cc084a204d} Name HarnessRequestId"
SCOPES = ["Calendars.ReadWrite"]


def msal_app(state, cfg):
    import msal
    cache = msal.SerializableTokenCache()
    saved = state.secret_get("graph-token-cache")
    if saved:
        cache.deserialize(saved)
    app = msal.PublicClientApplication(cfg["client_id"],authority="https://login.microsoftonline.com/"+cfg.get("tenant","common"),
                                       token_cache=cache, http_client=TimeoutHTTP())
    return app, cache


class TimeoutHTTP:
    def __init__(self): self.session=requests.Session()
    def get(self,url,**kwargs):
        kwargs.setdefault("timeout",30)
        return self.session.get(url,**kwargs)
    def post(self,url,**kwargs):
        kwargs.setdefault("timeout",30)
        return self.session.post(url,**kwargs)


def graph_login(state,cfg):
    import sys
    app, cache=msal_app(state,cfg)
    flow=app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise Problem("auth_failed","Microsoft device login could not start; check public-client app configuration")
    print(flow["message"],file=sys.stderr,flush=True)
    result=app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise Problem("auth_failed","Microsoft device login failed or expired")
    accounts=app.get_accounts()
    if not accounts:
        raise Problem("auth_failed","Microsoft account was not cached")
    account=next((a for a in accounts if a.get("local_account_id")==result.get("id_token_claims",{}).get("oid")),None)
    if account is None and len(accounts)==1: account=accounts[0]
    if account is None: raise Problem("auth_failed","Cannot disambiguate the signed-in Microsoft account; use a new profile")
    cfg=dict(cfg,account_id=account["home_account_id"],username=account.get("username", ""))
    state.secret_set("graph-token-cache",cache.serialize())
    state.save("calendar",cfg)
    return {"authenticated":True,"provider":"graph","username":cfg["username"]}


class Graph:
    def __init__(self,state,cfg,*,token=None,session=None):
        self.state,self.cfg,self.tz=state,cfg,cfg.get("timezone","Asia/Shanghai")
        self.session=session or requests.Session()
        self.token=token
        if token is None:
            app,cache=msal_app(state,cfg)
            account=next((a for a in app.get_accounts() if a["home_account_id"]==cfg.get("account_id")),None)
            result=app.acquire_token_silent(SCOPES,account=account) if account else None
            if cache.has_state_changed: state.secret_set("graph-token-cache",cache.serialize())
            if not result or "access_token" not in result:
                raise Problem("needs_login","Microsoft session expired; run mailcal auth interactively",exit_code=2)
            self.token=result["access_token"]

    def request(self,method,path,*,data=None,params=None,headers=None):
        url=path if path.startswith("https://") else BASE+path
        if urlparse(url).scheme!="https" or urlparse(url).netloc!="graph.microsoft.com" or not urlparse(url).path.startswith("/v1.0/"):
            raise Problem("server_error","Refused Graph URL outside graph.microsoft.com/v1.0")
        try:
            r=self.session.request(method,url,json=data,params=params,
                headers={"Authorization":"Bearer "+self.token,"Prefer":'outlook.timezone="UTC", outlook.body-content-type="text"',**(headers or {})},
                timeout=(10,30),allow_redirects=False)
        except requests.RequestException:
            raise Problem("network_error","Microsoft Graph request failed; no automatic write retry") from None
        if r.status_code in (401,403): raise Problem("needs_login","Microsoft session or calendar permissions rejected",exit_code=2)
        if r.status_code in (404,410): raise Problem("not_found","Microsoft calendar resource not found")
        if r.status_code in (409,412): raise Problem("conflict","Microsoft event changed; re-read it")
        if r.status_code>=300: raise Problem("server_error","Microsoft Graph returned HTTP "+str(r.status_code))
        if r.status_code==204: return {}
        try: return r.json()
        except ValueError: raise Problem("server_error","Invalid Graph response") from None

    def pages(self,path,params=None):
        out=[]
        for _ in range(100):
            body=self.request("GET",path,params=params)
            out.extend(body.get("value",[]))
            path=body.get("@odata.nextLink"); params=None
            if not path: return out
        raise Problem("server_error","Graph pagination exceeds limit; narrow the date range")

    def calendars(self):
        return [{"id":c["id"],"name":c["name"],"read_only":not c.get("canEdit",True)} for c in self.pages("/me/calendars")]

    def target(self,name): return choose(self.calendars(),name)

    @staticmethod
    def root(calendar): return "/me/calendars/"+quote(calendar["id"],safe="")

    def decode(self,item,calendar):
        def date(key):
            value=item[key]["dateTime"]
            # Requests explicitly ask Graph to return UTC.
            d=parse_time(value+"Z" if not value.endswith("Z") and "+" not in value[10:] else value,self.tz)
            return d.date().isoformat() if item.get("isAllDay") else d.isoformat()
        return {"id":item["id"],"calendar":calendar["id"],"title":item.get("subject", ""),
            "start":date("start"),"end":date("end"),"all_day":item.get("isAllDay",False),
            "notes":item.get("body",{}).get("content", ""),"location":item.get("location",{}).get("displayName", ""),
            "alarms":[item.get("reminderMinutesBeforeStart",0)] if item.get("isReminderOn") else [],
            "recurring":item.get("type","singleInstance")!="singleInstance","etag":item.get("@odata.etag")}

    def get(self,calendar,uid):
        if uid.startswith("request:"):
            key=uid.removeprefix("request:")
            if not key or any(c not in "0123456789ABCDEFabcdef-" for c in key):
                raise Problem("invalid_input","Invalid request locator")
            items=self.pages(self.root(calendar)+"/events",{"$filter":f"singleValueExtendedProperties/Any(ep: ep/id eq '{EXT}' and ep/value eq '{key}')"})
            if not items: raise Problem("not_found","No event found for this request ID")
            if len(items)!=1: raise Problem("conflict","More than one event has this request ID")
            return self.decode(items[0],calendar)
        return self.decode(self.request("GET",self.root(calendar)+"/events/"+quote(uid,safe="")),calendar)

    def events(self,calendar,start,end):
        return [self.decode(e,calendar) for e in self.pages(self.root(calendar)+"/calendarView",
            {"startDateTime":iso(start),"endDateTime":iso(end),"$top":100})]

    def payload(self,p):
        if len(p.get("alarms",[]))>1: raise Problem("invalid_input","Outlook supports one reminder per event; pass one --alarm value")
        def date(key):
            v=parse_time(p[key],self.tz,all_day=p["all_day"])
            local=dt.datetime.combine(v,dt.time()) if type(v) is dt.date else v.replace(tzinfo=None)
            return {"dateTime":local.isoformat(),"timeZone":self.tz}
        return {"subject":p["title"],"start":date("start"),"end":date("end"),"isAllDay":p["all_day"],
            "body":{"contentType":"text","content":p.get("notes", "")},"location":{"displayName":p.get("location", "")},
            "isReminderOn":bool(p.get("alarms")),"reminderMinutesBeforeStart":next(iter(p.get("alarms",[])),0)}

    def create(self,calendar,uid,payload):
        if calendar.get("read_only"): raise Problem("read_only","Calendar is read-only")
        key=uid.removeprefix("request:")
        body=self.payload(payload)
        body.update(transactionId=key,singleValueExtendedProperties=[{"id":EXT,"value":key}])
        response=self.request("POST",self.root(calendar)+"/events",data=body)
        if not response.get("id"): raise Problem("write_unconfirmed","Graph did not return an event ID",exit_code=4)

    def update(self,calendar,old,payload,*,changes=None):
        if old.get("recurring"): raise Problem("unsupported_event","Recurring events cannot be edited by this CLI")
        if calendar.get("read_only") or not old.get("etag"): raise Problem("conflict","Writable calendar and an ETag are required")
        body=self.payload(payload)
        if changes is not None:
            mapping={"title":{"subject"},"notes":{"body"},"location":{"location"},"alarms":{"isReminderOn","reminderMinutesBeforeStart"},
                     "start":{"start","end"},"end":{"end"},"all_day":{"start","end","isAllDay"},"duration":{"end"}}
            keep=set().union(*(mapping.get(k,set()) for k in changes))
            body={k:v for k,v in body.items() if k in keep}
        self.request("PATCH",self.root(calendar)+"/events/"+quote(old["id"],safe=""),data=body,headers={"If-Match":old["etag"]})

    def delete(self,calendar,old):
        if old.get("recurring"): raise Problem("unsupported_event","Recurring events cannot be deleted by this CLI")
        if calendar.get("read_only") or not old.get("etag"): raise Problem("conflict","Writable calendar and an ETag are required")
        self.request("DELETE",self.root(calendar)+"/events/"+quote(old["id"],safe=""),headers={"If-Match":old["etag"]})
