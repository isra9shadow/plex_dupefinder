# core/CONTRACT.md — frozen public API (FREEZE-2)

> **STATUS: FROZEN** (ADR-0004). This is the module-facing API of `core`. Modules
> and adapters depend ONLY on what is documented here. Internal implementation may
> change freely; **changing any signature below requires a new ADR**. All public
> functions/classes are fully type-annotated (`mypy --strict`).

---

## Module contract
Every business module is a single file under `modules/` that self-registers:

```python
from core.registry import register
from core.types import ModuleResult, RunContext

@register("module_name")
def run(ctx: RunContext) -> ModuleResult: ...
```
A module **never** imports another module; it depends only on `core/*`,
`integrations/*` and `adapters/*`. It must be report-only-capable, idempotent, and
must route every destructive/move operation through `ctx.fs`.

---

## core.types
```python
class SafetyMode(StrEnum):       # DRY_RUN | AUDIT | LIVE  ;  SafetyMode.default() -> DRY_RUN
class Verdict(StrEnum):          # OK | DOUBTFUL | BAD
class EventKind(StrEnum):        # RUN_START | RUN_OK | RUN_FAIL | ALERT | SUMMARY
class NotifyLevel(StrEnum):      # NONE | FAIL | ALL

@dataclass(frozen=True) FailureRecord:  category: str; message: str; src: str|None=None; dest: str|None=None
@dataclass(frozen=True) ActionRecord:   action: str; src: str; dest: str|None=None; bytes: int=0
@dataclass(frozen=True) QuarantineEntry: original_path: str; quarantine_path: str; sidecar_path: str
                                         reason: str; restore_command: str; ts: float
@dataclass(frozen=True) PurgeResult:    deleted: int; bytes_reclaimed: int; simulated: bool
@dataclass(frozen=True) HealthResult:   ok: bool; checks: dict[str,bool]; details: list[str]
@dataclass(frozen=True) NotificationEvent: kind: EventKind; module: str; run_id: str
                                           title: str; body: str=""; fields: dict[str,str]={}

@dataclass ModuleResult:
    module: str; run_id: str; mode: SafetyMode
    actions: int=0; quarantined: int=0; bytes_reclaimed: int=0; failures: list[FailureRecord]=[]
    metrics: dict[str, float]={}     # module-emitted metrics, merged into the run report
    @property ok -> bool            # True iff no failures
    add_failure(failure: FailureRecord) -> None

@dataclass RunReport:
    run_id: str; module: str; mode: SafetyMode; started: float; finished: float
    actions: list[ActionRecord]=[]; failures: list[FailureRecord]=[]; metrics: dict[str,float]={}

@dataclass(frozen=True) RunContext:     # injected into every module
    run_id: str; mode: SafetyMode; config: Config; logger: Logger
    fs: Fs; safety: SafetyPolicy; notify: Notifier
```

## core.config
```python
def load(path: Path|None=None, *, env: Mapping[str,str]|None=None) -> Config   # defaults→json→ENV; ConfigError/ValidationError
def with_mode(cfg: Config, mode: SafetyMode) -> Config                          # copy forced to mode

@dataclass(frozen=True) Config:
    safety: SafetyConfig; logging: LoggingConfig; reporting: ReportingConfig
    notify: NotifyConfig; paths: dict[str,str]; pipelines: dict[str,list[str]]
    integrations: dict[str, dict[str, object]]      # {service: {url, api_key_ref, ...}}
    @property mode -> SafetyMode
# Sub-dataclasses (frozen, with defaults):
#   SafetyConfig(mode, audit, min_file_age_hours, stability_check_seconds,
#                max_size_ratio, quarantine_dir: Path, retention_days, auto_purge)
#   LoggingConfig(level, dir: Path, rotate_mb, backups)
#   ReportingConfig(dir: Path, metrics)
#   NotifyConfig(enabled, level: NotifyLevel, provider, token_ref, chat_id_ref)
```

## core.logging
```python
def new_run_id() -> str                                  # e.g. 20260608-221500-ab12cd
def configure(*, level="INFO", log_dir: Path, run_id: str, rotate_mb=10, backups=5, console=True) -> None
def get_logger(name: str) -> Logger
class Logger:  debug|info|warning|error(msg: str, **fields: object) -> None   # structured JSON
```

## core.locks
```python
@contextmanager
def with_lock(name, *, lock_dir=None, ttl_seconds=21600.0) -> Iterator[None]  # raises LockError if held by a live, non-expired owner; breaks dead/expired locks
def is_locked(name: str, *, lock_dir: Path|None=None) -> bool
```

## core.safety
```python
class SafetyPolicy:
    __init__(config: Config)
    resolve_mode() -> SafetyMode
    min_age_ok(path: Path) -> bool
    is_stable(paths: Sequence[Path], wait_seconds: float|None=None) -> bool
    size_ratio_ok(candidate_size: int, keeper_size: int) -> bool
    passes_guards(paths: Sequence[Path], *, keeper_size: int|None=None, candidate_size: int|None=None) -> bool
    should_purge(entry: QuarantineEntry, retention_days: float|None=None) -> bool
```

