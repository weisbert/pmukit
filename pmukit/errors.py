"""The four-part error, shared by the CLI, the web API and the library.

UX_RULES: every error carries What / Why / Do / Where -- none may be omitted.
`PmuError.to_dict()` is exactly the body the web shell returns:
    {"error": {"what": ..., "why": ..., "do": [...], "where": ...}}
plus, rarely, the keys of `extra` (data a screen can act on, never a fifth sentence).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PmuError(Exception):
    """A user-facing failure. Never raise a bare ValueError across a module boundary."""

    what: str
    """One sentence: what happened."""
    why: str
    """One sentence: the mechanism -- state the mechanism, never a guess."""
    do: list[str] = field(default_factory=list)
    """One or two concrete next actions the user can take (or click)."""
    where: str = ""
    """File, line, log path or route that anchors the failure."""
    extra: dict = field(default_factory=dict, compare=False, repr=False)
    """Optional machine-readable data a screen can act on (e.g. the PMU instance candidates the
    New screen turns into a picker). Added to the error body only when non-empty."""

    def __post_init__(self) -> None:
        if isinstance(self.do, str):
            self.do = [self.do]
        if not self.what or not self.why or not self.do:
            raise AssertionError("PmuError needs all of what/why/do (where may be empty)")
        Exception.__init__(self, self.what)

    def to_dict(self) -> dict:
        err = {"what": self.what, "why": self.why, "do": list(self.do), "where": self.where}
        if self.extra:
            err.update({k: v for k, v in self.extra.items() if k not in err})
        return {"error": err}

    def __str__(self) -> str:  # CLI rendering
        lines = [f"What : {self.what}", f"Why  : {self.why}"]
        for i, d in enumerate(self.do):
            lines.append(f"{'Do   ' if i == 0 else '     '}: {d}")
        lines.append(f"Where: {self.where or '(n/a)'}")
        return "\n".join(lines)
