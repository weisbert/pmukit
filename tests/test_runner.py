"""The runner and its four backends.

Everything here is synthetic.  The properties that matter:

  * the `fake` engine takes the WHOLE pipeline (plan -> run dir -> PSF -> ratios -> dataset) with
    no simulator, and is impossible to mistake for a measurement;
  * resume is a property of the content hash: the same plan run twice is all `skipped_cached`;
  * `dry_run` writes the deck and claims NOTHING -- its ledger row stays `planned`, because
    `skipped_cached` would mean "we already have results" and hide it from the NOT RUN report;
  * `spectre_ssh` degrades to `dry_run` when the host is unreachable, and says DEGRADED;
  * the `donau_alps` command matches the validated TOOL_FACTS shape, flag for flag.

Anything needing a real simulator is gated on `available()` and skips cleanly.
"""
import pathlib

import numpy as np
import pytest

from pmukit import importer
from pmukit.backends import BACKENDS, make_backend, probe_all
from pmukit.backends.donau_alps import (DonauAlpsBackend, build_sim_cmd,
                                        engine_model_tree, map_state, parse_job_id)
from pmukit.backends.fake import MODEL, FakeBackend, parse_deck, selftest
from pmukit.backends.spectre_ssh import SpectreSSHBackend, remote_command
from pmukit.config import ProjectConfig, derive
from pmukit.errors import PmuError
from pmukit.ledger import Ledger
from pmukit.netlist import Netlist
from pmukit.plan import compile_plan
from pmukit.runner import Job, Runner, parse_spectre_log
from pmukit.site import SiteConfig

# --------------------------------------------------------------------------- a tiny PMU

DEMO = """\
simulator lang=spectre
global 0
parameters VSET=3
include "pdk/toplevel.scs" section=tt

subckt pmu_demo (vdda a ptat en vss)
    ma1 (a na vdda vdda) pmos w=40u l=0.5u
    ra1 (a fb1) resistor r=100k
    rb1 (fb1 vss) resistor r=100k
    mp1 (ptat np vss vss) nmos w=10u l=1u
    men (na en vdda vdda) pmos w=2u l=1u
ends pmu_demo

PMU_TOP (VDDA_1V0 VDD0P8_A IB_PTAT EN 0) pmu_demo
VS_VDDA_1V0 (VDDA_1V0 0) vsource dc=1.0
IL_VDD0P8_A (VDD0P8_A 0) isource dc=500u
VB_IB_PTAT (IB_PTAT 0) vsource dc=0.4
VEN_EN (EN 0) vsource dc=1.0

dcOp dc
"""

CFG = {
    "project": "demo_pmu",
    "netlist": "tb/input.scs",
    "pmu_inst": "PMU_TOP",
    "corners": ["tt"],
    "temps_c": [27],
    "vset_codes": [3],
    "state_note": "synthetic",
    "ports": {"a": "model", "ptat": "model", "vdda": "model", "en": "model"},
    "my_load": {"a": {"on_a": 5e-4, "off_a": 2e-6, "switches": True}},
    "care_up_to_hz": 1e8,
}


@pytest.fixture
def parts():
    nl = Netlist(DEMO, "tb/input.scs")
    cfg = ProjectConfig.from_dict(CFG)
    pins = nl.scan("PMU_TOP", ports=cfg.ports)
    site = SiteConfig(engine="fake")
    der = derive(cfg, pins, site)
    plan = compile_plan(cfg, der, nl, pins, site=site)
    return cfg, plan, site


@pytest.fixture
def workshop(tmp_path, parts):
    """A runner on the fake engine, with its own ledger, dataset and run root."""
    cfg, plan, site = parts
    ledger = Ledger(tmp_path / "runs.sqlite")
    ds = importer.open_or_create(tmp_path / "dataset", plan, project=cfg.project,
                                 config_sha=cfg.sha())
    r = Runner(cfg, plan, ledger, site, dataset=ds, root=tmp_path / "runs")
    return r, cfg, plan, ledger, ds


