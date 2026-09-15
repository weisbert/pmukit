#!/usr/bin/env python3
"""Generate the LOCAL, git-ignored `.pmukit-denylist` from a real project's files.

The denylist is what the pre-commit guard (tools/guard.py) refuses to let into this PUBLIC repo:
customer library / cell / net / source names and project identifiers. It never leaves the machine.

    python tools/make_denylist.py --manifest <old_repo>/cadence/insitu/manifests/REAL_*.json \
                                  --extra Hi1108 WuR ... --out .pmukit-denylist

Reads an LDO_modeling-style manifest (dut.lib/cell/tb_lib/tb_cell/extract_cell, every role's
pin/net/source names) and adds the --extra tokens. Tokens shorter than 3 chars are dropped.
"""
import argparse
import json
import pathlib


def tokens_from_manifest(path):
    m = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    out = set()
    d = m.get("dut", {})
    for k in ("lib", "cell", "tb_lib", "tb_cell", "extract_cell", "tb_inst"):
        if d.get(k):
            out.add(d[k])
    for role in ("supplies", "v_out", "i_out", "bias"):
        for v in (m.get(role) or {}).values():
            for key in ("pin", "tb_src", "src", "probe_src"):
                if v.get(key):
                    out.add(v[key])
            net = str(v.get("net", "")).replace("<net:", "").rstrip(">")
            if net:
                out.add(net)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", action="append", default=[], help="real manifest JSON (repeatable)")
    ap.add_argument("--extra", nargs="*", default=[], help="extra tokens (project codes, package names)")
    ap.add_argument("--out", default=".pmukit-denylist")
    a = ap.parse_args()
    toks = set(a.extra)
    for mp in a.manifest:
        toks |= tokens_from_manifest(mp)
    toks = sorted(t for t in toks if t and len(t) >= 3)
    pathlib.Path(a.out).write_text("\n".join(toks) + "\n", encoding="utf-8")
    print(f"{len(toks)} tokens -> {a.out}  (keep this file OUT of git; .gitignore already lists it)")


if __name__ == "__main__":
    main()
