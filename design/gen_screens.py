"""Generate the five pmukit screen artboards (.dc.html) for the Claude Design canvas.

Run:  python design/gen_screens.py   -> writes design/*.dc.html + design/canvas.json
Static demo data only (synthetic PMU 'PMU_DEMO'); nothing here touches real designs.
"""
import json
import pathlib

OUT = pathlib.Path(__file__).parent

ACC = "#1f6f9f"
GT = "#b45309"

CSS = """
body{margin:0;background:#f7f6f3;color:#1c1b18;font-family:'IBM Plex Sans',system-ui,'Segoe UI',sans-serif;font-size:13px;line-height:1.45}
a{color:#1f6f9f}a:hover{color:#16506f}
.mono{font-family:'IBM Plex Mono',ui-monospace,Consolas,monospace}
.app{width:1440px;height:900px;display:flex;flex-direction:column;background:#f7f6f3;overflow:hidden;box-sizing:border-box}
.nav{height:48px;display:flex;align-items:center;gap:8px;padding:0 20px;background:#ffffff;border-bottom:1px solid #dedbd3;flex:none}
.brand{font-family:'IBM Plex Mono',ui-monospace,Consolas,monospace;font-weight:600;font-size:14px;letter-spacing:.02em}
.proj{color:#5d5a53;font-size:12.5px;padding-left:12px;border-left:1px solid #dedbd3;margin-left:4px}
.steps{display:flex;gap:2px;margin-left:28px}
.step{display:flex;align-items:center;gap:8px;padding:5px 12px;border-radius:4px;color:#8a867d;font-size:12.5px}
.step.cur{background:#e8f1f7;color:#1f6f9f;font-weight:600}
.step .n{width:18px;height:18px;border-radius:50%;border:1px solid currentColor;display:flex;align-items:center;justify-content:center;font-size:11px;font-family:'IBM Plex Mono',ui-monospace,monospace}
.step.done{color:#1c1b18}.step.done .n{background:#1c1b18;color:#fff;border-color:#1c1b18}
.navr{margin-left:auto;display:flex;gap:16px;color:#8a867d;font-size:12px}
.main{flex:1;display:flex;gap:16px;padding:16px 20px;min-height:0}
.col{display:flex;flex-direction:column;gap:16px;min-height:0}
.panel{background:#ffffff;border:1px solid #dedbd3;border-radius:4px;display:flex;flex-direction:column;min-height:0}
.ph{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 14px;border-bottom:1px solid #ebe8e1;font-weight:600;font-size:13px;flex:none}
.ph .sub{font-weight:400;color:#8a867d;font-size:12px}
.pb{padding:12px 14px;overflow:auto;flex:1;min-height:0}
table.t{width:100%;border-collapse:collapse;font-size:12.5px}
.t th{text-align:left;font-weight:600;color:#5d5a53;font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;padding:6px 8px;border-bottom:1px solid #dedbd3;white-space:nowrap}
.t td{padding:6px 8px;border-bottom:1px solid #ebe8e1;vertical-align:middle}
.t tr.sel td{background:#e8f1f7}
.t tr.click{cursor:pointer}.t tr.click:hover td{background:#f3f1ec}
.t tr.sel.click:hover td{background:#e8f1f7}
.num{font-family:'IBM Plex Mono',ui-monospace,Consolas,monospace;font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.id{font-family:'IBM Plex Mono',ui-monospace,Consolas,monospace;font-size:12px;white-space:nowrap}
.badge{display:inline-flex;align-items:center;gap:5px;padding:1px 7px;border-radius:3px;font-size:11px;font-weight:600;border:1px solid;white-space:nowrap;line-height:16px}
.b-ok{color:#2f7d4f;border-color:#bfe0cb;background:#eef8f1}
.b-warn{color:#b7791f;border-color:#ecd9a8;background:#fbf5e6}
.b-bad{color:#b3362e;border-color:#efc2bd;background:#fdeeec}
.b-mute{color:#8a867d;border-color:#dedbd3;background:#f3f1ec}
.b-acc{color:#1f6f9f;border-color:#bcd7e8;background:#e8f1f7}
.b-ink{color:#1c1b18;border-color:#cfccc4;background:#f3f1ec}
.btn{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 14px;border-radius:4px;border:1px solid #cfccc4;background:#ffffff;color:#1c1b18;font:inherit;font-weight:600;font-size:13px;cursor:pointer;white-space:nowrap}
.btn:hover{background:#f3f1ec}
.btn.pri{background:#1f6f9f;border-color:#1f6f9f;color:#ffffff}.btn.pri:hover{background:#16506f}
.btn:disabled{opacity:.45;cursor:default}
.btn.sm{height:26px;padding:0 10px;font-size:12px}
.chip{display:inline-flex;align-items:center;height:24px;padding:0 10px;border-radius:12px;border:1px solid #cfccc4;background:#ffffff;font-size:12px;cursor:pointer;font-family:'IBM Plex Mono',ui-monospace,monospace}
.chip.on{background:#1c1b18;color:#ffffff;border-color:#1c1b18}
.kv{display:grid;grid-template-columns:150px 1fr;gap:5px 12px;font-size:12.5px}
.kv .k{color:#5d5a53}
.field{display:flex;flex-direction:column;gap:5px}
.lbl{font-size:10.5px;font-weight:600;color:#5d5a53;letter-spacing:.05em;text-transform:uppercase}
.inp{height:30px;border:1px solid #cfccc4;border-radius:4px;padding:0 8px;font:inherit;background:#ffffff;box-sizing:border-box}
.hint{color:#8a867d;font-size:12px}
.foot{height:52px;display:flex;align-items:center;justify-content:space-between;padding:0 20px;background:#ffffff;border-top:1px solid #dedbd3;flex:none}
.stat{display:flex;flex-direction:column;gap:2px;padding:0 18px 0 0;margin-right:18px;border-right:1px solid #ebe8e1}
.stat:last-child{border-right:0}
.stat .v{font-family:'IBM Plex Mono',ui-monospace,Consolas,monospace;font-size:20px;font-weight:600;line-height:1.1}
.stat .l{font-size:11px;color:#8a867d;text-transform:uppercase;letter-spacing:.05em}
.callout{border:1px solid #dedbd3;border-radius:4px;padding:10px 12px;background:#faf9f6;font-size:12.5px}
.callout.warn{border-color:#ecd9a8;background:#fbf5e6}
.callout.bad{border-color:#efc2bd;background:#fdeeec}
.trust{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.trust .box{border:1px solid #dedbd3;border-radius:4px;padding:10px 12px;background:#ffffff}
.trust .h{font-size:10.5px;font-weight:600;color:#5d5a53;letter-spacing:.05em;text-transform:uppercase;margin-bottom:6px}
.code{font-family:'IBM Plex Mono',ui-monospace,Consolas,monospace;font-size:12px;background:#1c1b18;color:#e9e6de;padding:10px 12px;border-radius:4px;white-space:pre;overflow:auto;line-height:1.5}
.bar{display:flex;height:10px;border-radius:5px;overflow:hidden;background:#ebe8e1}
.seg{height:100%}
.grid{display:grid;gap:4px}
.cell{height:30px;display:flex;align-items:center;justify-content:center;border-radius:3px;font-size:11px;font-weight:600;cursor:pointer;border:1px solid transparent;font-family:'IBM Plex Mono',ui-monospace,monospace}
.cell.sel{outline:2px solid #1c1b18;outline-offset:1px}
.c-ok{background:#eef8f1;color:#2f7d4f;border-color:#bfe0cb}
.c-warn{background:#fbf5e6;color:#b7791f;border-color:#ecd9a8}
.c-bad{background:#fdeeec;color:#b3362e;border-color:#efc2bd}
.c-mute{background:#f3f1ec;color:#8a867d;border-color:#dedbd3}
.c-head{background:transparent;color:#5d5a53;font-size:10.5px;cursor:default;font-weight:600;letter-spacing:.04em}
.legend{display:flex;gap:14px;font-size:12px;color:#5d5a53;align-items:center}
.sw{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
input[type=checkbox]{width:15px;height:15px;accent-color:#1f6f9f;margin:0}
"""

ICONS = {
    "check": '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5l3 3 7-7"></path></svg>',
    "alert": '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2.5l6 11H2z"></path><path d="M8 7v3"></path><path d="M8 12.2v.1"></path></svg>',
    "x": '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"></path></svg>',
    "dash": '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 8h8"></path></svg>',
    "play": '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M4 2.5v11l9-5.5z"></path></svg>',
    "file": '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M4 1.5h5.5L13 5v9.5H4z"></path><path d="M9.5 1.5V5H13"></path></svg>',
    "copy": '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="5.5" y="5.5" width="8" height="8" rx="1"></rect><path d="M10.5 5.5v-3h-8v8h3"></path></svg>',
    "retry": '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M13 8a5 5 0 1 1-1.5-3.6"></path><path d="M13 2.5v3h-3"></path></svg>',
    "arrow": '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8h10M9 4l4 4-4 4"></path></svg>',
    "spin": '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M8 2.5a5.5 5.5 0 1 1-5.2 3.7"></path></svg>',
    "upload": '<svg width="20" height="20" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8 11V3.5M4.5 7L8 3.5 11.5 7"></path><path d="M2.5 11.5v2h11v-2"></path></svg>',
}