# --------------------------------------------------------------------------- log parsing


def test_parse_spectre_log_units():
    log = ("Time accumulated: CPU = 179.695 ms, elapsed = 139.591 ms.\n"
           "Peak resident memory used = 101 Mbytes.\n"
           "Aggregate audit:\n"
           "Time used: CPU = 2.5 s, elapsed = 2.6 s, util. = 96%.\n"
           "Peak memory used = 128 Mbytes.\n")
    cpu, mem = parse_spectre_log(log)
    assert cpu == pytest.approx(2.5)          # the aggregate wins over the per-analysis lines
    assert mem == pytest.approx(128.0)


def test_parse_spectre_log_falls_back_to_accumulated():
    cpu, mem = parse_spectre_log("Time accumulated: CPU = 211 ms, elapsed = 211 ms.\n"
                                 "Peak resident memory used = 89.2 Mbytes.\n")
    assert cpu == pytest.approx(0.211)
    assert mem == pytest.approx(89.2)


def test_parse_spectre_log_says_nothing_rather_than_guessing():
    assert parse_spectre_log("no timing here at all") == (0.0, 0.0)


# --------------------------------------------------------------------------- the fake engine


def test_fake_model_is_the_shape_it_claims():
    selftest()                                 # |Z| peaks at exactly R on resonance
    import math
    f0 = 1.0 / (2 * math.pi * math.sqrt(MODEL["zout"]["l_h"] * MODEL["zout"]["c_f"]))
    assert 4e6 < f0 < 6e6


def test_fake_is_labelled_synthetic_everywhere(workshop):
    r, _cfg, plan, ledger, _ds = workshop
    ok, why = r.backend.available()
    assert ok and "SYNTHETIC" in why
    run = r.run_one(plan.runs()[0])
    assert run.engine == "fake"                # the ledger column the report reads
    assert run.source_path == ""               # nothing external was reused
    text = (r.workdir(run.run_id) / "raw").glob("*")
    assert any("pmukit-fake" in p.read_text(encoding="utf-8") for p in text)


def test_fake_end_to_end_fills_the_dataset(workshop):
    r, _cfg, plan, ledger, ds = workshop
    summary = r.run_all()
    assert summary["failed"] == 0
    assert summary["done"] == len(plan.runs())
    assert summary["stored"] > 0
    cov = ds.summary()
    assert cov["totals"]["never_run"] == 0
    assert cov["totals"]["missing"] == 0
    assert cov["totals"]["filled"] == cov["totals"]["declared"]
    # the physics actually arrived, with the right sign
    z = np.asarray(ds.get("ac_zout.a", {"process": "tt", "temp_c": 27.0, "vset": 3,
                                        "load_a": ds.axis("load_a", "a")[0]}))
    assert np.all(z.real > 0)                  # Zout of a stable rail is passive
    # (R_dc + jwL) || (esr + 1/jwC): a FINITE DC floor, a resonance well above it, an ESR floor
    # above that. Finite at DC is load-bearing -- see the comment on MODEL["zout"].
    assert abs(z[0]) == pytest.approx(MODEL["zout"]["r_dc_ohm"], rel=0.05)
    assert np.max(np.abs(z)) > 3 * abs(z[0])
    assert abs(z[-1]) < abs(z[0])


def test_a_run_pmukit_ran_itself_is_done_not_imported(workshop):
    """`imported` is contract 3's OTHER branch: results the user already had, with source_path."""
    r, _cfg, plan, ledger, _ds = workshop
    r.run_all()
    counts = ledger.counts_by_status()
    assert counts["done"] == len(plan.runs())
    assert counts["imported"] == 0
    assert all(run.source_path == "" for run in ledger.all())


def test_resume_reports_every_run_cached(workshop):
    r, cfg, plan, ledger, ds = workshop
    r.run_all()
    seen = []
    second = Runner(cfg, plan, ledger, r.site, dataset=ds, root=r.root, backend=r.backend)
    summary = second.run_all(on_event=lambda k, i, d: seen.append(k) if k == "skipped_cached"
                             else None)
    assert summary["skipped_cached"] == len(plan.runs())
    assert summary["done"] == 0
    assert len(seen) == len(plan.runs())


