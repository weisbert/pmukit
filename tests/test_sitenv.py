"""pmukit.sitenv -- the box's own environment, read first-class, with the source of every value."""
from pmukit import sitenv
from pmukit.backends.donau_alps import DonauAlpsBackend, alps_exe
from pmukit.site import SiteConfig

# the shape of the real site variables (values made up)
ALPS_ENV = {"ALPS_HOME": "/sw/empyrean/alps/2026.06.hf1/tools/alps",
            "ALPS_MPATH": "/sw/empyrean/alps/2026.06.hf1",
            "ALPS_ROOT": "/sw/empyrean/alps/2026.06.hf1"}


def test_alps_root_comes_from_the_sites_own_variable():
    f = sitenv.alps_root(ALPS_ENV, which=None)
    assert f.value == "/sw/empyrean/alps/2026.06.hf1" and f.source == "$ALPS_ROOT"
    assert alps_exe(f.value) == "/sw/empyrean/alps/2026.06.hf1/bin/alps"   # the WRAPPER


def test_an_explicit_override_beats_the_site_variable():
    f = sitenv.alps_root({**ALPS_ENV, "PMUKIT_ALPS_ROOT": "/mine/alps"}, which=None)
    assert f.value == "/mine/alps" and f.source == "$PMUKIT_ALPS_ROOT"


def test_alps_home_alone_is_enough():
    f = sitenv.alps_root({"ALPS_HOME": ALPS_ENV["ALPS_HOME"] + "/"}, which=None)
    assert f.value == "/sw/empyrean/alps/2026.06.hf1"
    assert f.source.startswith("$ALPS_HOME")


def test_which_alps_is_the_last_resort():
    f = sitenv.alps_root({}, which=lambda _n: "/sw/alps/9.9/bin/alps")
    assert f.value == "/sw/alps/9.9" and f.source == "`which alps`"
    assert not sitenv.alps_root({}, which=None).ok


def test_user_is_dollar_user():
    f = sitenv.user({"USER": "w00000001", "USERNAME": "someone-else"})
    assert f.value == "w00000001" and f.source == "$USER"


def test_simulator_defaults_to_alps_and_can_be_switched():
    assert sitenv.simulator(SiteConfig(), {}).value == "alps"
    assert sitenv.simulator(SiteConfig(simulator="spectre"), {}).value == "spectre"
    f = sitenv.simulator(SiteConfig(), {"PMUKIT_SIMULATOR": "spectre"})
    assert f.value == "spectre" and f.source == "$PMUKIT_SIMULATOR"
    assert sitenv.simulator(SiteConfig(), {"PMUKIT_CLUSTER_ENGINE": "spectre"}).value == "spectre"


def test_pdk_root_has_one_resolution_for_backend_and_probe():
    assert sitenv.pdk_root({"PDK": "/pdk/a"}).value == "/pdk/a"
    assert sitenv.pdk_root({"PDK": "/pdk/a", "PMUKIT_PDK_ROOT": "/pdk/b"}).value == "/pdk/b"


def test_the_backend_reads_the_site_alps_variables():
    site = SiteConfig(project_account="ug_demo.demoClass")
    be = DonauAlpsBackend(site, dry_run=True, env=ALPS_ENV)
    assert be.engine == "alps"
    assert be.alps_root == "/sw/empyrean/alps/2026.06.hf1"


def test_spectre_on_donau_when_the_site_says_so():
    site = SiteConfig(project_account="ug_demo.demoClass", simulator="spectre")
    assert DonauAlpsBackend(site, dry_run=True, env={}).engine == "spectre"


def test_recipe_submit_line_is_the_real_composer(monkeypatch):
    """The last line of a recipe used to hard-code `-q short` with no `-A`; it is now the backend's
    own dsub line, with only the run directory left as a placeholder."""
    from pmukit.plan import _submit_line
    for k, v in ALPS_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PMUKIT_ALPS_ROOT", raising=False)
    monkeypatch.delenv("PMUKIT_DONAU_ACCOUNT", raising=False)
    monkeypatch.delenv("PMUKIT_SIMULATOR", raising=False)
    monkeypatch.delenv("PMUKIT_CLUSTER_ENGINE", raising=False)
    site = SiteConfig(queue="short", cpus=16, project_account="ug_demo.demoClass")
    line = _submit_line(site, "tt", "abc123")
    assert line.startswith("dsub -A ug_demo.demoClass -q short -R 'cpu=16;mem=8000'")
    assert "/sw/empyrean/alps/2026.06.hf1/bin/alps input.scs" in line
    assert "-mt 16" in line and "-ade" in line
    no_acct = _submit_line(SiteConfig(), "tt", "abc123")
    assert "-A '<account>'" in no_acct