STEPS = [("1", "New"), ("2", "Plan"), ("3", "Run"), ("4", "Model"), ("5", "Deliver")]


def nav(cur):
    parts = []
    for i, (n, name) in enumerate(STEPS, 1):
        cls = "step cur" if i == cur else ("step done" if i < cur else "step")
        parts.append(f'<div class="{cls}"><span class="n">{n}</span><span>{name}</span></div>')
    return ('<div class="nav"><span class="brand">pmukit</span><span class="proj">demo_pmu · PMU_DEMO</span>'
            f'<div class="steps">{"".join(parts)}</div>'
            '<div class="navr"><span>PMUKIT_DATA ~/pmukit_data</span><span>v0.1 · engine alps</span></div></div>')


def page(title, cur, body, script, props="{}"):
    head = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <script src="./support.js"></script>
</head>
<body>
<x-dc>
<helmet>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&amp;family=IBM+Plex+Mono:wght@400;600&amp;display=swap">
  <style>{CSS}</style>
</helmet>
<div class="app">
{nav(cur)}
{body}
</div>
</x-dc>
<script data-dc-script data-props='{props}'>
{script}
</script>
</body>
</html>
"""
    return head


# ------------------------------------------------------------------ 1 New
NEW_BODY = f"""
<div class="main">
  <div class="col" style="flex:0 0 700px">
    <div class="panel" style="flex:1">
      <div class="ph"><span>Netlist</span><span class="sub">one file, exported at the nominal corner</span></div>
      <div class="pb">
        <sc-if value="{{{{ notLoaded }}}}" hint-placeholder-val="{{{{ true }}}}">
          <div style="border:1.5px dashed #cfccc4;border-radius:6px;height:220px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;color:#5d5a53">
            <span style="color:#8a867d">{ICONS['upload']}</span>
            <div style="font-weight:600;color:#1c1b18">Drop <span class="mono">input.scs</span> here</div>
            <div class="hint">Sources named IL_ / VB_ / VS_ / VEN_ tell pmukit which pin is which. No manifest.</div>
            <div style="display:flex;gap:8px;margin-top:6px"><button class="btn" onClick="{{{{ load }}}}">Choose file</button><button class="btn" onClick="{{{{ load }}}}">Use sample netlist</button></div>
          </div>
        </sc-if>
        <sc-if value="{{{{ loaded }}}}" hint-placeholder-val="{{{{ false }}}}">
          <div class="kv" style="margin-bottom:12px">
            <span class="k">File</span><span class="mono">tb/input.scs · 41 KB · sha 9c1e…b7</span>
            <span class="k">PMU instance</span><span><span class="mono">PMU_TOP</span> · cell <span class="mono">PMU_DEMO</span> · 10 pins</span>
            <span class="k">PDK include lines</span><span>2 with <span class="mono">section=tt</span> (toplevel.scs, rc.scs) · rewritten per corner</span>
            <span class="k">VSET parameter</span><span><span class="mono">parameters VSET=3</span> found · rewritten per code</span>
            <span class="k">Analyses in file</span><span>2 found · stripped, pmukit writes its own</span>
          </div>
          <table class="t">
            <thead><tr><th>Pin</th><th>Net</th><th>Role</th><th>From source</th><th>DC</th><th>Status</th></tr></thead>
            <tbody>
              <sc-for list="{{{{ pins }}}}" as="p" hint-placeholder-count="6">
                <tr><td class="mono">{{{{ p.pin }}}}</td><td class="mono" style="color:#5d5a53">{{{{ p.net }}}}</td><td><span class="badge {{{{ p.roleCls }}}}">{{{{ p.role }}}}</span></td><td class="mono">{{{{ p.src }}}}</td><td class="num" style="text-align:left">{{{{ p.dc }}}}</td><td><span class="badge {{{{ p.stCls }}}}">{{{{ p.st }}}}</span></td></tr>
              </sc-for>
            </tbody>
          </table>
          <div class="callout warn" style="margin-top:12px;display:flex;gap:8px;align-items:flex-start"><span style="color:#b7791f;margin-top:2px">{ICONS['alert']}</span><div><b>TESTMODE</b> has no IL_/VB_/VS_/VEN_ source, so it gets no role. It is left exactly as wired. If it is a rail or bias, add the named source and reload.</div></div>
        </sc-if>
      </div>
    </div>
  </div>
  <div class="col" style="flex:1">
    <div class="panel" style="flex:1">
      <div class="ph"><span>Three things only you know</span><span class="sub">everything else is derived</span></div>
      <div class="pb" style="display:flex;flex-direction:column;gap:18px">
        <div class="field"><span class="lbl">1 · Corners and temperatures you simulate at</span>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span class="hint" style="width:70px">corners</span>
            <sc-for list="{{{{ corners }}}}" as="c" hint-placeholder-count="3"><span class="chip {{{{ c.cls }}}}" onClick="{{{{ c.toggle }}}}">{{{{ c.name }}}}</span></sc-for>
            <input class="inp mono" style="width:150px;height:24px" placeholder="MOSff_RCss">
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center"><span class="hint" style="width:70px">temp °C</span>
            <sc-for list="{{{{ temps }}}}" as="c" hint-placeholder-count="3"><span class="chip {{{{ c.cls }}}}" onClick="{{{{ c.toggle }}}}">{{{{ c.name }}}}</span></sc-for>
            <input class="inp mono" style="width:70px;height:24px" placeholder="85">
          </div>
          <div style="display:flex;gap:6px;align-items:center"><span class="hint" style="width:70px">VSET codes</span><input class="inp mono" style="width:90px;height:24px" value="3"><span class="hint">from <span class="mono">parameters VSET=3</span></span></div>
        </div>
        <div class="field"><span class="lbl">2 · What your block draws from each rail</span>
          <table class="t">
            <thead><tr><th>Rail</th><th>On</th><th>Off</th><th>Switches on/off</th><th></th></tr></thead>
            <tbody>
              <sc-for list="{{{{ rails }}}}" as="r" hint-placeholder-count="2">
                <tr><td class="mono">{{{{ r.name }}}}</td><td><input class="inp mono" style="width:90px;height:26px" value="{{{{ r.on }}}}"></td><td><input class="inp mono" style="width:90px;height:26px" value="{{{{ r.off }}}}"></td><td><input type="checkbox" checked="{{{{ r.sw }}}}" onChange="{{{{ r.toggle }}}}"> <span class="hint">{{{{ r.swText }}}}</span></td><td><button class="btn sm" onClick="{{{{ r.measure }}}}">Measure from netlist</button></td></tr>
              </sc-for>
            </tbody>
          </table>
          <span class="hint">On/Off defaults come from the IL_ source's dc. "Switches" adds the load-EN on/off transient, the one large-signal event that is modeled.</span>
        </div>
        <div class="field"><span class="lbl">3 · Highest frequency you care about</span>
          <div style="display:flex;gap:8px;align-items:center"><input class="inp mono" style="width:120px" value="20 GHz"><span class="hint">default from the netlist's HB: fund 5.0 GHz × 4 harmonics. Zout/PSRR are characterized up to here and never extrapolated past it.</span></div>
        </div>
        <div class="field">
          <a href="#" onClick="{{{{ toggleAdv }}}}" style="font-size:12.5px;text-decoration:none">{{{{ advLabel }}}}</a>
          <sc-if value="{{{{ adv }}}}" hint-placeholder-val="{{{{ false }}}}">
            <div class="kv" style="margin-top:6px;padding:10px 12px;background:#faf9f6;border:1px solid #ebe8e1;border-radius:4px">
              <span class="k">Load grid VDD0P8_A</span><span class="mono">2 µ · 100 µ · 500 µ · 1 m  (off, 0.2×on, on, 2×on)</span>
              <span class="k">Load grid VDD0P8_B</span><span class="mono">20 µ · 400 µ · 2 m · 4 m</span>
              <span class="k">AC sweep</span><span class="mono">10 Hz → 20 GHz, 20 pts/dec</span>
              <span class="k">Noise sweep</span><span class="mono">10 Hz → 100 MHz</span>
              <span class="k">DC temp sweep</span><span class="mono">−40 → 125 °C step 5 (continuous T inside each corner)</span>
              <span class="k">Bias I-V sweep</span><span class="mono">0 → 1.0 V, 21 pts (compliance from VB_ dc)</span>
              <span class="k">Load-EN edge</span><span class="mono">2 ns (measured) · tstop set after the Zout fit</span>
              <span class="k">Cells</span><span class="mono">3 corners × 3 temps × 1 VSET = 9</span>
            </div>
          </sc-if>
        </div>
      </div>
    </div>
  </div>