def test_run_directory_holds_the_deck_and_the_recipe(workshop):
    r, _cfg, plan, _ledger, _ds = workshop
    pr = plan.runs()[0]
    r.run_one(pr)
    wd = r.workdir(pr.run_id)
    assert (wd / "input.scs").read_text(encoding="utf-8") == pr.netlist_text
    assert "[submit]" in (wd / "recipe.txt").read_text(encoding="utf-8")


def test_run_directory_is_written_with_lf(workshop):
    """The VM and the box are Linux; a CRLF deck is a parse error there."""
    r, _cfg, plan, _ledger, _ds = workshop
    pr = plan.runs()[0]
    r.run_one(pr)
    assert b"\r\n" not in (r.workdir(pr.run_id) / "input.scs").read_bytes()


def test_aux_files_are_copied_into_every_run(tmp_path, parts):
    cfg, plan, site = parts
    pdk = tmp_path / "pdk"
    pdk.mkdir()
    (pdk / "toplevel.scs").write_text("// models\n", encoding="utf-8")
    r = Runner(cfg, plan, Ledger(tmp_path / "l.sqlite"), site, dataset=False,
               root=tmp_path / "runs", aux=[pdk])
    pr = plan.runs()[0]
    r.run_one(pr)
    assert (r.workdir(pr.run_id) / "pdk" / "toplevel.scs").is_file()


def test_missing_aux_is_a_clear_error(tmp_path, parts):
    cfg, plan, site = parts
    r = Runner(cfg, plan, Ledger(tmp_path / "l.sqlite"), site, dataset=False,
               root=tmp_path / "runs", aux=[tmp_path / "nope"])
    with pytest.raises(PmuError) as exc:
        r.run_one(plan.runs()[0])
    assert "nope" in exc.value.what


def test_failure_lands_in_the_ledger_with_the_reason(tmp_path, parts):
    """A backend that fails must leave `failed` + a readable reason, and mark the cells missing."""
    cfg, plan, site = parts
    ledger = Ledger(tmp_path / "runs.sqlite")
    ds = importer.open_or_create(tmp_path / "dataset", plan, project=cfg.project)

    class Broken(FakeBackend):
        name = "fake"

        def submit(self, job):
            job.state = "failed"
            job.detail = "the solver gave up on the operating point"
            (pathlib.Path(job.workdir) / "spectre.log").write_text(
                "ERROR: no convergence\nTime used: CPU = 1 s\n", encoding="utf-8")
            return "fake-broken"

        def fetch(self, job):
            job.log_path = pathlib.Path(job.workdir) / "spectre.log"
            return pathlib.Path(job.workdir) / "raw"

    r = Runner(cfg, plan, ledger, site, dataset=ds, root=tmp_path / "runs", backend=Broken(site))
    run = r.run_one(plan.runs()[0])
    assert run.status == "failed"
    assert "gave up" in run.error
    assert "no convergence" in run.error       # the log tail travelled with it
    assert run.cpu_seconds == pytest.approx(1.0)


def test_a_hollow_done_is_recorded_failed_not_done(tmp_path, parts):
    """The scheduler says done, fetch() finds no output and marks the job failed.  The runner
    used to record `done` anyway -- and resume then skipped the run forever."""
    cfg, plan, site = parts
    ledger = Ledger(tmp_path / "runs.sqlite")
    ds = importer.open_or_create(tmp_path / "dataset", plan, project=cfg.project)

    class Hollow(FakeBackend):
        name = "fake"

        def submit(self, job):
            job.state = "running"
            return "job-1"

        def poll(self, job):
            return "done"

        def fetch(self, job):
            job.state = "failed"
            job.detail = "job 1 reported done but raw is empty"
            return pathlib.Path(job.workdir) / "raw"

    r = Runner(cfg, plan, ledger, site, dataset=ds, root=tmp_path / "runs", backend=Hollow(site))
    run = r.run_one(plan.runs()[0])
    assert run.status == "failed" and "is empty" in run.error