# ------------------------------------------------------------ Donau accounts (the dropdown)
def test_account_list_add_select_remove(tmp_path, monkeypatch):
    for v in ("PMUKIT_ENGINE", "PMUKIT_SSH_HOST", "PMUKIT_CPUS", "PMUKIT_DONAU_ACCOUNT"):
        monkeypatch.delenv(v, raising=False)
    s = SiteConfig()
    s.add_account("ug_demo.smallClass", "sims up to 512GB")
    s.add_account("ug_demo.bigClass", "sims up to 2TB")
    s.add_account("ug_demo.smallClass")                       # no duplicate, note kept
    assert [a["name"] for a in s.accounts] == ["ug_demo.smallClass", "ug_demo.bigClass"]
    assert s.accounts[0]["note"] == "sims up to 512GB"
    s.select_account("ug_demo.bigClass")
    assert s.project_account == "ug_demo.bigClass"
    p = s.save(tmp_path / "site.json")
    back = SiteConfig.load(p)
    assert back.accounts == s.accounts and back.project_account == "ug_demo.bigClass"
    back.remove_account("ug_demo.bigClass")
    assert back.project_account == "" and len(back.accounts) == 1


def test_accounts_must_be_name_note_objects():
    import pytest
    from pmukit.errors import PmuError
    with pytest.raises(PmuError):
        SiteConfig(accounts=["ug_demo.smallClass"]).validate()


def test_site_api_lists_and_selects(tmp_path, monkeypatch):
    from pmukit.server import Api
    for v in ("PMUKIT_ENGINE", "PMUKIT_DONAU_ACCOUNT"):
        monkeypatch.delenv(v, raising=False)
    s = SiteConfig()
    s.add_account("ug_demo.smallClass", "sims up to 512GB")
    s.add_account("ug_demo.bigClass", "sims up to 2TB")
    s.save(tmp_path / "site.json")
    api = Api(root=tmp_path)
    got = api.site_get()
    assert got["engine"] == "donau_alps" and got["account"] == ""
    assert [a["name"] for a in got["accounts"]] == ["ug_demo.smallClass", "ug_demo.bigClass"]
    got = api.site_put({"account": "ug_demo.bigClass"})
    assert got["account"] == "ug_demo.bigClass"
    assert SiteConfig.load(tmp_path / "site.json").project_account == "ug_demo.bigClass"


def test_cli_add_account_and_select(tmp_path, monkeypatch, capsys):
    from pmukit.cli import main
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path))
    for v in ("PMUKIT_ENGINE", "PMUKIT_DONAU_ACCOUNT"):
        monkeypatch.delenv(v, raising=False)
    main(["site", "--add-account", "ug_demo.smallClass=sims up to 512GB",
          "--add-account", "ug_demo.bigClass=sims up to 2TB", "--account", "ug_demo.smallClass"])
    out = capsys.readouterr().out
    assert "* ug_demo.smallClass" in out and "sims up to 2TB" in out
    s = SiteConfig.load(tmp_path / "site.json")
    assert s.project_account == "ug_demo.smallClass" and len(s.accounts) == 2


# ------------------------------------------------------------ what the real box exports
def test_model_root_is_the_pdk_root_and_alps_gets_its_subtree():
    from pmukit.backends.donau_alps import engine_model_tree
    f = sitenv.pdk_root({"MODEL_ROOT": "/pdk/models/demo"})
    assert f.value == "/pdk/models/demo" and f.source == "$MODEL_ROOT"
    assert engine_model_tree(f.value, "alps") == "/pdk/models/demo/alps"


def test_license_follows_the_simulator():
    env = {"LM_LICENSE_FILE": "8224@arm", "CDS_LIC_FILE": "5280@cds",
           "EMPYREAN_LICENSE_FILE": "4416@emp"}
    assert sitenv.license_(env, "alps").source == "$EMPYREAN_LICENSE_FILE"
    assert sitenv.license_(env, "spectre").source == "$CDS_LIC_FILE"
    assert sitenv.license_({"LM_LICENSE_FILE": "1@x"}, "alps").source == "$LM_LICENSE_FILE"


