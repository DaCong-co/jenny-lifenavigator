"""The Navigator section's client wiring, executed from the real source.

The one bug worth a test here is subtle: ``_numberField`` is shared with the
worker sections, and ``_wireWorkerSettings`` binds every ``[data-worker-key]``.
Without the ``navigator_`` guard a Navigator number would save to the memory
endpoint and then to the right one, showing two toasts and often the wrong
value. These tests run the extracted methods in node, like the worker client
tests.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[2] / "jenny" / "templates" / "ui" / "assets"
SETTINGS_JS = ASSETS / "mobile-settings.js"

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node non disponibile")


def _member(name: str) -> str:
    source = SETTINGS_JS.read_text(encoding="utf-8")
    match = re.search(
        rf"\n  ((?:async |get )?{re.escape(name)}\([^)]*\)\s*\{{.*?)\n  \}}",
        source,
        re.S,
    )
    assert match, f"{name} non trovato in mobile-settings.js"
    return match.group(1) + "\n  }"


_HARNESS = """
import assert from 'node:assert/strict';

function makeEl(extra = {}) {
  return {
    checked: false,
    value: '',
    dataset: {},
    _handlers: {},
    addEventListener(type, fn) { this._handlers[type] = fn; },
    async fire(type) { await this._handlers[type]?.(); },
    ...extra,
  };
}

const toasts = [];
const calls = [];
let failWith = null;

const i18n = { t: (key, params) => (params ? `${key}:${JSON.stringify(params)}` : key) };
const showToast = (text, kind) => toasts.push({ text, kind });
const api = {
  async updateNavigatorSettings(params) {
    calls.push(params);
    if (failWith) throw new Error(failWith);
    return {
      navigator: {
        ...PAYLOAD.navigator,
        watch_topics: [],
        interval_min: { ...PAYLOAD.navigator.interval_min, value: 30 },
      },
    };
  },
};

const PAYLOAD = {
  navigator: {
    interval_min: { value: 60, min: 1, max: 1440 },
    watch_topics: [],
  },
};

class Screen {
  constructor(elements) {
    this.data = JSON.parse(JSON.stringify(PAYLOAD));
    this._elements = elements;
    this.contentEl = {
      querySelector: (sel) => this._elements[sel] || null,
      querySelectorAll: (sel) => (sel === '[data-worker-key]'
        ? Object.values(this._elements).filter(e => e.dataset.workerKey) : []),
    };
    this._repaintNavigatorDerived = () => { this.repainted = true; };
  }
__MEMBERS__
}

export { Screen, makeEl, toasts, calls, api };
export function setFailure(v) { failWith = v; }
"""


def _run(tmp_path: Path, script: str, members: list[str]) -> subprocess.CompletedProcess:
    body = "\n".join(f"  {_member(name)}" for name in members)
    harness = _HARNESS.replace("__MEMBERS__", body)
    (tmp_path / "harness.mjs").write_text(harness, encoding="utf-8")
    (tmp_path / "test.mjs").write_text(script, encoding="utf-8")
    return subprocess.run(
        [_NODE, str(tmp_path / "test.mjs")],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_worker_wiring_skips_navigator_numbers(tmp_path) -> None:
    script = """
import { Screen, makeEl } from './harness.mjs';
const navigator = makeEl({ dataset: { workerKey: 'navigator_interval_min' } });
const memory = makeEl({ dataset: { workerKey: 'dream_interval_h' } });
const screen = new Screen({});
screen.contentEl.querySelectorAll = () => [navigator, memory];
screen._wireWorkerSettings();
if (navigator._handlers.change) throw new Error('navigator got worker wiring');
if (!memory._handlers.change) throw new Error('memory lost its handler');
"""
    result = _run(tmp_path, script, ["_wireWorkerSettings"])

    assert result.returncode == 0, result.stderr + result.stdout


def test_clearing_the_watch_list_sends_the_clear_flag(tmp_path) -> None:
    script = """
import { Screen, makeEl, calls } from './harness.mjs';
const screen = new Screen({});
const el = makeEl({ value: '   ' });
await screen._saveNavigatorList('navigator_watch_topics', el);
const sent = calls.at(-1);
if (sent.navigator_watch_topics !== '   ') throw new Error('value not sent');
if (sent.navigator_watch_topics_clear !== '1') throw new Error('clear flag missing');
"""
    result = _run(
        tmp_path,
        script,
        ["_saveNavigatorList", "_saveNavigatorParams"],
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_a_failed_number_save_rolls_the_input_back(tmp_path) -> None:
    script = """
import assert from 'node:assert/strict';
import { Screen, makeEl, setFailure, toasts } from './harness.mjs';
setFailure('nope');
const screen = new Screen({});
const el = makeEl({ value: '999' });
await screen._saveNavigatorNumber('navigator_interval_min', el);
assert.equal(el.value, 60);
assert.equal(toasts.at(-1).kind, 'error');
"""
    result = _run(
        tmp_path,
        script,
        ["_saveNavigatorNumber", "_saveNavigatorParams"],
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_the_number_is_sent_with_its_snake_case_name(tmp_path) -> None:
    script = """
import assert from 'node:assert/strict';
import { Screen, makeEl, calls } from './harness.mjs';
const screen = new Screen({});
const el = makeEl({ value: '30' });
await screen._saveNavigatorNumber('navigator_interval_min', el);
assert.equal(calls.at(-1).navigator_interval_min, '30');
"""
    result = _run(
        tmp_path,
        script,
        ["_saveNavigatorNumber", "_saveNavigatorParams"],
    )

    assert result.returncode == 0, result.stderr + result.stdout
