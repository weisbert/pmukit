"""Locks on the air-gap deploy pipeline (M12).

Every assertion here is a failure that already happened once on a real box:

  * a WindowsPath key in MANIFEST.json -> every file reads "missing" on Linux;
  * a CRLF in `apply` -> `set -euo pipefail\\r` -> "invalid option name";
  * a manylinux_2_28 wheel sneaking past the audit -> "GLIBC_2.28 not found" at import;
  * an incremental package that quietly ships everything (or forgets the delete list);
  * a shell syntax error in `apply` discovered only on the box.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import zipfile

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(DEPLOY))

import audit_wheels                                          # noqa: E402
import package as PKG                                        # noqa: E402
from pmukit.errors import PmuError                           # noqa: E402

TEXT_SUFFIXES = {".py", ".sh", ".md", ".txt", ".json", ".toml", ".lock", ".ps1", ".html",
                 ".js", ".css", ".cfg", ".ini", ""}


# ------------------------------------------------------------------ helpers / fixtures ------
def _find_bash():
    exe = shutil.which("bash")
    if exe:
        return exe
    for c in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe",
              r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if pathlib.Path(c).is_file():
            return c
    return None


def _fake_wheel(path: pathlib.Path, members=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("dummy/__init__.py", "# dummy\n")
        for name, blob in members:
            z.writestr(name, blob)
    return path


@pytest.fixture
def mini_repo(tmp_path):
    """A throwaway pmukit-shaped checkout: enough for the packager to stage something."""
    root = tmp_path / "repo"
    (root / "pmukit").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "deploy").mkdir()
    (root / "data").mkdir()                                   # must NEVER be packaged
    (root / "tests" / "fixtures").mkdir(parents=True)         # must NEVER be packaged
    (root / "pmukit" / "__init__.py").write_text("v = 1\n", encoding="utf-8", newline="\n")
    (root / "pmukit" / "cli.py").write_text("def main():\n    return 0\n",
                                            encoding="utf-8", newline="\n")
    (root / "tools" / "webprobe.py").write_text("# probe\n", encoding="utf-8", newline="\n")
    (root / "data" / "secret.npz").write_bytes(b"\0" * 16)
    (root / "tests" / "fixtures" / "real.npz").write_bytes(b"\0" * 16)
    (root / "README.md").write_text("# mini\n", encoding="utf-8", newline="\n")
    (root / "requirements.txt").write_text("numpy>=1.26,<3\n", encoding="utf-8", newline="\n")
    (root / "pyproject.toml").write_text('[project]\nname = "pmukit"\nversion = "9.9.9"\n',
                                         encoding="utf-8", newline="\n")
    # the real installers, so the staged package is the real shape
    for name in ("apply", "update.sh", "postinstall_check.py", "pmukit_install.sh"):
        shutil.copy2(DEPLOY / name, root / "deploy" / name)
    return root


def _build(mini_repo, out, **kw):
    """Build a package out of the mini repo without touching the real one."""
    return PKG.build(out, root=mini_repo, dry_run=True, cache=out.parent / "emptycache", **kw)


# ============================================================== MANIFEST: POSIX keys =========
def test_manifest_keys_are_posix(mini_repo, tmp_path):
    out = tmp_path / "pkg"
    man = _build(mini_repo, out)
    assert man["files"], "manifest has no files"
    for key in man["files"]:
        assert "\\" not in key, f"backslash in MANIFEST key {key!r}: every file reads 'missing' on Linux"
        assert not key.startswith("/"), key
        assert (out / key).is_file(), f"MANIFEST names {key} but it is not in the package"
    # the keys must round-trip as real relative POSIX paths
    assert "app/pmukit/__init__.py" in man["files"]
    assert man["files"]["app/pmukit/__init__.py"]["size"] == len("v = 1\n")


def test_manifest_excludes_itself_and_sha256sums(mini_repo, tmp_path):
    man = _build(mini_repo, tmp_path / "pkg")
    assert "MANIFEST.json" not in man["files"]
    assert "SHA256SUMS" not in man["files"]


def test_data_and_fixtures_never_ship(mini_repo, tmp_path):
    out = tmp_path / "pkg"
    man = _build(mini_repo, out)
    joined = "\n".join(man["files"])
    assert "data/" not in joined and "secret.npz" not in joined
    assert "tests/" not in joined and "real.npz" not in joined
    assert not (out / "app" / "data").exists()
    assert not (out / "app" / "tests").exists()


# ============================================================== LF-only text artifacts =======
def _cr_offenders(root: pathlib.Path):
    bad = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() == ".whl":
            continue
        if p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if b"\r" in p.read_bytes():
            bad.append(p.relative_to(root).as_posix())
    return bad


def test_shipped_package_is_lf_only(mini_repo, tmp_path):
    out = tmp_path / "pkg"
    _build(mini_repo, out)
    assert _cr_offenders(out) == []


def test_tar_ships_the_one_step_installer_next_to_it(mini_repo, tmp_path):
    """The box gets three files: <pkg>.tar.gz, its .sha256, and pmukit_install.sh -- the
    installer has to sit OUTSIDE the tarball, because it is what unpacks it."""
    out = tmp_path / "pkg"
    _build(mini_repo, out, make_tar=True)
    inst = tmp_path / "pmukit_install.sh"
    assert (tmp_path / "pkg.tar.gz").is_file() and (tmp_path / "pkg.tar.gz.sha256").is_file()
    assert inst.is_file()
    assert b"\r" not in inst.read_bytes()
    assert inst.read_text(encoding="utf-8") == (DEPLOY / "pmukit_install.sh").read_text(encoding="utf-8")


def test_code_package_is_the_whole_source_without_wheels(mini_repo, tmp_path):
    """The routine update: every source file (self-contained, no base to diff against) and no
    wheels, plus the requirements hash the box checks against its full install."""
    full = _build(mini_repo, tmp_path / "pkg")
    code = _build(mini_repo, tmp_path / "pkg_code", mode="code")
    assert code["mode"] == "code"
    assert code["wheels"] == [] and not (tmp_path / "pkg_code" / "wheels").exists()
    app = lambda m: {k for k in m["files"] if k.startswith("app/")}
    assert app(code) == app(full)
    assert code["requirements_input_hash"] == full["requirements_input_hash"] != ""
    assert (tmp_path / "pkg_code" / "apply").is_file()


def test_apply_guards_a_code_package():
    """No install yet -> refuse; requirements moved -> refuse; never pip, never rewrite the
    venv's dependency record."""
    text = (DEPLOY / "apply").read_text(encoding="utf-8")
    assert 'if [ "$MODE" = "code" ]; then' in text
    assert "code-only package, and there is no pmukit install" in text
    assert "requirements.txt changed since this box's full install" in text
    assert '[ "$MODE" != "code" ]' in text          # MANIFEST.deployed.json left alone


