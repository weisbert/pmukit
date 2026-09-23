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
