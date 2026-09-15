"""The command line. Everything the web shell can do, the shell can do too.

UX_RULES: each screen shows a live `$ pmukit ...` echo of what the user just did, and that command
has to actually work -- it is the entry point for scripting and for an overnight batch, and it is
the honest answer to "what did I just do?".  So the verbs here mirror the screens one for one:

    new / pins / config   the New screen
    plan                  the Plan screen
    run / status          the Run screen
    fit / verify / report the Model screen
    deliver               the Deliver screen
    digest / reproduce    Copy for desk
    list / open / ui      Home
    help <screen>         the same text the `? Help` panel shows

Modules that arrive later in the build are imported LAZILY inside each command, so a partially
built install still runs every command that is ready and gives a four-part error -- not an
ImportError traceback -- for the ones that are not.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

from . import helptext, jsonio, paths
from .errors import PmuError

PROG = "pmukit"


# ------------------------------------------------------------------------------ plumbing
def _need(modname: str, what: str):
    """Import a pmukit submodule, or explain in four parts why the command cannot run yet."""
    import importlib
    try:
        return importlib.import_module(f"pmukit.{modname}")
    except ImportError as exc:
        raise PmuError(
            what=f"`{PROG} {what}` needs pmukit.{modname}, which is not installed.",
            why=f"The module failed to import: {exc}",
            do=[f"Re-install pmukit (bash apply) so pmukit/{modname.replace('.', '/')}.py is present.",
                f"Until then the other commands still work -- try `{PROG} status <project>`."],
            where=f"pmukit.{modname}") from exc


def _project_dir(project: str) -> pathlib.Path:
    d = paths.project_dir(project)
    if not (d / "config.json").exists():
        known = [p.name for p in paths.data_root().iterdir()
                 if (p / "config.json").exists()] if paths.data_root().exists() else []
        raise PmuError(
            what=f"no project named '{project}'.",
            why=f"A project is a directory under {paths.data_root()} holding config.json; "
                "there is none with that name.",
            do=[f"Projects here: {', '.join(known) or '(none yet)'}.",
                f"Create one: {PROG} new {project} --netlist <file> --pmu-inst <name>"],
            where=str(d))
    return d


def _load(project: str):
    """(ProjectConfig, DerivedConfig|None, project_dir) for an existing project."""
    cfgmod = _need("config", "config")
    d = _project_dir(project)
    cfg = cfgmod.ProjectConfig.load(d / "config.json")
    der = cfgmod.DerivedConfig.load(d / "derived.json") if (d / "derived.json").exists() else None
    return cfg, der, d


def _netlist_of(cfg, d: pathlib.Path):
    nlmod = _need("netlist", "pins")
    p = pathlib.Path(cfg.netlist)
    if not p.is_absolute():
        for base in (d, pathlib.Path.cwd()):
            if (base / p).exists():
                p = base / p
                break
    return nlmod.Netlist.from_file(p)


def _plan_of(cfg, der, d: pathlib.Path, *, site=None):
    planmod = _need("plan", "plan")
    nl = _netlist_of(cfg, d)
    pins = nl.scan(cfg.pmu_inst, ports=cfg.ports)
    return planmod.compile_plan(cfg, der, nl, pins, site=site), nl, pins


def _site():
    return _need("site", "run").SiteConfig.load()


def _num_list(text: str, cast=float):
    return [cast(x) for x in str(text).replace(",", " ").split()]


def _out(obj, as_json: bool, text: str = "") -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, default=str))
    else:
        print(text if text else obj)


def _table(rows: list[list], headers: list[str]) -> str:
    if not rows:
        return "(nothing)"
    cols = [[str(h)] + [str(r[i]) for r in rows] for i, h in enumerate(headers)]
    w = [max(len(c) for c in col) for col in cols]
    line = "  ".join(h.ljust(w[i]) for i, h in enumerate(headers))
    out = [line, "  ".join("-" * x for x in w)]
    for r in rows:
        out.append("  ".join(str(r[i]).ljust(w[i]) for i in range(len(headers))))
    return "\n".join(out)


# ------------------------------------------------------------------------------- commands
def cmd_list(a) -> int:
    root = paths.data_root()
    rows = []
    if root.exists():
        for p in sorted(root.iterdir()):
            cfgp = p / "config.json"
            if not cfgp.exists():
                continue
            try:
                cfg = jsonio.read(cfgp)
            except Exception:
                continue
            step = "new"
            if (p / "derived.json").exists():
                step = "planned"
            if (p / "runs.sqlite").exists():
                step = "run"
            if (p / "dataset" / "index.json").exists():
                step = "characterized"
            if (p / "deliver").exists() and any((p / "deliver").iterdir()):
                step = "delivered"
            rows.append([cfg.get("project", p.name), step,
                         ",".join(map(str, cfg.get("corners", []) if isinstance(cfg.get("corners"), list)
                                      else list(cfg.get("corners", {})))),
                         cfg.get("pmu_inst", ""), str(p)])
    _out([dict(zip(["project", "step", "corners", "pmu_inst", "path"], r)) for r in rows], a.json,
         _table(rows, ["project", "step", "corners", "instance", "path"]))
    if not rows and not a.json:
        print(f"\nNo projects under {root}.  Start one:\n"
              f"  {PROG} new demo_pmu --netlist tb/input.scs --pmu-inst PMU_TOP")
    return 0


def cmd_new(a) -> int:
    cfgmod = _need("config", "new")
    nlmod = _need("netlist", "new")

    nl = nlmod.Netlist.from_file(a.netlist)
    table = nl.scan(a.pmu_inst)

    ports: dict[str, str] = {}
    for pin in sorted(table.pins.values(), key=lambda p: p.index):
        ports[pin.name] = "ignore" if (pin.is_ground or pin.role == "none") else "model"
    for spec_ in (a.port or []):
        pin, _, fate = spec_.partition("=")
        ports[pin] = fate or "model"

    my_load: dict[str, dict] = {}
    for spec_ in (a.load or []):
        rail, _, vals = spec_.partition("=")
        nums = _num_list(vals)
        if len(nums) < 2:
            raise PmuError(what=f"--load {spec_}: need at least on,off currents.",
                           why="The load-EN event is characterized between your module's ON and "
                               "OFF current; without both there is nothing to step between.",
                           do=[f"Write it as --load {rail}=<on_a>,<off_a>[,<edge_s>]",
                               "Omit --load for that rail and it is reported NOT RUN instead."],
                           where="command line")
        entry = {"on_a": nums[0], "off_a": nums[1], "switches": True}
        if len(nums) > 2:
            entry["edge_s"] = nums[2]
        my_load[rail] = entry

    cfg = cfgmod.ProjectConfig.from_dict({
        "project": a.project,
        "netlist": str(pathlib.Path(a.netlist).resolve()),
        "pmu_inst": a.pmu_inst,
        "corners": [c.strip() for c in a.corners.split(",") if c.strip()],
        "temps_c": _num_list(a.temps),
        "vset_codes": _num_list(a.vset, int),
        "state_note": a.note or "",
        "ports": ports,
        "my_load": my_load,
        "care_up_to_hz": float(a.care_up_to),
    })
    d = paths.ensure_project(a.project)
    cfg.save(d / "config.json")
    cfgmod.ConfigHistory(d / "config_history.json").push(cfg, "created")

    table.apply_fates(cfg.ports)
    der = cfgmod.derive(cfg, table, _site())
    der.save(d / "derived.json")

    unclassified = [p.name for p in table.unclassified()]
    payload = {"project": a.project, "path": str(d), "config_sha": cfg.sha(),
               "pins": table.to_dict(), "unclassified": unclassified, "notes": table.notes}
    if a.json:
        _out(payload, True)
        return 0
    print(f"Created {a.project} in {d}   config {cfg.sha()}")
    print()
    print(_pin_table_text(table))
    for n in table.notes:
        print(f"  note: {n}")
    if unclassified:
        print(f"\n  {len(unclassified)} pin(s) have no role: {', '.join(unclassified)}")
        print(f"  They are set to `ignore`. To characterize one, give it a convention source in "
              f"the testbench\n  (IL_ rail / VB_ bias / VS_ supply / VEN_ enable) and re-run "
              f"`{PROG} new`.")
    print(f"\nNext:  {PROG} plan {a.project}")
    return 0


def _pin_table_text(table) -> str:
    rows = []
    for p in sorted(table.pins.values(), key=lambda x: x.index):
        rows.append([p.name, p.net, p.role, p.fate, p.src or "-",
                     "-" if p.dc is None else f"{p.dc:g}", p.gnd or "-",
                     p.reason or p.gnd_from or ""])
    return _table(rows, ["pin", "net", "role", "fate", "source", "dc", "gnd", "note"])


def cmd_pins(a) -> int:
    cfg, _der, d = _load(a.project)
    nl = _netlist_of(cfg, d)
    table = nl.scan(cfg.pmu_inst, ports=cfg.ports)
    _out(table.to_dict(), a.json, _pin_table_text(table))
    return 0


def cmd_check(a) -> int:
    """Read a netlist and say whether it satisfies the convention -- before any project exists.

    This is the first thing to run after exporting a testbench: it prints the pin table pmukit
    would read, names every pin it cannot classify and why, and says what is missing for corners
    and codes. It creates nothing and changes nothing.
    """
    nlmod = _need("netlist", "check")
    nl = nlmod.Netlist.from_file(a.netlist)
    table = nl.scan(a.pmu_inst)
    problems: list[str] = []
    if not any(s for _f, s in nl.includes()):
        problems.append("no `include ... section=` line -- pmukit cannot generate process corners; "
                        "add section=<nominal> to the PDK include")
    if "VSET" not in nl.parameters():
        problems.append("no `parameters VSET=<n>` -- pmukit cannot switch output codes; add it if "
                        "your PMU has one (harmless to omit if it does not)")
    if not table.of_role("supply"):
        problems.append("no VS_* supply source -- PSRR cannot be characterized and the bias I-V "
                        "sweep has no upper limit")
    if not table.of_role("rail") and not table.of_role("bias"):
        problems.append("no IL_* rail and no VB_* bias source -- there is nothing to model")
    payload = {"pins": table.to_dict(), "unclassified": [p.name for p in table.unclassified()],
               "notes": table.notes, "problems": problems,
               "sections": dict(nl.includes()), "parameters": nl.parameters(),
               "analyses_to_strip": table.analyses}
    if a.json:
        _out(payload, True)
        return 0 if not problems else 1
    print(_pin_table_text(table))
    for n in table.notes:
        print(f"\n  note: {n}")
    unclassified = payload["unclassified"]
    if unclassified:
        print(f"\n  no role: {', '.join(unclassified)}")
        print("  That is fine for test and configuration pins -- mark them `ignore`.")
        print("  A pin you want modelled needs a convention source:")
        print("    IL_<pin>  isource   a voltage rail  (dc = your typical load)")
        print("    VB_<pin>  vsource   a current bias  (dc = the pin's operating voltage)")
        print("    VS_<pin>  vsource   a supply        (dc = nominal)")
        print("    VEN_<pin> vsource   the enable")
    if table.analyses:
        print(f"\n  {len(table.analyses)} analysis statement(s) will be stripped; "
              "pmukit writes its own.")
    if problems:
        print()
        for pr in problems:
            print(f"  PROBLEM: {pr}")
        print("\nSee docs/TESTBENCH.md.")
        return 1
    print("\nConvention OK.  Next:")
    print(f"  {PROG} new <project> --netlist {a.netlist} --pmu-inst {a.pmu_inst} \\")
    print("      --corners tt,ss,ff --temps -40,25,125 --load <rail>=<on_a>,<off_a>")
    return 0


def cmd_config(a) -> int:
    cfgmod = _need("config", "config")
    cfg, der, d = _load(a.project)
    hist = cfgmod.ConfigHistory(d / "config_history.json")
    if a.undo:
        cfg = hist.undo()
        cfg.save(d / "config.json")
        nl = _netlist_of(cfg, d)
        table = nl.scan(cfg.pmu_inst, ports=cfg.ports)
        cfgmod.derive(cfg, table, _site()).save(d / "derived.json")
        print(f"Undone. config is now {cfg.sha()}")
        return 0
    if a.set:
        raw = cfg.to_dict()
        for item in a.set:
            key, _, val = item.partition("=")
            try:
                raw[key] = json.loads(val)
            except json.JSONDecodeError:
                raw[key] = val
        cfg = cfgmod.ProjectConfig.from_dict(raw)
        hist.push(cfg, " ".join(a.set))
        cfg.save(d / "config.json")
        nl = _netlist_of(cfg, d)
        table = nl.scan(cfg.pmu_inst, ports=cfg.ports)
        der = cfgmod.derive(cfg, table, _site())
        der.save(d / "derived.json")
        print(f"config is now {cfg.sha()}   (Ctrl-Z equivalent: {PROG} config {a.project} --undo)")
        return 0
    if a.derived:
        _out(der.to_dict() if der else {}, True)
        return 0
    _out(cfg.to_dict(), True)
    return 0


def cmd_plan(a) -> int:
    cfg, der, d = _load(a.project)
    lmod = _need("ledger", "plan")
    plan, _nl, _pins = _plan_of(cfg, der, d, site=_site())
    for gid in (a.off or []):
        plan.set_enabled(gid, False)
    if a.recipe:
        for r in plan.runs(enabled_only=False):
            if r.run_id.startswith(a.recipe):
                print(r.run.recipe)
                return 0
        raise PmuError(what=f"no planned run starting with '{a.recipe}'.",
                       why="Run ids are the first 12 hex of the content hash.",
                       do=[f"List them with `{PROG} plan {a.project} --runs <group>`."],
                       where="plan")
    if a.runs:
        g = plan.group(a.runs)
        rows = [[r.run_id, r.run.process, r.run.cell_text(), r.run.stimulus,
                 ",".join(r.run.reads)] for r in g.runs]
        _out([dict(zip(["run_id", "process", "cell", "stimulus", "reads"], r)) for r in rows],
             a.json, _table(rows, ["run_id", "process", "cell", "stimulus", "reads"]))
        return 0
    rows = [[g.id, g.n_runs, f"{g.cost_s:.0f}", "on" if g.enabled else "OFF", g.title]
            for g in plan.groups]
    summary = plan.cost_summary()
    if a.json:
        _out({"groups": plan.to_rows(), "cost": summary,
              "consequences": plan.consequences()}, True)
        return 0
    print(_table(rows, ["group", "runs", "cpu_s", "", "what it measures"]))
    print(f"\n{summary['runs']} runs, {summary['cpu_hours']:.2f} CPU-hours estimated "
          f"({len(plan.states)} load states, {len(plan.groups)} groups)")
    for n in plan.notes:
        print(f"  note: {n}")
    for c in plan.consequences():
        print(f"  NOT RUN: {c['port']} {c['block']} -- {', '.join(c['params'])}")
    if a.submit:
        led = lmod.Ledger.for_project(a.project)
        counts = plan.commit(led)
        led.close()
        print(f"\nsubmitted to the ledger: {counts}")
        print(f"Next:  {PROG} run {a.project}")
    else:
        print(f"\nNext:  {PROG} plan {a.project} --submit   then   {PROG} run {a.project}")
    return 0


def _aux_for(cfg, d: pathlib.Path) -> list[pathlib.Path]:
    """Everything a run directory needs besides input.scs -- the netlist's relative includes.

    A testbench normally includes its PDK by a RELATIVE path (`include "pdk/toplevel.scs"`). The
    run directory is somewhere else (a scratch dir on the VM, a netlist dir on the queue), so
    unless those files travel with it the simulator answers
    `ERROR (SFE-868): Can not open input file 'pdk/toplevel.scs'`. Absolute includes are left
    alone -- they resolve on the far side or they do not, and copying a whole PDK would be worse.
    """
    nl = _netlist_of(cfg, d)
    base = pathlib.Path(nl.path).resolve().parent if nl.path else pathlib.Path.cwd()
    out: list[pathlib.Path] = []
    for file_path, _section in nl.includes():
        p = pathlib.Path(file_path)
        if p.is_absolute():
            continue
        top = base / p.parts[0]              # copy the whole `pdk/` tree, not one file of it
        if top.exists() and top not in out:
            out.append(top)
    return out


def cmd_run(a) -> int:
    cfg, der, d = _load(a.project)
    lmod = _need("ledger", "run")
    rmod = _need("runner", "run")
    dsmod = _need("dataset", "run")
    site = _site()
    if a.engine:
        site.engine = a.engine
    plan, _nl, _pins = _plan_of(cfg, der, d, site=site)
    for gid in (a.off or []):
        plan.set_enabled(gid, False)
    led = lmod.Ledger.for_project(a.project)
    plan.commit(led)
    dpath = d / "dataset"
    ds = (dsmod.Dataset.open(dpath) if (dpath / "index.json").exists()
          else dsmod.Dataset.create(dpath, project=cfg.project, config_sha=cfg.sha(),
                                    dims=_dims_for(der, plan)))
    runner = rmod.Runner(cfg.project, plan, led, site, dataset=ds, root=d,
                         jobs=a.jobs, aux=_aux_for(cfg, d))
    result = runner.run_all(resume=not a.no_resume,
                            on_event=None if a.json else _progress)
    ds.close()
    led.close()
    _out(result, a.json, json.dumps(result, indent=2, default=str))
    return 0


def _progress(*args) -> None:
    """Render a progress event from either producer.

    The runner emits `(kind, run_id, detail)`; the fitter emits a single dict per fitted block.
    The two grew up in different modules and have not been unified -- this renders both rather
    than pretending they are the same shape.
    """
    if len(args) == 3:
        kind, run_id, detail = args
        print(f"  [{kind:<8}] {run_id}  {detail}", flush=True)
        return
    ev = args[0] if args else {}
    if isinstance(ev, dict):
        port = ev.get("port", "?")
        block = ev.get("block", "?")
        bits = []
        if ev.get("missing"):
            bits.append("NOT RUN")
        elif ev.get("score") is not None:
            bits.append(f"{ev['score']:.3g} {ev.get('metric', '')}".strip())
        for key in ("corner", "process", "temp_c", "vset", "load"):
            if ev.get(key) is not None:
                bits.append(f"{key}={ev[key]}")
        print(f"  [fit     ] {port}.{block:<10} {'  '.join(bits)}", flush=True)
    else:
        print(f"  [progress] {ev}", flush=True)


def _dims_for(der, plan) -> dict:
    """The dataset axes implied by the derived config and the plan's load states."""
    return {
        "process": list((der.process or {}).get("corners", [])),
        "temp_c": list((der.temps_c or {}).get("points", [])),
        "vset": list((der.vset or {}).get("codes", [])),
        "load_a": {rail: list((der.loads.get(rail) or {}).get("points_a", []))
                   for rail in der.rails},
    }