def test_one_step_installer_pins_everything_inside_its_folder():
    """Nothing in $HOME or /tmp: prefix, data, temp and pip cache all point under the folder."""
    text = (DEPLOY / "pmukit_install.sh").read_text(encoding="utf-8")
    for line in ('export PMUKIT_PREFIX="$ROOT/install"', 'export PMUKIT_DATA="$ROOT/data"',
                 'export TMPDIR="$ROOT/tmp"', "export PIP_NO_CACHE_DIR=1"):
        assert line in text, line


def test_repo_deploy_sources_are_lf_only():
    """The scar itself: a CRLF `apply` gives `set -euo pipefail\\r` -> invalid option name."""
    assert _cr_offenders(DEPLOY) == []


def test_sha256sums_is_lf_and_parses(mini_repo, tmp_path):
    out = tmp_path / "pkg"
    _build(mini_repo, out)
    raw = (out / "SHA256SUMS").read_bytes()
    assert b"\r" not in raw, "a CR in SHA256SUMS makes `sha256sum -c` fail on every line"
    import hashlib
    n = 0
    for line in raw.decode("utf-8").splitlines():
        want, rel = line.split("  ", 1)
        assert "\\" not in rel
        assert hashlib.sha256((out / rel).read_bytes()).hexdigest() == want, rel
        n += 1
    assert n == len(list(p for p in out.rglob("*") if p.is_file())) - 1   # all but SHA256SUMS


# ============================================================== wheel auditor ================
def test_auditor_rejects_2_28_and_accepts_2_17(tmp_path):
    w = tmp_path / "wheels"
    _fake_wheel(w / "numpy-2.2.6-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl")
    _fake_wheel(w / "scipy-1.16.3-cp311-cp311-manylinux_2_28_x86_64.whl")
    rows, viol = audit_wheels.audit_dir(w, max_glibc=(2, 17), arch="x86_64")
    by = {r["name"]: r for r in rows}
    good = by["numpy-2.2.6-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"]
    bad = by["scipy-1.16.3-cp311-cp311-manylinux_2_28_x86_64.whl"]
    assert good["ok"] and good["min_glibc"] == (2, 17), good
    assert not bad["ok"] and "2.28" in bad["verdict"], bad
    assert [r["name"] for r in viol] == [bad["name"]]