def test_skip_keeps_a_run_in_the_not_run_report(workshop):
    r, _cfg, plan, ledger, _ds = workshop
    victim = plan.runs()[0]
    r.run_one(victim)                          # make the row exist
    r.skip(victim.run_id)
    assert ledger.get(victim.run_id).status == "planned"
    assert victim.run_id in [x.run_id for x in ledger.not_run()]
    summary = r.run_all()
    assert victim.run_id not in [row["run_id"] for row in summary["runs"]]


def test_retry_clears_the_stored_failure(tmp_path, parts):
    cfg, plan, site = parts
    ledger = Ledger(tmp_path / "runs.sqlite")
    r = Runner(cfg, plan, ledger, site, dataset=False, root=tmp_path / "runs")
    pr = plan.runs()[0]
    r.run_one(pr)
    ledger.set_status(pr.run_id, "failed", error="a previous disaster")
    run = r.retry(pr.run_id)
    assert run.status == "done"
    assert run.error == ""


def test_retry_and_kill_name_an_unknown_id(workshop):
    r, _cfg, _plan, _ledger, _ds = workshop
    for fn in (r.retry, r.skip, r.kill):
        with pytest.raises(PmuError) as exc:
            fn("deadbeef0000")
        assert "deadbeef0000" in exc.value.what


def test_parallel_jobs_give_the_same_result(tmp_path, parts):
    cfg, plan, site = parts
    ds = importer.open_or_create(tmp_path / "dataset", plan, project=cfg.project)
    r = Runner(cfg, plan, Ledger(tmp_path / "l.sqlite"), site, dataset=ds,
               root=tmp_path / "runs", jobs=4)
    summary = r.run_all()
    assert summary["failed"] == 0
    assert ds.summary()["totals"]["never_run"] == 0


def test_fake_parses_the_pwl_wave_whole():
    """`wave=[0 1e-6 ...]` has spaces: a k=v token split keeps only `wave=[0`."""
    deck = parse_deck("IL_A (A 0) isource type=pwl wave=[0 2e-06 1e-06 2e-06 1.1e-06 5e-04]\n")
    assert deck["sources"]["IL_A"]["wave"] == "0 2e-06 1e-06 2e-06 1.1e-06 5e-04"


# --------------------------------------------------------------------------- dry run


def test_dry_run_claims_nothing(tmp_path, parts):
    cfg, plan, _site = parts
    site = SiteConfig(engine="dry_run")
    ledger = Ledger(tmp_path / "runs.sqlite")
    r = Runner(cfg, plan, ledger, site, dataset=False, root=tmp_path / "runs")
    summary = r.run_all()
    assert summary["dry_run"] == len(plan.runs())
    assert summary["done"] == 0
    for run in ledger.all():
        # `skipped_cached` would claim results and hide the run from the NOT RUN report.
        assert run.status == "planned"
        assert "dry run" in run.error.lower()
    assert len(ledger.not_run()) == len(plan.runs())
    pr = plan.runs()[0]
    assert (r.workdir(pr.run_id) / "input.scs").is_file()
    assert not (r.workdir(pr.run_id) / "raw").exists()


# --------------------------------------------------------------------------- spectre over ssh


def test_remote_command_sources_cshrc_and_uses_64_bit():
    cmd = remote_command("~/pmukit_work/abc123")
    assert cmd.startswith('tcsh -c "source ~/.cshrc;')      # the Cadence env lives ONLY there
    assert "spectre -64 input.scs" in cmd                   # else ahdlcmi compiles -m32
    assert "-format psfascii" in cmd                        # what pmukit.psf parses
    assert "-raw raw" in cmd and "+log spectre.log" in cmd


