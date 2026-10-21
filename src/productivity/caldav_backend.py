from __future__ import annotations

import datetime as dt
from urllib.parse import quote, urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
from icalendar import Alarm, Calendar, Event

from .common import Problem, UTC, choose, iso, parse_time, zone

DAV = "{DAV:}"
CAL = "{urn:ietf:params:xml:ns:caldav}"


def xml(text):
    if len(text) > 12_000_000 or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise Problem("server_error", "Unsupported or oversized XML response")
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        raise Problem("server_error", "Invalid CalDAV XML response") from None


class CalDAV:
    def __init__(self, cfg, password, *, session=None):
        self.cfg = cfg
        self.root = cfg["server"].rstrip("/") + "/"
        parsed = urlparse(self.root)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise Problem("invalid_input", "CalDAV server must be HTTPS without embedded credentials")
        self.host = parsed.hostname
        self.auth = (cfg["username"], password)
        self.session = session or requests.Session()
        self.tz = cfg.get("timezone", "Asia/Shanghai")
        self._calendars = None

    def trusted(self, url):
        u = urlparse(url)
        if u.scheme != "https" or u.username or u.password:
            return False
        if u.hostname == self.host:
            return True
        for suffix in ("icloud.com", "icloud.com.cn"):
            if (self.host == suffix or self.host.endswith("." + suffix)) and (u.hostname == suffix or (u.hostname or "").endswith("." + suffix)):
                return True
        return False

    def request(self, method, url, *, body=None, headers=None):
        for _ in range(6):
            if not self.trusted(url):
                raise Problem("server_error", "Refused CalDAV URL outside the configured trusted hosts")
            try:
                r = self.session.request(method, url, data=body, auth=self.auth, headers=headers or {},
                                         timeout=(10, 30), allow_redirects=False)
            except requests.RequestException:
                raise Problem("network_error", "CalDAV request failed; no automatic write retry") from None
            if r.status_code in (301, 302, 303, 307, 308):
                if not r.headers.get("Location"):
                    break
                url = urljoin(url, r.headers["Location"])
                continue
            if r.status_code in (401, 403):
                raise Problem("needs_login", "CalDAV credentials rejected or access denied; run auth", exit_code=2)
            if r.status_code in (404, 410):
                raise Problem("not_found", "CalDAV resource not found")
            if r.status_code in (409, 412, 423):
                raise Problem("conflict", "Calendar item already exists or changed; re-read before updating")
            if r.status_code >= 400:
                raise Problem("server_error", "CalDAV returned HTTP " + str(r.status_code))
            if len(r.content) > 12_000_000:
                raise Problem("server_error", "CalDAV response exceeds size limit")
            return r, url
        raise Problem("server_error", "Too many CalDAV redirects")

    def propfind(self, url, props, depth="0"):
        root = ET.Element(DAV + "propfind")
        prop = ET.SubElement(root, DAV + "prop")
        for p in props:
            ET.SubElement(prop, p)
        r, final = self.request("PROPFIND", url, body=ET.tostring(root),
                                headers={"Depth": depth, "Content-Type":"application/xml; charset=utf-8"})
        return xml(r.text), final

    @staticmethod
    def properties(response):
        result = {}
        for part in response.findall(DAV + "propstat"):
            if " 200 " in (part.findtext(DAV + "status") or ""):
                prop = part.find(DAV + "prop")
                if prop is not None:
                    result.update({x.tag:x for x in prop})
        return result

    def calendars(self):
        if self._calendars is not None:
            return self._calendars
        tree, final = self.propfind(self.root, [DAV + "current-user-principal"])
        href = tree.findtext(".//" + DAV + "current-user-principal/" + DAV + "href")
        principal = urljoin(final, href) if href else final
        tree, final = self.propfind(principal, [CAL + "calendar-home-set"])
        href = tree.findtext(".//" + CAL + "calendar-home-set/" + DAV + "href")
        if not href:
            raise Problem("server_error", "Server did not expose calendar-home-set")
        home = urljoin(final, href)
        tree, final = self.propfind(home, [DAV+"displayname", DAV+"resourcetype", CAL+"supported-calendar-component-set",
                                           DAV+"current-user-privilege-set"], "1")
        out = []
        for response in tree.findall(DAV + "response"):
            props = self.properties(response)
            rtype = props.get(DAV + "resourcetype")
            if rtype is None or rtype.find(CAL + "calendar") is None:
                continue
            components = props.get(CAL + "supported-calendar-component-set")
            if components is not None and not any(c.attrib.get("name") == "VEVENT" for c in components):
                continue
            href = response.findtext(DAV + "href")
            if not href:
                continue
            url = urljoin(final, href).rstrip("/") + "/"
            if not self.trusted(url):
                raise Problem("server_error", "Calendar href is outside the trusted server")
            priv = props.get(DAV + "current-user-privilege-set")
            writable = priv is None or any(priv.find(".//" + DAV + p) is not None for p in ("write", "write-content", "all"))
            display = props.get(DAV + "displayname")
            out.append({"id":url,"name":display.text if display is not None else url,"read_only":not writable})
        self._calendars = out
        return out

    def target(self, name):
        return choose(self.calendars(), name)

    @staticmethod
    def href(calendar, uid):
        if not uid or len(uid) > 512:
            raise Problem("invalid_input", "A full event UID is required")
        return calendar["id"] + quote(uid, safe="") + ".ics"

    def decode(self, data, calendar, *, etag=None, href=None):
        try:
            doc = Calendar.from_ical(data)
            events = doc.walk("VEVENT")
            masters = [e for e in events if "RECURRENCE-ID" not in e]
            if len(masters) != 1:
                raise Problem("unsupported_event", "Expected one calendar event master")
            e = masters[0]
            start = e.decoded("DTSTART")
            if "DTEND" in e:
                end = e.decoded("DTEND")
            else:
                end = start + (e.decoded("DURATION") if "DURATION" in e else dt.timedelta(days=1) if type(start) is dt.date else dt.timedelta())
            all_day = type(start) is dt.date
            def display(value):
                if isinstance(value, dt.datetime):
                    return value.replace(tzinfo=zone(self.tz)).isoformat() if value.tzinfo is None else value.astimezone(zone(self.tz)).isoformat()
                return value.isoformat()
            alarms = []
            for a in e.subcomponents:
                if a.name == "VALARM":
                    trigger = a.decoded("TRIGGER")
                    if isinstance(trigger, dt.timedelta) and str(a.get("ACTION", "")).upper() == "DISPLAY":
                        alarms.append(int(-trigger.total_seconds() / 60))
            return {"id":str(e["UID"]),"calendar":calendar["id"],"title":str(e.get("SUMMARY", "")),
                    "start":display(start),"end":display(end),"all_day":all_day,
                    "notes":str(e.get("DESCRIPTION", "")),"location":str(e.get("LOCATION", "")),
                    "alarms":sorted(alarms),"recurring":bool(e.get("RRULE") or len(events)>1),
                    "etag":etag,"href":href}
        except Problem:
            raise
        except Exception:
            raise Problem("server_error", "Cannot parse the server's calendar object") from None

    def get(self, calendar, uid):
        href = self.href(calendar, uid)
        try:
            r, final = self.request("GET", href)
            item = self.decode(r.content, calendar, etag=r.headers.get("ETag"), href=final)
            if item["id"] == uid:
                return item
        except Problem as exc:
            if exc.code != "not_found":
                raise
        # Server-chosen filenames need not equal UID. Query by UID and verify the exact match.
        root = ET.Element(CAL+"calendar-query")
        prop = ET.SubElement(root,DAV+"prop")
        ET.SubElement(prop,DAV+"getetag"); ET.SubElement(prop,CAL+"calendar-data")
        f = ET.SubElement(root,CAL+"filter")
        v = ET.SubElement(ET.SubElement(f,CAL+"comp-filter",name="VCALENDAR"), CAL+"comp-filter",name="VEVENT")
        p = ET.SubElement(v,CAL+"prop-filter",name="UID")
        ET.SubElement(p,CAL+"text-match",collation="i;octet").text = uid
        try:
            r, final = self.request("REPORT",calendar["id"],body=ET.tostring(root),headers={"Depth":"1","Content-Type":"application/xml"})
        except Problem as exc:
            if exc.code!="conflict": raise
            # iCloud may reject UID prop-filters with HTTP 412. Scan this collection once.
            v.remove(p)
            r, final = self.request("REPORT",calendar["id"],body=ET.tostring(root),headers={"Depth":"1","Content-Type":"application/xml"})
        for response in xml(r.text).findall(DAV+"response"):
            props = self.properties(response)
            data = props.get(CAL+"calendar-data")
            if data is not None and data.text:
                item = self.decode(data.text,calendar,etag=props.get(DAV+"getetag").text if DAV+"getetag" in props else None,
                                   href=urljoin(final,response.findtext(DAV+"href") or ""))
                if item["id"] == uid:
                    return item
        raise Problem("not_found","Calendar event UID not found")

    def events(self, calendar, start, end):
        root = ET.Element(CAL+"calendar-query")
        prop = ET.SubElement(root,DAV+"prop")
        ET.SubElement(prop,DAV+"getetag"); ET.SubElement(prop,CAL+"calendar-data")
        f = ET.SubElement(root,CAL+"filter")
        v = ET.SubElement(ET.SubElement(f,CAL+"comp-filter",name="VCALENDAR"),CAL+"comp-filter",name="VEVENT")
        ET.SubElement(v,CAL+"time-range",start=start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"),end=end.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"))
        r, final = self.request("REPORT",calendar["id"],body=ET.tostring(root),headers={"Depth":"1","Content-Type":"application/xml"})
        result = []
        for response in xml(r.text).findall(DAV+"response"):
            props = self.properties(response)
            data = props.get(CAL+"calendar-data")
            if data is not None and data.text:
                result.append(self.decode(data.text,calendar,href=urljoin(final,response.findtext(DAV+"href") or "")))
        return sorted(result,key=lambda x:x["start"])

    def encode(self, uid, payload):
        doc = Calendar(); doc.add("PRODID","-//HarnessProductivity//EN"); doc.add("VERSION","2.0")
        e = Event(); e.add("UID",uid); e.add("DTSTAMP",dt.datetime.now(UTC))
        e.add("SUMMARY",payload["title"])
        for key, name in (("start","DTSTART"),("end","DTEND")):
            value = parse_time(payload[key],self.tz,all_day=payload["all_day"])
            e.add(name,value if type(value) is dt.date else value.astimezone(UTC))
        e.add("DESCRIPTION",payload.get("notes", "")); e.add("LOCATION",payload.get("location", ""))
        for i, minutes in enumerate(payload.get("alarms", [])):
            a=Alarm(); a.add("ACTION","DISPLAY"); a.add("DESCRIPTION",payload["title"])
            a.add("TRIGGER",-dt.timedelta(minutes=minutes)); a.add("UID",uid+"-alarm-"+str(i))
            e.add_component(a)
        doc.add_component(e)
        return doc.to_ical()

    def create(self, calendar, uid, payload):
        if calendar.get("read_only"):
            raise Problem("read_only", "Calendar is read-only")
        self.request("PUT",self.href(calendar,uid),body=self.encode(uid,payload),
                     headers={"If-None-Match":"*","Content-Type":"text/calendar; charset=utf-8"})

    def update(self, calendar, old, payload, *, changes=None):
        if old.get("recurring"):
            raise Problem("unsupported_event", "Recurring events cannot be edited by this CLI")
        if calendar.get("read_only") or not old.get("etag"):
            raise Problem("conflict", "Writable calendar and an ETag are required")
        # Edit the original VCALENDAR, retaining attendees and unknown properties.
        r, _ = self.request("GET",old["href"])
        if r.headers.get("ETag") != old["etag"]:
            raise Problem("conflict","Event changed before update")
        doc = Calendar.from_ical(r.content)
        new = Calendar.from_ical(self.encode(old["id"],payload)).walk("VEVENT")[0]
        e = doc.walk("VEVENT")[0]
        touched=set((changes or payload).keys())
        propmap={"title":"SUMMARY","start":"DTSTART","end":"DTEND","notes":"DESCRIPTION","location":"LOCATION"}
        props={v for k,v in propmap.items() if k in touched}|{"DTSTAMP"}
        if {"start","end","all_day","duration"}&touched: props|={"DTSTART","DTEND","DURATION"}
        for key in props:
            e.pop(key,None)
            if key in new: e[key]=new[key]
        if "alarms" in touched:
            e.subcomponents = [c for c in e.subcomponents if c.name != "VALARM"] + new.subcomponents
        self.request("PUT",old["href"],body=doc.to_ical(),headers={"If-Match":old["etag"],"Content-Type":"text/calendar; charset=utf-8"})

    def delete(self, calendar, old):
        if old.get("recurring"):
            raise Problem("unsupported_event","Recurring events cannot be deleted by this CLI")
        if calendar.get("read_only") or not old.get("etag"):
            raise Problem("conflict","Writable calendar and an ETag are required")
        self.request("DELETE",old["href"],headers={"If-Match":old["etag"]})