</div>
<div class="foot"><span class="hint">{{{{ footText }}}}</span><button class="btn pri" disabled="{{{{ notLoaded }}}}">Build plan {ICONS['arrow']}</button></div>
"""

NEW_SCRIPT = """
class Component extends DCLogic {
  constructor(p){ super(p); this.state = { loaded: false, adv: false,
    corners: ['tt','ss','ff'], cornersOn: {tt:true, ss:true, ff:true},
    temps: ['-40','25','125'], tempsOn: {'-40':true,'25':true,'125':true},
    sw: {VDD0P8_A: true, VDD0P8_B: false} }; }
  renderVals(){
    const s = this.state;
    const pins = [
      {pin:'AVDD1P0', net:'AVDD1P0', role:'supply', roleCls:'b-ink', src:'VS_AVDD1P0', dc:'1.00 V', st:'ok', stCls:'b-ok'},
      {pin:'VDD0P8_A', net:'VDD0P8_A', role:'rail', roleCls:'b-acc', src:'IL_VDD0P8_A', dc:'500 µA', st:'ok', stCls:'b-ok'},
      {pin:'VDD0P8_B', net:'VDD0P8_B', role:'rail', roleCls:'b-acc', src:'IL_VDD0P8_B', dc:'2.0 mA', st:'ok', stCls:'b-ok'},
      {pin:'IB_PTAT', net:'ib_ptat', role:'bias', roleCls:'b-acc', src:'VB_IB_PTAT', dc:'0.667 V', st:'ok', stCls:'b-ok'},
      {pin:'IB_CONST', net:'ib_const', role:'bias', roleCls:'b-acc', src:'VB_IB_CONST', dc:'1.28 V', st:'ok', stCls:'b-ok'},
      {pin:'EN', net:'en', role:'enable', roleCls:'b-ink', src:'VEN_EN', dc:'0 → 1 V', st:'ok', stCls:'b-ok'},
      {pin:'VSS_A · VSS_B · AGND', net:'(3 grounds)', role:'ground', roleCls:'b-mute', src:'from wiring', dc:'—', st:'ok', stCls:'b-ok'},
      {pin:'TESTMODE', net:'testmode', role:'none', roleCls:'b-warn', src:'—', dc:'—', st:'unclassified', stCls:'b-warn'},
    ];
    const chips = (names, on, key) => names.map(n => ({ name:n, cls: on[n] ? 'on' : '',
      toggle: () => { const o = Object.assign({}, on); o[n] = !o[n]; this.setState({[key]: o}); } }));
    const rails = [
      {name:'VDD0P8_A', on:'500 µA', off:'2 µA'}, {name:'VDD0P8_B', on:'2.0 mA', off:'20 µA'},
    ].map(r => ({ ...r, sw: !!s.sw[r.name], swText: s.sw[r.name] ? 'load-EN on/off characterized' : 'static load only',
      toggle: () => { const o = Object.assign({}, s.sw); o[r.name] = !o[r.name]; this.setState({sw:o}); },
      measure: () => {} }));
    const nc = s.corners.filter(c => s.cornersOn[c]).length, nt = s.temps.filter(t => s.tempsOn[t]).length;
    return {
      loaded: s.loaded, notLoaded: !s.loaded, adv: s.adv,
      advLabel: s.adv ? 'Hide derived settings' : 'Show derived settings (pmukit decided these)',
      load: () => this.setState({loaded:true}), toggleAdv: (e) => { if (e && e.preventDefault) e.preventDefault(); this.setState({adv: !s.adv}); },
      pins, rails, corners: chips(s.corners, s.cornersOn, 'cornersOn'), temps: chips(s.temps, s.tempsOn, 'tempsOn'),
      footText: s.loaded ? `${nc} corners × ${nt} temps × 1 VSET = ${nc*nt} cells · 2 rails · 2 biases · EN` : 'Load a netlist to continue',
    };
  }
}
"""

# ------------------------------------------------------------------ 2 Plan
PLAN_GROUPS = [
    dict(id="g1", an="dc_load", ports="VDD0P8_A, VDD0P8_B", runs=18, stim="IL_* swept 0 → 2×on", reads="vout(load)", feeds="vout table · dropout · ilimit", h=0.3,
         why="Every rail's DC level at every load and corner. Also finds dropout and the current limit, which the hot corner can cut by 8×.", cons="Rail DC unknown → nothing else can be fit. Cannot be skipped."),
    dict(id="g2", an="dc_temp", ports="all ports", runs=3, stim="temp −40 → 125 step 5", reads="vout(T), idc(T)", feeds="continuous-T tables", h=0.2,
         why="One sweep per corner makes temperature continuous inside the .va, so you can set any temperature, not just the three points.", cons="Model valid only at −40/25/125 exactly; any other temperature is refused."),
    dict(id="g3", an="dc_iv", ports="IB_PTAT, IB_CONST", runs=18, stim="VB_* swept 0 → 1.0 V", reads="I(Vpin)", feeds="idc · compliance · gds", h=0.3,
         why="Bias current vs pin voltage: the PTAT slope goes straight into KVCO drift, and the knee sets the compliance range.", cons="Bias ports emitted as ideal current sources with no knee and no temperature slope."),
    dict(id="g4", an="ac · inject VDD0P8_A", ports="VDD0P8_A", runs=36, stim="IL_VDD0P8_A ac=1 (9 cells × 4 loads)", reads="Zout.A", feeds="zout.A ladder", h=1.8,
         why="Output impedance vs frequency. Your block's current ripple × Zout = rail ripple = AM-PM.", cons="zout.A NOT RUN → rail A emitted as an ideal voltage source; spur/pushing paths missing."),
    dict(id="g5", an="ac · inject VDD0P8_B", ports="VDD0P8_B", runs=36, stim="IL_VDD0P8_B ac=1", reads="Zout.B", feeds="zout.B ladder", h=1.8,
         why="Same as above for rail B.", cons="zout.B NOT RUN → rail B emitted as an ideal voltage source."),
    dict(id="g6", an="ac · inject AVDD1P0", ports="all outputs", runs=36, stim="VS_AVDD1P0 ac=1", reads="PSRR.A, PSRR.B, psrr.IB_PTAT, psrr.IB_CONST", feeds="4 psrr blocks", h=1.8,
         why="One supply injection is read at every output at once (AC superposition), so 4 transfers cost 1 run per cell.", cons="All 4 PSRR blocks NOT RUN → supply ripple never reaches the outputs in your sim."),
    dict(id="g7", an="noise VDD0P8_A", ports="VDD0P8_A", runs=36, stim="—", reads="noise_v.A", feeds="noise.A (white + 1/f + shaped)", h=2.4,
         why="Rail voltage noise; supply pushing turns it into phase noise. Depends on load, hence 4 loads.", cons="noise.A NOT RUN → rail A is noiseless in .noise / pnoise / hbnoise. Report will be red."),
    dict(id="g8", an="noise VDD0P8_B", ports="VDD0P8_B", runs=36, stim="—", reads="noise_v.B", feeds="noise.B", h=2.4,
         why="Same for rail B.", cons="noise.B NOT RUN → rail B noiseless."),
    dict(id="g9", an="noise IB_*", ports="IB_PTAT, IB_CONST", runs=18, stim="—", reads="noise_i.PTAT, noise_i.CONST", feeds="bias noise blocks", h=1.2,
         why="Bias current noise up-converts into VCO phase noise and is often the dominant term.", cons="Bias ports noiseless → phase noise optimistic by up to 10 dB close-in."),
    dict(id="g10", an="tran · load-EN A", ports="VDD0P8_A", runs=18, stim="IL_VDD0P8_A pwl 2 µ ↔ 500 µ, 2 ns edge", reads="tran_load_on.A, tran_load_off.A", feeds="load_en.A (large-signal, opt-in)", h=1.6,
         why="Your block switching on: how deep the rail dips and how it overshoots on switch-off (the LDO cannot sink).", cons="load_en.A not fit → rail A stays linear; dips under-predicted for mA steps. HB deliverable unaffected."),
    dict(id="g11", an="tran · load-EN B", ports="VDD0P8_B", runs=18, stim="IL_VDD0P8_B pwl 20 µ ↔ 2 m, 2 ns edge", reads="tran_load_on.B, tran_load_off.B", feeds="load_en.B", h=1.6,
         why="Same for rail B.", cons="load_en.B not fit → rail B stays linear."),
    dict(id="g12", an="tran · EN", ports="all outputs", runs=9, stim="VEN_EN 0 → 1", reads="tran_en", feeds="en.ramp (usable, not sign-off)", h=0.9,
         why="Rails and biases come up with the measured rise time so a testbench that toggles EN does not blow up. Startup sign-off still uses the real LDO.", cons="EN pin emitted as an instant switch."),
]

PLAN_BODY = f"""
<div class="main">
  <div class="col" style="flex:1">
    <div class="panel" style="flex:none">
      <div class="pb" style="display:flex;padding:14px 18px">
        <div class="stat"><span class="v">9</span><span class="l">cells</span></div>
        <div class="stat"><span class="v">{{{{ runs }}}}</span><span class="l">runs</span></div>
        <div class="stat"><span class="v">{{{{ cpuh }}}}</span><span class="l">CPU-h est.</span></div>
        <div class="stat"><span class="v">0</span><span class="l">cached</span></div>
        <div class="stat"><span class="v">{{{{ nGroups }}}}</span><span class="l">groups on</span></div>
        <div style="margin-left:auto;align-self:center" class="hint">Estimates from the last 3 projects on this queue. Groups merge runs by AC superposition; nothing here is duplicated.</div>
      </div>
    </div>
    <div class="panel" style="flex:1">
      <div class="ph"><span>What will run, and why</span><span class="sub">click a row for the reason · untick to see what you lose</span></div>
      <div class="pb" style="padding:0">
        <table class="t">
          <thead><tr><th style="width:28px"></th><th>Analysis</th><th>Ports</th><th class="num" style="text-align:right">Runs</th><th>Stimulus</th><th>Reads</th><th>Feeds</th><th class="num" style="text-align:right">CPU-h</th></tr></thead>
          <tbody>
            <sc-for list="{{{{ groups }}}}" as="g" hint-placeholder-count="12">
              <tr class="click {{{{ g.cls }}}}" onClick="{{{{ g.select }}}}"><td><input type="checkbox" checked="{{{{ g.on }}}}" onChange="{{{{ g.toggle }}}}" onClick="{{{{ g.stop }}}}"></td><td class="mono" style="{{{{ g.style }}}}">{{{{ g.an }}}}</td><td class="mono" style="color:#5d5a53">{{{{ g.ports }}}}</td><td class="num">{{{{ g.runs }}}}</td><td class="hint">{{{{ g.stim }}}}</td><td class="mono" style="font-size:11.5px">{{{{ g.reads }}}}</td><td style="font-size:12px">{{{{ g.feeds }}}}</td><td class="num">{{{{ g.h }}}}</td></tr>
            </sc-for>
          </tbody>
        </table>
      </div>
    </div>
  </div>
  <div class="col" style="flex:0 0 380px">
    <div class="panel" style="flex:1">
      <div class="ph"><span>Why this run exists</span><span class="sub mono">{{{{ sel.an }}}}</span></div>
      <div class="pb" style="display:flex;flex-direction:column;gap:14px">
        <div style="font-size:13px;line-height:1.55">{{{{ sel.why }}}}</div>
        <div class="kv"><span class="k">Feeds</span><span class="mono">{{{{ sel.feeds }}}}</span><span class="k">Runs</span><span class="mono">{{{{ sel.runs }}}} across 9 cells</span><span class="k">Stimulus</span><span class="mono">{{{{ sel.stim }}}}</span></div>
        <div><div class="lbl" style="margin-bottom:6px">If you skip it</div><div class="callout warn">{{{{ sel.cons }}}}</div></div>
      </div>
    </div>
    <div class="panel" style="flex:none">
      <div class="ph"><span>Consequences of current selection</span></div>
      <div class="pb">
        <sc-if value="{{{{ allOn }}}}" hint-placeholder-val="{{{{ true }}}}"><div style="display:flex;gap:8px;align-items:center;color:#2f7d4f">{ICONS['check']}<span>Every block in the model spec has its data. Nothing will be reported as NOT RUN.</span></div></sc-if>
        <sc-if value="{{{{ anyOff }}}}" hint-placeholder-val="{{{{ false }}}}">
          <div style="display:flex;flex-direction:column;gap:8px">
            <sc-for list="{{{{ offList }}}}" as="o" hint-placeholder-count="1"><div class="callout bad" style="display:flex;gap:8px"><span style="color:#b3362e;margin-top:2px">{ICONS['x']}</span><div><b class="mono">{{{{ o.an }}}}</b> — {{{{ o.cons }}}}</div></div></sc-for>
          </div>
        </sc-if>
      </div>
    </div>
  </div>
