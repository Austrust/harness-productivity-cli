import json

import pytest
import requests

from productivity.caldav_backend import CAL, DAV, CalDAV
from productivity.common import Problem
from productivity.engine import calendar_create
from productivity.state import State
from test_core import CFG, CREATE, MemoryHTTP


def multistatus(property_xml, *, href="/", name="response"):
    return (f'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            f'<d:response><d:href>{href}</d:href><d:propstat><d:prop>{property_xml}'
            '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
            '</d:response></d:multistatus>').encode()


class DiscoveryHTTP(MemoryHTTP):
    def __init__(self, root, other, redirect):
        super().__init__()
        self.root, self.other, self.redirect = root, other, redirect
        self.principal = other + "synthetic-owner/principal/"
        self.home = other + "synthetic-owner/calendars/"
        self.calendar = self.home + "work/"
        self.calls = []
        self.bad_stage = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if method != "PROPFIND":
            return super().request(method, url, **kwargs)
        self.auths.append(kwargs.get("auth"))
        result = requests.Response()
        result.status_code = 207
        if url == self.root and self.redirect:
            result.status_code = 307
            result.headers["Location"] = self.other
            result._content = b""
        elif url in (self.root, self.other):
            target = "https://untrusted.invalid/private-owner/?secret=never-print" if self.bad_stage == "principal" else self.principal
            result._content = multistatus(f'<d:current-user-principal><d:href>\n {target}\n </d:href></d:current-user-principal>')
        elif url == self.principal:
            target = "https://untrusted.invalid/private-owner/?secret=never-print" if self.bad_stage == "home" else self.home
            result._content = multistatus(f'<c:calendar-home-set><d:href>\n {target}\n </d:href></c:calendar-home-set>')
        elif url == self.home:
            target = "https://untrusted.invalid/private-owner/?secret=never-print" if self.bad_stage == "calendar" else self.calendar
            result._content = multistatus('<d:displayname>Work</d:displayname><d:resourcetype><c:calendar/></d:resourcetype>'
                                         '<c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>',
                                         href="\n " + target + "\n ")
        else:
            pytest.fail("Unexpected discovery request")
        return result


@pytest.mark.parametrize("root,other", [
    ("https://caldav.icloud.com/", "https://p42-caldav.icloud.com.cn/"),
    ("https://caldav.icloud.com.cn/", "https://p42-caldav.icloud.com/"),
])
@pytest.mark.parametrize("redirect", [False, True])
def test_cross_region_discovery_and_verified_write(tmp_path, root, other, redirect):
    http = DiscoveryHTTP(root, other, redirect)
    cfg = {**CFG, "server": root}
    backend = CalDAV(cfg, "synthetic-app-password", session=http)
    calendars = backend.calendars()
    assert calendars == [{"id":http.calendar, "name":"Work", "read_only":False}]
    state = State(tmp_path)
    state.path.mkdir(parents=True)
    result = calendar_create(state, cfg, backend, CREATE)
    assert result["verification"]["verified"] and result["item"]["calendar"] == http.calendar
    replay = calendar_create(state, cfg, backend, CREATE)
    assert replay["replayed"] and http.puts == 1
    assert all(auth == (CFG["username"], "synthetic-app-password") for auth in http.auths)


@pytest.mark.parametrize("bad_stage,expected", [
    ("principal", "calendar_home_lookup"), ("home", "calendar_list"), ("calendar", "calendar_href"),
])
def test_discovery_rejects_external_origins_before_credentials_are_sent(bad_stage, expected):
    http = DiscoveryHTTP("https://caldav.icloud.com/", "https://p42-caldav.icloud.com.cn/", False)
    http.bad_stage = bad_stage
    backend = CalDAV(CFG, "synthetic-app-password", session=http)
    with pytest.raises(Problem) as error:
        backend.calendars()
    assert error.value.details == {"configured_origin":"https://caldav.icloud.com",
                                   "blocked_origin":"https://untrusted.invalid", "caldav_stage":expected}
    assert not any("untrusted.invalid" in url for _, url in http.calls)
    diagnostic = json.dumps(error.value.details)
    assert "private-owner" not in diagnostic and "never-print" not in diagnostic


@pytest.mark.parametrize("url", [
    "https://icloud.com.attacker.invalid/", "https://icloud.com.cn.attacker.invalid/",
    "http://p42-caldav.icloud.com.cn/", "https://p42-caldav.icloud.com.cn:8443/",
    "https://user:secret@p42-caldav.icloud.com.cn/", "https://p42-caldav.icloud.com.cn:not-a-port/",
    "https://p42-caldav.icloud.com.cn\\@attacker.invalid/",
])
def test_cross_region_support_keeps_untrusted_targets_blocked(url):
    http = MemoryHTTP()
    backend = CalDAV(CFG, "synthetic-app-password", session=http)
    with pytest.raises(Problem):
        backend.request("GET", url)
    assert not http.auths


def test_custom_caldav_trust_stays_on_configured_origin():
    backend = CalDAV({**CFG, "server":"https://dav.example.invalid:8443/"}, "synthetic")
    assert backend.trusted("https://dav.example.invalid:8443/calendars/")
    for url in ("https://dav.example.invalid/", "https://caldav.icloud.com/", "https://caldav.icloud.com.cn/"):
        assert not backend.trusted(url)


def test_untrusted_redirect_is_blocked_before_second_request_and_diagnostics_omit_userinfo():
    class RedirectHTTP:
        calls = 0

        def request(self, *args, **kwargs):
            self.calls += 1
            result = requests.Response()
            result.status_code = 302
            result.headers["Location"] = "https://private-user:private-password@attacker.invalid/private-path?private-query"
            return result

    http = RedirectHTTP()
    backend = CalDAV(CFG, "synthetic", session=http)
    with pytest.raises(Problem) as error:
        backend.request("PROPFIND", backend.root, stage="principal_lookup")
    assert http.calls == 1
    assert error.value.details["blocked_origin"] == "https://attacker.invalid"
    assert "private-" not in json.dumps(error.value.details)