def test_ade_repeats_one_model_file_per_model_library_row():
    """The box's netlists carry the corner row AND e.g. pre_Sim / Noise_Worst from the same
    toplevel.scs. Only the first is the process corner; the notes must say the rest were kept."""
    from pmukit.netlist import Netlist
    nl = Netlist('simulator lang=spectre\n'
                 'include "toplevel.scs" section=TOP_TT_X\n'
                 'include "toplevel.scs" section=pre_Sim\n'
                 'include "toplevel.scs" section=Noise_Worst\n'
                 'R0 (a 0) resistor r=1\n')
    notes = nl.set_section_all("TOP_SS_X")
    lines = [ln for ln in nl.text.splitlines() if ln.startswith("include")]
    assert lines == ['include "toplevel.scs" section=TOP_SS_X',
                     'include "toplevel.scs" section=pre_Sim',
                     'include "toplevel.scs" section=Noise_Worst']
    assert not any("set on 3 includes" in n for n in notes)
    assert sum("left as is" in n for n in notes) == 2


# ------------------------------------------------------------ the queue probe
def _fake_tools(monkeypatch, present, rc=0, out="", err=""):
    import pmukit.server as srv
    monkeypatch.setattr(srv.shutil, "which",
                        lambda n: f"/opt/batch/cli/bin/{n}" if n in present else None)
    calls = []

    def run(cmd, timeout):
        calls.append(cmd)
        return rc, out, err
    monkeypatch.setattr(srv, "_run_cmd", run)
    return srv, calls


def test_queue_probe_never_asks_dsub_for_a_version(tmp_path, monkeypatch):
    """Donau's dsub has no --version ("Unexpected argument \\"version\\""): a healthy box read
    as a dead queue. The probe asks dqueue, which must reach the scheduler to answer."""
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path))
    srv, calls = _fake_tools(monkeypatch, {"dsub", "dqueue"},
                             out="QUEUE_NAME  STATUS\nshort       Open:Active\nlong  Open\n")
    r = srv.probe_queue()
    assert r["ok"] and "queue 'short' listed" in r["detail"]
    assert calls == [["/opt/batch/cli/bin/dqueue"]]
    assert not any("--version" in c for cmd in calls for c in cmd)


def test_queue_probe_notes_but_does_not_fail_an_unlisted_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path))
    srv, _ = _fake_tools(monkeypatch, {"dsub", "dqueue"}, out="QUEUE_NAME\nlong\n")
    r = srv.probe_queue()
    assert r["ok"] and "not found" in r["detail"]


def test_queue_probe_reports_a_scheduler_that_does_not_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path))
    srv, _ = _fake_tools(monkeypatch, {"dsub", "dqueue"}, rc=1, err="connect timeout")
    r = srv.probe_queue()
    assert not r["ok"] and "connect timeout" in r["reason"]["why"]


def test_queue_probe_falls_back_to_dversion(tmp_path, monkeypatch):
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path))
    srv, calls = _fake_tools(monkeypatch, {"dsub", "dversion"}, out="Donau 1.2.3\n")
    r = srv.probe_queue()
    assert r["ok"] and r["how"] == "dversion" and calls == [["/opt/batch/cli/bin/dversion"]]


# ------------------------------------------------------------ what LDO_modeling ran on the box
def test_noise_total_falls_back_to_the_probe_keys():
    import numpy as np
    from pmukit.importer import _noise_total
    parsed = {"VB_X:p": np.array([2.0, 3.0]), "_types": {"VB_X:p": "A/sqrt(Hz)"}}
    assert list(_noise_total(parsed, "t", probe="VB_X:p")) == [4.0, 9.0]
    parsed = {"out": np.array([1.0]), "VB_X:p": np.array([5.0]),
              "_types": {"out": "V^2/Hz", "VB_X:p": "A/sqrt(Hz)"}}
    assert list(_noise_total(parsed, "t", probe="VB_X:p")) == [1.0]      # `out` still wins