</div>
<div class="foot"><span class="hint">Queue <span class="mono">rf_short</span> · 8 CPU per job · resume: runs already in the ledger are skipped</span><div style="display:flex;gap:8px"><button class="btn">Export plan (CSV)</button><button class="btn pri">{ICONS['play']} Submit {{{{ runs }}}} runs</button></div></div>
"""

PLAN_SCRIPT = "const GROUPS = " + json.dumps(PLAN_GROUPS, ensure_ascii=False) + """;
class Component extends DCLogic {
  constructor(p){ super(p); const on = {}; GROUPS.forEach(g => on[g.id] = true); this.state = { on, sel: 'g7' }; }
  renderVals(){
    const s = this.state;
    const groups = GROUPS.map(g => ({ ...g, on: !!s.on[g.id], cls: s.sel === g.id ? 'sel' : '',
      style: s.on[g.id] ? '' : 'text-decoration:line-through;color:#8a867d',
      select: () => this.setState({sel: g.id}),
      toggle: () => { const o = Object.assign({}, s.on); o[g.id] = !o[g.id]; this.setState({on:o}); },
      stop: (e) => { if (e && e.stopPropagation) e.stopPropagation(); } }));
    const onG = groups.filter(g => g.on);
    const runs = onG.reduce((a,g) => a + g.runs, 0), cpuh = onG.reduce((a,g) => a + g.h, 0);
    const offList = groups.filter(g => !g.on);
    return { groups, runs, cpuh: cpuh.toFixed(1), nGroups: onG.length + ' / ' + GROUPS.length,
      sel: GROUPS.find(g => g.id === s.sel), allOn: offList.length === 0, anyOff: offList.length > 0, offList };
  }
}
"""

# ------------------------------------------------------------------ 3 Run
RUN_ROWS = [
    ("7c3e91a04bd2", "tt / 25 °C / 3", "noise VDD0P8_A · 500 µ", "done", "4m 12s", "0.56"),
    ("b19f0c72e4a8", "ss / 125 °C / 3", "ac · inject AVDD1P0 · 2 m", "running", "1m 03s", "—"),
    ("e4d27a5c1f90", "ss / 125 °C / 3", "tran · load-EN B · off", "failed", "12m 40s", "1.69"),
    ("02aa8e6b7d31", "ff / −40 °C / 3", "noise IB_CONST", "running", "0m 41s", "—"),
    ("5f6c1d9e2ab7", "tt / 25 °C / 3", "dc_load VDD0P8_B", "cached", "—", "0.00"),
    ("a8e05b3c9d14", "ss / 25 °C / 3", "ac · inject VDD0P8_A · 100 µ", "done", "3m 08s", "0.42"),
    ("c73b2f8e6a05", "ff / 125 °C / 3", "tran · EN", "queued", "—", "—"),
    ("d1e94a7f0c26", "tt / 125 °C / 3", "noise VDD0P8_B · 4 m", "done", "5m 51s", "0.78"),
    ("39f8c6b1e2d7", "ss / −40 °C / 3", "dc_iv IB_PTAT", "done", "0m 22s", "0.05"),
    ("6b2d0e9a4c83", "ff / 25 °C / 3", "tran · load-EN A · on", "queued", "—", "—"),
    ("f0a7c4d2b8e1", "ss / 125 °C / 3", "tran · load-EN B · on", "failed", "12m 38s", "1.68"),
    ("8e1b5f3a7d09", "tt / −40 °C / 3", "dc_temp", "done", "1m 47s", "0.24"),
]

RUN_BODY = f"""
<div class="main">
  <div class="col" style="flex:1">
    <div class="panel" style="flex:none">
      <div class="pb" style="padding:14px 18px;display:flex;flex-direction:column;gap:10px">
        <div style="display:flex">
          <div class="stat"><span class="v">{{{{ c.done }}}}</span><span class="l">done</span></div>
          <div class="stat"><span class="v" style="color:#1f6f9f">{{{{ c.running }}}}</span><span class="l">running</span></div>
          <div class="stat"><span class="v">{{{{ c.queued }}}}</span><span class="l">queued</span></div>
          <div class="stat"><span class="v" style="color:#b3362e">{{{{ c.failed }}}}</span><span class="l">failed</span></div>
          <div class="stat"><span class="v" style="color:#8a867d">{{{{ c.cached }}}}</span><span class="l">cached</span></div>
          <div class="stat"><span class="v">{{{{ c.cpuh }}}}</span><span class="l">CPU-h used</span></div>
          <div style="margin-left:auto;align-self:center;display:flex;gap:8px"><span class="hint">ETA {{{{ c.eta }}}}</span><button class="btn sm" onClick="{{{{ retryAll }}}}">{ICONS['retry']} Retry failed</button><button class="btn sm">Pause queue</button></div>
        </div>
        <div class="bar"><div class="seg" style="background:#2f7d4f;width:{{{{ w.done }}}}%"></div><div class="seg" style="background:#1f6f9f;width:{{{{ w.running }}}}%"></div><div class="seg" style="background:#b3362e;width:{{{{ w.failed }}}}%"></div><div class="seg" style="background:#bdb9b0;width:{{{{ w.cached }}}}%"></div></div>
      </div>
    </div>
    <div class="panel" style="flex:1">
      <div class="ph"><span>Ledger</span><div style="display:flex;gap:6px"><sc-for list="{{{{ filters }}}}" as="f" hint-placeholder-count="4"><span class="chip {{{{ f.cls }}}}" onClick="{{{{ f.pick }}}}">{{{{ f.name }}}}</span></sc-for></div></div>
      <div class="pb" style="padding:0">
        <table class="t">
          <thead><tr><th>Run</th><th>Cell</th><th>Analysis</th><th>Status</th><th class="num" style="text-align:right">Elapsed</th><th class="num" style="text-align:right">CPU-h</th><th></th></tr></thead>
          <tbody>
            <sc-for list="{{{{ rows }}}}" as="r" hint-placeholder-count="12">
              <tr class="click {{{{ r.cls }}}}" onClick="{{{{ r.select }}}}"><td class="id">{{{{ r.id }}}}</td><td class="mono" style="font-size:12px">{{{{ r.cell }}}}</td><td>{{{{ r.an }}}}</td><td><span class="badge {{{{ r.bcls }}}}">{{{{ r.status }}}}</span></td><td class="num">{{{{ r.el }}}}</td><td class="num">{{{{ r.cpu }}}}</td><td><sc-if value="{{{{ r.isFailed }}}}" hint-placeholder-val="{{{{ false }}}}"><button class="btn sm" onClick="{{{{ r.retry }}}}">{ICONS['retry']} Retry</button></sc-if></td></tr>
            </sc-for>
          </tbody>
        </table>
      </div>
    </div>
  </div>
  <div class="col" style="flex:0 0 420px">
    <div class="panel" style="flex:1">
      <div class="ph"><span>Run <span class="id">{{{{ sel.id }}}}</span></span><span class="badge {{{{ sel.bcls }}}}">{{{{ sel.status }}}}</span></div>
      <div class="pb" style="display:flex;flex-direction:column;gap:12px">
        <div class="kv"><span class="k">Cell</span><span class="mono">{{{{ sel.cell }}}}</span><span class="k">Analysis</span><span>{{{{ sel.an }}}}</span><span class="k">Feeds</span><span class="mono">{{{{ sel.feeds }}}}</span><span class="k">Job</span><span class="mono">donau 48812903 · rf_short · 8 cpu</span><span class="k">Netlist</span><span class="mono">…/ss_125c_v3/{{{{ sel.id }}}}/input.scs</span><span class="k">PSF</span><span class="mono">{{{{ sel.psf }}}}</span></div>
        <div><div class="lbl" style="margin-bottom:6px">Log tail</div><div class="code" style="height:300px">{{{{ sel.log }}}}</div></div>
        <sc-if value="{{{{ sel.isFailed }}}}" hint-placeholder-val="{{{{ false }}}}"><div class="callout bad">Failed twice with the same message. If it fails again pmukit stops retrying and the fit proceeds with <b>tran_load_off.B @ ss/125 °C = NOT RUN</b>, flagged in the report.</div></sc-if>
      </div>
    </div>
  </div>