@pytest.mark.parametrize("name,ok", [
    ("numpy-1.26.4-cp311-cp311-manylinux_2_17_x86_64.whl", True),
    ("numpy-1.26.4-cp311-cp311-manylinux2014_x86_64.whl", True),
    ("pytest-9.1.1-py3-none-any.whl", True),
    ("colorama-0.4.6-py2.py3-none-any.whl", True),
    ("numpy-1.26.4-cp311-cp311-manylinux_2_31_x86_64.whl", False),
    ("numpy-1.26.4-cp311-cp311-manylinux_2_34_x86_64.whl", False),
    ("numpy-1.26.4-cp311-cp311-manylinux2_28_x86_64.whl", False),      # typo'd spelling
    ("numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.whl", False),     # wrong ABI
    ("numpy-1.26.4-cp311-cp311-musllinux_1_1_x86_64.whl", False),      # musl
    ("numpy-1.26.4-cp311-cp311-linux_x86_64.whl", False),              # not portable
    ("numpy-1.26.4-cp311-cp311-win_amd64.whl", False),                 # wrong OS
    ("numpy-1.26.4-cp311-cp311-manylinux_2_17_aarch64.whl", False),    # wrong arch
])
def test_auditor_tag_table(tmp_path, name, ok):
    w = tmp_path / name.replace(".whl", "") / "wheels"
    _fake_wheel(w / name)
    rows, viol = audit_wheels.audit_dir(w, max_glibc=(2, 17), arch="x86_64")
    assert rows[0]["ok"] is ok, rows[0]["verdict"]
    assert (len(viol) == 0) is ok


def test_auditor_rejects_none_any_with_compiled_extension(tmp_path):
    """A 'pure python' wheel that carries a .so is a glibc landmine the tag does not show."""
    w = tmp_path / "wheels"
    _fake_wheel(w / "sneaky-1.0-py3-none-any.whl", members=[("sneaky/_c.so", b"\x7fELF\x00")])
    rows, viol = audit_wheels.audit_dir(w, max_glibc=(2, 17))
    assert not rows[0]["ok"] and "compiled extension" in rows[0]["verdict"], rows[0]
    assert viol


def test_auditor_deep_scan_catches_a_lying_filename(tmp_path):
    """Filename says manylinux_2_17; the ELF inside references GLIBC_2.28."""
    w = tmp_path / "wheels"
    blob = b"\x7fELF" + b"\x00" * 32 + b"GLIBC_2.2.5\x00GLIBC_2.28\x00"
    _fake_wheel(w / "liar-1.0-cp311-cp311-manylinux_2_17_x86_64.whl",
                members=[("liar/_c.so", blob)])
    shallow, _ = audit_wheels.audit_dir(w, max_glibc=(2, 17))
    assert shallow[0]["ok"], "tag-only audit cannot see inside -- that is why --deep exists"
    deep, viol = audit_wheels.audit_dir(w, max_glibc=(2, 17), deep=True)
    assert not deep[0]["ok"] and "GLIBC_2.28" in deep[0]["verdict"], deep[0]
    assert viol


def test_auditor_cli_exit_codes(tmp_path, capsys):
    w = tmp_path / "wheels"
    _fake_wheel(w / "numpy-2.2.6-cp311-cp311-manylinux_2_17_x86_64.whl")
    assert audit_wheels.main([str(w), "--explain"]) == 0
    assert "1/1 PASS" in capsys.readouterr().out
    _fake_wheel(w / "scipy-1.16.3-cp311-cp311-manylinux_2_28_x86_64.whl")
    assert audit_wheels.main([str(w)]) == 1
    out = capsys.readouterr().out
    assert "1/2 PASS" in out and "AUDIT FAIL" in out


def test_auditor_missing_dir_is_a_pmuerror(tmp_path):
    with pytest.raises(PmuError) as e:
        audit_wheels.main([str(tmp_path / "nope")])
    assert e.value.do and e.value.what


