# pmukit air-gap deployment

How pmukit gets from a networked desk onto the box (RHEL8-class, **tcsh**, no PyPI, `/opt`
not writable) and how it gets updated afterwards.

Runtime dependencies are **stdlib + numpy + scipy**. No Qt, no CDN, no third-party JavaScript:
the UI is a local `http.server` you open in the box's Firefox.

---

## What a package contains

```
<package>/
  apply                the ONE command run on the box
  update.sh            incremental refresh of an existing install
  VERSION              one line, e.g. 0.1.0+g71fd0d8
  MANIFEST.json        POSIX keys -> {sha256, size}, plus mode / wheels / delete list
  SHA256SUMS           LF; `sha256sum -c SHA256SUMS` from this directory
  requirements.lock    the exact pins resolved from the downloaded wheels   (full only)
  wheels/*.whl         cp311 / x86_64 / manylinux2014 (glibc 2.17)          (full only)
  app/                 the tool source: pmukit/, tools/, deploy/, web/, README, pyproject
```

Never packaged: `.venv`, `data/`, `work*/`, `runs/`, `deliver/`, `dist/`, `tests/`,
`.pmukit-denylist`, `__pycache__`, and anything `.gitignore` covers. Real measurements and
delivered models live in `$PMUKIT_DATA` and never cross into a package.

---

## On the desk (build)

```bash
# full package: source + cross-downloaded Linux wheels, audited against glibc 2.17
python deploy/package.py --out dist/pkg

# same, but no network at all (wheels come from the local cache filled by a previous run)
python deploy/package.py --out dist/pkg --dry-run

# code-only update against a package you already shipped: changed files + a delete list
python deploy/package.py --out dist/pkg_i --incremental dist/pkg

# also emit a single artifact to carry: dist/pkg.tar.gz + dist/pkg.tar.gz.sha256
python deploy/package.py --out dist/pkg --tar
```

Windows convenience wrapper (same thing, picks an interpreter for you). The desk needs any
Python **3.10+**, not 3.11: the box's cp311 wheels are cross-downloaded with
`--python-version 311 --platform manylinux2014_x86_64`, whatever runs pip. Only the box needs 3.11.

```powershell
.\deploy\package.ps1                      # full
.\deploy\package.ps1 -Mode incremental -Previous dist\pkg
.\deploy\package.ps1 -DryRun
```

Check the wheels by hand at any time:

```bash
python deploy/audit_wheels.py dist/pkg/wheels --explain
python deploy/audit_wheels.py dist/pkg/wheels --deep      # also scan the ELF members
bash   deploy/dryrun_manylinux2014.sh dist/pkg            # docker rehearsal, or static fallback
```

`audit_wheels.py` exits non-zero and prints an `N/N PASS` table. It rejects:

* any wheel needing glibc newer than 2.17 (`manylinux_2_28`, `_2_31`, `_2_34`, `manylinux2_28`, ...);
* any wheel for the wrong CPython / ABI (cp310, cp312, ...);
* any `none-any` wheel that secretly carries a compiled extension;
* musllinux and bare `linux_*` wheels.

`--deep` additionally scans every `.so` inside each wheel for `GLIBC_x.y` version references, so
a wheel whose *filename* claims 2.17 while its binary needs 2.28 is caught on the desk.

---

## On the box (install)

### One step, everything inside one folder (recommended)

`package.py --tar` / `package.ps1 -Tar` leave three files in `dist/`. Upload them into a folder
you made (e.g. `<workarea>/pmukit`) and run the installer from there:

```tcsh
cd <workarea>/pmukit            # holds pkg.tar.gz  pkg.tar.gz.sha256  pmukit_install.sh
bash pmukit_install.sh
source env.csh                  # every new shell; add your own setenv lines at its end
```

It checks python3.11 and the tarball's sha256, unpacks into `tmp/`, runs `apply` with
`PMUKIT_PREFIX=<folder>/install`, `PMUKIT_DATA=<folder>/data`, `TMPDIR=<folder>/tmp` and no pip
cache, writes `env.csh` / `env.sh` (once; later runs keep your edits), and reports whether anything
landed outside the folder. numpy/scipy live in `<folder>/install/.venv`; the only outside
dependency is the system `python3.11` the venv is built from. Output goes to `install.log`.
Re-run with a newer tarball to update.

**Routine updates ship no dependencies.** On the desk, `.\deploy\package.ps1 -Mode code -Tar`
(or `python deploy/package.py --code --tar`) builds `dist/pkg_code.tar.gz`: the whole source,
no wheels, ~0.5 MB. Upload it with its `.sha256` into the same folder and run
`bash pmukit_install.sh` again (it takes the newest tarball). The venv and `data/` are kept, and
`app/` is replaced whole, so files deleted on the desk disappear on the box too. Every code
package is complete on its own: skipping one, or installing an older one, leaves no hole.
It is refused when there is no install yet, and when `requirements.txt` moved since the full
install -- then ship a full package (`-Tar` without `-Mode`).

### By hand

Either copy the package over, or `git clone` the public repo — `apply` handles both and says
which one it detected.

```tcsh
# from an unpacked offline package
cd <package>
bash apply

# or from a git clone (no wheels in git; apply will say so and use the network for pip)
git clone <repo> pmukit
cd pmukit
bash deploy/apply
```

Everything lands under **one user-writable folder**:

