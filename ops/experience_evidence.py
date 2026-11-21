#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run real browser experience checks and emit structured evidence.

This module deliberately separates two claims:

* a command ran and produced a machine-readable result;
* the declared experience contract is fully covered by fresh, passing evidence.

Only the second claim can satisfy the pre-land experience gate.
"""

from __future__ import print_function

import argparse
import base64
import errno
import hashlib
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PANEL = ROOT / "plugin" / "ui" / "panel.html"
CONTRACT = ROOT / "docs" / "experience" / "experience-contract.json"
DEFAULT_EVIDENCE = ROOT / "docs" / "experience" / "experience-evidence.json"
SCREENSHOT_DIR_NAME = "experience-screenshots"
VIEWPORTS = [
    {"name": "host_panel", "width": 780, "height": 900},
    {"name": "minimum_viewport", "width": 360, "height": 720},
]
CHECK_NAMES = ("no-overflow", "no-clipping", "accessible-names", "focus-visible")
STATES = ("default", "loading", "empty", "error", "success", "focus")


class EvidenceError(RuntimeError):
    pass


def _terminate_chrome(proc):
    """Stop only the browser process group started by this runner."""
    if proc is None:
        return
    if proc.poll() is None:
        if os.name == "posix":
            try:
                if os.getpgid(proc.pid) == proc.pid:
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
            except ProcessLookupError:
                pass
        else:
            proc.terminate()
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
    if os.name == "posix":
        try:
            if os.getpgid(proc.pid) == proc.pid:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
    else:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _remove_tempdir(path, attempts=6, delay=0.2):
    """Remove our own scratch profile even if Chrome writes during cleanup."""
    path = Path(path)
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if attempt == attempts - 1:
                print("warning: temporary browser profile was not removed: %s" % exc,
                      file=sys.stderr)
                return
            for child in path.rglob("*"):
                try:
                    child.chmod(0o700)
                except OSError:
                    pass
            time.sleep(delay)


class MiniWebSocket(object):
    """Tiny RFC 6455 client for local Chrome DevTools, standard library only."""

    def __init__(self, url, timeout=15):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "http"):
            raise EvidenceError("unsupported websocket scheme: %s" % parsed.scheme)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)
        )
        self.sock.sendall(request.encode("utf-8"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EvidenceError("websocket handshake closed")
            response += chunk
            if len(response) > 65536:
                raise EvidenceError("websocket handshake too large")
        head = response.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        if " 101 " not in head.splitlines()[0]:
            raise EvidenceError("websocket handshake failed: %s" % head.splitlines()[0])
        accept = ""
        for line in head.splitlines()[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                accept = value.strip()
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if accept != expected:
            raise EvidenceError("websocket handshake accept mismatch")
        self._buffer = response.split(b"\r\n\r\n", 1)[1]

    def _read_exact(self, size):
        while len(self._buffer) < size:
            chunk = self.sock.recv(size - len(self._buffer))
            if not chunk:
                raise EvidenceError("websocket closed while reading frame")
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def send(self, text):
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        size = len(payload)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", size)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self):
        chunks = []
        while True:
            first = self._read_exact(2)
            fin = bool(first[0] & 0x80)
            opcode = first[0] & 0x0F
            masked = bool(first[1] & 0x80)
            size = first[1] & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._read_exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(size)
            if mask:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
            if opcode == 0x9:
                self.send(payload.decode("utf-8", "replace"))
                continue
            if opcode == 0x8:
                raise EvidenceError("websocket close frame")
            if opcode not in (0x0, 0x1):
                continue
            chunks.append(payload)
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _find_chrome():
    configured = os.environ.get("KG_EXPERIENCE_CHROME")
    candidates = [configured] if configured else []
    candidates += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _state_payload(state):
    """Minimal but real panel state; values mirror the runtime status shape."""
    return {
        "state": state if state not in ("focus",) else "idle",
        "state_label": {
            "default": "自动运行中",
            "loading": "正在判断下一步",
            "empty": "暂无待处理",
            "error": "状态读取失败",
            "success": "已落盘",
            "focus": "自动运行中",
        }[state],
        "enabled": True,
        "paused": False,
        "closure_rate": {"value": 8, "total": 12, "label": "8/12"},
        "project": {"name": "codex-autopilot", "path": "/tmp/experience-evidence"},
        "focus": {
            "doing": "体验证据门禁",
            "step": "Chrome 多视口实测",
            "status": "running",
        },
        "product_state": [
            {"module": "自动挡", "behavior": "规则必须有真实证据才能落盘"},
        ],
        "roadmap": [
            {"title": "唯一入口", "status": "done"},
            {"title": "自动实现", "status": "doing"},
        ],
        "mainline": {"product": "Codex 自动挡", "phase": "内部可用"},
        "recent_completed": ["体验契约 land 门禁"],
        "work_rows": [
            {"id": "L-0001", "title": "体验证据门禁", "lane": "gate", "state": "running"},
        ],
        "events": ["体验契约已生成"],
        "last_verification": [
            {"name": "experience", "status": "pass", "cmd": "python3 ops/experience_evidence.py"},
        ],
        "machine_verified": ["experience contract"],
        "stewards": {"items": [{"name": "上下文管家", "state": "ok"}]},
        "human_acceptance": [],
    }


def _driver_js(state):
    """Return JS that injects a real state and measures the rendered panel."""
    payload = json.dumps(_state_payload(state), ensure_ascii=False)
    return r"""
