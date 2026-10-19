"""Execute the whole bundled play script, including its failure and retry paths.

Syntax checks cannot catch an undefined helper inside an error callback. These
tests use a small DOM double and the actual JavaScript rather than extracting or
rewriting functions, so the same lexical scope rules apply as in Android.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from asmrlib_archiver.mobile_discover import build_discover_html, build_play_html

_ROOT = Path(__file__).resolve().parents[1]
_ASSET = _ROOT / "android/app/src/main/assets/play.html"
_NODE = shutil.which("node")

_HARNESS = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const elements = {};
function element(id) {
  if (!elements[id]) elements[id] = {
    hidden: true, textContent: '', innerHTML: '', className: '', handlers: {},
    style: {},
    addEventListener(type, fn) { this.handlers[type] = fn; },
    querySelector(selector) { return selector === '.ds-proxy-form' ? form : null; },
    setAttribute() {}, removeAttribute() {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } }
  };
  return elements[id];
}
element('pl-config').textContent = JSON.stringify({site:'https://asmrlib.com'});
element('ds-config').textContent = JSON.stringify({site:'https://asmrlib.com'});
const errorId = input.page === 'discover' ? 'ds-error' : 'pl-error';
const requests = [], proxyWrites = [], timers = [];
const form = {hidden: true, host: {value:'127.0.0.1'}, port: {value:'7890'}};
const post = 'https://asmrlib.com/posts/test-fixture';
let currentProxy = '';
const bridge = {
  resolvePost(url,id) { requests.push({url,id}); return id; },
  feed(url,id) { requests.push({url,id}); return id; },
  currentProxy() {
    if (input.action === 'proxy_read_failure') throw new Error('Bridge unavailable');
    return currentProxy;
  },
  setProxy(host,port) {
    proxyWrites.push({host,port});
    currentProxy = host && port ? host + ':' + port : '';
    return currentProxy;
  }
};
const window = input.action === 'missing_bridge' ? {} : {__asmrlibDiscover: bridge};
window.addEventListener = function() {};
window.scrollTo = function() {};
const context = {
  window,
  location: {hash:'#' + encodeURIComponent(post)},
  document: {
    getElementById: element, addEventListener() {},
    querySelector: element, documentElement: {scrollTop:0}
  },
  sessionStorage: {getItem() { return null; }, removeItem() {}},
  setTimeout(fn,delay) { timers.push({fn,delay}); return timers.length; },
  clearTimeout() {},
  console,
};
vm.createContext(context);
vm.runInContext(input.script,context,{timeout:2000});
function deliver(payload) {
  const request = requests[requests.length-1];
  window.__asmrlibDeliver(request.id,JSON.stringify(payload));
}
function click(action) {
  elements[errorId].handlers.click({
    target: {closest() { return {getAttribute() { return action; }}; }}
  });
}
if (input.action === 'success') {
  deliver({ok:true,title:'Playback fixture',sources:[
    {label:'Available source',url:'https://player.example/watch/fixture'}
  ]});
} else if (input.action === 'missing_bridge') {
  for (let i=0; i<12 && timers.length; i++) timers.shift().fn();
} else if (input.action === 'timeout') {
  timers.find(timer => timer.delay >= 20000).fn();
} else {
  deliver({ok:false,error:'Fixture connection failed'});
  if (input.action === 'retry') click('retry');
  if (input.action === 'proxy_save') {
    elements[errorId].handlers.submit({
      preventDefault() {}, target: {closest() { return form; }}
    });
  }
  if (input.action === 'proxy_clear') click('proxy-clear');
}
console.log(JSON.stringify({
  errorHtml:element(errorId).innerHTML,
  errorHidden:element(errorId).hidden,
  dockHtml:element('pl-dock').innerHTML,
  dockHidden:element('pl-dock').hidden,
  skeletonHidden:element('pl-skel').hidden,
  requests,proxyWrites
}));
"""


@unittest.skipUnless(_NODE, "Node.js is required to execute the bundled script")
class MobilePlayRuntimeTests(unittest.TestCase):
    def probe(self, action: str, html: str | None = None) -> dict:
        page = build_play_html() if html is None else html
        script = re.search(r"<script>(.*?)</script>", page, re.DOTALL)
        self.assertIsNotNone(script)
        result = subprocess.run(
            [_NODE, "-e", _HARNESS],
            input=json.dumps({"script": script.group(1), "action": action,
                              "page": "discover" if "id='ds-config'" in page else "play"}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_resolve_failure_renders_an_actionable_error(self) -> None:
        for name, html in (("generated", build_play_html()),
                           ("shipped", _ASSET.read_text(encoding="utf-8"))):
            with self.subTest(page=name):
                result = self.probe("failure", html)
                self.assertFalse(result["errorHidden"])
                self.assertTrue(result["skeletonHidden"])
                self.assertIn("Fixture connection failed", result["errorHtml"])
                self.assertIn('data-action="retry"', result["errorHtml"])

    def test_missing_bridge_and_lost_delivery_do_not_leave_a_blank_page(self) -> None:
        for action, message in (("missing_bridge", "数据桥不可用"),
                                ("timeout", "连接超时")):
            with self.subTest(action=action):
                result = self.probe(action)
                self.assertFalse(result["errorHidden"])
                self.assertIn(message, result["errorHtml"])

    def test_retry_submits_a_fresh_request_for_the_same_post(self) -> None:
        result = self.probe("retry")
        self.assertEqual(len(result["requests"]), 2)
        self.assertEqual(result["requests"][0]["url"], result["requests"][1]["url"])
        self.assertNotEqual(result["requests"][0]["id"], result["requests"][1]["id"])
        self.assertTrue(result["errorHidden"])

    def test_proxy_save_and_clear_can_reach_the_native_bridge(self) -> None:
        saved = self.probe("proxy_save")
        self.assertEqual(saved["proxyWrites"], [{"host": "127.0.0.1", "port": 7890}])
        self.assertEqual(len(saved["requests"]), 2)
        cleared = self.probe("proxy_clear")
        self.assertEqual(cleared["proxyWrites"], [{"host": "", "port": 0}])
        self.assertFalse(cleared["errorHidden"])
        self.assertTrue(cleared["errorHtml"])

    def test_success_still_renders_the_source(self) -> None:
        result = self.probe("success")
        self.assertFalse(result["dockHidden"])
        self.assertTrue(result["errorHidden"])
        self.assertTrue(result["skeletonHidden"])
        self.assertIn("https://player.example/watch/fixture", result["dockHtml"])

    def test_discover_failure_and_proxy_actions_execute_in_their_real_scope(self) -> None:
        page = build_discover_html()
        result = self.probe("failure", page)
        self.assertFalse(result["errorHidden"])
        self.assertIn("Fixture connection failed", result["errorHtml"])
        saved = self.probe("proxy_save", page)
        self.assertEqual(saved["proxyWrites"], [{"host": "127.0.0.1", "port": 7890}])
        self.assertEqual(len(saved["requests"]), 2)
        cleared = self.probe("proxy_clear", page)
        self.assertEqual(cleared["proxyWrites"], [{"host": "", "port": 0}])
        self.assertIn("代理已清除", cleared["errorHtml"])

    def test_proxy_read_failure_does_not_break_either_error_panel(self) -> None:
        for build in (build_discover_html, build_play_html):
            with self.subTest(page=build.__name__):
                result = self.probe("proxy_read_failure", build())
                self.assertFalse(result["errorHidden"])
                self.assertIn("Fixture connection failed", result["errorHtml"])


if __name__ == "__main__":
    unittest.main()
