#!/usr/bin/env python3
"""NGINX-BrowserMCP - an MCP server that drives a real Chromium over CDP.

Exists so an assistant can *look* at a page instead of inferring it from CSS
text: it renders, hovers, clicks, switches device profiles and hands back
actual PNG pixels as MCP image content.

Targets any URL, with first-class shortcuts for the sites this pool serves
(see the pool_sites tool, which reads nginx-pool's own map files).

Transport: JSON-RPC 2.0 over stdio. No dependencies beyond `requests` and
`websocket-client`, both already required by the pool tooling.
"""
import base64
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

import requests
import websocket

CDP_HOST = os.environ.get("NGINX_POOL_CDP", "http://127.0.0.1:9222")
CHROME_FLAGS = [
    "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
    "--disable-dev-shm-usage", "--force-color-profile=srgb",
    "--font-render-hinting=none",
]
MAX_IMAGE_BYTES = 4_500_000   # keep a single tool result comfortably under limits


# --------------------------------------------------------------------------
# CDP plumbing
# --------------------------------------------------------------------------
def _browser_up(timeout=1.5):
    try:
        requests.get(CDP_HOST + "/json/version", timeout=timeout)
        return True
    except Exception:
        return False


def ensure_browser():
    """Reuse a running Chromium; launch a headless one if there isn't one."""
    if _browser_up():
        return "attached"
    binary = next((b for b in ("chromium", "chromium-browser", "google-chrome",
                               "google-chrome-stable", "chrome")
                   if shutil.which(b)), None)
    if not binary:
        raise RuntimeError("no Chromium/Chrome binary found on PATH")
    port = CDP_HOST.rsplit(":", 1)[-1]
    profile = "/tmp/nginx-pool-mcp-profile"
    subprocess.Popen(
        [binary, *CHROME_FLAGS, "--remote-debugging-port=" + port,
         "--user-data-dir=" + profile, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        time.sleep(.5)
        if _browser_up():
            return "launched"
    raise RuntimeError("Chromium did not expose %s in time" % CDP_HOST)


DEVICES = {
    #            w     h    dpr  mobile  UA
    "phone":    (390, 844,  3, True,
                 "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                 "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
    "phone-sm": (360, 740,  3, True,
                 "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"),
    "tablet":   (820, 1180, 2, True,
                 "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                 "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
    "laptop":   (1440, 900, 2, False, None),
    "desktop":  (1920, 1080, 1, False, None),
}


# Ids of tabs THIS process created. The browser profile is shared by every MCP
# server running against it, so a reaper must never close a tab it did not open -
# a blank tab that looks abandoned may be another session's, a moment before it
# navigates.
_OWNED = set()


def reap_owned(keep=None):
    """Close tabs we created and lost track of. Ours only, never anyone else's."""
    for tab_id in list(_OWNED):
        if tab_id == keep:
            continue
        try:
            requests.get("%s/json/close/%s" % (CDP_HOST, tab_id), timeout=5)
        except Exception:
            pass
        _OWNED.discard(tab_id)


class Tab:
    def __init__(self, width=1440, height=900):
        r = requests.put(CDP_HOST + "/json/new?about:blank", timeout=15)
        self.meta = r.json()
        self.id = self.meta["id"]
        self.ws = None
        # The tab EXISTS in the browser from here on, so everything below has to
        # be unwound on failure. It was not: when the websocket handshake started
        # returning 403 (see the note below), every browser_open left behind an
        # about:blank page nobody held a reference to, each pinning a renderer
        # process for the life of the browser. Eleven of them had accumulated in
        # the shared profile, ~300MB, on a host already deep in swap.
        _OWNED.add(self.id)
        try:
            self._connect(width, height)
        except Exception:
            self.close()
            raise

    def _connect(self, width, height):
        # suppress_origin=True, NOT origin="".
        #
        # Chrome's DevTools endpoint refuses any websocket upgrade that carries
        # a disallowed Origin header (its DNS-rebinding defence). websocket-client
        # treats origin="" as "send an empty Origin", so the header was still
        # present and every connection came back 403 Forbidden - while plain HTTP
        # to /json kept working, which makes it look like the browser is fine and
        # only the MCP layer is broken. suppress_origin omits the header entirely,
        # which is what the endpoint wants. Newer Chrome tightened this check, so
        # the same code passed on older builds.
        self.ws = websocket.create_connection(self.meta["webSocketDebuggerUrl"],
                                              timeout=60, suppress_origin=True)
        self._i = 0
        self.console = []
        self.failures = []
        for domain in ("Page", "Runtime", "Network", "Log"):
            self.send(domain + ".enable")
        self.send("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})

    # -- transport ---------------------------------------------------------
    def send(self, method, params=None, timeout=60):
        self._i += 1
        mid = self._i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(1, deadline - time.time()))
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})
            self._event(msg)
        raise TimeoutError(method)

    def _event(self, msg):
        m = msg.get("method")
        p = msg.get("params", {})
        if m == "Runtime.consoleAPICalled":
            text = " ".join(str(a.get("value", a.get("description", "")))
                            for a in p.get("args", []))
            self.console.append({"level": p.get("type"), "text": text[:400]})
        elif m == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            self.console.append({"level": "exception", "text": str(d.get("text"))[:400]})
        elif m == "Log.entryAdded":
            e = p.get("entry", {})
            if e.get("level") in ("error", "warning"):
                self.console.append({"level": e.get("level"), "text": str(e.get("text"))[:400]})
        elif m == "Network.loadingFailed":
            self.failures.append({"type": p.get("type"), "error": p.get("errorText")})
        elif m == "Network.responseReceived":
            r = p.get("response", {})
            if r.get("status", 0) >= 400:
                self.failures.append({"status": r["status"], "url": str(r.get("url"))[:200]})

    def drain(self, seconds=.4):
        end = time.time() + seconds
        while time.time() < end:
            self.ws.settimeout(max(.05, end - time.time()))
            try:
                self._event(json.loads(self.ws.recv()))
            except Exception:
                break

    # -- page --------------------------------------------------------------
    def goto(self, url, wait=3.0):
        self.console, self.failures = [], []
        self.send("Page.navigate", {"url": url})
        end = time.time() + 45
        while time.time() < end:
            self.ws.settimeout(max(1, end - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                break
            if msg.get("method") == "Page.loadEventFired":
                break
            self._event(msg)
        time.sleep(wait)
        self.drain()

    def js(self, expr):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in r:
            raise RuntimeError(str(r["exceptionDetails"].get("text"))
                               + " " + str(r["exceptionDetails"].get("exception", {}).get("description", ""))[:300])
        return r.get("result", {}).get("value")

    def rect(self, selector, scroll=True):
        """Viewport-relative box, plus the page-absolute one.

        Both are needed and they are NOT interchangeable: Input.dispatchMouseEvent
        takes viewport coordinates, while Page.captureScreenshot's clip is
        page-absolute whenever captureBeyondViewport is set. Passing the viewport
        box as a clip silently photographs the wrong region of any scrolled page.
        """
        v = self.js("""(function(){var e=document.querySelector(%s);
            if(!e) return null; %s
            var r=e.getBoundingClientRect();
            return {x:r.left,y:r.top,w:r.width,h:r.height,
                    px:r.left+window.scrollX,py:r.top+window.scrollY};})()"""
            % (json.dumps(selector),
               "e.scrollIntoView({block:'center',inline:'center'});" if scroll else ""))
        if not v:
            raise LookupError("no element matches %r" % selector)
        return v

    def mouse(self, kind, x, y, **kw):
        self.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, **kw})

    def center(self, selector):
        r = self.rect(selector)
        return r["x"] + r["w"] / 2, r["y"] + r["h"] / 2

    def shot(self, clip=None, scale=2):
        params = {"format": "png", "captureBeyondViewport": True}
        if clip:
            # Chromium refuses clips with zero area and chokes on huge ones.
            w = max(1.0, min(clip["width"], 8000))
            h = max(1.0, min(clip["height"], 8000))
            while w * h * scale * scale > 26_000_000 and scale > .5:
                scale /= 1.5
            params["clip"] = {"x": clip["x"], "y": clip["y"], "width": w,
                              "height": h, "scale": round(scale, 3)}
        data = self.send("Page.captureScreenshot", params)["data"]
        if len(data) > MAX_IMAGE_BYTES and clip:
            params["clip"]["scale"] = round(params["clip"]["scale"] / 2, 3)
            data = self.send("Page.captureScreenshot", params)["data"]
        return data

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
        try:
            requests.get("%s/json/close/%s" % (CDP_HOST, self.id), timeout=10)
        except Exception:
            pass
        _OWNED.discard(self.id)


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------
TAB = None
STATE = {"url": None, "device": None}


def tab():
    if TAB is None:
        raise RuntimeError("no page open - call browser_open first")
    return TAB


def load_cookiejar(path):
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        if not line.strip() or (line.startswith("#") and "HttpOnly" not in line):
            continue
        p = line.strip().split("\t")
        if len(p) >= 7:
            out.append({"name": p[5], "value": p[6],
                        "domain": p[0].replace("#HttpOnly_", ""), "path": "/"})
    return out


def apply_device(t, name=None, width=None, height=None, dpr=None, mobile=None):
    if name:
        if name not in DEVICES:
            raise KeyError("unknown device %r (have: %s)" % (name, ", ".join(DEVICES)))
        width, height, dpr, mobile, ua = DEVICES[name]
        if ua:
            t.send("Emulation.setUserAgentOverride", {"userAgent": ua})
        else:
            try:
                t.send("Emulation.setUserAgentOverride", {"userAgent": ""})
            except Exception:
                pass
    width = width or 1440
    height = height or 900
    dpr = dpr if dpr is not None else 2
    mobile = bool(mobile)
    t.send("Emulation.setDeviceMetricsOverride",
           {"width": int(width), "height": int(height),
            "deviceScaleFactor": float(dpr), "mobile": mobile})
    # maxTouchPoints must stay in 1..16 even when disabling touch - CDP rejects 0.
    t.send("Emulation.setTouchEmulationEnabled", {"enabled": mobile, "maxTouchPoints": 5})
    STATE["device"] = name or ("%dx%d" % (width, height))
    time.sleep(1.0)
    return {"device": STATE["device"], "viewport": "%dx%d@%gx" % (width, height, dpr),
            "touch": mobile}


STYLE_PROPS = ["backgroundColor", "color", "borderColor", "borderWidth", "borderRadius",
               "boxShadow", "padding", "margin", "transform", "filter", "opacity",
               "display", "position", "fontSize", "fontWeight", "zIndex"]


def styles_of(t, selector, props=None):
    props = props or STYLE_PROPS
    return t.js("""(function(){var e=document.querySelector(%s); if(!e) return null;
        var s=getComputedStyle(e), o={}; %s.forEach(function(p){o[p]=s[p];});
        var r=e.getBoundingClientRect();
        o.__box={x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),h:Math.round(r.height)};
        return o;})()""" % (json.dumps(selector), json.dumps(props)))


# --------------------------------------------------------------------------
# pool discovery
# --------------------------------------------------------------------------
def pool_sites(filter_=None):
    """Every site this pool serves, from nginx-pool's own maps (falls back to
    scanning /home/*/www when the maps aren't readable)."""
    sites, seen = [], set()
    for mapfile in ("/etc/nginx/domain.map", "/etc/nginx/user.map"):
        try:
            for line in open(mapfile, encoding="utf-8", errors="replace"):
                line = line.strip().rstrip(";")
                if not line or line.startswith(("#", "map ", "default", "}", "{", "hostnames")):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    host = parts[0].strip('"~^$')
                    if host and host not in seen and "." in host:
                        seen.add(host)
                        sites.append({"host": host, "target": parts[1].strip('"'),
                                      "source": os.path.basename(mapfile)})
        except Exception:
            continue
    if not sites:
        for path in glob.glob("/home/*/www/*"):
            if os.path.isdir(path):
                host = os.path.basename(path)
                if "." in host and host not in seen:
                    seen.add(host)
                    sites.append({"host": host, "target": path, "source": "filesystem"})
    if filter_:
        rx = re.compile(filter_, re.I)
        sites = [s for s in sites if rx.search(s["host"]) or rx.search(str(s["target"]))]
    return sorted(sites, key=lambda s: s["host"])


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------
def _img(data, caption=None):
    out = []
    if caption:
        out.append({"type": "text", "text": caption})
    out.append({"type": "image", "data": data, "mimeType": "image/png"})
    return out


def t_browser_open(url, device=None, basic_auth=None, cookies_file=None, wait=3.0):
    global TAB
    status = ensure_browser()
    if TAB is not None:
        TAB.close()
    TAB = Tab()
    # Anything of ours still open from an earlier failure - or from a previous
    # run of this process - goes now that we hold a good tab again.
    reap_owned(keep=TAB.id)
    if basic_auth:
        user, _, pwd = basic_auth.partition(":")
        tok = base64.b64encode(("%s:%s" % (user, pwd)).encode()).decode()
        TAB.send("Network.setExtraHTTPHeaders", {"headers": {"Authorization": "Basic " + tok}})
    if cookies_file:
        for c in load_cookiejar(cookies_file):
            try:
                TAB.send("Network.setCookie", c)
            except Exception:
                pass
    info = apply_device(TAB, device) if device else {"device": "default 1440x900"}
    TAB.send("Network.setCacheDisabled", {"cacheDisabled": True})
    TAB.goto(url, wait)
    STATE["url"] = TAB.js("location.href")
    return [{"type": "text", "text": json.dumps(
        {"browser": status, "url": STATE["url"], "title": TAB.js("document.title"),
         **info, "console_errors": len([c for c in TAB.console if c["level"] in ("error", "exception")]),
         "failed_requests": len(TAB.failures)}, indent=2)}]


def t_browser_goto(url, wait=3.0):
    t = tab()
    t.goto(url, wait)
    STATE["url"] = t.js("location.href")
    return [{"type": "text", "text": json.dumps(
        {"url": STATE["url"], "title": t.js("document.title"),
         "console_errors": len([c for c in t.console if c["level"] in ("error", "exception")]),
         "failed_requests": len(t.failures)}, indent=2)}]


def t_browser_device(name=None, width=None, height=None, dpr=None, mobile=None):
    return [{"type": "text", "text": json.dumps(
        apply_device(tab(), name, width, height, dpr, mobile), indent=2)}]


def t_browser_screenshot(selector=None, full=False, scale=2, pad=16):
    t = tab()
    if selector:
        r = t.rect(selector)
        clip = {"x": r["px"] - pad, "y": r["py"] - pad,
                "width": r["w"] + 2 * pad, "height": r["h"] + 2 * pad}
        cap = "%s at %dx%d" % (selector, round(r["w"]), round(r["h"]))
    elif full:
        m = t.send("Page.getLayoutMetrics")
        c = m.get("cssContentSize") or m["contentSize"]
        clip = {"x": 0, "y": 0, "width": c["width"], "height": min(c["height"], 6000)}
        cap = "full page %dx%d" % (c["width"], c["height"])
        scale = min(scale, 1)
    else:
        clip, cap = None, "viewport"  # no clip: viewport capture needs no mapping
    return _img(t.shot(clip, scale), "%s - %s" % (cap, STATE.get("device") or "default"))


def t_browser_states(selector, pad=18, scale=3):
    """rest / hover / active, driven with real mouse input."""
    t = tab()
    r = t.rect(selector)
    clip = {"x": r["px"] - pad, "y": r["py"] - pad,
            "width": r["w"] + 2 * pad, "height": r["h"] + 2 * pad}
    out, report = [], {}
    t.mouse("mouseMoved", 2, 2); time.sleep(.35)
    out += _img(t.shot(clip, scale), "REST - " + selector)
    report["rest"] = styles_of(t, selector)
    cx, cy = t.center(selector)
    t.mouse("mouseMoved", cx, cy); time.sleep(.45)
    out += _img(t.shot(clip, scale), "HOVER - " + selector)
    report["hover"] = styles_of(t, selector)
    t.mouse("mousePressed", cx, cy, button="left", clickCount=1); time.sleep(.45)
    out += _img(t.shot(clip, scale), "ACTIVE - " + selector)
    report["active"] = styles_of(t, selector)
    t.mouse("mouseReleased", cx, cy, button="left", clickCount=1)
    t.mouse("mouseMoved", 2, 2)
    diff = {k: {s: report[s][k] for s in ("rest", "hover", "active")}
            for k in STYLE_PROPS
            if len({json.dumps(report[s].get(k)) for s in ("rest", "hover", "active")}) > 1}
    out.append({"type": "text", "text": "changed between states:\n" + json.dumps(diff, indent=2)})
    return out


def t_browser_hover(selector):
    t = tab()
    cx, cy = t.center(selector)
    t.mouse("mouseMoved", cx, cy)
    time.sleep(.4)
    return [{"type": "text", "text": json.dumps(styles_of(t, selector), indent=2)}]


def t_browser_click(selector, wait=1.5):
    t = tab()
    cx, cy = t.center(selector)
    t.mouse("mouseMoved", cx, cy); time.sleep(.1)
    t.mouse("mousePressed", cx, cy, button="left", clickCount=1)
    t.mouse("mouseReleased", cx, cy, button="left", clickCount=1)
    time.sleep(wait)
    t.drain()
    STATE["url"] = t.js("location.href")
    return [{"type": "text", "text": json.dumps(
        {"clicked": selector, "url": STATE["url"],
         "console_errors": [c for c in t.console if c["level"] in ("error", "exception")][-5:]},
        indent=2)}]


def t_browser_type(selector, text, clear=True):
    t = tab()
    cx, cy = t.center(selector)
    t.mouse("mousePressed", cx, cy, button="left", clickCount=1)
    t.mouse("mouseReleased", cx, cy, button="left", clickCount=1)
    time.sleep(.2)
    if clear:
        t.js("""(function(){var e=document.querySelector(%s);
            if(e){e.focus();e.select&&e.select();}})()""" % json.dumps(selector))
        t.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Delete",
                                          "windowsVirtualKeyCode": 46})
        t.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Delete",
                                          "windowsVirtualKeyCode": 46})
    for ch in text:
        t.send("Input.dispatchKeyEvent", {"type": "char", "text": ch})
    time.sleep(.3)
    return [{"type": "text", "text": "typed %d chars into %s" % (len(text), selector)}]


