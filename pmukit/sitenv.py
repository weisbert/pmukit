"""Site facts the box's own environment already knows -- each one with WHERE it came from.

The box's login scripts export most of what pmukit needs (the ALPS install, the user id, the
license server).  Reading those beats asking the user to copy them into `PMUKIT_*` variables: a
copy goes stale the day the site upgrades ALPS, the original does not.

Resolution order for every fact: an explicit `PMUKIT_*` override, then the site's own variable(s),
then `site.json`, then a default.  Each fact reports the variable it was actually read from, and
`pmukit site` prints that table, so "why is it using THAT alps" has a one-line answer.

    user        $USER, $USERNAME, $LOGNAME          (on the box $USER is the employee id)
    alps_root   $PMUKIT_ALPS_ROOT, $ALPS_ROOT, $ALPS_HOME minus /tools/alps, `which alps`
    simulator   $PMUKIT_SIMULATOR (alias $PMUKIT_CLUSTER_ENGINE), site.json simulator, 'alps'
    pdk_root    $PMUKIT_PDK_ROOT, $MODEL_ROOT, $PDK, $PDK_HOME   (the dir holding alps/ spectre/)
    account     $PMUKIT_DONAU_ACCOUNT, site.json project_account
    license     ALPS: $EMPYREAN_LICENSE_FILE; Spectre: $CDS_LIC_FILE; else $LM_LICENSE_FILE
"""
from __future__ import annotations

import os
import posixpath
import shutil
import socket
from dataclasses import dataclass

@dataclass(frozen=True)
class Fact:
    name: str
    value: str
    source: str          # "$ALPS_ROOT", "site.json", "default", "`which alps`" -- "" when missing

    @property
    def ok(self) -> bool:
        return bool(self.value)


def _env(env):
    return os.environ if env is None else env


def _first(env, name: str, *vars_: str) -> Fact:
    for v in vars_:
        val = str(env.get(v, "") or "").strip()
        if val:
            return Fact(name, val, f"${v}")
    return Fact(name, "", "")


def user(env=None) -> Fact:
    e = _env(env)
    f = _first(e, "user", "USER", "USERNAME", "LOGNAME")
    if f.ok:
        return f
    try:
        import getpass
        return Fact("user", getpass.getuser(), "getpass")
    except Exception:                              # noqa: BLE001 -- no user is not an error
        return Fact("user", "", "")


def host() -> Fact:
    try:
        return Fact("host", socket.gethostname(), "hostname")
    except OSError:
        return Fact("host", "", "")


def _root_from_home(home: str) -> str:
    """$ALPS_HOME is `<root>/tools/alps`; the wrapper lives at `<root>/bin/alps`."""
    h = home.replace("\\", "/").rstrip("/")
    if h.endswith("/tools/alps"):
        return h[: -len("/tools/alps")]
    return h


def alps_root(env=None, which=shutil.which) -> Fact:
    e = _env(env)
    f = _first(e, "alps_root", "PMUKIT_ALPS_ROOT", "ALPS_ROOT")
    if f.ok:
        return f
    home = str(e.get("ALPS_HOME", "") or "").strip()
    if home:
        return Fact("alps_root", _root_from_home(home), "$ALPS_HOME (minus /tools/alps)")
    exe = which("alps") if which else None
    if exe:
        # <root>/bin/alps -> <root>
        return Fact("alps_root", posixpath.dirname(posixpath.dirname(exe.replace("\\", "/"))),
                    "`which alps`")
    return Fact("alps_root", "", "")


def simulator(site=None, env=None) -> Fact:
    e = _env(env)
    f = _first(e, "simulator", "PMUKIT_SIMULATOR", "PMUKIT_CLUSTER_ENGINE")
    if f.ok:
        return f
    val = str(getattr(site, "simulator", "") or "").strip()
    if val:
        return Fact("simulator", val, "site config")
    return Fact("simulator", "alps", "default")


def pdk_root(env=None) -> Fact:
    return _first(_env(env), "pdk_root", "PMUKIT_PDK_ROOT", "MODEL_ROOT", "PDK", "PDK_HOME")


def account(site=None, env=None) -> Fact:
    f = _first(_env(env), "account", "PMUKIT_DONAU_ACCOUNT")
    if f.ok:
        return f
    val = str(getattr(site, "project_account", "") or "").strip()
    return Fact("account", val, "site config" if val else "")


def license_(env=None, sim: str = "alps") -> Fact:
    """The license the SITE SIMULATOR checks out: Empyrean for ALPS, Cadence for Spectre.
    (The box exports a dozen *_LICENSE_FILE vars; LM_LICENSE_FILE there is not ALPS's.)"""
    first = (("EMPYREAN_LICENSE_FILE", "CDS_LIC_FILE") if sim == "alps"
             else ("CDS_LIC_FILE", "EMPYREAN_LICENSE_FILE"))
    return _first(_env(env), "license", *first, "LM_LICENSE_FILE")


def facts(site=None, env=None) -> list[Fact]:
    """Everything `pmukit site` shows under 'read from this machine'."""
    sim = simulator(site, env)
    return [user(env), host(), sim, alps_root(env), pdk_root(env),
            account(site, env), license_(env, sim.value)]
