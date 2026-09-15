#!/usr/bin/env python3
"""ngspice -> Spectre translator for the ground-truth LDO / current-source library.

The inputs are the synthetic transistor-level ground-truth circuits (ngspice `.lib`
subcircuits plus the two BSIM3 `.mod` cards) vendored under
`tests/fixtures/ldo_gt/ngspice/`.  The outputs are Spectre-language `.scs` files in
`tests/fixtures/ldo_gt/`, which the M5 simulation backend can run for real.

    python tools/gt_convert.py              # translate everything, write the .scs files
    python tools/gt_convert.py --check      # translate in memory, report untranslated
    python tools/gt_convert.py --check -v   # ... and list every non-trivial translation

It is written as a small line-oriented translator rather than a pile of regex
substitutions, because a netlist translator that guesses is worse than one that
refuses: every logical statement is CLASSIFIED, and anything the classifier does not
recognise is reported by name and line number instead of being passed through.
`--check` exits non-zero when anything is unclassified.

WHAT IS TRANSLATED AND WHY
--------------------------
statement
  `* ...`, `; ...`, `$ ...`     -> `// ...`          comment syntax
  `.subckt n a b p=1`           -> `subckt n (a b)` + `parameters p=1`
                                   Spectre splits nodes (parenthesised) from parameters
                                   (a separate statement inside the subcircuit).
  `.ends [n]`                   -> `ends [n]`
  `.param a=1`                  -> `parameters a=1`
  `.model m NMOS Level=8 ...`   -> kept in SPICE language, `level=49`
                                   ngspice level 8 IS BSIM3; Spectre's level 8 is a
                                   generic mos8 that REJECTS a BSIM3 card, and 49 is
                                   the level number Spectre maps to BSIM3v3.
  `.include`/`.lib`/`.end`      -> dropped with a note (the caller supplies the deck)

device instances (`name nodes... master params` in Spectre, parentheses around nodes)
  `mX d g s b mdl W=.. L=..`    -> `mX (d g s b) mdl w=.. l=..`
  `rX a b VAL`                  -> `rX (a b) resistor r=VAL`
  `cX a b VAL`                  -> `cX (a b) capacitor c=VAL`
  `lX a b VAL`                  -> `lX (a b) inductor l=VAL`
  `iX p n DC V [SIN(o a f)]`    -> `iX (p n) isource dc=V [type=sine sinedc=o ampl=a freq=f]`
  `vX p n DC V [SIN(o a f)]`    -> `vX (p n) vsource dc=V [...]`
  `xX nodes... sub`             -> `xX (nodes...) sub`
  Current direction is the same in both simulators (positive current flows from the
  first node through the source to the second), so no node swap is needed.

values
  `{expr}`                      -> `expr`      Spectre takes bare expressions in a body
  `500k`, `1meg`, `0.3u`, `2p`  -> `500e3`, `1e6`, `0.3e-6`, `2e-12`
                                   EVERY suffixed literal is expanded to an explicit
                                   exponent.  ngspice reads `meg` as 1e6 and is
                                   case-insensitive; Spectre has no `meg` and reads
                                   `M` as 1e6 but `m` as 1e-3 -- the one silent-wrong
                                   trap in this whole translation, so we remove the
                                   suffixes rather than map them.
  `version=3.3.0`               -> `version=3.3`  (Spectre wants a number)
  `vgs_max`/`vds_max`/`vbs_max` -> dropped (ngspice-only soft-limit annotations)
"""
from __future__ import annotations

import argparse
import dataclasses
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC_DEFAULT = REPO / "tests" / "fixtures" / "ldo_gt" / "ngspice"
OUT_DEFAULT = REPO / "tests" / "fixtures" / "ldo_gt"

# ngspice scale suffixes -> exponent.  Order matters: 'meg' before 'm'.
SUFFIX = [("meg", 6), ("mil", -6 + 1.4034), ("t", 12), ("g", 9), ("k", 3), ("x", 6),
          ("m", -3), ("u", -6), ("n", -9), ("p", -12), ("f", -15), ("a", -18)]
NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?([A-Za-z]+)?")
# ngspice-only BSIM3 keys that Spectre does not know
DROP_MODEL_KEYS = {"vgs_max", "vds_max", "vbs_max"}


