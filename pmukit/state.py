"""Per-project UI state: where the user is, what they typed, what they can undo.

This is the web shell's own little file, one per project, next to the real artifacts:

    $PMUKIT_DATA/<project>/state.json

It holds only what the *screens* need to come back looking the same: the current screen, the
three intake answers as typed, the plan's tick state, the last background job, and a short
activity list for Home. Nothing here is authoritative -- the config, the ledger and the dataset
are. A missing file is a brand-new project, never an error.

Undo (UX_RULES: "configuration changes are undoable, submitted runs are not"):
the config snapshots live in `config.ConfigHistory` -- this module does NOT reimplement them.
What it adds is (a) tick snapshots for the Plan screen and (b) `undo_log`, the *order* in which
the two kinds of configuration change happened, so one Ctrl-Z undoes whichever came last.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import os
import pathlib
import tempfile
from dataclasses import dataclass, field

from . import jsonio, paths
from .config import ConfigHistory
from .errors import PmuError
from .helptext import SCREENS

KIND = "pmukit.ui_state/1"
STATE_FILE = "state.json"
CONFIG_FILE = "config.json"
HISTORY_FILE = "config_history.json"

__all__ = ["UiState", "SCREENS", "SCREEN_INDEX", "state_dir_projects"]
"""`SCREENS` is re-exported from `helptext` -- one list, no second copy to drift."""

SCREEN_INDEX = {"home": 0, "new": 1, "plan": 2, "run": 3, "model": 4, "deliver": 5}
"""Keys 0..5 in the UI. `digest` and `states` are reached from buttons, not from a digit."""

UNDO_KINDS = ("config", "plan_ticks")
RECENT_CAP = 24
TICK_CAP = 40


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _err(what: str, why: str, do, where: str) -> PmuError:
    return PmuError(what=what, why=why, do=list(do), where=where)


def _atomic_write(path: pathlib.Path, text: str) -> None:
    """Write LF text so that a kill at any moment leaves either the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class UiState:
    """What the shell remembers about one project between page loads."""

    project: str
    screen: str = "new"
    answers: dict = field(default_factory=dict)
    """The three intake answers *as typed* -- staging for the config, so a half-filled form
    survives a reload. Keys: cells (corners/temps/vset), loads (per rail), fmax."""
    plan_ticks: dict = field(default_factory=dict)
    """group id -> bool. Absent id means 'enabled', which is the compiler's default."""
    plan_batch: dict = field(default_factory=dict)
    """The Plan screen's batch: {"temps": [...], "corners": [...]}, a key absent = all of them
    (plan.Plan.batch). Undone with the ticks: one Plan selection, one Ctrl-Z stack."""
    last_job: str = ""
    recent: list = field(default_factory=list)
    """Newest first: [{"at", "text", "screen"}] -- Home's activity list."""
    netlist: str = ""
    """Path of the netlist last parsed, so New comes back loaded."""
    undo_log: list = field(default_factory=list)
    """Newest last: which kind of configuration change happened, so one Ctrl-Z picks the right
    stack. Entries: {"kind", "at", "note"}."""
    tick_history: list = field(default_factory=list)
    """Newest last: the Plan selection *before* each change, {"ticks", "batch"}, paired with
    the 'plan_ticks' entries of undo_log."""
    exists: bool = False
    """False when state.json was never written: a brand-new project, not an error."""
    updated_at: str = ""

    _root = None
    """Data-root override (tests). Not a field: it never gets written to state.json."""

    # ------------------------------------------------------------------ paths
    @staticmethod
    def path_for(project: str, root=None) -> pathlib.Path:
        base = pathlib.Path(root) if root is not None else paths.data_root()
        return base / project / STATE_FILE

    @property
    def path(self) -> pathlib.Path:
        return self.path_for(self.project, self._root)

    @property
    def dir(self) -> pathlib.Path:
        return self.path.parent

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls, project: str, root=None) -> "UiState":
        """Read the state of `project`. A missing file yields a fresh, unsaved state."""
        if not isinstance(project, str) or not project.strip():
            raise _err("no project name given.",
                       "Every screen below Home belongs to exactly one project directory "
                       "under $PMUKIT_DATA.",
                       ["Open a project from Home, or create one with `pmukit new <name>`"],
                       "UiState.load")
        st = cls(project=project)
        st._root = root
        p = cls.path_for(project, root)
        if not p.is_file():
            return st
        try:
            d = jsonio.read(p)
        except (OSError, ValueError) as exc:
            raise _err(f"could not read the UI state of {project}.",
                       f"{p} is not valid UTF-8 JSON: {exc}.",
                       [f"Delete {p} -- it is only the screen position, nothing is lost",
                        "The config, ledger and dataset are untouched by this file"],
                       str(p)) from None
        return cls.from_dict(d, project=project, root=root)

    @classmethod
    def from_dict(cls, d, *, project: str = "", root=None) -> "UiState":
        d = d if isinstance(d, dict) else {}
        screen = str(d.get("screen") or "new")
        st = cls(project=str(d.get("project") or project),
                 screen=screen if screen in SCREENS else "new",
                 answers=dict(d.get("answers") or {}),
                 plan_ticks={str(k): bool(v) for k, v in (d.get("plan_ticks") or {}).items()},
                 last_job=str(d.get("last_job") or ""),
                 recent=list(d.get("recent") or []),
                 netlist=str(d.get("netlist") or ""),
                 undo_log=list(d.get("undo_log") or []),
                 plan_batch=_batch(d.get("plan_batch")),
                 tick_history=[_selection(e) for e in (d.get("tick_history") or [])],
                 exists=True,
                 updated_at=str(d.get("updated_at") or ""))
        st._root = root
        return st

    def to_dict(self) -> dict:
        return {"kind": KIND, "project": self.project, "screen": self.screen,
                "answers": self.answers, "plan_ticks": self.plan_ticks,
                "plan_batch": self.plan_batch,
                "last_job": self.last_job, "recent": self.recent[:RECENT_CAP],
                "netlist": self.netlist, "undo_log": self.undo_log[-TICK_CAP:],
                "tick_history": self.tick_history[-TICK_CAP:],
                "updated_at": self.updated_at or _now()}

    def save(self) -> pathlib.Path:
        self.updated_at = _now()
        text = _json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        _atomic_write(self.path, text)
        self.exists = True
        return self.path

    # ------------------------------------------------------------------ screen + activity
    def go(self, screen: str) -> "UiState":
        if screen not in SCREENS:
            raise _err(f"there is no screen called {screen!r}.",
                       "The shell has exactly these screens: " + ", ".join(SCREENS) + ".",
                       [f"Use one of: {', '.join(SCREENS)}"], "UiState.go")
        self.screen = screen
        return self

    def note(self, text: str, screen: str = "") -> "UiState":
        """Record one line for Home's 'recent activity'. Newest first, capped."""
        self.recent.insert(0, {"at": _now(), "text": str(text), "screen": screen or self.screen})
        del self.recent[RECENT_CAP:]
        return self

    # ------------------------------------------------------------------ undo
    def history(self) -> ConfigHistory:
        """The shared config snapshot stack -- config.ConfigHistory, not a second copy."""
        return ConfigHistory(self.dir / HISTORY_FILE)

    def record_config_change(self, note: str = "") -> "UiState":
        """Call AFTER pushing the new config into ConfigHistory."""
        self.undo_log.append({"kind": "config", "at": _now(), "note": str(note)})
        del self.undo_log[:-TICK_CAP]
        return self

    def set_ticks(self, ticks: dict, note: str = "", *, batch: dict | None = None) -> "UiState":
        """Replace the plan tick state (and, given, the batch), remembering the previous
        selection for Ctrl-Z."""
        self.tick_history.append({"ticks": dict(self.plan_ticks), "batch": dict(self.plan_batch)})
        self.undo_log.append({"kind": "plan_ticks", "at": _now(), "note": str(note)})
        del self.tick_history[:-TICK_CAP]
        del self.undo_log[:-TICK_CAP]
        self.plan_ticks = {str(k): bool(v) for k, v in (ticks or {}).items()}
        if batch is not None:
            self.plan_batch = _batch(batch)
        return self

    def undoable(self) -> str:
        """Which kind of change one Ctrl-Z would undo, or '' when there is nothing to undo."""
        if not self.undo_log:
            return ""
        kind = str(self.undo_log[-1].get("kind") or "")
        if kind == "plan_ticks":
            return "plan_ticks" if self.tick_history else ""
        if kind == "config":
            return "config" if len(self.history().entries()) >= 2 else ""
        return ""

    def undo(self):
        """Undo the newest configuration change.

        Returns (kind, payload): ("config", ProjectConfig) or ("plan_ticks", dict).
        Submitted runs are never undone -- they are skipped or killed, per UX_RULES.
        """
        kind = self.undoable()
        if not kind:
            raise _err("nothing to undo.",
                       "The undo stack holds only configuration changes -- the three answers and "
                       "the plan ticks. A submitted run is never undone.",
                       ["Change the configuration or a plan tick first",
                        "To stop work already submitted, use Skip or Kill on the Run screen"],
                       str(self.path))
        while self.undo_log and str(self.undo_log[-1].get("kind")) != kind:
            self.undo_log.pop()
        if self.undo_log:
            self.undo_log.pop()
        if kind == "plan_ticks":
            was = self.tick_history.pop()
            self.plan_ticks, self.plan_batch = dict(was["ticks"]), dict(was["batch"])
            return "plan_ticks", dict(self.plan_ticks)
        cfg = self.history().undo()
        return "config", cfg