def test_spectre_ssh_degrades_loudly_when_the_host_is_unreachable(tmp_path, parts):
    cfg, plan, _site = parts
    site = SiteConfig(engine="spectre_ssh", ssh_host="pmukit-no-such-host.invalid")
    be = SpectreSSHBackend(site, ssh="pmukit-no-such-ssh-binary")
    ok, why = be.available()
    assert not ok and "ssh" in why
    ledger = Ledger(tmp_path / "runs.sqlite")
    r = Runner(cfg, plan, ledger, site, dataset=False, root=tmp_path / "runs", backend=be)
    said = []
    summary = r.run_all(on_event=lambda k, i, d: said.append(d) if k == "engine" else None)
    assert summary["dry_run"] == len(plan.runs())
    assert any("not usable" in s for s in said)             # never silent
    assert all("DEGRADED" in run.error for run in ledger.all())


def test_spectre_ssh_refuses_instead_of_degrading_when_told_to(parts):
    cfg, plan, _site = parts
    site = SiteConfig(engine="spectre_ssh", ssh_host="pmukit-no-such-host.invalid")
    be = SpectreSSHBackend(site, ssh="pmukit-no-such-ssh-binary", allow_degrade=False)
    job = Job(run=plan.runs()[0].run, workdir=pathlib.Path("."),
              netlist_text=plan.runs()[0].netlist_text, site=site)
    with pytest.raises(PmuError) as exc:
        be.submit(job)
    assert "pmukit-no-such-host.invalid" in exc.value.what


def test_spectre_ssh_refuses_an_unsafe_remote_path(parts):
    cfg, plan, _site = parts
    site = SiteConfig(engine="spectre_ssh", ssh_host="h", remote_workdir="~/pmu kit; rm -rf /")
    be = SpectreSSHBackend(site)
    job = Job(run=plan.runs()[0].run, workdir=pathlib.Path("."), netlist_text="", site=site)
    with pytest.raises(PmuError) as exc:
        be.remote_dir(job)
    assert "remote_workdir" in exc.value.what


VM = SiteConfig(engine="spectre_ssh", ssh_host="ewave-vm")
_vm_ok, _vm_why = SpectreSSHBackend(VM).available()


@pytest.mark.skipif(not _vm_ok, reason=f"no simulation host: {_vm_why}")
def test_spectre_ssh_probe_reports_a_version():
    ok, why = SpectreSSHBackend(VM).available()
    assert ok and "spectre" in why.lower()


# --------------------------------------------------------------------------- donau / alps


def test_dsub_command_matches_the_validated_shape(tmp_path, parts):
    """TOOL_FACTS 'ALPS / Donau' -- the line that is known to work on the box, flag for flag."""
    cfg, plan, _site = parts
    site = SiteConfig(engine="donau_alps", queue="short", cpus=8,
                      project_account="ug_demo.demoClass")
    env = {"PMUKIT_ALPS_ROOT": "/opt/demo/alps/2026.03.hf1",
           "PMUKIT_PDK_ROOT": "/opt/demo/pdk/models",
           "PMUKIT_AHDLLIBDIR": "/opt/demo/work/input.ahdlSimDB"}
    be = DonauAlpsBackend(site, dry_run=True, env=env)
    pr = plan.runs()[0]
    job = Job(run=pr.run, workdir=tmp_path / "netlist", netlist_text=pr.netlist_text, site=site)
    cmd = [str(x) for x in be.dsub_command(job)]

    assert cmd[0] == "dsub"
    assert cmd[1:3] == ["-A", "ug_demo.demoClass"]
    assert cmd[3:5] == ["-q", "short"]
    assert cmd[5:7] == ["-R", "cpu=8;mem=8000"]
    assert cmd[7:9] == ["-x", "all"]                  # FlexLM env to the node, or no license
    assert cmd[9] == "-EP" and cmd[10].endswith("netlist")
    assert cmd[11] == "-J"                            # JSON, so the JOBID is parseable
    # the payload: the WRAPPER, classic PSF, the -o dir, the PDK DIRECTORY, -mt == cpu, -ade
    assert cmd[12] == "/opt/demo/alps/2026.03.hf1/bin/alps"
    assert cmd[13] == "input.scs"
    assert cmd[14:16] == ["-format", "ps"]            # never psfxl
    assert cmd[16:18] == ["-o", "raw"]
    assert cmd[18:20] == ["-I", "/opt/demo/pdk/models/alps"]
    assert cmd[20:22] == ["-ahdllibdir", "/opt/demo/work/input.ahdlSimDB"]
    assert cmd[22:24] == ["-mt", "8"]                 # MUST equal cpu=8
    assert cmd[24] == "-ade"                          # ADE names + the .simDone sentinel
    assert len(cmd) == 25