def cmd_status(a) -> int:
    lmod = _need("ledger", "status")
    _project_dir(a.project)
    led = lmod.Ledger.for_project(a.project)
    counts = led.counts_by_status()
    runs = led.all(status=a.status, analysis=a.analysis, limit=a.limit)
    cost = led.cost_by_analysis()
    if a.why:
        print(led.why(a.why))
        led.close()
        return 0
    rows = [[r.run_id, r.status, r.analysis, r.process, r.cell_text(), f"{r.cpu_seconds:.0f}",
             (r.error or "")[:60]] for r in runs]
    if a.json:
        _out({"counts": counts, "cost": cost, "runs": led.to_rows(runs)}, True)
        led.close()
        return 0
    print("  ".join(f"{k}={v}" for k, v in counts.items()))
    print()
    print(_table(rows, ["run_id", "status", "analysis", "corner", "cell", "cpu_s", "error"]))
    print(f"\n{led.total_cpu_hours():.2f} CPU-hours spent")
    for r in led.not_run()[:1]:
        print(f"still to run: {len(led.not_run())} (e.g. {r.run_id} {r.analysis})")
    led.close()
    return 0


def cmd_fit(a) -> int:
    cfg, der, d = _load(a.project)
    fitmod = _need("fit", "fit")
    dsmod = _need("dataset", "fit")
    ds = dsmod.Dataset.open(d / "dataset")
    result = fitmod.fit_project(ds, der, on_event=None if a.json else _progress)
    jsonio.write(d / "fit.json", result.to_dict())
    ds.close()
    _out(result.to_dict(), a.json, f"fitted -> {d / 'fit.json'}\nNext:  {PROG} verify {a.project}")
    return 0


