"""The commit gate must bite on denylisted tokens and stay quiet otherwise."""
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools import guard  # noqa: E402


def test_selftest_passes():
    assert guard.selftest() == 0


def test_word_boundary(tmp_path):
    toks = ["ACMECHIP7"]
    f = tmp_path / "a.scs"
    f.write_text("ACMECHIP7X ok\nxi (ACMECHIP7) sub\n", encoding="utf-8")
    hits = guard.scan([f], toks)
    assert len(hits) == 1, hits          # 'ACMECHIP7X' on line 1 must NOT match
    assert hits[0][1] == 2                # the bare token on line 2 must match


def test_case_insensitive(tmp_path):
    f = tmp_path / "a.scs"
    f.write_text("* acmechip7 note\n", encoding="utf-8")
    assert guard.scan([f], ["ACMECHIP7"])


def test_short_tokens_ignored(tmp_path):
    dl = tmp_path / "dl"
    dl.write_text("ab\nACMECHIP7\n", encoding="utf-8")
    toks = guard.load_tokens(dl)
    assert "ab" not in toks and "ACMECHIP7" in toks


def test_repo_is_clean():
    """Every tracked file must survive the gate against the local denylist."""
    r = subprocess.run([sys.executable, "tools/guard.py", "--all"], cwd=REPO,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
