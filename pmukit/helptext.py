"""The help text, written once and served twice.

`pmukit help <screen>` prints exactly what the web shell's `? Help` panel shows and exactly what
`GET /api/help/<screen>` returns (UX_RULES: "`pmukit help <screen>` 打印同一段文字").  Both the CLI
and the server import this module -- there is no second copy to drift.

Each screen carries three sentences, its own shortcuts, and the global ones.  The wording is aimed
at the primary user: a sub-block designer for whom the LDO is a black box, who is asking one
question -- can I trust this model in my simulation?
"""
from __future__ import annotations

SCREENS = ("home", "new", "plan", "run", "model", "deliver", "digest", "states")

GLOBAL_KEYS = (
    ("?", "this help panel"),
    ("Ctrl K", "command palette"),
    ("0 .. 5", "jump to a screen"),
    ("Ctrl Enter", "the screen's main button"),
    ("Ctrl Z", "undo a configuration change"),
    ("Esc", "close a panel or menu"),
)

HELP: dict[str, dict] = {
    "home": {
        "title": "Home -- your projects",
        "lines": [
            "Every project you have characterized, and which step it stopped at.",
            "The machine row probes the simulator, the queue, the PDK and the licence, each with "
            "its own timeout, so a dead queue never hangs the page.",
            "Pick two delivered versions to see exactly what changed between them -- provenance, "
            "validity envelope, files and grades.",
        ],
        "keys": (("n", "new project"), ("Enter", "open the selected project"),
                 ("d", "compare two deliverables")),
        "cli": "pmukit list",
    },
    "new": {
        "title": "New -- your testbench, read back to you",
        "lines": [
            "Hand over one Spectre netlist exported at the nominal corner; pmukit reads the pin "
            "roles out of it from the source names (IL_ rail, VB_ bias, VS_ supply, VEN_ enable).",
            "Answer three questions: which corners and temperatures and VSET codes, what your own "
            "module draws, and how high in frequency you care -- everything else is derived.",
            "A pin with no convention source is reported, never guessed: right-click it to assign "
            "a role and pmukit writes the source for you, or mark it ignore.",
        ],
        "keys": (("m / s / i", "set the selected pin to model / stub / ignore"),
                 ("right-click", "every verb for the pin"),
                 ("Ctrl Z", "undo the last configuration change")),
        "cli": "pmukit new <project> --netlist <file> --pmu-inst <name>",
    },
    "plan": {
        "title": "Plan -- what will run, and why",
        "lines": [
            "Each row is a group of simulations that share a purpose; the Why panel names the "
            "parameters that would go unmeasured without it.",
            "Runs that one simulation can produce together are already merged -- one supply "
            "injection reads every port -- so this is close to the smallest set that answers the "
            "model spec.",
            "Untick a group and the consequences panel lists, in red, exactly what will be "
            "reported NOT RUN in the final report.",
        ],
        "keys": (("space", "tick or untick the selected group"),
                 ("r", "show that group's runs and recipes"),
                 ("Ctrl Enter", "submit")),
        "cli": "pmukit plan <project>",
    },
    "run": {
        "title": "Run -- the ledger",
        "lines": [
            "One row per simulation, with its status, its cell, what it cost and what it feeds.",
            "A run whose content hash matches a finished one is skipped from cache -- re-running a "
            "plan after a crash costs nothing for the parts that already succeeded.",
            "A failed run keeps its log and its recipe: the recipe shows the exact lines pmukit "
            "changed in your netlist, marked ~ edited, + added, - stripped.",
        ],
        "keys": (("l", "stream the log"), ("t", "retry"), ("k", "kill"), ("s", "skip"),
                 ("c", "copy failure bundle for the desk")),
        "cli": "pmukit status <project>",
    },
    "model": {
        "title": "Model -- can I trust this?",
        "lines": [
            "The four tiles at the top answer the only question that matters: inside which loads, "
            "temperatures, frequencies and corners this model is backed by measurement.",
            "Green / yellow / red per corner per rail, and per block underneath; the curves "
            "overlay the model on the measurement at the same frequency points.",
            "The model curve is computed analytically from the fitted parameters -- drawing it "
            "never launches a simulator, so it is instant and it is exactly what gets emitted.",
        ],
        "keys": (("g", "grades"), ("b", "blocks"), ("c", "curve"),
                 ("d", "copy for desk"), ("v", "run the HB health check")),
        "cli": "pmukit verify <project>",
    },
    "deliver": {
        "title": "Deliver -- the files you take away",
        "lines": [
            "One .scs library with a section per process corner, each including its own .va; add "
            "one include line to your corner setup and switch corners by section name.",
            "envelope.json is the validity envelope and report.md says, in the first paragraph, "
            "what the model is good for and what was never run.",
            "Every .va repeats the provenance in its header, so a file that leaves this directory "
            "can still be traced back to the configuration and the data it came from.",
        ],
        "keys": (("Enter", "preview a file"), ("p", "copy the include line")),
        "cli": "pmukit deliver <project>",
    },
    "digest": {
        "title": "Copy for desk -- the air-gap return path",
        "lines": [
            "The box has no network and no agent, so the way back to the desk is plain text you "
            "paste.",
            "Pick the blocks you need and a budget; anything that does not fit is dropped by "
            "priority and NAMED in the trailer -- a digest is never silently truncated.",
            "At the desk, `pmukit digest import` rebuilds a dataset subset and `pmukit reproduce "
            "--from-digest` refits and compares the numbers one by one.",
        ],
        "keys": (("space", "include or exclude a block"), ("1 / 2 / 3", "32 / 64 / 128 KB"),
                 ("c", "copy this part")),
        "cli": "pmukit digest <project> --budget 64000",
    },
    "states": {
        "title": "Screen states",
        "lines": [
            "Every screen has four states: empty, loading, error and partial.",
            "Loading shows skeleton rows and, after two seconds, a line saying what is happening.",
            "An error never clears the table it came from: the four parts are What, Why, Do and "
            "Where, and the Do actions are buttons.",
        ],
        "keys": (),
        "cli": "",
    },
}


def render(screen: str) -> str:
    """The plain-text help for one screen -- what `pmukit help <screen>` prints."""
    key = (screen or "").strip().lower()
    if key not in HELP:
        known = ", ".join(SCREENS)
        return f"No help for '{screen}'. Screens: {known}\n"
    h = HELP[key]
    out = [h["title"], "=" * len(h["title"]), ""]
    out += [f"  {line}" for line in h["lines"]]
    if h["keys"]:
        out += ["", "  Keys on this screen:"]
        out += [f"    {k:<12} {v}" for k, v in h["keys"]]
    out += ["", "  Everywhere:"]
    out += [f"    {k:<12} {v}" for k, v in GLOBAL_KEYS]
    if h["cli"]:
        out += ["", f"  From the shell:  {h['cli']}"]
    return "\n".join(out) + "\n"


def as_dict(screen: str) -> dict:
    """The same content as JSON, for `GET /api/help/<screen>`."""
    key = (screen or "").strip().lower()
    h = HELP.get(key)
    if h is None:
        return {"screen": key, "known": list(SCREENS), "text": render(key)}
    return {"screen": key, "title": h["title"], "lines": list(h["lines"]),
            "keys": [list(k) for k in h["keys"]],
            "global_keys": [list(k) for k in GLOBAL_KEYS],
            "cli": h["cli"], "text": render(key)}