def cmd_verify(a) -> int:
    cfg, der, d = _load(a.project)
    vmod = _need("verify", "verify")
    out = vmod.verify_project(a.project, root=d)
    jsonio.write(d / "verify.json", out)
    _out(out, a.json, json.dumps(out, indent=2, default=str))
    return 0


def cmd_report(a) -> int:
    cfg, _der, d = _load(a.project)
    dmod = _need("deliverable", "report")
    dl = dmod.Deliverable.latest(a.project)
    if dl is None:
        raise PmuError(what=f"{a.project} has no deliverable yet.",
                       why="`report` prints the report.md of the most recent delivery.",
                       do=[f"Run `{PROG} deliver {a.project}` first."],
                       where=str(d / "deliver"))
    print(dl.read_file("report.md"))
    return 0


def cmd_deliver(a) -> int:
    cfg, der, d = _load(a.project)
    emod = _need("emit", "deliver")
    path = emod.deliver(a.project, root=d)
    print(f"delivered -> {path}")
    print(f"\nAdd to your corner setup:\n  include \"{path}/PMU_{a.project}.scs\" section=<corner>")
    return 0


def cmd_digest(a) -> int:
    dgmod = _need("digest", "digest")
    if a.project == "import":
        if not a.file:
            raise PmuError(what="`digest import` needs the pasted text file.",
                           why="The desk rebuilds a contract-2 dataset subset from the digest.",
                           do=[f"{PROG} digest import <file.txt>"], where="command line")
        rp = _need("reproduce", "digest import")
        payload = dgmod.parse(pathlib.Path(a.file).read_text(encoding="utf-8"))
        name = (payload.get("meta") or {}).get("project") or "from_digest"
        target = paths.ensure_project(name)
        jsonio.write(target / "digest_payload.json", payload)
        ds = rp.rebuild_dataset(payload, target / "digest" / "dataset")
        info = {"project": name, "path": str(target), "variables": ds.variables(),
                "coverage": {v: ds.coverage(v) for v in ds.variables()},
                "missing": ds.missing(), "dropped": payload.get("dropped") or []}
        ds.close()
        if a.json:
            _out(info, True)
            return 0
        print(f"imported -> {target}")
        print(f"  {len(info['variables'])} variable(s) rebuilt into "
              f"{target / 'digest' / 'dataset'}")
        if not info["variables"]:
            print("  This digest carried no curve or transient block -- either the box had not "
                  "characterized")
            print("  anything yet, or those blocks were not selected. The ledger and any fitted "
                  "parameters")
            print("  it did carry are in digest_payload.json.")
        for var in info["variables"]:
            cov = info["coverage"][var]
            print(f"    {var:<28s} filled {cov['filled']}  missing {cov['missing']}  "
                  f"never run {cov['never_run']}")
        for blk in info["dropped"]:
            print(f"  the box dropped block {blk} to fit its budget -- registered as missing, "
                  "not guessed")
        print()
        print(f"  Next:  {PROG} reproduce --from-digest {a.file}")
        return 0
    cfg, _der, d = _load(a.project)
    payload = _digest_payload(a.project, d)
    parts = dgmod.export(payload, budget=a.budget, project=a.project,
                         blocks=(a.blocks.split(",") if a.blocks else None))
    if a.out:
        base = pathlib.Path(a.out)
        for i, part in enumerate(parts, 1):
            p = base if len(parts) == 1 else base.with_suffix(f".{i}{base.suffix}")
            p.write_text(part, encoding="utf-8", newline="\n")
            print(f"wrote {p}  ({len(part)} bytes)")
    else:
        for part in parts:
            print(part)
    return 0