def t_browser_key(key, selector=None):
    t = tab()
    codes = {"Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8, "Delete": 46,
             "ArrowDown": 40, "ArrowUp": 38, "ArrowLeft": 37, "ArrowRight": 39}
    if selector:
        t.js("document.querySelector(%s).focus()" % json.dumps(selector))
    for kind in ("keyDown", "keyUp"):
        t.send("Input.dispatchKeyEvent",
               {"type": kind, "key": key, "windowsVirtualKeyCode": codes.get(key, 0)})
    time.sleep(.4)
    return [{"type": "text", "text": "pressed " + key}]


def t_browser_scroll(y=None, selector=None):
    t = tab()
    if selector:
        t.js("document.querySelector(%s).scrollIntoView({block:'center'})" % json.dumps(selector))
    else:
        t.js("window.scrollTo(0,%d)" % int(y or 0))
    time.sleep(.5)
    return [{"type": "text", "text": "scrollY=" + str(t.js("window.scrollY"))}]


def t_browser_eval(js):
    return [{"type": "text", "text": json.dumps(tab().js(js), indent=2, default=str)}]


def t_browser_measure(selectors):
    t = tab()
    boxes = t.js("""(function(){var out={};%s.forEach(function(sel){
        var e=document.querySelector(sel);
        if(!e){out[sel]=null;return;}
        var r=e.getBoundingClientRect();
        out[sel]={x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),
                  h:Math.round(r.height),right:Math.round(r.right),bottom:Math.round(r.bottom)};});
        out.__viewport={w:window.innerWidth,h:window.innerHeight,
                        scrollW:document.documentElement.scrollWidth};
        return out;})()""" % json.dumps(selectors))
    overlaps = []
    keys = [k for k in boxes if k != "__viewport" and boxes[k]]
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            A, B = boxes[a], boxes[b]
            if not (A["right"] <= B["x"] or B["right"] <= A["x"]
                    or A["bottom"] <= B["y"] or B["bottom"] <= A["y"]):
                overlaps.append([a, b])
    vp = boxes.get("__viewport", {})
    notes = []
    if vp.get("scrollW", 0) > vp.get("w", 0) + 1:
        notes.append("horizontal overflow: scrollWidth %s > viewport %s"
                     % (vp["scrollW"], vp["w"]))
    return [{"type": "text", "text": json.dumps(
        {"boxes": boxes, "overlapping_pairs": overlaps, "notes": notes}, indent=2)}]