</div>
<div class="foot"><span class="hint">Runs write to <span class="mono">~/pmukit_data/demo_pmu/runs.sqlite</span> · closing this page does not stop the queue</span><button class="btn pri" disabled="{{{{ notDone }}}}">Fit model {ICONS['arrow']}</button></div>
"""

RUN_SCRIPT = "const ROWS = " + json.dumps([dict(id=a, cell=b, an=c, status=d, el=e, cpu=f) for a, b, c, d, e, f in RUN_ROWS]) + """;
const LOGS = {
  failed: `alps 2026.03.hf1  -mt 8  -format ps
tran: tstop=10u  step=2n (load-EN off, 2 m -> 20 u)
  t=1.9998e-06  step reduced 128x at VDD0P8_B (dV/dt)
  t=2.0011e-06  ERROR: timestep too small (1.2e-19)
  convergence failure near IL_VDD0P8_B edge
job FAILED  rc=1  12m 38s  peak mem 1.9 GB`,
  running: `alps 2026.03.hf1  -mt 8  -format ps
ac: 10 Hz -> 20 GHz  20 pts/dec  (acm_VS_AVDD1P0=1)
  dc op converged in 214 iterations
  ac sweep 41% ... 63% ... 78%`,
  done: `alps 2026.03.hf1  -mt 8  -format ps
noise: 10 Hz -> 100 MHz  oprobe=VDD0P8_A
  dc op converged in 190 iterations
  noise sweep complete (161 pts)
  wrote psf/noise.noise  .simDone
job DONE  rc=0  4m 12s  peak mem 1.4 GB`,
  queued: `waiting for a rf_short slot (71 ahead)`,
  cached: `identical run_id already in ledger (2026-09-14 18:02) -> reused`,
};
const BCLS = { done:'b-ok', running:'b-acc', failed:'b-bad', queued:'b-mute', cached:'b-mute' };
class Component extends DCLogic {
  constructor(p){ super(p); this.state = { rows: ROWS.map(r => ({...r})), sel: 'e4d27a5c1f90', filter: 'All', counts: {done:191, running:8, queued:71, failed:2, cached:10} }; }
  componentDidMount(){ this.timer = setInterval(() => { const c = Object.assign({}, this.state.counts); if (c.queued > 0) { c.queued -= 1; c.done += 1; this.setState({counts:c}); } }, 1800); }
  componentWillUnmount(){ clearInterval(this.timer); }
  renderVals(){
    const s = this.state, c = s.counts, total = 282;
    const pct = k => (100 * c[k] / total).toFixed(1);
    const rows = s.rows.filter(r => s.filter === 'All' || r.status === s.filter.toLowerCase()).map(r => ({ ...r,
      cls: r.id === s.sel ? 'sel' : '', bcls: BCLS[r.status], isFailed: r.status === 'failed',
      select: () => this.setState({sel: r.id}),
      retry: (e) => { if (e && e.stopPropagation) e.stopPropagation(); const rows = s.rows.map(x => x.id === r.id ? {...x, status:'queued', el:'—', cpu:'—'} : x); const cc = Object.assign({}, c); cc.failed = Math.max(0, cc.failed - 1); cc.queued += 1; this.setState({rows, counts: cc}); } }));
    const selRow = s.rows.find(r => r.id === s.sel) || s.rows[0];
    const sel = { ...selRow, bcls: BCLS[selRow.status], isFailed: selRow.status === 'failed', log: LOGS[selRow.status],
      feeds: selRow.an.startsWith('noise') ? 'noise block' : selRow.an.startsWith('ac') ? 'zout / psrr blocks' : selRow.an.startsWith('tran') ? 'load_en (large-signal)' : 'dc tables',
      psf: selRow.status === 'done' || selRow.status === 'cached' ? '…/psf/' + selRow.id + '/' : '—' };
    const filters = ['All','Running','Failed','Queued'].map(n => ({ name:n, cls: s.filter === n ? 'on' : '', pick: () => this.setState({filter:n}) }));
    const cpuh = (c.done * 0.052).toFixed(1);
    return { c: { ...c, cpuh, eta: c.queued > 0 ? Math.ceil(c.queued * 4.2 / 8) + ' min' : 'done' }, w: { done: pct('done'), running: pct('running'), failed: pct('failed'), cached: pct('cached') },
      rows, sel, filters, notDone: c.queued > 0 || c.running > 0,
      retryAll: () => { const rows = s.rows.map(x => x.status === 'failed' ? {...x, status:'queued', el:'—', cpu:'—'} : x); const cc = Object.assign({}, c); cc.queued += cc.failed; cc.failed = 0; this.setState({rows, counts: cc}); } };
  }
}
"""

# ------------------------------------------------------------------ 4 Model
MODEL_BODY = f"""
<div class="main">
  <div class="col" style="flex:1">
    <div class="panel" style="flex:none">
      <div class="ph"><span>Can I trust this model in my simulation?</span><span class="sub">the same text opens report.md</span></div>
      <div class="pb">
        <div class="trust">
          <div class="box"><div class="h">Valid range</div><div class="kv" style="grid-template-columns:70px 1fr"><span class="k">load A</span><span class="mono">2 µ – 1 mA</span><span class="k">load B</span><span class="mono">20 µ – 4 mA</span><span class="k">temp</span><span class="mono">−40 – 125 °C (continuous)</span><span class="k">freq</span><span class="mono">≤ 20 GHz</span><span class="k">corners</span><span class="mono">tt · ss · ff</span><span class="k">VSET</span><span class="mono">3</span></div></div>
          <div class="box"><div class="h">Usable, not sign-off</div><div style="display:flex;flex-direction:column;gap:6px"><span class="badge b-warn">{ICONS['alert']} EN power-up ramp</span><span class="hint">Rails and biases rise with the measured time. Use the real LDO for startup sign-off.</span></div></div>
          <div class="box"><div class="h">Not run</div><div style="display:flex;flex-direction:column;gap:6px"><span class="badge b-bad">{ICONS['x']} tran_load_off.B @ ss / 125 °C</span><span class="hint">Failed twice. load_en.B at that cell uses the ss / 25 °C fit; overshoot there is unverified.</span></div></div>
          <div class="box"><div class="h">HB health check</div><div style="display:flex;flex-direction:column;gap:6px"><span class="badge b-ok">{ICONS['check']} first-step residual 7.7e-3</span><span class="hint">Driven HB, every large-signal term toggled one at a time; none exceeds 10× the all-off residual.</span></div></div>
        </div>
      </div>
    </div>
    <div class="panel" style="flex:1">
      <div class="ph"><span>Grade per port and cell</span><span class="sub">worst block in the cell · click a cell</span></div>
      <div class="pb">
        <div class="grid" style="grid-template-columns:110px repeat(9, minmax(0,1fr))">
          <div class="cell c-head"></div>
          <sc-for list="{{{{ heads }}}}" as="h" hint-placeholder-count="9"><div class="cell c-head">{{{{ h }}}}</div></sc-for>
          <sc-for list="{{{{ rows }}}}" as="r" hint-placeholder-count="4">
            <div class="cell c-head" style="justify-content:flex-start;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;color:#1c1b18">{{{{ r.name }}}}</div>
            <sc-for list="{{{{ r.cells }}}}" as="c" hint-placeholder-count="9"><div class="cell {{{{ c.cls }}}}" onClick="{{{{ c.pick }}}}">{{{{ c.label }}}}</div></sc-for>
          </sc-for>
        </div>
        <div class="legend" style="margin-top:12px"><span><span class="badge b-ok">OK</span> all blocks within tolerance</span><span><span class="badge b-warn">MARG</span> one block near its limit</span><span><span class="badge b-bad">FAIL</span> block outside tolerance</span><span><span class="badge b-mute">N/R</span> data not run</span></div>
      </div>
    </div>
  </div>
  <div class="col" style="flex:0 0 560px">
    <div class="panel" style="flex:1">
      <div class="ph"><span><span class="mono">{{{{ selPort }}}}</span> · {{{{ selCell }}}}</span><span class="badge {{{{ selCls }}}}">{{{{ selGrade }}}}</span></div>
      <div class="pb" style="display:flex;flex-direction:column;gap:12px">
        <table class="t">
          <thead><tr><th>Block</th><th>Metric</th><th class="num" style="text-align:right">Model vs GT</th><th class="num" style="text-align:right">Limit</th><th>Grade</th></tr></thead>
          <tbody><sc-for list="{{{{ blocks }}}}" as="b" hint-placeholder-count="5"><tr class="click {{{{ b.cls }}}}" onClick="{{{{ b.pick }}}}"><td class="mono">{{{{ b.name }}}}</td><td class="hint">{{{{ b.metric }}}}</td><td class="num">{{{{ b.val }}}}</td><td class="num" style="color:#8a867d">{{{{ b.lim }}}}</td><td><span class="badge {{{{ b.gcls }}}}">{{{{ b.grade }}}}</span></td></tr></sc-for></tbody>
        </table>
        <div style="display:flex;justify-content:space-between;align-items:center"><span style="font-weight:600">{{{{ chartTitle }}}}</span><div style="display:flex;gap:6px"><span class="chip {{{{ viewChartCls }}}}" onClick="{{{{ showChart }}}}">Chart</span><span class="chip {{{{ viewTableCls }}}}" onClick="{{{{ showTable }}}}">Table</span></div></div>
        <sc-if value="{{{{ isChart }}}}" hint-placeholder-val="{{{{ true }}}}">
          <div style="position:relative">
            <svg width="520" height="250" viewBox="0 0 520 250" style="display:block;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10.5px" onMouseMove="{{{{ hover }}}}" onMouseLeave="{{{{ unhover }}}}">
              <sc-for list="{{{{ gridV }}}}" as="g" hint-placeholder-count="9"><line x1="{{{{ g.x }}}}" x2="{{{{ g.x }}}}" y1="14" y2="216" stroke="#ebe8e1"></line><text x="{{{{ g.x }}}}" y="232" text-anchor="middle" fill="#8a867d">{{{{ g.t }}}}</text></sc-for>
              <sc-for list="{{{{ gridH }}}}" as="g" hint-placeholder-count="4"><line x1="48" x2="512" y1="{{{{ g.y }}}}" y2="{{{{ g.y }}}}" stroke="#ebe8e1"></line><text x="42" y="{{{{ g.ty }}}}" text-anchor="end" fill="#8a867d">{{{{ g.t }}}}</text></sc-for>
              <line x1="48" x2="512" y1="216" y2="216" stroke="#cfccc4"></line>
              <path d="{{{{ pathGT }}}}" fill="none" stroke="{GT}" stroke-width="2" stroke-linejoin="round"></path>
              <path d="{{{{ pathModel }}}}" fill="none" stroke="{ACC}" stroke-width="2" stroke-linejoin="round"></path>
              <text x="{{{{ lblGT.x }}}}" y="{{{{ lblGT.y }}}}" fill="#5d5a53" font-family="'IBM Plex Sans',system-ui,sans-serif" font-size="11">ground truth</text>
              <text x="{{{{ lblM.x }}}}" y="{{{{ lblM.y }}}}" fill="#5d5a53" font-family="'IBM Plex Sans',system-ui,sans-serif" font-size="11">model</text>
              <sc-if value="{{{{ hov.on }}}}" hint-placeholder-val="{{{{ false }}}}">
                <line x1="{{{{ hov.x }}}}" x2="{{{{ hov.x }}}}" y1="14" y2="216" stroke="#8a867d" stroke-dasharray="3 3"></line>
                <circle cx="{{{{ hov.x }}}}" cy="{{{{ hov.yg }}}}" r="4" fill="{GT}" stroke="#ffffff" stroke-width="2"></circle>
                <circle cx="{{{{ hov.x }}}}" cy="{{{{ hov.ym }}}}" r="4" fill="{ACC}" stroke="#ffffff" stroke-width="2"></circle>
              </sc-if>
            </svg>
            <sc-if value="{{{{ hov.on }}}}" hint-placeholder-val="{{{{ false }}}}"><div style="position:absolute;left:{{{{ hov.tx }}}}px;top:8px;background:#1c1b18;color:#e9e6de;padding:6px 8px;border-radius:4px;font-size:11px;white-space:nowrap;pointer-events:none" class="mono">{{{{ hov.text }}}}</div></sc-if>
          </div>
          <div class="legend"><span><span class="sw" style="background:{GT}"></span>ground truth (transistor PMU)</span><span><span class="sw" style="background:{ACC}"></span>model (.va)</span><span class="hint">|Zout| in Ω vs frequency · log–log · hover for values</span></div>
        </sc-if>
        <sc-if value="{{{{ isTable }}}}" hint-placeholder-val="{{{{ false }}}}">
          <table class="t"><thead><tr><th>Frequency</th><th class="num" style="text-align:right">GT |Z| Ω</th><th class="num" style="text-align:right">Model |Z| Ω</th><th class="num" style="text-align:right">Δ dB</th></tr></thead>
          <tbody><sc-for list="{{{{ tableRows }}}}" as="t" hint-placeholder-count="8"><tr><td class="mono">{{{{ t.f }}}}</td><td class="num">{{{{ t.g }}}}</td><td class="num">{{{{ t.m }}}}</td><td class="num">{{{{ t.d }}}}</td></tr></sc-for></tbody></table>
        </sc-if>
      </div>
    </div>
  </div>