def _digest_payload(project: str, d: pathlib.Path) -> dict:
    """Assemble what the digest carries from whatever this project has on disk."""
    lmod = _need("ledger", "digest")
    payload: dict = {"meta": {"project": project}}
    for name, key in (("fit.json", "params"), ("verify.json", "grades")):
        if (d / name).exists():
            payload[key] = jsonio.read(d / name)
    if (d / "derived.json").exists():
        payload["provenance"] = {"config_sha": jsonio.read(d / "config.json").get("project", ""),
                                 "derived_sha": jsonio.read(d / "derived.json").get("config_sha", "")}
    if (d / "runs.sqlite").exists():
        led = lmod.Ledger.for_project(project)
        payload["ledger"] = led.to_rows()
        payload["faillog"] = [{"run_id": r.run_id, "error": r.error}
                              for r in led.all(status="failed")]
        led.close()
    return payload


def cmd_reproduce(a) -> int:
    dgmod = _need("digest", "reproduce")
    rp = _need("reproduce", "reproduce")
    payload = dgmod.parse(pathlib.Path(a.from_digest).read_text(encoding="utf-8"))
    out = rp.reproduce(payload, workdir=a.workdir)
    _out(out, a.json, rp.summary(out))
    return 0


def cmd_open(a) -> int:
    a.demo = False
    a.open = True
    return cmd_ui(a)


