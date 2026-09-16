# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""GammonView Analysis — bgsage-powered backgammon analysis.

This is the *analysis* layer: it drives the bgsage engine to evaluate a match
or position, and hands the result to the pure-stdlib ``gvformat`` codec for
serialization. It depends on ``gvformat`` + ``bgsage`` (the ``[engine]`` extra);
``gvformat`` on its own has no such dependency.

Typical server use — analyze a dropped match file and get the analyzed
``.gvab`` bytes back, in one call::

    from gvanalysis import analyze_match
    data = analyze_match("match.mat", preset="world_class")   # bytes to send to the client

Or the pieces:

    from gvanalysis import analyze_mat
    from gvformat import to_ogxm_json, write_gvab, read_gvab
    result = analyze_mat("match.mat", preset="fast")
    data = write_gvab(to_ogxm_json(result))

A single position is a different scale of problem -- fast enough to answer in
a request, so it returns a plain OGXM-shaped dict rather than a file::

    from gvanalysis import analyze_position
    result = analyze_position("XGID=...", level="3ply")   # ~0.07s at 3ply

CLIs: ``gvan-match`` / ``gvan-position`` (or ``python -m gvanalysis.match``).

Note: ``analyze_mat`` / ``analyze_match`` (and the ``match`` / ``position`` /
``game_eval`` submodules) require the bgsage engine — the ``[engine]`` extra.
Importing ``gvanalysis`` itself, and the engine-free tool submodules
(``reconstruct_mat``, ``compare_gva``), works without it; the engine
requirement only surfaces — with a clear message — when you actually reach for
the analyzer.

The ``.mat`` parser/converter is *not* here: it needs no engine, so it lives in
the codec layer beside the other source-format converters — ``gvformat``'s
``convert_mat``/``mat_to_ogxm`` (and ``mat_parser`` / ``game_reconstructor``),
mirroring ``convert_xg`` and ``convert_bgf``.
"""

_ENGINE_HINT = (
    "the bgsage engine is required for this, but isn't installed. Add the "
    "analysis extra:\n"
    "    pip install 'gammonview[engine]'      (or, in this repo: uv sync)\n"
    "The pure-codec layer `gvformat` needs no engine."
)


def _load_match():
    """Import the engine-backed ``match`` module, or raise a clear, actionable
    error when the bgsage engine (the ``[engine]`` extra) isn't installed."""
    try:
        from . import match
    except ModuleNotFoundError as e:
        if (e.name or "") in ("bgsage", "bgbot_cpp") or (e.name or "").startswith("bgsage."):
            raise ModuleNotFoundError(f"gvanalysis: {_ENGINE_HINT}") from e
        raise
    return match


def analyze_match(input_path, *, preset: str | None = None, quiet: bool = True, **kwargs) -> bytes:
    """Analyze a match file and return the analyzed ``.gvab`` bytes.

    Returns **bytes**, not a dict: this is the one call that hands back the
    storable artifact. ``analyze_file`` is the same work stopping one step
    earlier, at the OGXM document -- ``analyze_match(p)`` is exactly
    ``write_gvab(analyze_file(p))``, and ``read_gvab`` of these bytes is that
    document back.

    One call for the common server path: a match in (``.mat``, ``.gva``/
    ``.ogxm``, or ``.gvab`` — OGXM is the internal representation, so ``.mat``
    is converted first), canonical ``.gvab`` bytes out, carrying the match
    *and* the analysis this call just ran (ready to store and ship to the
    client). An OGXM input's existing analysis blocks are preserved and ours is
    appended. Equivalent to ``write_gvab(analyze_file(...))``. Requires the
    ``[engine]`` extra; without it, raises a ``ModuleNotFoundError`` explaining
    how to install.

    Extra keyword arguments go to ``analyze_ogxm``; notably
    ``on_progress=lambda done, total: ...`` reports decision-level progress, for
    a server driving a progress bar in a client.
    """
    analyze_file = _load_match().analyze_file
    from gvformat import write_gvab
    return write_gvab(analyze_file(input_path, preset=preset, quiet=quiet, **kwargs))


#: The former name. It read as a format conversion and named an input format
#: this call never required, so the analysis it runs was invisible in it; kept
#: working because callers exist.
mat_to_gvab = analyze_match


def __getattr__(name):
    # PEP 562: expose the engine-backed entry points lazily so `import
    # gvanalysis` (and the engine-free tool submodules) load without bgsage; the
    # engine requirement only bites — with a clear message — when the analyzer
    # is actually used.
    if name in ("analyze_mat", "analyze_ogxm", "analyze_file"):
        return getattr(_load_match(), name)
    # `position` imports no engine at module scope (it reaches for bgsage only
    # inside the call), so it needs none of _load_match's guarding — but it is
    # still lazy, to keep `import gvanalysis` as cheap as it has always been.
    if name == "analyze_position":
        from . import position
        return position.analyze_position
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "analyze_mat", "analyze_ogxm", "analyze_file",
    "analyze_match", "mat_to_gvab",
    "analyze_position",
]
