"""The Model screen and the Home "This machine" panel, rendered by the page's own script in node.

Pinned from a browser QA pass on the fake engine:

* the chart sits at the TOP of the cell detail, right under the cell header, the block table
  below it (at the bottom of a long side panel it was off screen);
* neither the cell detail nor "This machine" scrolls sideways: fixed table layout, long values
  wrap, and no `overflow-x:auto|scroll` container in either;
* a grade held at MARG says why ON THE ROW (and the grid cell carries a mark), not only in a
  hover tooltip; a block that ships switched off is named beside the cell, not in its colour;
* a dB curve (the rail PSRR) is drawn on a linear y axis with dB ticks, never decades.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from tests.test_server import PAGE

RENDER_JS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
function node(id){ return { id, innerHTML:'', textContent:'', style:{}, value:'', files:null,
  classList:{add(){},remove(){}}, setAttribute(){}, getAttribute(){return null;},
  querySelector(){return null;}, querySelectorAll(){return [];}, closest(){return null;},
  focus(){}, click(){}, appendChild(){}, removeChild(){} }; }
const nodes = {}; for (const id of ['nav','main','cli','foot','overlays']) nodes[id] = node(id);
const document = { getElementById:(id)=>nodes[id]||null, querySelector:()=>null,
  querySelectorAll:()=>[], createElement:(t)=>node(t), addEventListener:()=>{},
  body:{appendChild(){},removeChild(){}}, execCommand:()=>true };
function fetch(){ return new Promise(()=>{}); }               // everything is pre-loaded
const sandbox = { document, navigator:{}, console, Math, JSON, Date, Number, String, Object,
  Array, Boolean, Infinity, isFinite, parseFloat, parseInt, encodeURIComponent,
  decodeURIComponent, RegExp, Error, Promise, Set, Map, URLSearchParams,
  window:{ innerWidth:1440, innerHeight:900, location:{search:'',reload(){}},
           addEventListener:()=>{} },
  fetch, setTimeout:()=>0, clearTimeout:()=>{}, setInterval:()=>0 };
sandbox.globalThis = sandbox; sandbox.window.document = document;
vm.createContext(sandbox); vm.runInContext(src, sandbox, {filename:'index.html'});
const S = sandbox.S;
const P = 'VDD0P8_B', CK = 'tt/25C/vset3/1.0e-03A';
const fs_ = []; for (let i = 0; i < 41; i++) fs_.push(Math.pow(10, 1 + i / 5));
const psrr = { port:P, block:'psrr', cell:CK, cell_label:'tt / 25 C', x:fs_,
  x_label:'frequency [Hz]', x_log:true, y_log:false, y_scale:'linear', y_db:true,
  unit:'dB', label:'PSRR, supply to rail: 20 log10 |Vout/Vsupply|', complex:true,
  gt:{ mag:fs_.map(f => -60 + 20 * Math.log10(1 + f / 1e5)), phase_deg:fs_.map(() => 0) },
  model:{ mag:fs_.map(f => -59.6 + 20 * Math.log10(1 + f / 1e5)), phase_deg:fs_.map(() => 0) },
  points:fs_.length, source:'ac_psrr.' + P, score:0.4, metric:'PSRR dB RMS' };
const row = (name, extra) => Object.assign({ name, metric:'|Zout| dB RMS', value:'0.039',
  score:0.039, limit:'<= 1 / 3 dB', grade:'green', row_grade:'green', detail:'', reason:'',
  reason_full:'', held:false, held_by:[], default_off:false, switch:'', off_note:'',
  missing:false, n_points:40, load_a:1e-3, load:'1 mA', vset:3, temp:'', cell_key:CK,
  notes:[], identifiability:{} }, extra || {});
const cell = { port:P, corner:'tt', temp_c:'25', grade:'yellow', held:true, held_by:['zout'],
  off_by_default:[{ block:'load_en', grade:'red', switch:'load_en_' + P + '=1',
    note:'off by default: load_en FAIL -- not part of this grade; turn on with load_en_' + P
         + '=1 only if you need the load event and accept this grade' }],
  graded_by:'verify', verify_stale:false, runs:[], why:'',
  blocks:[
    row('psrr', { metric:'PSRR dB RMS', grade:'green', row_grade:'green' }),
    row('zout', { grade:'yellow', row_grade:'yellow', held:true, held_by:['Rpl'],
      reason:'held at yellow: the data does not pin Rpl',
      reason_full:'the residual is inside the green limit, but the data does not pin Rpl -- a '
        + 'tight fit to an undetermined parameter is the classic false green, so this is held at yellow',
      detail:'held' }),
    row('load_en', { metric:'load-step droop % error', value:'208', grade:'red', row_grade:'red',
      reason:'outside the acceptance limit', reason_full:'the fit misses this quantity',
      default_off:true, switch:'load_en_' + P + '=1',
      off_note:'off by default: load_en FAIL -- not part of this grade; turn on with load_en_'
        + P + '=1 only if you need the load event and accept this grade',
      cell_key:'tt/25C/averyveryverylongcellkeythatwouldneverwrapwithoutbreakinganywhere' })]};
const grades = { fitted:true, graded_by:'verify', why:'', ungraded:[], grades:[],
  cells:[{ corner:'tt', temp_c:25, label:'tt 25' }],
  rows:[{ port:P, cells:[{ corner:'tt', temp_c:25, grade:'yellow', block:'zout', held:true,
    held_by:['zout'], off:[{ block:'load_en', grade:'red', switch:'load_en_' + P + '=1',
    note:cell.off_by_default[0].note }] }] }] };
const summary = { fitted:true, graded_by:'verify', valid:{}, usable_not_signoff:[],
  not_run:[], hb:null, dataset:'x', runs_consumed:1, blocks:3 };
const out = {};

S.screen = 'model'; S.project = 'p'; S.loading = {}; S.failed = {}; S.errs = {}; S.job = null;
S.sel = { port:P, corner:'tt', temp:25, cell:'tt/25C', block:'psrr', blockCell:CK };
S.data = { 'model:summary':summary, 'model:grades':grades };
S.data['cell:' + P + 'tt25'] = cell;
S.data['curve:' + P + CK + 'psrr'] = psrr;
sandbox.render();
out.model = nodes.main.innerHTML;

// open the held row's full reason
const more = out.model.match(/<tr class="why"><td colspan="7">[\s\S]*?held:[\s\S]*?data-act="(a\d+)">more/);
sandbox.ACTS[more[1]]();
out.model_open = nodes.main.innerHTML;

S.screen = 'home'; S.project = null; S.sel = {};
S.data = { projects:{ projects:[], data_root:'/d' }, machine:{ ready:true, probes:[
  { name:'engine', ok:true, needed:true, ms:3,
    detail:'spectre at /opt/cadence/SPECTRE231/tools.lnx86/bin/averyveryverylongpathwithoutanyspaces/spectre' }],
  data_root:'/home/someone/cadence_work/workarea/pmukit/data/averyveryverylongdirectorynamewithoutspaces',
  pmukit:'0.0.0', python:'3.11.9' } };
sandbox.render();
out.home = nodes.main.innerHTML;
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    node_exe = shutil.which("node")
    if not node_exe:
        pytest.skip("node is not on PATH; the page gate needs it")
    d = tmp_path_factory.mktemp("model_page")
    text = PAGE.read_text(encoding="utf-8")
    (d / "page.js").write_text("\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>", text)),
                               encoding="utf-8", newline="\n")
    (d / "render.js").write_text(RENDER_JS, encoding="utf-8", newline="\n")
    p = subprocess.run([node_exe, str(d / "render.js"), str(d / "page.js")],
                       capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def _cell_panel(html: str) -> str:
    """The cell-detail panel: from its header (the port name) to the end of the page."""
    i = html.index('<span class="mono">VDD0P8_B</span> &middot; tt')
    return html[html.rfind('<div class="panel"', 0, i):]


def test_the_chart_comes_before_the_block_table(page):
    cell = _cell_panel(page["model"])
    chart, table = cell.find('<svg class="chart"'), cell.find('<table class="t fixed"')
    assert chart > 0 and table > 0, "both the chart and the table are in the cell detail"
    assert chart < table, "the chart sits right under the cell header, the table below it"


def test_the_side_panels_never_scroll_sideways(page):
    cell = _cell_panel(page["model"])
    assert not re.search(r"overflow-x\s*:\s*(auto|scroll)", cell)
    assert "overflow-x:hidden" in cell
    assert '<table class="t fixed"' in cell and "<colgroup>" in cell
    css = PAGE.read_text(encoding="utf-8")
    assert re.search(r"table\.t\.fixed\{[^}]*table-layout:fixed", css)
    assert re.search(r"\.t\.fixed td\{[^}]*overflow-wrap:anywhere", css)

    home = page["home"]
    i = home.index("This machine")
    mach = home[home.rfind('<div class="panel"', 0, i):]
    mach = mach[:mach.index("Probe again")]
    assert '<div class="kv wrap">' in mach and "overflow-x:hidden" in mach
    assert not re.search(r"overflow-x\s*:\s*(auto|scroll)", mach)
    assert re.search(r"\.kv\.wrap>span\{[^}]*overflow-wrap:anywhere", css)
    assert re.search(r"\.kv\.wrap\{[^}]*minmax\(0,1fr\)", css)


def test_a_held_grade_says_why_on_the_row_and_can_be_expanded(page):
    cell = _cell_panel(page["model"])
    why = re.findall(r'<tr class="why"><td colspan="7">([\s\S]*?)</td></tr>', cell)
    held = [w for w in why if "held:" in w]
    assert held and "the data does not pin Rpl" in held[0]
    assert "classic false green" not in held[0], "the long sentence is behind 'more'"
    opened = _cell_panel(page["model_open"])
    assert "classic false green" in opened and ">less</a>" in opened
    # the badge says it too, and the header carries a 'held' mark
    assert 'MARG</span><div class="hint" style="color:var(--warn)">held</div>' in cell
    assert '">held</span>' in cell


def test_a_default_off_block_is_named_not_counted(page):
    html = page["model"]
    cell = _cell_panel(html)
    assert "1 off by default" in cell
    assert "off by default</span> load_en FAIL" in cell and "load_en_VDD0P8_B=1" in cell
    assert "off by default</span> off by default" not in cell, "said once, not twice"
    # the grid cell keeps its own colour and carries the two marks
    grid = html[:html.index('<span class="mono">VDD0P8_B</span> &middot; tt')]
    cellhtml = re.search(r'<div class="cell rc c-warn[^>]*>MARG([\s\S]*?)</div>', grid)
    assert cellhtml, "the grid cell is MARG (yellow), not FAIL"
    assert '<i class="gm">h</i>' in cellhtml.group(1)
    assert '<i class="gm off">off</i>' in cellhtml.group(1)


def test_a_db_curve_is_drawn_on_a_linear_axis_with_db_ticks(page):
    cell = _cell_panel(page["model"])
    svg = cell[cell.index('<svg class="chart"'):cell.index("</svg>")]
    svg = svg[:svg.index("<path ")]                 # the magnitude grid; the phase pane follows
    ticks = re.findall(r'text-anchor="end" fill="var\(--ink3\)">([^<]+)</text>', svg)
    assert ticks, "the y axis has labels"
    assert not any(t.startswith("1e") for t in ticks), f"decade ticks on a dB axis: {ticks}"
    nums = [float(t) for t in ticks if re.fullmatch(r"-?\d+(\.\d+)?", t)]
    assert any(v < 0 for v in nums), ticks        # dB of a supply rejection
    steps = {round(b - a, 6) for a, b in zip(nums, nums[1:]) if b > a}
    assert len(steps) == 1, f"linear axis: evenly spaced ticks, got {ticks}"
    assert len(nums) >= 4, f"enough ticks to read a dB value off: {ticks}"
    assert ">dB</text>" in cell[cell.index('<svg class="chart"'):cell.index("</svg>")]