(function(){
  var payload = %s;
  function visible(el){
    if (!el || !el.getClientRects().length) return false;
    var cs = getComputedStyle(el);
    return cs.display !== "none" && cs.visibility !== "hidden" && Number(cs.opacity) > 0;
  }
  function label(el){
    if (!el) return "";
    var aria = el.getAttribute("aria-label");
    if (aria && aria.trim()) return aria.trim();
    var labelled = el.getAttribute("aria-labelledby");
    if (labelled) {
      var parts = labelled.split(/\s+/).map(function(id){
        var node = document.getElementById(id);
        return node ? (node.textContent || "").trim() : "";
      }).filter(Boolean);
      if (parts.length) return parts.join(" ");
    }
    var text = (el.innerText || el.textContent || "").trim();
    if (text) return text;
    var title = el.getAttribute("title");
    return title ? title.trim() : "";
  }
  function measureOverflow(){
    var bad = [];
    var vw = document.documentElement.clientWidth;
    if (document.documentElement.scrollWidth > vw + 1) {
      bad.push("document.scrollWidth=" + document.documentElement.scrollWidth + " viewport=" + vw);
    }
    var nodes = document.querySelectorAll("body *");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (!visible(el)) continue;
      var r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      if (r.right > vw + 1 || r.left < -1) {
        bad.push((el.id || el.className || el.tagName) + ":right=" + Math.round(r.right) + ",left=" + Math.round(r.left));
      }
      if (el.scrollWidth > el.clientWidth + 4 && getComputedStyle(el).overflowX === "visible") {
        bad.push((el.id || el.className || el.tagName) + ":scrollWidth=" + el.scrollWidth + ",clientWidth=" + el.clientWidth);
      }
    }
    return bad.slice(0, 20);
  }
  function measureClipping(){
    var bad = [];
    var nodes = document.querySelectorAll("button,[role=button],h1,h2,h3,.hero-state,.folder-title,.route-node .v,.triple .v,.qhead h2");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (!visible(el)) continue;
      if (el.scrollHeight > el.clientHeight + 2 && getComputedStyle(el).overflowY !== "visible") {
        bad.push((el.id || el.className || el.tagName) + ":vertical " + el.scrollHeight + ">" + el.clientHeight);
      }
      if (el.scrollWidth > el.clientWidth + 2 && getComputedStyle(el).overflowX !== "visible") {
        bad.push((el.id || el.className || el.tagName) + ":horizontal " + el.scrollWidth + ">" + el.clientWidth);
      }
    }
    return bad.slice(0, 20);
  }
  function measureNames(){
    var bad = [];
    var controls = document.querySelectorAll("button,a[href],input,summary,[role=button]");
    for (var i = 0; i < controls.length; i++) {
      var el = controls[i];
      if (!visible(el)) continue;
      if (!label(el)) bad.push((el.id || el.className || el.tagName) + ":missing accessible name");
    }
    return bad.slice(0, 20);
  }
  function measureFocus(){
    var bad = [];
    var controls = Array.prototype.slice.call(document.querySelectorAll("button,a[href],input,summary,[role=button]"))
      .filter(visible);
    if (!controls.length) return ["no visible focusable control"];
    var target = controls[0];
    target.focus();
    var cs = getComputedStyle(target);
    var outlined = (cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0) ||
      cs.boxShadow !== "none" || cs.borderWidth !== "0px";
    if (!outlined) bad.push((target.id || target.className || target.tagName) + ":no visible focus ring");
    var active = document.activeElement;
    if (active !== target) bad.push("focus moved to " + (active && active.tagName));
    return bad;
  }
  window.dispatchEvent(new MessageEvent("message", {
    source: window.parent,
    data: {jsonrpc:"2.0", method:"ui/notifications/tool-result",
           params:{structuredContent:payload}}
  }));
  return new Promise(function(resolve){
    setTimeout(function(){
      resolve({
        title: document.title,
        state_class: document.body.className,
        state_label: (document.getElementById("stateLabel") || {}).textContent || "",
        text_length: (document.body.innerText || "").length,
        no_overflow: measureOverflow(),
        no_clipping: measureClipping(),
        accessible_names: measureNames(),
        focus_visible: measureFocus()
      });
    }, 180);
  });
})()
""" % payload


class CDP(object):
    def __init__(self, websocket, url):
        self.ws = websocket
        self.url = url
        self.next_id = 0

    def call(self, method, params=None):
        self.next_id += 1
        request_id = self.next_id
        self.ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise EvidenceError("%s: %s" % (method, message["error"]))
            return message.get("result", {})

    def evaluate(self, expression, await_promise=True):
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
        })
        if result.get("exceptionDetails"):
            raise EvidenceError("JS exception: %s" % result["exceptionDetails"])
        return result.get("result", {}).get("value")


def _wait_for_json(url, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            time.sleep(0.1)
    raise EvidenceError("Chrome DevTools endpoint did not start: %s" % url)


def _launch_chrome(chrome, port, user_dir):
    args = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-features=TranslateUI",
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % user_dir,
        "about:blank",
    ]
    launch_kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "posix":
        launch_kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, **launch_kwargs)
    try:
        version = _wait_for_json("http://127.0.0.1:%d/json/version" % port)
        targets = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/json" % port, timeout=2).read().decode("utf-8"))
        page = next(t for t in targets if t.get("type") == "page")
        return proc, version.get("Browser", "unknown"), page["webSocketDebuggerUrl"]
    except Exception:
        _terminate_chrome(proc)
        raise


def run_browser_checks(panel=PANEL, chrome=None, include_screenshots=True):
    panel = Path(panel)
    if not panel.exists():
        raise EvidenceError("panel not found: %s" % panel)
    chrome = chrome or _find_chrome()
    if not chrome:
        raise EvidenceError("Chrome/Chromium not found; set KG_EXPERIENCE_CHROME")

    port = _free_port()
    checks = []
    screenshots = []
    tmp = tempfile.mkdtemp(prefix="autopilot-experience-")
    proc = None
    ws = None
    try:
        user_dir = str(Path(tmp) / "chrome-profile")
        proc, browser, ws_url = _launch_chrome(chrome, port, user_dir)
        ws = MiniWebSocket(ws_url, timeout=15)
        cdp = CDP(ws, ws_url)
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        for viewport in VIEWPORTS:
            cdp.call("Emulation.setDeviceMetricsOverride", {
                "width": viewport["width"],
                "height": viewport["height"],
                "deviceScaleFactor": 1,
                "mobile": False,
            })
            cdp.call("Page.navigate", {"url": panel.resolve().as_uri()})
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                ready = cdp.evaluate("document.readyState === 'complete' && !!document.getElementById('stateLabel')")
                if ready:
                    break
                time.sleep(0.1)
            for state in STATES:
                measurement = cdp.evaluate(_driver_js(state))
                if not isinstance(measurement, dict):
                    raise EvidenceError("bad measurement for %s/%s" % (viewport["name"], state))
                if measurement.get("text_length", 0) < 50:
                    raise EvidenceError("fixture/real content missing for %s/%s" %
                                        (viewport["name"], state))
                expected_label = _state_payload(state)["state_label"]
                if measurement.get("state_label") != expected_label:
                    raise EvidenceError("state was not applied for %s/%s: %r" %
                                        (viewport["name"], state, measurement.get("state_label")))
                for check_name, key in (
                    ("no-overflow", "no_overflow"),
                    ("no-clipping", "no_clipping"),
                    ("accessible-names", "accessible_names"),
                    ("focus-visible", "focus_visible"),
                ):
                    failures = measurement.get(key) or []
                    checks.append({
                        "name": check_name,
                        "viewport": viewport["name"],
                        "state": state,
                        "status": "pass" if not failures else "fail",
                        "details": failures[:20],
                        "text_length": measurement.get("text_length", 0),
                        "state_label": measurement.get("state_label", ""),
                        "source": "chrome-cdp-rendered-panel",
                    })
                if include_screenshots and state in ("default", "focus"):
                    shot = cdp.call("Page.captureScreenshot", {"format": "png"})
                    screenshots.append({
                        "name": "%s-%s.png" % (viewport["name"], state),
                        "data": shot["data"],
                    })
        return {
            "browser": browser,
            "panel_sha256": _sha256(panel),
            "checks": checks,
            "screenshots": screenshots,
        }
    finally:
        if ws:
            ws.close()
        _terminate_chrome(proc)
        _remove_tempdir(tmp)


def evidence_summary(result, panel=PANEL):
    expected = []
    for viewport in VIEWPORTS:
        for state in STATES:
            for check in CHECK_NAMES:
                expected.append((viewport["name"], state, check))
    seen = {}
    for item in result.get("checks", []):
        key = (item.get("viewport"), item.get("state"), item.get("name"))
        seen[key] = item.get("status")
    missing = ["%s/%s/%s" % item for item in expected if seen.get(item) != "pass"]
    failures = ["%s/%s/%s: %s" % (item.get("viewport"), item.get("state"), item.get("name"), item.get("details"))
                for item in result.get("checks", []) if item.get("status") != "pass"]
    panel_hash = _sha256(panel)
    fresh = result.get("panel_sha256") == panel_hash
    ok = fresh and not missing and not failures
    return {
        "ok": ok,
        "status": "pass" if ok else "fail",
        "reason": "真实浏览器证据完整" if ok else "真实浏览器证据不完整或不通过",
        "expected": len(expected),
        "passed": sum(1 for item in expected if seen.get(item) == "pass"),
        "missing": missing[:40],
        "failures": failures[:40],
        "panel_sha256": panel_hash,
        "fresh": fresh,
    }


def write_evidence(path=DEFAULT_EVIDENCE, panel=PANEL, contract=CONTRACT):
    result = run_browser_checks(panel=panel)
    screenshots = result.pop("screenshots")
    payload = dict(result)
    contract_raw = Path(contract).read_text(encoding="utf-8") if Path(contract).exists() else ""
    payload.update({
        "schema_version": "1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": "chrome-cdp",
        "contract_sha256": _sha256_text(contract_raw),
        "screenshots": [],
    })
    payload["summary"] = evidence_summary(payload, panel=panel)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    shot_dir = path.parent / SCREENSHOT_DIR_NAME
    if screenshots:
        shot_dir.mkdir(parents=True, exist_ok=True)
    for shot in screenshots:
        data = base64.b64decode(shot["data"])
        target = shot_dir / shot["name"]
        target.write_bytes(data)
        payload["screenshots"].append({
            "path": "%s/%s" % (SCREENSHOT_DIR_NAME, shot["name"]),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        })
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="运行真实浏览器体验检查并写结构化证据")
    parser.add_argument("--out", default=str(DEFAULT_EVIDENCE))
    parser.add_argument("--panel", default=str(PANEL))
    parser.add_argument("--contract", default=str(CONTRACT))
    args = parser.parse_args(argv)
    try:
        payload = write_evidence(args.out, args.panel, args.contract)
    except Exception as exc:
        print("experience-evidence: FAIL: %s" % exc, file=sys.stderr)
        return 1
    summary = payload["summary"]
    print("experience-evidence: %s (%d/%d, fresh=%s, screenshots=%d)" % (
        summary["status"].upper(), summary["passed"], summary["expected"],
        summary["fresh"], len(payload.get("screenshots") or [])))
    if summary["failures"]:
        for failure in summary["failures"][:5]:
            print("  - %s" % failure, file=sys.stderr)
    if summary["missing"]:
        print("  - missing: %s" % ", ".join(summary["missing"][:8]), file=sys.stderr)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