## core.fs  — the ONLY mover/deleter (INVARIANT I1)
```python
class Fs:
    __init__(config: Config, *, dry_run: bool)
    @property dry_run -> bool
    @property quarantine_dir -> Path
    quarantine(path: Path, *, reason: str) -> QuarantineEntry   # MOVES + writes restore sidecar
    relocate(src: Path, dest: Path, *, reason: str, allowed_roots: Sequence[Path]|None=None) -> ActionRecord  # MOVES to canonical path; never overwrites (SafetyError); if allowed_roots given, dest must resolve inside one (else SafetyError)
    restore(entry: QuarantineEntry) -> Path
    purge(retention_days: float|None=None, *, simulate: bool=False) -> PurgeResult   # only real delete
```

## core.notify
```python
class Transport(Protocol):  send(text: str) -> None
class NullTransport:        send(text) ; .sent: list[str]            # no-network default
class Notifier:
    __init__(config: Config, transport: Transport|None=None)
    @property transport -> Transport
    send(event: NotificationEvent) -> None                          # respects NotifyLevel; never raises
```

## core.secrets  — fail-closed (INVARIANT I3)
```python
def get_secret(name: str, *, default: str|None=None, required: bool=True) -> str|None   # SecretError if required & missing
def require(name: str) -> str
def reset_cache() -> None
```

## core.registry
```python
ModuleFn = Callable[[RunContext], ModuleResult]
def register(name: str) -> Callable[[ModuleFn], ModuleFn]
def get(name: str) -> ModuleFn|None
def names() -> list[str]
def discover(package: str = "modules") -> None     # imports submodules so @register runs
```

## core.report
```python
def to_dict(report: RunReport) -> dict[str, object]
def write_report(report: RunReport, directory: Path) -> Path     # → directory/<run_id>.json
```

## core.cache
```python
class Cache:                                   # persistent JSON-backed cross-run cache (IMP-06)
    def __init__(self, path: Path) -> None: ...
    def get(self, key: str) -> object | None: ...
    def set(self, key: str, value: object) -> None: ...
    def get_for_file(self, path: Path, namespace: str) -> object | None: ...   # keyed by mtime+size
    def set_for_file(self, path: Path, namespace: str, value: object) -> None: ...
    def save(self) -> None: ...

@dataclass(frozen=True, slots=True)
class MediaRecord:                             # one row of the queryable media cache
    path: str; fingerprint: str; media_id: str | None; score: float | None
    bitrate: int | None; duration: float | None; last_seen: str
    extra: dict[str, object] | None

class SqliteCache:                             # queryable per-media SQLite cache (IMP-06 upgrade)
    def __init__(self, path: Path) -> None: ...
    def get(self, path: Path) -> MediaRecord | None: ...                # None unless fingerprint matches
    def put(self, path: Path, *, media_id=None, score=None,
            bitrate=None, duration=None, extra=None) -> None: ...        # refreshes last_seen
    def prune(self, *, older_than_days: int) -> int: ...                # evict stale rows; returns count
    def query(self, where: str = "", params: Iterable[object] = ()) -> list[MediaRecord]: ...  # raw SQL: literals only
    def query_by(self, column: str, op: str, value: object) -> list[MediaRecord]: ...          # safe: allowlisted column/op, parametrised value
    def count(self) -> int: ...                                         # cached media rows
    def record_run(self, run_id: str, metrics: Mapping[str, object]) -> None: ...  # append run metrics history
    def recent_runs(self, limit: int = 10) -> list[dict[str, object]]: ...      # newest-first run metrics
    def save(self) -> None: ...                                         # commit buffered writes
    def close(self) -> None: ...                                        # save + close; also a context manager
# Backed by SQLite: WAL + synchronous=NORMAL, index on last_seen, `runs` metrics table.
```

## core.timing
```python
class Profiler:                                # per-phase wall-clock profiling (IMP-03)
    @contextmanager
    def phase(self, name: str) -> Iterator[None]: ...
    def record(self, name: str, seconds: float) -> None: ...
    def metrics(self) -> dict[str, float]: ...  # {name_ms, name_count} → ModuleResult.metrics
```

## core.errors
```python
IzumiError(message, *, category=None)   # .category, .message
  ConfigError(config) · SecretError(secret) · SafetyError(safety) · QuarantineError(quarantine)
  LockError(lock) · IntegrationError(integration) · ValidationError(validation)
```

---

## Adapter contract (host access)
`adapters/command.run(argv, *, timeout=30.0, env=None) -> CommandResult` is the
**single audited subprocess boundary** (list args, no shell, stdin closed, never
raises; inspect `.ok`). `env` (optional `Mapping[str,str]`) is merged over the
inherited environment. No module/integration may call `subprocess` directly
(enforced by `tests/security/test_no_direct_subprocess.py`).