</div>
<div class="foot"><span class="hint">Fit from dataset <span class="mono">a91f…c3</span> · 271 of 282 runs consumed · 11 cached</span><div style="display:flex;gap:8px"><button class="btn">Open report.md</button><button class="btn pri">Deliver {ICONS['arrow']}</button></div></div>
"""

MODEL_SCRIPT = """
const CORNERS = ['tt','ss','ff'], TEMPS = ['−40','25','125'];
const PORTS = ['VDD0P8_A','VDD0P8_B','IB_PTAT','IB_CONST'];
const GRADE = {};  // default OK; exceptions:
GRADE['VDD0P8_B|ss|125'] = 'nr'; GRADE['VDD0P8_B|ss|25'] = 'warn'; GRADE['VDD0P8_B|ff|−40'] = 'warn'; GRADE['IB_CONST|ff|−40'] = 'warn'; GRADE['VDD0P8_A|ss|125'] = 'warn';
const CLS = { ok:'c-ok', warn:'c-warn', bad:'c-bad', nr:'c-mute' }, LBL = { ok:'OK', warn:'MARG', bad:'FAIL', nr:'N/R' }, BADGE = { ok:'b-ok', warn:'b-warn', bad:'b-bad', nr:'b-mute' };
function blocksFor(port, g){
  const rail = port.startsWith('VDD');
  if (rail) return [
    {name:'dc', metric:'vout error · dropout', val:'0.8 mV · 2 %', lim:'5 mV · 10 %', grade:'ok'},
    {name:'zout', metric:'|Z| RMS · peak freq', val: g==='warn' ? '1.9 dB · 12 %' : '0.31 dB · 3 %', lim:'2 dB · 15 %', grade: g==='warn' ? 'warn' : 'ok'},
    {name:'psrr', metric:'|H| RMS · phase', val:'0.9 dB · 4°', lim:'2 dB · 15°', grade:'ok'},
    {name:'noise', metric:'PSD log-RMS', val:'0.4 dB', lim:'1.5 dB', grade:'ok'},
    {name:'load_en', metric:'dip · overshoot', val: g==='nr' ? '3.8 % · not run' : '3.8 % · 6.1 %', lim:'10 % · 10 %', grade: g==='nr' ? 'nr' : 'ok'},
  ];
  return [
    {name:'idc', metric:'I error · PTAT slope', val:'0.6 % · 1.1 %', lim:'2 % · 5 %', grade:'ok'},
    {name:'yout', metric:'gds · Cout', val:'3 % · 5 %', lim:'10 % · 20 %', grade:'ok'},
    {name:'noise', metric:'PSD log-RMS', val: g==='warn' ? '1.3 dB' : '0.5 dB', lim:'1.5 dB', grade: g==='warn' ? 'warn' : 'ok'},
    {name:'psrr', metric:'|H| RMS', val:'1.0 dB', lim:'2 dB', grade:'ok'},
  ];
}
// |Zout| model vs GT: 2nd-order peak + cap roll-off, log axes
function zGT(f){ const w = f/1.78e6; const peak = 388/Math.sqrt(Math.pow(1-w*w,2)+Math.pow(w/2.6,2)); const lf = 23/Math.sqrt(1+Math.pow(f/4e4,2)); const hf = 1/(2*Math.PI*f*1e-9) + 0.4; return Math.min(peak+lf, 1e4) + (f>3e7 ? 0 : 0) + (f>1e8 ? hf : 0); }
function zModel(f){ const w = f/1.74e6; const peak = 380/Math.sqrt(Math.pow(1-w*w,2)+Math.pow(w/2.5,2)); const lf = 23.4/Math.sqrt(1+Math.pow(f/4.2e4,2)); const hf = 1/(2*Math.PI*f*1e-9) + 0.4; return Math.min(peak+lf, 1e4) + (f>1e8 ? hf : 0); }
const FMIN = 1e1, FMAX = 1e10, ZMIN = 0.1, ZMAX = 1e3;
const X0 = 48, X1 = 512, Y0 = 14, Y1 = 216;
const xOf = f => X0 + (X1-X0) * (Math.log10(f)-1)/9;
const yOf = z => Y1 - (Y1-Y0) * (Math.log10(Math.max(z, ZMIN))-Math.log10(ZMIN))/4;
const FS = []; for (let i = 0; i <= 180; i++) FS.push(Math.pow(10, 1 + i/20));
const pathOf = fn => FS.map((f,i) => (i?'L':'M') + xOf(f).toFixed(1) + ' ' + yOf(fn(f)).toFixed(1)).join(' ');
const fmtF = f => f >= 1e9 ? (f/1e9).toFixed(f>=1e10?0:1)+' GHz' : f >= 1e6 ? (f/1e6).toFixed(0)+' MHz' : f >= 1e3 ? (f/1e3).toFixed(0)+' kHz' : f.toFixed(0)+' Hz';
class Component extends DCLogic {
  constructor(p){ super(p); this.state = { port:'VDD0P8_B', corner:'ss', temp:'25', block:'zout', view:'chart', hov:null }; }
  renderVals(){
    const s = this.state;
    const heads = []; CORNERS.forEach(c => TEMPS.forEach(t => heads.push(c + ' ' + t)));
    const rows = PORTS.map(p => ({ name:p, cells: CORNERS.flatMap(c => TEMPS.map(t => { const g = GRADE[p+'|'+c+'|'+t] || 'ok'; const sel = s.port===p && s.corner===c && s.temp===t;
      return { cls: CLS[g] + (sel ? ' sel' : ''), label: LBL[g], pick: () => this.setState({port:p, corner:c, temp:t, hov:null}) }; })) }));
    const g = GRADE[s.port+'|'+s.corner+'|'+s.temp] || 'ok';
    const blocks = blocksFor(s.port, g).map(b => ({ ...b, gcls: BADGE[b.grade], grade: LBL[b.grade], cls: b.name === s.block ? 'sel' : '', pick: () => this.setState({block:b.name}) }));
    const hov = s.hov ? { on:true, x: s.hov.x, yg: yOf(zGT(s.hov.f)), ym: yOf(zModel(s.hov.f)), tx: Math.min(s.hov.x + 10, 360), text: fmtF(s.hov.f) + '  GT ' + zGT(s.hov.f).toFixed(1) + ' Ω  model ' + zModel(s.hov.f).toFixed(1) + ' Ω' } : { on:false };
    const gridV = [1e1,1e2,1e3,1e4,1e5,1e6,1e7,1e8,1e9,1e10].map(f => ({ x: xOf(f).toFixed(1), t: f>=1e9 ? (f/1e9)+'G' : f>=1e6 ? (f/1e6)+'M' : f>=1e3 ? (f/1e3)+'k' : f }));
    const gridH = [0.1,1,10,100,1000].map(z => ({ y: yOf(z).toFixed(1), ty: (yOf(z)+3.5).toFixed(1), t: z>=1 ? z : z }));
    const tableRows = [1e2,1e4,1e5,1e6,1.78e6,1e7,1e8,1e9,1e10].map(f => { const gv = zGT(f), mv = zModel(f); return { f: fmtF(f), g: gv.toFixed(2), m: mv.toFixed(2), d: (20*Math.log10(mv/gv)).toFixed(2) }; });
    return { heads, rows, blocks, selPort: s.port, selCell: s.corner + ' / ' + s.temp + ' °C / VSET 3', selGrade: LBL[g], selCls: BADGE[g],
      chartTitle: s.block === 'zout' ? '|Zout| · load ' + (s.port === 'VDD0P8_A' ? '500 µA' : '2 mA') : s.block + ' · model vs ground truth',
      isChart: s.view === 'chart', isTable: s.view === 'table', viewChartCls: s.view === 'chart' ? 'on' : '', viewTableCls: s.view === 'table' ? 'on' : '',
      showChart: () => this.setState({view:'chart'}), showTable: () => this.setState({view:'table'}),
      pathGT: pathOf(zGT), pathModel: pathOf(zModel), lblGT: { x: xOf(3e3).toFixed(0), y: (yOf(zGT(3e3)) - 8).toFixed(0) }, lblM: { x: xOf(2e8).toFixed(0), y: (yOf(zModel(2e8)) + 14).toFixed(0) },
      hov, gridV, gridH, tableRows,
      hover: (e) => { const svg = e.currentTarget, r = svg.getBoundingClientRect(); const x = Math.max(X0, Math.min(X1, e.clientX - r.left)); const f = Math.pow(10, 1 + 9*(x-X0)/(X1-X0)); this.setState({hov:{x: x.toFixed(1), f}}); },
      unhover: () => this.setState({hov:null}) };
  }
}
"""

# ------------------------------------------------------------------ 5 Deliver
FILES = [
    dict(name="PMU_demo_pmu.scs", kind="Spectre library", size="1.2 KB", sha="4e0a…91", desc="library with sections tt / ss / ff, each including its .va"),
    dict(name="PMU_demo_pmu_tt.va", kind="Verilog-A", size="38 KB", sha="c17b…2d", desc="tt corner · temperature continuous · vset, load-EN switches as instance params"),
    dict(name="PMU_demo_pmu_ss.va", kind="Verilog-A", size="38 KB", sha="9f42…7a", desc="ss corner"),
    dict(name="PMU_demo_pmu_ff.va", kind="Verilog-A", size="38 KB", sha="1b8d…e5", desc="ff corner"),
    dict(name="envelope.json", kind="validity", size="0.9 KB", sha="77c0…03", desc="load / temp / freq / corner / VSET ranges · which large-signal terms are on"),
    dict(name="report.md", kind="report", size="14 KB", sha="a3e6…b8", desc="trust summary · per-cell grades · HB health check · not-run list"),
    dict(name="provenance.json", kind="provenance", size="0.6 KB", sha="e90d…4f", desc="config sha · dataset sha · pmukit version · TB state at characterization"),
]

DELIVER_BODY = f"""
<div class="main">
  <div class="col" style="flex:0 0 620px">
    <div class="panel" style="flex:1">
      <div class="ph"><span>Deliverable</span><span class="sub mono">~/pmukit_data/demo_pmu/deliver/2026-09-15T14-02/</span></div>
      <div class="pb" style="padding:0">
        <table class="t">
          <thead><tr><th></th><th>File</th><th>What</th><th class="num" style="text-align:right">Size</th><th>sha</th></tr></thead>
          <tbody><sc-for list="{{{{ files }}}}" as="f" hint-placeholder-count="7"><tr class="click {{{{ f.cls }}}}" onClick="{{{{ f.pick }}}}"><td style="color:#8a867d;width:20px">{ICONS['file']}</td><td class="mono">{{{{ f.name }}}}</td><td><div>{{{{ f.kind }}}}</div><div class="hint">{{{{ f.desc }}}}</div></td><td class="num">{{{{ f.size }}}}</td><td class="id" style="color:#8a867d">{{{{ f.sha }}}}</td></tr></sc-for></tbody>
        </table>
      </div>
    </div>
    <div class="panel" style="flex:none">
      <div class="ph"><span>Use it in your testbench</span><button class="btn sm" onClick="{{{{ copy }}}}">{ICONS['copy']} {{{{ copyLabel }}}}</button></div>
      <div class="pb"><div class="code">// model setup — pick the section with the same corner variable as the PDK
