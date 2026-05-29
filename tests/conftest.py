"""Test bootstrap for plex_dupefinder.

The safety functions under test (get_score, select_keeper, check_file_exists,
_quarantine_logical_path, detect_inconsistencies) are pure decision logic and
never call into Plex, the network, or tabulate. So we stub those heavy
third-party imports here, letting the suite run with nothing installed beyond
pytest — including in CI and on the Unraid box.

This relies on the import being side-effect-free: plex_dupefinder defers the
Plex connection, config validation and log-file creation to its entrypoint, and
config.py falls back to built-in defaults when no config.json is present and the
session is non-interactive.
"""
import os
import sys
import types

# Make the repository root importable (tests/ live one level below it).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ensure(name):
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    return mod


# tabulate.tabulate — only used to render the interactive table (never in tests).
_tab = _ensure('tabulate')
if not hasattr(_tab, 'tabulate'):
    _tab.tabulate = lambda *a, **k: ''

# requests — RequestException is referenced in except clauses at import-eval time.
_req = _ensure('requests')
if not hasattr(_req, 'RequestException'):
    _req.RequestException = type('RequestException', (Exception,), {})
    _req.delete = lambda *a, **k: None
    _req.post = lambda *a, **k: None

# plexapi.server.PlexServer and plexapi.myplex.MyPlexAccount (imported at module load).
_ensure('plexapi')
_server = _ensure('plexapi.server')
if not hasattr(_server, 'PlexServer'):
    class _PlexServer:  # pragma: no cover - never instantiated in tests
        def __init__(self, *a, **k):
            pass
    _server.PlexServer = _PlexServer
_myplex = _ensure('plexapi.myplex')
if not hasattr(_myplex, 'MyPlexAccount'):
    class _MyPlexAccount:  # pragma: no cover
        def __init__(self, *a, **k):
            pass
    _myplex.MyPlexAccount = _MyPlexAccount