| what | where | override |
|---|---|---|
| install prefix | `$HOME/pmukit` | `setenv PMUKIT_PREFIX /some/path` |
| real data | `$HOME/pmukit_data` | `setenv PMUKIT_DATA /some/path` |
| interpreter | `python3.11` on PATH | `setenv PMUKIT_PYTHON /path/to/python3.11` |

`apply` prints the **tcsh** lines to add to `~/.cshrc` when it finishes:

```tcsh
setenv PATH        $HOME/pmukit/bin:$PATH
setenv PMUKIT_DATA $HOME/pmukit_data
```

Then, in a new shell:

```tcsh
pmukit ui          # starts the local web shell and prints a http://... URL for Firefox
pmukit --help
```

### What `bash apply` verifies

1. **The package itself** — every path in `MANIFEST.json` exists and its sha256 matches, and
   every line of `SHA256SUMS` re-checks. A mismatch names the offending file and refuses to
   install anything. `SHA256SUMS` is also rejected if it contains a `\r`.
2. **The interpreter** — must be CPython **3.11**, because the wheels are tagged `cp311`.
3. **The prefix** — must be an absolute path.
4. **The dependency install** — offline `pip install --no-index --find-links wheels`.
5. **The browser path** — `deploy/postinstall_check.py` drives the four `tools/webprobe.py`
   checks headlessly and prints them:

   ```
   PASS  0. runtime stack (numpy + scipy + sqlite3)
   PASS  1. server alive (ping)
   PASS  2. run a command, read its output
   PASS  3. streaming (lines arrive as they happen)
   PASS  4. files (download a .va, upload text back)
   PASS  5. CLI entry point (python -m pmukit)
   ```

   If a check fails, the install still completes and says so — the tool is usable from the CLI
   while the browser path is sorted out. Re-run it any time:

   ```tcsh
   $HOME/pmukit/.venv/bin/python $HOME/pmukit/app/deploy/postinstall_check.py
   ```

### Update

An incremental package carries only what changed plus a delete list, and never touches the venv
or `$PMUKIT_DATA`:

```tcsh
cd <incremental-package>
bash apply                 # detects mode=incremental and hands off to update.sh
```

It refuses if `requirements.txt` moved since the deployed full package — that needs a full
package, because an incremental ships no wheels.

---

## Troubleshooting

Each of these is a scar, not a hypothetical.

**`apply: line 1: set: -: invalid option name` / `syntax error near unexpected token`**
The script picked up CRLF line endings (Windows). `.gitattributes` forces LF in git and the
packager rewrites every text artifact with `newline="\n"`, so this means the file was edited or
transferred through something that converted it. Fix:
`sed -i 's/\r$//' apply update.sh` then re-run.

**`ImportError: /lib64/libc.so.6: version 'GLIBC_2.28' not found`**
A wheel newer than the glibc-2.17 baseline got in. `pip download` on Windows will happily fetch
one. Re-run the packager (it cross-downloads `--platform manylinux2014_x86_64` /
`manylinux_2_17_x86_64` and audits), and if a pin has no 2.17 wheel, downpin it in
`requirements.txt`. Diagnose with `python deploy/audit_wheels.py <pkg>/wheels --deep --explain`.

**`VSET=1.8: Command not found.` / `PMUKIT_DATA=...: Command not found.`**
The box is **tcsh**, not bash. `VAR=val` is not assignment there. Use `setenv NAME value`, `$PWD`,
backticks for substitution, `|&` to merge stderr. The lines `apply` prints at the end are already
tcsh.

**`mkdir: cannot create directory '/opt/pmukit': Permission denied`**
`/opt` is not writable on the shared box, which is why nothing installs there. The prefix
defaults to `$HOME/pmukit`; set `PMUKIT_PREFIX` if you want it elsewhere, but keep it inside a
directory you own.

**`syntax error` from `apply`/`update.sh` *after* the work already succeeded**
A launcher overwrote itself while running: a plain `cp` truncates and rewrites the file whose fd
the running shell is still reading, so bash resumes at a stale byte offset. Both installers write
a temp file and `mv` it into place (rename swaps the inode, the running fd stays valid). If you
add a launcher, install it the same way.

**`sha256sum: WARNING: 1 computed checksum did NOT match`, but the file looks fine**
A `\r` in `SHA256SUMS` or in the checked file. All text artifacts are written LF; re-copy the
package rather than editing it on Windows.

**Every file reads "missing" during verification on the box**
`MANIFEST.json` keys were written as Windows paths (backslashes). The packager uses
`.as_posix()`; `apply` also normalizes backslashes defensively. If you see this, the manifest was
produced by something other than `deploy/package.py`.

**`No Python 3.11 on PATH`**
The wheels are `cp311`; any other version is refused before anything is installed.
`setenv PMUKIT_PYTHON /path/to/python3.11` and re-run.

**`No module named pmukit.__main__; 'pmukit' is a package and cannot be directly executed`**
Check 5 fails and `pmukit ui` will not start. The launcher execs `python -m pmukit`, which needs
`pmukit/__main__.py` to exist and call `pmukit.cli:main`. Everything else (venv, wheels, source,
launchers) is installed correctly; add that module and re-run `bash apply` — or just re-run
`postinstall_check.py`, since the launcher reads the source out of `$PMUKIT_PREFIX/app`.