@dataclasses.dataclass
class Note:
    file: str
    lineno: int
    kind: str          # "note" (translated, worth saying) or "UNTRANSLATED"
    text: str

    def __str__(self) -> str:
        return f"{self.file}:{self.lineno}: {self.kind}: {self.text}"


# --------------------------------------------------------------------------- #
# lexing
# --------------------------------------------------------------------------- #
def strip_comment(line: str) -> tuple[str, str]:
    """Split off a trailing ngspice inline comment (`;` or `$`)."""
    for mark in (";", "$"):
        i = line.find(mark)
        if i >= 0:
            return line[:i].rstrip(), line[i + 1:].strip()
    return line.rstrip(), ""


def logical_lines(text: str):
    """Yield (lineno, kind, code, comment) with `+` continuations folded in.

    kind is "comment" for a whole-line comment / blank, else "stmt".
    """
    pending_no = None
    pending_code = ""
    pending_cmt: list[str] = []
    for no, raw in enumerate(text.splitlines(), 1):
        s = raw.rstrip()
        if not s.strip():
            if pending_no is not None:
                yield pending_no, "stmt", pending_code, " ".join(pending_cmt)
                pending_no, pending_code, pending_cmt = None, "", []
            yield no, "comment", "", ""
            continue
        if s.lstrip().startswith("*"):
            if pending_no is not None:
                yield pending_no, "stmt", pending_code, " ".join(pending_cmt)
                pending_no, pending_code, pending_cmt = None, "", []
            yield no, "comment", "", s.lstrip()[1:]
            continue
        if s.lstrip().startswith("+"):
            code, cmt = strip_comment(s.lstrip()[1:])
            pending_code += " " + code.strip()
            if cmt:
                pending_cmt.append(cmt)
            continue
        if pending_no is not None:
            yield pending_no, "stmt", pending_code, " ".join(pending_cmt)
        code, cmt = strip_comment(s)
        pending_no, pending_code, pending_cmt = no, code.strip(), ([cmt] if cmt else [])
    if pending_no is not None:
        yield pending_no, "stmt", pending_code, " ".join(pending_cmt)


# --------------------------------------------------------------------------- #
# values
# --------------------------------------------------------------------------- #
def expand_number(m: re.Match) -> str:
    mant, exp, suf = m.group(1), m.group(2) or "", (m.group(3) or "").lower()
    if not suf:
        return mant + exp
    for name, power in SUFFIX:
        if suf.startswith(name):
            if exp:                       # e.g. "1e3k" -- ngspice allows it, we refuse
                raise ValueError(f"number with both exponent and suffix: {m.group(0)}")
            return f"{mant}e{int(power):+d}".replace("e+", "e")
    raise ValueError(f"unknown scale suffix in {m.group(0)!r}")


def convert_value(s: str) -> str:
    """`{expr}` -> `expr`, and every suffixed literal -> explicit exponent."""
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1].strip()
    if "{" in s or "}" in s:
        raise ValueError(f"nested or unbalanced braces in {s!r}")
    return NUM_RE.sub(expand_number, s)


