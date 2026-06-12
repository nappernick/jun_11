"""
Centralized, multi-agent-safe credential broker.

The single entry point every Bedrock / OpenSearch-Serverless (AOSS) client uses to
obtain a boto3 :class:`~boto3.session.Session`. It exists to kill two distinct,
load-bearing failure modes that repeatedly took down long optimizer runs:

1. **Refresh that does not refresh.** The previous "credential-expiry resilience"
   only rebuilt the boto3 client from the *same on-disk credential file*
   (``self._client = self._client_factory()``). When the underlying STS/Bedrock
   token had actually expired, the rebuilt client re-read the same expired token
   and the retry budget burned out — the run died with ``ExpiredTokenException``
   (observed live on ``InvokeInlineAgent`` ~2 minutes into a run). Nothing ever
   re-ran ``ada`` to *mint* a new token. The broker's refresh actually invokes
   ``ada credentials update`` and only then rebuilds the session.

2. **Fragility to other agents on the box.** Every adapter did a bare
   ``boto3.Session()``, resolving the **ambient** ``AWS_PROFILE`` / ``default`` —
   shared, mutable global state any sibling agent could flip or clobber, silently
   redirecting this app's calls to the wrong account. The broker binds every
   session to an **explicit named profile** from
   :data:`bakeoff.config.CREDENTIAL_PROFILES`, never the ambient env.

Design (grounded in observed ``ada`` behavior on this host, not assumption):

* **Explicit profile binding.** :func:`get_session` / :func:`get_credentials`
  always pass ``profile_name=<named profile>`` to boto3. A profile the broker does
  not know how to refresh (absent from the registry) still yields a session, but
  the broker will not attempt to mint for it.

* **Real refresh via ``ada``, under a cross-process lock.** :func:`refresh` runs
  ``ada credentials update --account ... --role ... --provider ... --profile ...
  --once`` for the named profile. The invocation is serialized across *every
  process on the box* by an :mod:`fcntl` file lock (``CREDENTIAL_LOCK_DIR/<profile>.lock``)
  plus a last-mint-timestamp sidecar, so a burst of concurrent auth-expiry retries
  (many in-flight Bedrock calls expiring at once, or a sibling agent) coalesces
  into a **single** real ``ada`` call — the rest wait on the lock and then observe
  the fresh mint instead of stampeding ``ada`` (which itself serializes on Midway).

* **TTL-based proactive freshness.** :func:`get_session` refreshes lazily when the
  cached mint for a profile is older than
  :data:`bakeoff.config.CREDENTIAL_REFRESH_TTL_S` (well inside the ~1h token
  lifetime), so a long run renews *before* a call fails rather than after.

* **Midway is surfaced, not looped.** When ``ada`` fails because the Midway session
  has lapsed (``ada`` exits non-zero with a 401 / "run mwinit" message), the broker
  raises :class:`CredentialRefreshError` carrying ``needs_mwinit=True`` so the
  caller can tell the operator to run ``mwinit`` — the one thing only a human can
  do — instead of spinning the retry budget on something no automated refresh can
  fix.

* **Drop-in refresh hook.** :meth:`CredentialBroker.refresh_callback_for` returns a
  zero-arg callable suitable as the ``refresh_credentials`` seam of
  :func:`bakeoff.resilience.call_with_resilience`, so wiring the broker into the
  existing adapters is a one-line change of the refresh hook (the client factory
  stays the same; it just rebuilds from a now-actually-fresh profile).

Import-light: standard library + :mod:`bakeoff.config`. ``boto3`` is imported
lazily inside the methods that build a session, so importing this module needs no
boto3 and opens no network. A module-level singleton (:func:`get_broker`) gives the
whole process one coordinator, but the class is instantiable for tests with an
injected ``ada`` runner and clock (no real subprocess, no real AWS).
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import bakeoff.config as config

__all__ = [
    "CredentialError",
    "CredentialRefreshError",
    "UnknownProfileError",
    "CredentialBroker",
    "get_broker",
    "get_session",
    "get_credentials",
    "refresh",
]

log = logging.getLogger("bakeoff.credentials")

#: An ``ada`` runner: ``(profile, profile_spec) -> AdaResult``. Injected by tests
#: so the broker exercises its locking/TTL logic with no real subprocess.
AdaRunner = Callable[[str, "dict[str, str]"], "AdaResult"]


class CredentialError(RuntimeError):
    """Base class for credential-broker failures."""


class UnknownProfileError(CredentialError):
    """Raised when a profile is not present in ``config.CREDENTIAL_PROFILES``."""


class CredentialRefreshError(CredentialError):
    """Raised when ``ada credentials update`` fails for a known profile.

    ``needs_mwinit`` is ``True`` when the failure looks like a lapsed Midway
    session (``ada`` reported a 401 / told the user to run ``mwinit``) — the one
    case an automated refresh cannot fix, so the caller should surface it to the
    operator rather than retry.
    """

    def __init__(self, profile: str, message: str, *, needs_mwinit: bool = False):
        self.profile = profile
        self.needs_mwinit = needs_mwinit
        super().__init__(message)


@dataclass(frozen=True)
class AdaResult:
    """Outcome of one ``ada credentials update`` invocation."""

    returncode: int
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# Substrings (lower-cased) in ada's stderr that mark a lapsed Midway session — the
# refresh cannot succeed until the human runs ``mwinit``. Matched case-insensitively.
_MWINIT_SIGNATURES: tuple[str, ...] = (
    "mwinit",
    "did not redirect",
    "status code: 401",
    "midway",
)


def _looks_like_mwinit(stderr: str) -> bool:
    s = (stderr or "").lower()
    return any(sig in s for sig in _MWINIT_SIGNATURES)


class CredentialBroker:
    """Process-wide coordinator that hands out profile-bound boto3 sessions.

    One instance per process is the norm (:func:`get_broker`), but the class is
    instantiable directly for tests with an injected ``ada_runner`` and ``clock``.

    Thread-safety: a per-broker lock guards the in-memory mint timestamps and the
    cached sessions; the cross-process ``ada`` serialization is a separate
    :mod:`fcntl` file lock per profile (so coordination spans processes, not just
    threads).
    """

    def __init__(
        self,
        *,
        profiles: "Optional[dict[str, dict[str, str]]]" = None,
        default_profile: Optional[str] = None,
        refresh_ttl_s: Optional[float] = None,
        min_refresh_interval_s: Optional[float] = None,
        ada_timeout_s: Optional[float] = None,
        lock_dir: "Optional[Path]" = None,
        ada_runner: Optional[AdaRunner] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._profiles = profiles if profiles is not None else dict(config.CREDENTIAL_PROFILES)
        self._default_profile = default_profile or config.CREDENTIAL_DEFAULT_PROFILE
        self._refresh_ttl_s = (
            refresh_ttl_s if refresh_ttl_s is not None else config.CREDENTIAL_REFRESH_TTL_S
        )
        self._min_refresh_interval_s = (
            min_refresh_interval_s
            if min_refresh_interval_s is not None
            else config.CREDENTIAL_MIN_REFRESH_INTERVAL_S
        )
        self._ada_timeout_s = (
            ada_timeout_s if ada_timeout_s is not None else config.CREDENTIAL_ADA_TIMEOUT_S
        )
        self._lock_dir = Path(lock_dir) if lock_dir is not None else config.CREDENTIAL_LOCK_DIR
        self._ada_runner = ada_runner or self._default_ada_runner
        self._clock = clock

        self._guard = threading.Lock()
        #: Serializes the refresh critical section WITHIN this process (held for the
        #: whole ada-mint window). Kept SEPARATE from ``self._guard`` — which only
        #: protects brief reads/writes of the dicts below — so methods called inside
        #: the refresh section (e.g. _invalidate_session) can still take ``self._guard``
        #: without self-deadlocking against the refresh lock.
        self._refresh_guard = threading.Lock()
        #: profile -> monotonic-ish wall time of the last successful mint we know of.
        self._last_mint: dict[str, float] = {}
        #: profile -> cached boto3 Session (rebuilt on refresh).
        self._sessions: dict[str, Any] = {}

        try:
            self._lock_dir.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - defensive; lock just degrades to in-proc
            log.warning("could not create credential lock dir %s", self._lock_dir, exc_info=True)

    # -- profile resolution ------------------------------------------------
    def resolve_profile(self, profile: Optional[str]) -> str:
        """Return the profile name to use, defaulting to the configured default."""
        return profile or self._default_profile

    def knows(self, profile: str) -> bool:
        """True iff the broker has refresh identity for ``profile``."""
        return profile in self._profiles

    # -- public: session / credentials ------------------------------------
    def get_session(
        self, profile: Optional[str] = None, *, region: Optional[str] = None
    ) -> Any:
        """Return a boto3 Session bound to ``profile`` (proactively refreshed by TTL).

        If the cached mint for the profile is older than ``refresh_ttl_s`` (or there
        is no cached session yet), a refresh is attempted first. A refresh failure on
        a *stale-but-present* session is swallowed with a warning — a slightly old
        token still works — but a failure when there is **no** usable session
        re-raises, because there is nothing to fall back to.
        """
        import boto3  # lazy

        name = self.resolve_profile(profile)
        region_name = region or self._profile_region(name)

        if self._should_proactively_refresh(name):
            try:
                self.refresh(name)
            except CredentialRefreshError:
                with self._guard:
                    have_session = name in self._sessions
                if not have_session:
                    raise
                log.warning(
                    "proactive refresh of profile %r failed; using existing session", name,
                    exc_info=True,
                )

        with self._guard:
            session = self._sessions.get(name)
            if session is None:
                session = boto3.Session(profile_name=name, region_name=region_name)
                self._sessions[name] = session
                self._last_mint.setdefault(name, self._clock())
            return session

    def get_credentials(self, profile: Optional[str] = None) -> Any:
        """Return frozen botocore credentials for ``profile`` (for SigV4 signing).

        Used by the AOSS signer path. Returns the live credentials object boto3
        resolves from the profile; raises :class:`CredentialError` if the profile
        yields no credentials at all (an empty/never-minted profile).
        """
        session = self.get_session(profile)
        creds = session.get_credentials()
        if creds is None:
            raise CredentialError(
                f"profile {self.resolve_profile(profile)!r} resolved no credentials; "
                f"run a refresh (ada) or check ~/.aws/config for the profile"
            )
        return creds

    # -- public: refresh ---------------------------------------------------
    def refresh(self, profile: Optional[str] = None, *, force: bool = False) -> bool:
        """Mint fresh credentials for ``profile`` via ``ada``, cross-process-coordinated.

        Returns ``True`` if a real ``ada`` mint ran and succeeded; ``False`` if the
        call was coalesced (another process/thread refreshed within
        ``min_refresh_interval_s``, so we reuse that mint without re-running ``ada``).

        Raises:
            UnknownProfileError: the profile is not in the registry (the broker has
                no identity with which to call ``ada``).
            CredentialRefreshError: ``ada`` ran and failed; ``needs_mwinit`` is set
                when the failure is a lapsed Midway session.
        """
        name = self.resolve_profile(profile)
        spec = self._profiles.get(name)
        if spec is None:
            raise UnknownProfileError(
                f"profile {name!r} is not in config.CREDENTIAL_PROFILES; the broker "
                f"cannot refresh it. Known profiles: {sorted(self._profiles)}"
            )

        # Cross-process lock so only one process runs ada for this profile at a time.
        with self._cross_process_lock(name):
            now = self._clock()
            # Anti-stampede coalescing applies ALWAYS, even when force=True: if a real
            # mint happened within the min-interval (this process or any other holding
            # the cross-process lock just before us), reuse it rather than re-running
            # ada. `force` overrides the PROACTIVE TTL decision (in get_session), not
            # this hard min-interval guard — a burst of concurrent auth-expiry retries
            # all calling the force=True refresh callback must still collapse to ONE
            # ada call, not one per caller.
            last = self._read_last_mint(name)
            if last is not None and (now - last) < self._min_refresh_interval_s:
                # A fresh mint exists; reuse it (drop the cached session so the next
                # get_session rebuilds from the freshly-written credentials file).
                self._invalidate_session(name)
                with self._guard:
                    self._last_mint[name] = last
                log.info("refresh(%s) coalesced: fresh mint %.1fs ago", name, now - last)
                return False

            log.info("refresh(%s): running ada credentials update", name)
            result = self._ada_runner(name, spec)
            if not result.ok:
                needs_mwinit = _looks_like_mwinit(result.stderr)
                msg = (
                    f"ada credentials update failed for profile {name!r} "
                    f"(exit {result.returncode}): {result.stderr.strip()[:300]}"
                )
                if needs_mwinit:
                    msg += " — Midway session appears lapsed; run `mwinit` and retry."
                raise CredentialRefreshError(name, msg, needs_mwinit=needs_mwinit)

            minted = self._clock()
            self._write_last_mint(name, minted)
            self._invalidate_session(name)
            with self._guard:
                self._last_mint[name] = minted
            log.info("refresh(%s): ada mint OK", name)
            return True

    def refresh_callback_for(
        self, profile: Optional[str] = None
    ) -> Callable[[], None]:
        """Return a zero-arg callback that refreshes ``profile`` then drops its session.

        Drop-in for the ``refresh_credentials`` seam of
        :func:`bakeoff.resilience.call_with_resilience`. On auth-expiry the resilience
        loop calls this; it forces a real ``ada`` mint (subject to the cross-process
        coalescing window) so the subsequent client rebuild reads a genuinely fresh
        token instead of the same expired one. A :class:`CredentialRefreshError` with
        ``needs_mwinit`` propagates so the run fails fast with an actionable message
        rather than looping the retry budget on an unfixable Midway lapse.
        """
        name = self.resolve_profile(profile)

        def _cb() -> None:
            self.refresh(name, force=True)

        return _cb

    # -- internals: TTL / region ------------------------------------------
    def _profile_region(self, profile: str) -> str:
        spec = self._profiles.get(profile) or {}
        return spec.get("region") or config.AWS_REGION

    def _should_proactively_refresh(self, profile: str) -> bool:
        if not self.knows(profile):
            return False  # nothing to refresh with; hand out a bare session
        with self._guard:
            last = self._last_mint.get(profile)
        if last is None:
            last = self._read_last_mint(profile)
        if last is None:
            return True  # never minted in this process and no sidecar -> refresh
        return (self._clock() - last) >= self._refresh_ttl_s

    def _invalidate_session(self, profile: str) -> None:
        with self._guard:
            self._sessions.pop(profile, None)

    # -- internals: last-mint sidecar (cross-process timestamp) -----------
    def _mint_stamp_path(self, profile: str) -> Path:
        return self._lock_dir / f"{profile}.mint"

    def _read_last_mint(self, profile: str) -> Optional[float]:
        path = self._mint_stamp_path(profile)
        try:
            return float(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _write_last_mint(self, profile: str, when: float) -> None:
        path = self._mint_stamp_path(profile)
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(f"{when:.3f}", encoding="utf-8")
            os.replace(tmp, path)
        except OSError:  # pragma: no cover - defensive
            log.warning("could not write mint stamp %s", path, exc_info=True)

    # -- internals: cross-process file lock -------------------------------
    def _cross_process_lock(self, profile: str):
        """Context manager taking an exclusive ``fcntl`` lock on the profile's lock file.

        Holds the per-process ``self._refresh_guard`` (a dedicated mutex, NOT the
        short-lived ``self._guard`` used for dict mutations — so refresh-internal calls
        that take ``self._guard`` cannot self-deadlock) for the whole refresh window,
        and additionally takes a POSIX file lock so coordination spans processes.
        Degrades gracefully to an in-process-only lock if ``fcntl`` is unavailable
        (non-POSIX) or the lock file cannot be opened.
        """
        return _ProfileFileLock(self._lock_dir / f"{profile}.lock", self._refresh_guard)

    # -- internals: the real ada runner -----------------------------------
    def _default_ada_runner(self, profile: str, spec: "dict[str, str]") -> AdaResult:
        """Run ``ada credentials update --once`` for ``profile`` (real subprocess)."""
        import shutil

        ada = shutil.which("ada")
        if ada is None:
            return AdaResult(returncode=127, stderr="ada not found on PATH")
        cmd = [
            ada, "credentials", "update",
            f"--account={spec['account']}",
            f"--role={spec['role']}",
            f"--provider={spec.get('provider', 'conduit')}",
            "--profile", profile,
            "--once",
        ]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=self._ada_timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return AdaResult(
                returncode=124,
                stderr=f"ada timed out after {self._ada_timeout_s}s (Midway hang? run mwinit)",
            )
        return AdaResult(
            returncode=proc.returncode,
            stderr=(proc.stderr or b"").decode("utf-8", "replace"),
        )


class _ProfileFileLock:
    """Exclusive cross-process lock via ``fcntl.flock`` with an in-process fallback.

    Always holds the in-process ``threading.Lock`` for the duration; additionally
    takes a POSIX file lock when ``fcntl`` and the lock file are available, so the
    serialization spans every process on the box. If the file lock cannot be
    acquired (no fcntl, or open failure), coordination degrades to in-process only
    rather than failing the refresh.
    """

    def __init__(self, lock_path: Path, guard: "threading.Lock") -> None:
        self._lock_path = lock_path
        self._guard = guard
        self._fh = None

    def __enter__(self) -> "_ProfileFileLock":
        self._guard.acquire()
        try:
            import fcntl

            self._fh = open(self._lock_path, "w", encoding="utf-8")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:  # noqa: BLE001 - any failure -> in-process-only coordination
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self._fh is not None:
                try:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                finally:
                    self._fh.close()
                    self._fh = None
        finally:
            self._guard.release()


# ---------------------------------------------------------------------------
# Module-level singleton + convenience functions
# ---------------------------------------------------------------------------
_BROKER_LOCK = threading.Lock()
_BROKER: Optional[CredentialBroker] = None


def get_broker() -> CredentialBroker:
    """Return the process-wide :class:`CredentialBroker` singleton (built on first use)."""
    global _BROKER
    if _BROKER is None:
        with _BROKER_LOCK:
            if _BROKER is None:
                _BROKER = CredentialBroker()
    return _BROKER


def get_session(profile: Optional[str] = None, *, region: Optional[str] = None) -> Any:
    """Convenience: a profile-bound boto3 Session from the singleton broker."""
    return get_broker().get_session(profile, region=region)


def get_credentials(profile: Optional[str] = None) -> Any:
    """Convenience: frozen credentials for ``profile`` from the singleton broker."""
    return get_broker().get_credentials(profile)


def refresh(profile: Optional[str] = None, *, force: bool = False) -> bool:
    """Convenience: refresh ``profile`` via the singleton broker."""
    return get_broker().refresh(profile, force=force)