def cmd_ui(a) -> int:
    smod = _need("server", "ui")
    return smod.main(host=getattr(a, "host", "127.0.0.1"), port=getattr(a, "port", 8765),
                     demo=getattr(a, "demo", False), open_browser=getattr(a, "open", False),
                     project=getattr(a, "project", None))


def cmd_help(a) -> int:
    if not a.screen:
        print(f"Screens: {', '.join(helptext.SCREENS)}\n")
        print(f"Try:  {PROG} help plan")
        return 0
    print(helptext.render(a.screen), end="")
    return 0


# --------------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=PROG, description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--data", help="override $PMUKIT_DATA for this command")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="every project and which step it stopped at")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("new", help="start a project from one exported netlist")
    p.add_argument("project")
    p.add_argument("--netlist", required=True)
    p.add_argument("--pmu-inst", required=True, help="the PMU instance name in the testbench")
    p.add_argument("--corners", default="tt", help="comma separated, e.g. tt,ss,ff")
    p.add_argument("--temps", default="-40,25,125", help="degrees C, comma separated")
    p.add_argument("--vset", default="0", help="VSET codes, comma separated")
    p.add_argument("--care-up-to", default="1e9", help="highest frequency you care about, Hz")
    p.add_argument("--load", action="append",
                   help="RAIL=<on_a>,<off_a>[,<edge_s>] -- what YOUR module draws (repeatable)")
    p.add_argument("--port", action="append", help="PIN=model|stub|ignore (repeatable)")
    p.add_argument("--note", help="what state the testbench was in (goes into provenance)")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("check", help="does this netlist satisfy the convention? (creates nothing)")
    p.add_argument("netlist")
    p.add_argument("--pmu-inst", required=True)
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("pins", help="the pin table read out of the testbench")
    p.add_argument("project")
    p.set_defaults(fn=cmd_pins)

    p = sub.add_parser("config", help="show or change the project configuration")
    p.add_argument("project")
    p.add_argument("--set", action="append", help="key=value (JSON value), repeatable")
    p.add_argument("--undo", action="store_true", help="the Ctrl-Z of the New screen")
    p.add_argument("--derived", action="store_true", help="show the derived config instead")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("plan", help="what will run, and why")
    p.add_argument("project")
    p.add_argument("--off", action="append", help="untick a group (repeatable)")
    p.add_argument("--runs", help="list the runs of one group")
    p.add_argument("--recipe", help="print one run's recipe (run id prefix)")
    p.add_argument("--submit", action="store_true", help="write the plan into the ledger")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("run", help="run the plan")
    p.add_argument("project")
    p.add_argument("--engine", help="spectre_ssh | donau_alps | dry_run | fake")
    p.add_argument("--off", action="append", help="untick a group (repeatable)")
    p.add_argument("--no-resume", action="store_true", help="re-run even the cached ones")
    p.add_argument("--jobs", type=int, default=1)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("status", help="the run ledger")
    p.add_argument("project")
    p.add_argument("--status", help="filter: planned|submitted|running|done|failed|...")
    p.add_argument("--analysis", help="filter by analysis")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--why", help="explain why one run exists (run id)")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("fit", help="fit the model from the dataset")
    p.add_argument("project")
    p.set_defaults(fn=cmd_fit)

    p = sub.add_parser("verify", help="grades, envelope and the HB health check")
    p.add_argument("project")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("report", help="print the latest deliverable's report.md")
    p.add_argument("project")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("deliver", help="write the model library, envelope, report and provenance")
    p.add_argument("project")
    p.set_defaults(fn=cmd_deliver)

    p = sub.add_parser("digest", help="copy for desk, or `digest import <file>`")
    p.add_argument("project", help="project name, or the literal word `import`")
    p.add_argument("file", nargs="?", help="with `import`: the pasted digest text")
    p.add_argument("--blocks", help="comma separated block ids, e.g. D0,D1,D6")
    p.add_argument("--budget", type=int, default=64000)
    p.add_argument("--out", help="write to a file instead of stdout")
    p.set_defaults(fn=cmd_digest)

    p = sub.add_parser("reproduce", help="refit a digest at the desk and compare")
    p.add_argument("--from-digest", required=True)
    p.add_argument("--workdir", help="where to rebuild the dataset (default: ./reproduce)")
    p.set_defaults(fn=cmd_reproduce)

    p = sub.add_parser("open", help="open a project in the web shell")
    p.add_argument("project")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(fn=cmd_open)

    p = sub.add_parser("ui", help="start the web shell and print its URL")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--demo", action="store_true", help="fake data, no $PMUKIT_DATA needed")
    p.add_argument("--open", action="store_true", help="also launch a browser")
    p.set_defaults(fn=cmd_ui, project=None)

    p = sub.add_parser("help", help="the same text the `? Help` panel shows")
    p.add_argument("screen", nargs="?", choices=list(helptext.SCREENS))
    p.set_defaults(fn=cmd_help)
    return ap