include "~/pmukit_data/demo_pmu/deliver/2026-09-15T14-02/PMU_demo_pmu.scs" section=tt

// instance — same pins as PMU_DEMO, plus per-rail switches
PMU_TOP (AVDD1P0 VDD0P8_A VDD0P8_B IB_PTAT IB_CONST EN VSS_A VSS_B AGND) PMU_demo_pmu \\
    vset=3  load_en_A=1  load_en_B=0</div>
        <div class="hint" style="margin-top:8px">Temperature comes from your <span class="mono">options temp=</span>. Anything outside envelope.json is reported, never silently extrapolated.</div>
      </div>
    </div>
  </div>
  <div class="col" style="flex:1">
    <div class="panel" style="flex:1">
      <div class="ph"><span class="mono">{{{{ sel.name }}}}</span><span class="sub">{{{{ sel.kind }}}}</span></div>
      <div class="pb"><div class="code" style="height:100%;box-sizing:border-box">{{{{ sel.body }}}}</div></div>
    </div>
    <div class="panel" style="flex:none">
      <div class="ph"><span>Provenance</span><span class="badge b-ok">{ICONS['check']} reproducible</span></div>
      <div class="pb"><div class="kv"><span class="k">Config</span><span class="mono">demo_pmu.json · sha 5d8c…a1</span><span class="k">Dataset</span><span class="mono">a91f…c3 · 271 runs consumed · 2026-09-15</span><span class="k">pmukit</span><span class="mono">0.1.0 · emitter hb_safe</span><span class="k">TB state</span><span>RX mode, register 0x12 = 0x03 (as characterized)</span><span class="k">Header</span><span class="hint">every .va repeats this block in its first 12 lines</span></div></div>
    </div>
  </div>
