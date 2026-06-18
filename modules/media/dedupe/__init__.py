"""Native dedupe logic ported onto core (MIGRATION_PLAN §9, CTO-12).

Additive and UNUSED by the running engine — the legacy ``plex_dupefinder.py`` stays
the source of truth until a parity window proves these match. Every ported piece is
locked by a parity test against the legacy implementation before it can be wired in.
"""