# Options whose value legitimately starts with a minus sign. argparse would read `--temps -40,25`
# as a flag followed by an unknown option, so the value is re-attached with `=` before parsing.
# (`--temps=-40,25,125` always worked; this makes the natural spelling work too.)
NUMERIC_OPTS = ("--temps", "--vset", "--care-up-to")
_NUMERIC = re.compile(r"^-?[\d][\d.eE+\-,\s]*$")


def _fixup_negative_numbers(argv: list[str]) -> list[str]:
    out, i = [], 0
    while i < len(argv):
        tok = argv[i]
        if (tok in NUMERIC_OPTS and i + 1 < len(argv)
                and argv[i + 1].startswith("-") and _NUMERIC.match(argv[i + 1])):
            out.append(f"{tok}={argv[i + 1]}")
            i += 2
            continue
        out.append(tok)
        i += 1
    return out


def main(argv=None) -> int:
    ap = build_parser()
    a = ap.parse_args(_fixup_negative_numbers(list(sys.argv[1:] if argv is None else argv)))
    if getattr(a, "data", None):
        import os
        os.environ["PMUKIT_DATA"] = a.data
    try:
        return int(a.fn(a) or 0)
    except PmuError as e:
        if getattr(a, "json", False):
            print(json.dumps(e.to_dict(), indent=2, ensure_ascii=False), file=sys.stderr)
        else:
            print(str(e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