# --------------------------------------------------------------------------- #
# statements
# --------------------------------------------------------------------------- #
def split_nodes_params(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Leading tokens without `=` are nodes; from the first `key=value` on, parameters."""
    for i, t in enumerate(tokens):
        if "=" in t:
            return tokens[:i], tokens[i:]
    return tokens, []


def kv(tokens: list[str], lower_keys: bool = True) -> list[str]:
    out = []
    for t in tokens:
        k, _, v = t.partition("=")
        out.append(f"{k.lower() if lower_keys else k}={convert_value(v)}")
    return out


def sine_params(rest: list[str]) -> list[str]:
    """`SIN(offset ampl freq)` (possibly split across tokens) -> Spectre sine params."""
    blob = " ".join(rest)
    m = re.fullmatch(r"(?i)\s*sin\s*\((.*)\)\s*", blob)
    if not m:
        raise ValueError(f"unsupported source waveform {blob!r}")
    args = [a for a in re.split(r"[,\s]+", m.group(1).strip()) if a]
    if len(args) != 3:
        raise ValueError(f"SIN() needs offset/ampl/freq, got {args}")
    off, amp, frq = (convert_value(a) for a in args)
    return ["type=sine", f"sinedc={off}", f"ampl={amp}", f"freq={frq}"]


def convert_source(name: str, tokens: list[str], master: str) -> str:
    nodes, tokens = tokens[:2], tokens[2:]
    params: list[str] = []
    if tokens and tokens[0].lower() in ("dc",):
        params.append(f"dc={convert_value(tokens[1])}")
        tokens = tokens[2:]
    elif tokens and "=" not in tokens[0] and not tokens[0].lower().startswith("sin"):
        params.append(f"dc={convert_value(tokens[0])}")
        tokens = tokens[1:]
    if tokens and tokens[0].lower().startswith("sin"):
        params += sine_params(tokens)
        tokens = []
    params += kv(tokens)
    return f"{name} ({' '.join(nodes)}) {master} {' '.join(params)}".rstrip()


def convert_rlc(name: str, tokens: list[str], master: str, key: str) -> str:
    nodes, tokens = tokens[:2], tokens[2:]
    if not tokens:
        raise ValueError(f"{master} with no value")
    if "=" in tokens[0]:
        params = kv(tokens)
        params[0] = f"{key}=" + params[0].split("=", 1)[1]
    else:
        params = [f"{key}={convert_value(tokens[0])}"] + kv(tokens[1:])
    return f"{name} ({' '.join(nodes)}) {master} {' '.join(params)}".rstrip()


def convert_mos(name: str, tokens: list[str]) -> str:
    if len(tokens) < 5:
        raise ValueError("MOS needs 4 nodes and a model")
    nodes, model, params = tokens[:4], tokens[4], tokens[5:]
    return f"{name} ({' '.join(nodes)}) {model} {' '.join(kv(params))}".rstrip()


def convert_xinst(name: str, tokens: list[str]) -> str:
    nodes, params = split_nodes_params(tokens)
    if len(nodes) < 2:
        raise ValueError("subcircuit call needs a master name")
    master, nodes = nodes[-1], nodes[:-1]
    return f"{name} ({' '.join(nodes)}) {master} {' '.join(kv(params, lower_keys=False))}".rstrip()


DEVICES = {
    "r": lambda n, t: convert_rlc(n, t, "resistor", "r"),
    "c": lambda n, t: convert_rlc(n, t, "capacitor", "c"),
    "l": lambda n, t: convert_rlc(n, t, "inductor", "l"),
    "i": lambda n, t: convert_source(n, t, "isource"),
    "v": lambda n, t: convert_source(n, t, "vsource"),
    "m": convert_mos,
    "x": convert_xinst,
}


def convert_model(tokens: list[str], notes, fname, lineno) -> list[str]:
    """`.model name TYPE key=val ...` -> a SPICE-language `.model` with level=49."""
    name, mtype, rest = tokens[0], tokens[1].lower(), tokens[2:]
    params = []
    for t in rest:
        if "=" not in t:
            raise ValueError(f"stray token {t!r} in .model")
        k, _, v = t.partition("=")
        k = k.lower()
        if k in DROP_MODEL_KEYS:
            notes.append(Note(fname, lineno, "note", f"dropped ngspice-only model key {k}="))
            continue
        if k == "level":
            if v.strip() != "8":
                raise ValueError(f"unexpected ngspice level={v} (only BSIM3 level 8 is handled)")
            notes.append(Note(fname, lineno, "note", "level=8 (ngspice BSIM3) -> level=49 (Spectre)"))
            params.append("level=49")
            continue
        if k == "version" and v.count(".") > 1:
            short = ".".join(v.split(".")[:2])
            notes.append(Note(fname, lineno, "note", f"version={v} -> {short} (Spectre wants a number)"))
            params.append(f"version={short}")
            continue
        params.append(f"{k}={v}")
    out = [f".model {name} {mtype} " + " ".join(params[:4])]
    for i in range(4, len(params), 6):
        out.append("+ " + " ".join(params[i:i + 6]))
    return out


# --------------------------------------------------------------------------- #
# file conversion
# --------------------------------------------------------------------------- #
def convert_text(text: str, fname: str) -> tuple[str, list[Note]]:
    notes: list[Note] = []
    out: list[str] = []
    in_model = False          # a run of .model cards -> wrap in `simulator lang=spice`

    def lang(spice: bool):
        nonlocal in_model
        if spice and not in_model:
            out.append("simulator lang=spice")
            in_model = True
        elif not spice and in_model:
            out.append("simulator lang=spectre")
            in_model = False

    out.append("simulator lang=spectre")
    for lineno, kind, code, cmt in logical_lines(text):
        if kind == "comment":
            lang(False)
            out.append(("// " + cmt).rstrip() if cmt else "")
            continue
        # ngspice tolerates whitespace around `=` (`Level=        8`, `Dwg = -6e-9`);
        # tighten it first so a `key=value` is always ONE token.  Nothing in these decks
        # uses `=` for anything but assignment, so this is safe here.
        tokens = re.sub(r"\s*=\s*", "=", code).split()
        head = tokens[0]
        low = head.lower()
        tail = (" // " + cmt) if cmt else ""
        try:
            if low in (".end", ".include", ".lib", ".endl"):
                notes.append(Note(fname, lineno, "note", f"dropped deck-level statement {head}"))
                continue
            if low == ".subckt":
                lang(False)
                nodes, params = split_nodes_params(tokens[2:])
                out.append(f"subckt {tokens[1]} ({' '.join(nodes)})")
                if params:
                    out.append("parameters " + " ".join(kv(params, lower_keys=False)))
                continue
            if low == ".ends":
                lang(False)
                out.append("ends" + (f" {tokens[1]}" if len(tokens) > 1 else ""))
                continue
            if low == ".param":
                lang(False)
                out.append("parameters " + " ".join(kv(tokens[1:], lower_keys=False)))
                continue
            if low == ".model":
                lang(True)
                out.extend(convert_model(tokens[1:], notes, fname, lineno))
                continue
            handler = DEVICES.get(low[0])
            if handler is None:
                notes.append(Note(fname, lineno, "UNTRANSLATED",
                                  f"unknown statement type {head!r}: {code}"))
                out.append(f"// UNTRANSLATED: {code}")
                continue
            lang(False)
            out.append(handler(head, tokens[1:]) + tail)
        except Exception as exc:                                  # noqa: BLE001
            notes.append(Note(fname, lineno, "UNTRANSLATED", f"{code}  ({exc})"))
            out.append(f"// UNTRANSLATED: {code}")
    lang(False)

    banner = [
        "// ---------------------------------------------------------------------------",
        f"// GENERATED by tools/gt_convert.py from ngspice/{fname} -- do not hand-edit.",
        "// Synthetic ground-truth circuit; regenerate with `python tools/gt_convert.py`.",
        "// ---------------------------------------------------------------------------",
    ]
    return "\n".join(banner + out).rstrip() + "\n", notes


def convert_file(src: pathlib.Path) -> tuple[str, list[Note]]:
    return convert_text(src.read_text(encoding="utf-8"), src.name)


def out_name(src: pathlib.Path) -> str:
    return src.stem + ".scs"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=pathlib.Path, default=SRC_DEFAULT,
                    help="directory of ngspice .lib/.mod sources")
    ap.add_argument("--out", type=pathlib.Path, default=OUT_DEFAULT,
                    help="directory to write .scs files into")
    ap.add_argument("--check", action="store_true",
                    help="translate in memory and report untranslated constructs; write nothing")
    ap.add_argument("-v", "--verbose", action="store_true", help="also list the applied notes")
    a = ap.parse_args(argv)

    sources = sorted(list(a.src.glob("*.lib")) + list(a.src.glob("*.mod")))
    if not sources:
        print(f"gt_convert: no .lib/.mod under {a.src}", file=sys.stderr)
        return 2

    bad = 0
    for src in sources:
        text, notes = convert_file(src)
        problems = [n for n in notes if n.kind == "UNTRANSLATED"]
        bad += len(problems)
        if not a.check:
            dst = a.out / out_name(src)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text, encoding="utf-8", newline="\n")
        status = "OK" if not problems else f"{len(problems)} UNTRANSLATED"
        print(f"{src.name:<22} -> {out_name(src):<22} {len(text.splitlines()):>4} lines  {status}")
        for n in problems:
            print("    " + str(n), file=sys.stderr)
        if a.verbose:
            for n in notes:
                if n.kind != "UNTRANSLATED":
                    print("    " + str(n))

    if bad:
        print(f"\ngt_convert: {bad} untranslated construct(s) -- nothing was guessed.",
              file=sys.stderr)
        return 1
    print(f"\ngt_convert: {len(sources)} file(s), 0 untranslated constructs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