def _batch(d) -> dict:
    """A stored batch, cleaned: only `temps` (numbers) and `corners` (names), each a list."""
    d = d if isinstance(d, dict) else {}
    out = {}
    if isinstance(d.get("temps"), list):
        out["temps"] = [float(t) for t in d["temps"]]
    if isinstance(d.get("corners"), list):
        out["corners"] = [str(c) for c in d["corners"]]
    return out


def _selection(e) -> dict:
    """A tick_history entry: {"ticks", "batch"}; one written before the batch existed is the
    bare tick map."""
    if isinstance(e, dict) and isinstance(e.get("ticks"), dict) and "batch" in e:
        return {"ticks": {str(k): bool(v) for k, v in e["ticks"].items()},
                "batch": _batch(e.get("batch"))}
    return {"ticks": {str(k): bool(v) for k, v in (e if isinstance(e, dict) else {}).items()},
            "batch": {}}


def state_dir_projects(root=None) -> list:
    """Every project directory under $PMUKIT_DATA, newest touched first.

    A project *is* a folder: delete the folder and it is gone from Home. A folder without a
    config.json is still listed -- that is the 'netlist loaded, plan not built' case.
    """
    base = pathlib.Path(root) if root is not None else paths.data_root()
    if not base.is_dir():
        return []
    out = []
    for d in base.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        try:
            mtime = max([d.stat().st_mtime] +
                        [(d / n).stat().st_mtime for n in (STATE_FILE, CONFIG_FILE)
                         if (d / n).is_file()])
        except OSError:                                    # pragma: no cover - fs race
            mtime = 0.0
        out.append({"name": d.name, "path": str(d), "mtime": mtime})
    out.sort(key=lambda e: (-e["mtime"], e["name"]))
    return out