</div>
<div class="foot"><span class="hint">Nothing in this folder enters git. report.md carries numbers only.</span><div style="display:flex;gap:8px"><button class="btn">Copy folder path</button><button class="btn pri">Export report PDF</button></div></div>
"""

DELIVER_SCRIPT = "const FILES = " + json.dumps(FILES, ensure_ascii=False) + """;
const BODIES = {
  'PMU_demo_pmu.scs': `// pmukit 0.1.0 · demo_pmu · 2026-09-15T14:02 · config 5d8c…a1 · dataset a91f…c3
library PMU_demo_pmu
  section tt
    ahdl_include "PMU_demo_pmu_tt.va"
  endsection tt
  section ss
    ahdl_include "PMU_demo_pmu_ss.va"
  endsection ss
  section ff
    ahdl_include "PMU_demo_pmu_ff.va"
  endsection ff
endlibrary PMU_demo_pmu`,
  'PMU_demo_pmu_tt.va': `// pmukit 0.1.0 · demo_pmu · corner tt · 2026-09-15T14:02
// config 5d8c…a1 · dataset a91f…c3 · TB state: RX mode, reg 0x12=0x03
// valid: load_A 2u..1m  load_B 20u..4m  temp -40..125  f<=20G  vset 3
// large-signal: load_en_A ON (HB check 7.7e-3)  load_en_B OFF (default)  en_ramp usable-only
\\`include "disciplines.vams"
module PMU_demo_pmu(AVDD1P0, VDD0P8_A, VDD0P8_B, IB_PTAT, IB_CONST, EN, VSS_A, VSS_B, AGND);
  inout AVDD1P0, VDD0P8_A, VDD0P8_B, IB_PTAT, IB_CONST, EN, VSS_A, VSS_B, AGND;
  electrical AVDD1P0, VDD0P8_A, VDD0P8_B, IB_PTAT, IB_CONST, EN, VSS_A, VSS_B, AGND;
  parameter integer vset = 3;
  parameter integer load_en_A = 1, load_en_B = 0;
  // ---- rail A: dc table(T) · zout ladder · psrr gm-C biquad · noise · load_en (opt-in)
  ...`,
  'envelope.json': `{
  "load_a": {"VDD0P8_A": [2e-6, 1e-3], "VDD0P8_B": [2e-5, 4e-3]},
  "temp_c": [-40, 125],
  "freq_hz_max": 2e10,
  "corners": ["tt", "ss", "ff"],
  "vset": [3],
  "large_signal_default_on": {"load_en_A": true, "load_en_B": false},
  "usable_not_signoff": ["en_ramp"],
  "not_run": [["tran_load_off.B", "ss", 125, 3]]
}`,
  'report.md': `# demo_pmu — can I trust this model in my simulation?

Valid for: load_A 2 µ–1 mA · load_B 20 µ–4 mA · −40–125 °C · ≤ 20 GHz · tt/ss/ff · VSET 3
Usable, not sign-off: EN power-up ramp
Not run: tran_load_off.B @ ss / 125 °C (failed twice)
HB health: first-step residual 7.7e-3, all large-signal terms individually checked

| port | tt −40 | tt 25 | tt 125 | ss −40 | ss 25 | ss 125 | ff −40 | ff 25 | ff 125 |
| VDD0P8_A | OK | OK | OK | OK | OK | MARG | OK | OK | OK |
...`,
  'provenance.json': `{
  "pmukit": "0.1.0",
  "config_sha": "5d8c…a1",
  "dataset_sha": "a91f…c3",
  "runs_consumed": 271,
  "tb_state": "RX mode, register 0x12 = 0x03",
  "created": "2026-09-15T14:02:11"
}`,
};
class Component extends DCLogic {
  constructor(p){ super(p); this.state = { sel: 'PMU_demo_pmu.scs', copied: false }; }
  renderVals(){
    const s = this.state;
    const files = FILES.map(f => ({ ...f, cls: f.name === s.sel ? 'sel' : '', pick: () => this.setState({sel: f.name}) }));
    const f = FILES.find(x => x.name === s.sel);
    return { files, sel: { ...f, body: BODIES[f.name] || BODIES['PMU_demo_pmu_tt.va'] }, copyLabel: s.copied ? 'Copied' : 'Copy',
      copy: () => { this.setState({copied:true}); setTimeout(() => this.setState({copied:false}), 1500); } };
  }
}
"""


def main():
    (OUT / "Main.dc.html").write_text(page("New", 1, NEW_BODY, NEW_SCRIPT), encoding="utf-8")
    (OUT / "Plan.dc.html").write_text(page("Plan", 2, PLAN_BODY, PLAN_SCRIPT), encoding="utf-8")
    (OUT / "Run.dc.html").write_text(page("Run", 3, RUN_BODY, RUN_SCRIPT), encoding="utf-8")
    (OUT / "Model.dc.html").write_text(page("Model", 4, MODEL_BODY, MODEL_SCRIPT), encoding="utf-8")
    (OUT / "Deliver.dc.html").write_text(page("Deliver", 5, DELIVER_BODY, DELIVER_SCRIPT), encoding="utf-8")
    W, H, GX, GY = 1440, 900, 100, 170
    canvas = {
        "artboards": [
            {"file": "Main.dc.html", "title": "1 · New project", "x": 0, "y": 0, "w": W, "h": H, "is_interactive": True},
            {"file": "Plan.dc.html", "title": "2 · Plan", "x": W + GX, "y": 0, "w": W, "h": H, "is_interactive": True},
            {"file": "Run.dc.html", "title": "3 · Run", "x": 2 * (W + GX), "y": 0, "w": W, "h": H, "is_interactive": True},
            {"file": "Model.dc.html", "title": "4 · Model", "x": 0, "y": H + GY, "w": W, "h": H, "is_interactive": True},
            {"file": "Deliver.dc.html", "title": "5 · Deliver", "x": W + GX, "y": H + GY, "w": W, "h": H, "is_interactive": True},
        ],
        "annotations": [
            {"id": "journey", "x": 2 * (W + GX), "y": H + GY, "w": 520,
             "text": "pmukit — the sub-block designer's journey.\nThe PMU is a black box to them. They bring one netlist built to the naming convention and answer three questions; everything else is derived and shown, never asked.\n\nAll data is synthetic (PMU_DEMO). Each screen is clickable on its own: load the sample netlist, untick a plan group, retry a failed run, click a grade cell, pick a file."},
        ],
        "launch": {"view": "canvas"},
    }
    (OUT / "canvas.json").write_text(json.dumps(canvas, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote 5 artboards + canvas.json")


if __name__ == "__main__":
    main()