# ============================================================== incremental packaging ========
def test_incremental_ships_only_changes_plus_delete_list(mini_repo, tmp_path):
    full_out = tmp_path / "pkg"
    _build(mini_repo, full_out)

    # one file edited, one added, one removed
    (mini_repo / "pmukit" / "cli.py").write_text("def main():\n    return 1\n",
                                                 encoding="utf-8", newline="\n")
    (mini_repo / "pmukit" / "brand_new.py").write_text("x = 2\n", encoding="utf-8", newline="\n")
    os.remove(mini_repo / "tools" / "webprobe.py")

    incr_out = tmp_path / "pkg_i"
    man = PKG.build(incr_out, root=mini_repo, mode="incremental", prev=full_out,
                    dry_run=True, cache=tmp_path / "emptycache")

    shipped = {k for k in man["files"] if k.startswith("app/")}
    assert shipped == {"app/pmukit/cli.py", "app/pmukit/brand_new.py"}, shipped
    assert man["deleted"] == ["app/tools/webprobe.py"], man["deleted"]
    assert man["mode"] == "incremental"
    # unchanged files must NOT be re-shipped
    assert not (incr_out / "app" / "pmukit" / "__init__.py").exists()
    assert not (incr_out / "app" / "README.md").exists()
    # and an incremental carries no wheels at all
    assert man["wheels"] == [] and not (incr_out / "wheels").exists()
    # it still carries the installers, so `bash apply` works from inside it
    assert (incr_out / "apply").is_file() and (incr_out / "update.sh").is_file()


def test_incremental_refuses_when_requirements_moved(mini_repo, tmp_path):
    full_out = tmp_path / "pkg"
    _build(mini_repo, full_out)
    (mini_repo / "requirements.txt").write_text("numpy>=1.26,<3\nscipy>=1.11,<2\n",
                                                encoding="utf-8", newline="\n")
    with pytest.raises(PmuError) as e:
        PKG.build(tmp_path / "pkg_i", root=mini_repo, mode="incremental", prev=full_out,
                  dry_run=True, cache=tmp_path / "emptycache")
    assert "full" in " ".join(e.value.do).lower()


def test_incremental_without_previous_is_a_pmuerror(tmp_path):
    with pytest.raises(PmuError):
        PKG.load_manifest(tmp_path / "does_not_exist")


# ============================================================== shell scripts ================
BASH = _find_bash()


@pytest.mark.skipif(BASH is None, reason="no bash on this machine")
@pytest.mark.parametrize("script", ["apply", "update.sh", "dryrun_manylinux2014.sh",
                                    "pmukit_install.sh"])
def test_shell_scripts_parse(script):
    r = subprocess.run([BASH, "-n", str(DEPLOY / script)], capture_output=True, text=True)
    assert r.returncode == 0, f"{script}:\n{r.stderr}"


@pytest.mark.skipif(BASH is None, reason="no bash on this machine")
def test_apply_refuses_a_directory_that_is_not_a_package(tmp_path):
    shutil.copy2(DEPLOY / "apply", tmp_path / "apply")
    r = subprocess.run([BASH, str(tmp_path / "apply")], capture_output=True, text=True,
                       cwd=str(tmp_path))
    assert r.returncode == 1
    assert "What :" in r.stderr and "Do   :" in r.stderr, r.stderr


def test_apply_prints_tcsh_not_bash_env_lines():
    text = (DEPLOY / "apply").read_text(encoding="utf-8")
    assert "setenv PATH" in text and "setenv PMUKIT_DATA" in text
    assert "export PMUKIT_DATA=" not in text, "the box is tcsh; 'export VAR=' is a syntax error there"


def test_no_customer_prefix_leaked():
    """The old repo's install prefix was a real customer path. The default must be neutral."""
    for p in sorted(DEPLOY.rglob("*")):
        if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES:
            text = p.read_text(encoding="utf-8", errors="replace")
            assert "/opt/ldo_modeler" not in text, p
            assert "/data/RFIC" not in text, p
    text = (DEPLOY / "apply").read_text(encoding="utf-8")
    assert 'PREFIX="${PMUKIT_PREFIX:-$HOME/pmukit}"' in text


def test_launchers_are_installed_atomically():
    """temp + mv, never an in-place cp: a running launcher must not be truncated under itself."""
    for name in ("apply", "update.sh"):
        text = (DEPLOY / name).read_text(encoding="utf-8")
        assert "install_launcher" in text, name
        assert 'mv -f "$tmp"' in text, name


def test_no_qt_anywhere_in_deploy():
    """The old pipeline shipped PyQt5. Dropping it is the point of this milestone."""
    for p in sorted(DEPLOY.rglob("*")):
        if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES:
            low = p.read_text(encoding="utf-8", errors="replace").lower()
            for token in ("pyqt5", "qt_qpa_platform", "requirements-gui"):
                assert token not in low, f"{p.name} still mentions {token}"