def test_noise_reference_is_the_testbench_net_not_the_pmu_port():
    from types import SimpleNamespace as NS
    from pmukit.plan import _ground_of
    derived = NS(grounds={"by_pin": {"VDD_A": "VSS_A"}})
    pins = NS(pins={"VSS_A": NS(net="0")})
    assert _ground_of(derived, "VDD_A", pins) == "0"
    assert _ground_of(derived, "VDD_A", NS(pins={"VSS_A": NS(net="gnd!")})) == "gnd!"
    assert _ground_of(derived, "VDD_A") == "0"          # no pin table: the global ground
    assert _ground_of(derived, "OTHER", pins) == "0"


def test_donau_defaults_follow_ldo_modeling():
    from pmukit.backends.donau_alps import DonauAlpsBackend
    assert DonauAlpsBackend.poll_interval_s == 5.0
    assert DonauAlpsBackend.default_job_timeout_s == 3 * 3600
    assert DonauAlpsBackend.default_jobs == 4
    be = DonauAlpsBackend(SiteConfig(project_account="a"), dry_run=True, env=ALPS_ENV)
    assert be.timeout_s == DonauAlpsBackend.cmd_timeout_s   # every CLI call is bounded


def test_a_hollow_done_is_recorded_failed(tmp_path):
    """Donau says done, the PSF dir is empty: fetch() marks the job failed, and the runner must
    record THAT -- not `done`, which resume would then skip forever."""
    from types import SimpleNamespace as NS
    from pmukit.backends.donau_alps import DonauAlpsBackend

    class R:
        def __call__(self, argv, timeout=None):
            if argv[0] == "dsub":
                return NS(returncode=0, stdout='{"data":{"jobId":"123"}}', stderr="")
            if argv[0] == "djob":
                return NS(returncode=0, stdout="State: DONE Exit: 0", stderr="")
            return NS(returncode=0, stdout="", stderr="")

    be = DonauAlpsBackend(SiteConfig(project_account="a"), runner=R(), env=ALPS_ENV)
    job = NS(workdir=tmp_path, run=NS(run_id="r1"), detail="", state="", job_id="",
             log_path=None, console="")
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "stale.ac").write_text("old")          # a previous attempt's output
    be.submit(job)
    assert not (tmp_path / "raw").exists()                     # cleared before resubmitting
    assert be.poll(job) == "done"
    be.fetch(job)
    assert job.state == "failed" and "is empty" in job.detail


# ------------------------------------------------------------ simulations run apart from the tool
def test_sim_root_is_work_root_like_ldo_modeling():
    f = sitenv.sim_root({"WORK_ROOT": "/tmpdata/share/w00000001"})
    assert f.value == "/tmpdata/share/w00000001/pmukit" and f.source == "$WORK_ROOT/pmukit"
    f = sitenv.sim_root({"WORK_ROOT": "/x", "PMUKIT_SIM_ROOT": "/fast/sims"})
    assert f.value == "/fast/sims" and f.source == "$PMUKIT_SIM_ROOT"
    assert not sitenv.sim_root({}).ok                    # the desk: no simulation area


def test_runs_live_under_work_root_and_data_stays_put(tmp_path, monkeypatch):
    import pathlib
    from pmukit import paths
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("PMUKIT_SIM_ROOT", raising=False)
    monkeypatch.setenv("WORK_ROOT", str(tmp_path / "work"))
    assert paths.runs_dir("demo") == pathlib.Path(tmp_path / "work" / "pmukit" / "demo" / "runs")
    assert paths.project_dir("demo") == tmp_path / "data" / "demo"     # ledger, dataset, deliver
    monkeypatch.delenv("WORK_ROOT")
    assert paths.runs_dir("demo") == tmp_path / "data" / "demo" / "runs"


def test_runner_puts_its_run_dirs_in_the_simulation_area(tmp_path, monkeypatch):
    from pmukit.ledger import Ledger
    from pmukit.runner import Runner
    from types import SimpleNamespace as NS
    monkeypatch.setenv("PMUKIT_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.delenv("PMUKIT_SIM_ROOT", raising=False)
    led = Ledger(tmp_path / "runs.sqlite")
    plan = NS(runs=lambda enabled_only=True: [])
    r = Runner("demo", plan, led, SiteConfig(engine="fake"), backend=NS(name="fake"))
    assert r.root == tmp_path / "work" / "pmukit" / "demo" / "runs"
    led.close()