def test_mt_always_equals_the_donau_cpu_count(tmp_path, parts):
    cfg, plan, _site = parts
    for cpus in (1, 4, 16):
        site = SiteConfig(engine="donau_alps", queue="short", cpus=cpus,
                          project_account="ug_demo.demoClass")
        be = DonauAlpsBackend(site, dry_run=True, env={"PMUKIT_ALPS_ROOT": "/opt/demo/alps"})
        job = Job(run=plan.runs()[0].run, workdir=tmp_path, netlist_text="", site=site)
        cmd = [str(x) for x in be.dsub_command(job)]
        assert f"cpu={cpus};" in cmd[cmd.index("-R") + 1]
        assert cmd[cmd.index("-mt") + 1] == str(cpus)


def test_alps_include_is_a_directory_not_the_model_file():
    """The footgun: pasting `$ROOT/alps/toplevel.scs` made `-I .../toplevel.scs/alps`."""
    assert engine_model_tree("/pdk/models", "alps") == "/pdk/models/alps"
    assert engine_model_tree("/pdk/models/alps", "alps") == "/pdk/models/alps"
    assert engine_model_tree("/pdk/models/alps/toplevel.scs", "alps") == "/pdk/models/alps"


def test_alps_wrapper_not_the_raw_binary():
    cmd = build_sim_cmd("alps", "input.scs", "raw", alps_root="/opt/demo/alps/2026.03.hf1")
    assert cmd[0].endswith("/bin/alps")
    assert build_sim_cmd("alps", "i.scs", "raw", alps_root="/x/bin/alps")[0] == "/x/bin/alps"
    assert build_sim_cmd("alps", "i.scs", "raw", alps_root="/x/bin")[0] == "/x/bin/alps"


def test_alps_without_a_wrapper_root_says_exactly_what_to_set():
    with pytest.raises(PmuError) as exc:
        build_sim_cmd("alps", "input.scs", "raw")
    assert "PMUKIT_ALPS_ROOT" in exc.value.what
    assert "libsvadv" in exc.value.why


def test_donau_needs_an_account_and_a_queue(tmp_path, parts):
    cfg, plan, _site = parts
    job = Job(run=plan.runs()[0].run, workdir=tmp_path, netlist_text="", site=None)
    be = DonauAlpsBackend(SiteConfig(engine="dry_run", queue="short"), dry_run=True,
                          env={"PMUKIT_ALPS_ROOT": "/opt/demo/alps"})
    with pytest.raises(PmuError) as exc:
        be.dsub_command(job)
    assert "account" in exc.value.what.lower()


def test_donau_state_mapping_and_jobid():
    assert map_state("State: RUNNING") == "running"
    assert map_state("status=PEND") == "pending"
    assert map_state("Exit: 0") == "done"               # an exit CODE is not the state word EXIT
    assert map_state("exited 3") == "failed"
    assert map_state("nothing useful") is None
    assert parse_job_id('{"data":{"jobId":"37322154"},"code":"success"}') == "37322154"
    assert parse_job_id("... JOBID 37238970 ...") == "37238970"
    assert parse_job_id("no id here") is None