def t_browser_console():
    t = tab()
    t.drain(.3)
    return [{"type": "text", "text": json.dumps(
        {"console": t.console[-40:], "failed_requests": t.failures[-40:]}, indent=2)}]


def t_browser_close():
    global TAB
    if TAB is not None:
        TAB.close()
        TAB = None
    return [{"type": "text", "text": "closed"}]


def t_pool_sites(filter=None):  # noqa: A002 - MCP arg name
    sites = pool_sites(filter)
    return [{"type": "text", "text": json.dumps(
        {"count": len(sites), "sites": sites[:200]}, indent=2)}]


TOOLS = [
    ("browser_open", "Open a URL in a real headless Chromium and attach to it. Any URL works; "
                     "use pool_sites to discover locally served hosts. Optional HTTP basic auth "
                     "and a Netscape cookie jar for authenticated pages.",
     {"url": ("string", "Absolute URL to load", True),
      "device": ("string", "Device profile: phone, phone-sm, tablet, laptop, desktop", False),
      "basic_auth": ("string", "HTTP basic auth as 'user:password'", False),
      "cookies_file": ("string", "Path to a Netscape-format cookie jar", False),
      "wait": ("number", "Extra seconds to settle after load (default 3)", False)},
     t_browser_open),
    ("browser_goto", "Navigate the open page to another URL.",
     {"url": ("string", "Absolute URL", True),
      "wait": ("number", "Extra settle seconds", False)}, t_browser_goto),
    ("browser_device", "Switch viewport/device: sets size, devicePixelRatio, touch and UA. "
                       "Use a named profile or explicit width/height to walk breakpoints.",
     {"name": ("string", "phone | phone-sm | tablet | laptop | desktop", False),
      "width": ("number", "Explicit CSS width", False),
      "height": ("number", "Explicit CSS height", False),
      "dpr": ("number", "devicePixelRatio (default 2)", False),
      "mobile": ("boolean", "Mobile flag + touch emulation", False)}, t_browser_device),
    ("browser_screenshot", "Render PNG pixels: one element (selector), the whole page (full), "
                           "or the current viewport.",
     {"selector": ("string", "CSS selector to frame", False),
      "full": ("boolean", "Capture the entire scrollable page", False),
      "scale": ("number", "Pixel scale, default 2", False),
      "pad": ("number", "Padding around the element, default 16", False)}, t_browser_screenshot),
    ("browser_states", "Capture one element at rest, hover and active using real mouse input, "
                       "and diff the computed styles between the three.",
     {"selector": ("string", "CSS selector", True),
      "pad": ("number", "Padding, default 18", False),
      "scale": ("number", "Pixel scale, default 3", False)}, t_browser_states),
    ("browser_hover", "Move the real mouse over an element and report its computed style.",
     {"selector": ("string", "CSS selector", True)}, t_browser_hover),
    ("browser_click", "Click an element with real mouse events; reports the resulting URL "
                      "and any console errors.",
     {"selector": ("string", "CSS selector", True),
      "wait": ("number", "Seconds to wait after the click", False)}, t_browser_click),
    ("browser_type", "Focus a field and type text as real key events.",
     {"selector": ("string", "CSS selector", True),
      "text": ("string", "Text to type", True),
      "clear": ("boolean", "Clear the field first (default true)", False)}, t_browser_type),
    ("browser_key", "Press a key (Enter, Tab, Escape, arrows...).",
     {"key": ("string", "Key name", True),
      "selector": ("string", "Focus this element first", False)}, t_browser_key),
    ("browser_scroll", "Scroll the page, or scroll an element into view.",
     {"y": ("number", "Absolute scroll position", False),
      "selector": ("string", "Element to scroll into view", False)}, t_browser_scroll),
    ("browser_eval", "Evaluate JavaScript in the page and return the result.",
     {"js": ("string", "Expression to evaluate", True)}, t_browser_eval),
    ("browser_measure", "Box-measure several selectors at once; flags overlapping pairs and "
                        "horizontal overflow. Useful for responsive regressions.",
     {"selectors": ("array", "CSS selectors to measure", True)}, t_browser_measure),
    ("browser_console", "Console messages, page exceptions and failed/4xx-5xx requests "
                        "collected since the last navigation.", {}, t_browser_console),
    ("pool_sites", "List the websites this nginx pool serves, read from nginx-pool's own "
                   "map files (falls back to scanning /home/*/www).",
     {"filter": ("string", "Case-insensitive regex on host or target", False)}, t_pool_sites),
    ("browser_close", "Close the page and release the tab.", {}, t_browser_close),
]


def schema_for(params):
    props, required = {}, []
    for name, (typ, desc, req) in params.items():
        entry = {"type": typ, "description": desc}
        if typ == "array":
            entry["items"] = {"type": "string"}
        props[name] = entry
        if req:
            required.append(name)
    return {"type": "object", "properties": props, "required": required}


TOOL_INDEX = {name: (desc, params, fn) for name, desc, params, fn in TOOLS}


# --------------------------------------------------------------------------
# JSON-RPC over stdio
# --------------------------------------------------------------------------
def reply(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(req):
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": req.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "NGINX-BrowserMCP", "version": "1.0.0"}}}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": n, "description": d, "inputSchema": schema_for(p)}
            for n, d, p, _ in TOOLS]}}
    if method == "tools/call":
        params = req.get("params", {})
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOL_INDEX:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": "unknown tool " + str(name)}}
        try:
            content = TOOL_INDEX[name][2](**args)
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": content}}
        except Exception as exc:  # surfaced to the caller, never crashes the server
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": "%s: %s" % (type(exc).__name__, exc)}],
                "isError": True}}
    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": "unknown method " + str(method)}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        out = handle(req)
        if out is not None:
            reply(out)


if __name__ == "__main__":
    main()