def test_donau_dry_run_submits_nothing(tmp_path, parts):
    cfg, plan, _site = parts
    site = SiteConfig(engine="donau_alps", queue="short", cpus=8,
                      project_account="ug_demo.demoClass")
    be = DonauAlpsBackend(site, dry_run=True, env={"PMUKIT_ALPS_ROOT": "/opt/demo/alps"})
    job = Job(run=plan.runs()[0].run, workdir=tmp_path, netlist_text="", site=site)
    assert be.submit(job) == ""
    assert be.poll(job) == "skipped"
    assert be.available()[0] is True


def test_donau_submit_uses_the_injected_runner(tmp_path, parts):
    """The state machine is testable with no dsub anywhere: the executor is injected."""
    cfg, plan, _site = parts
    calls = []

    class Fake:
        def __call__(self, argv, timeout=None):
            calls.append(list(argv))

            class R:
                returncode = 0
                stdout = '{"data":{"jobId":"4242"}}' if argv[0] == "dsub" else "State: DONE"
                stderr = ""
            return R()

    site = SiteConfig(engine="donau_alps", queue="short", cpus=8,
                      project_account="ug_demo.demoClass")
    be = DonauAlpsBackend(site, runner=Fake(), env={"PMUKIT_ALPS_ROOT": "/opt/demo/alps"})
    job = Job(run=plan.runs()[0].run, workdir=tmp_path, netlist_text="", site=site)
    assert be.submit(job) == "4242"
    assert calls[0][0] == "dsub"
    assert be.poll(job) == "done"
    assert calls[1] == ["djob", "4242"]


# --------------------------------------------------------------------------- the registry


def test_every_backend_satisfies_the_interface():
    site = SiteConfig(engine="dry_run", queue="short", project_account="ug_demo.demoClass")
    for name in BACKENDS:
        be = make_backend(name, site)
        assert be.name == name
        for method in ("available", "submit", "poll", "fetch", "kill"):
            assert callable(getattr(be, method))
        ok, why = be.available()               # by contract this never raises
        assert isinstance(ok, bool) and isinstance(why, str) and why


def test_unknown_engine_names_the_ones_that_exist():
    with pytest.raises(PmuError) as exc:
        make_backend("spice3", SiteConfig(engine="dry_run"))
    assert "spice3" in exc.value.what
    assert "dry_run" in " ".join(exc.value.do)


def test_probe_all_never_raises():
    got = probe_all(SiteConfig(engine="dry_run"))
    assert set(got) == set(BACKENDS)
    assert all(isinstance(v, tuple) and len(v) == 2 for v in got.values())


def test_backend_states_are_the_only_legal_answers(tmp_path, parts):
    cfg, plan, site = parts

    class Rogue(FakeBackend):
        def poll(self, job):
            return "probably fine"

    r = Runner(cfg, plan, Ledger(tmp_path / "l.sqlite"), site, dataset=False,
               root=tmp_path / "runs", backend=Rogue(site))
    with pytest.raises(PmuError) as exc:
        r.run_one(plan.runs()[0])
    assert "probably fine" in exc.value.what


def test_ac_runs_drive_exactly_one_source():
    """An ADE testbench can leave mag=1 on a supply.  Every AC run must zero every other role
    source that carries mag=, or two sources are hot at once (LDO_modeling zeroed them)."""
    import re as _re
    nl = Netlist(DEMO.replace("VS_VDDA_1V0 (VDDA_1V0 0) vsource dc=1.0",
                              "VS_VDDA_1V0 (VDDA_1V0 0) vsource dc=1.0 mag=1"), "tb/input.scs")
    cfg = ProjectConfig.from_dict(CFG)
    pins = nl.scan("PMU_TOP", ports=cfg.ports)
    site = SiteConfig(engine="fake")
    plan = compile_plan(cfg, derive(cfg, pins, site), nl, pins, site=site)
    ac = [p for p in plan.runs() if p.run.analysis == "ac"]
    assert ac
    for p in ac:
        hot = _re.findall(r"^\s*(\w+)\s*\(.*\bmag=1\b", p.netlist_text, _re.MULTILINE)
        assert hot == [p.run.stimulus], (p.run.stimulus, hot)
