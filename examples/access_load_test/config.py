"""
Run configuration, validation and host safety checks for the access load test.

Nothing in this module talks to the network. It holds the ``RunConfig`` dataclass, the
built-in host blocklist, the generator-host resource estimate, and small parsers for CLI
values such as ``--ramp`` and ``--mix``.
"""

from __future__ import annotations

import os
import platform
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

SCENARIOS = ("human", "good-bots", "bad-bots", "mix")
PERSONAS = ("human", "good-bot", "bad-bot")

# Hosts this tool refuses to target, with no override. The DSpace community demo and
# sandbox servers are shared infrastructure; load testing them harms everybody.
BLOCKED_HOST_SUFFIXES = (".dspace.org",)
BLOCKED_HOSTS = ("dspace.org",)

# Generator-side budget per simulated human (one Chromium context with one page) and per
# Chromium browser process. Deliberately conservative; the README explains how to calibrate.
RAM_MB_PER_HUMAN = 150
RAM_MB_PER_BROWSER = 200
CPU_CORES_PER_HUMAN = 0.3
HUMANS_PER_BROWSER = 10
MAX_HUMAN_USERS_DEFAULT = 60
MAX_USERS_HARD = 1000

DEFAULT_GOOD_BOT_UA = (
    "Mozilla/5.0 (compatible; dspace-access-load-test/1.0; "
    "+https://github.com/atmire/dspace-python-client)"
)
# Bad bots masquerade as a desktop browser. Chromium's real UA is used for humans.
DEFAULT_BAD_BOT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_HUMAN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

RUN_ID_HEADER = "X-DSpace-Load-Test"


class ConfigError(ValueError):
    """Raised for invalid or unsafe configuration."""


@dataclass
class RampConfig:
    """Add users gradually: start with ``start``, add ``step`` every ``every_s`` seconds."""

    start: int
    step: int
    every_s: float

    def users_at(self, elapsed_s: float, total: int) -> int:
        if elapsed_s < 0:
            return 0
        n = self.start + self.step * int(elapsed_s // self.every_s)
        return max(0, min(total, n))


@dataclass
class MixConfig:
    """Persona split for the mix scenario, in percent (must sum to 100)."""

    human: int = 50
    good_bot: int = 20
    bad_bot: int = 30

    def validate(self) -> None:
        total = self.human + self.good_bot + self.bad_bot
        if total != 100:
            raise ConfigError(f"--mix percentages must sum to 100, got {total}")
        if min(self.human, self.good_bot, self.bad_bot) < 0:
            raise ConfigError("--mix percentages must be non-negative")

    def assign(self, users: int) -> list[str]:
        """Deterministic persona list for ``users`` threads, largest remainder rounding."""
        weights = {"human": self.human, "good-bot": self.good_bot, "bad-bot": self.bad_bot}
        raw = {k: users * v / 100 for k, v in weights.items()}
        counts = {k: int(v) for k, v in raw.items()}
        remainder = users - sum(counts.values())
        for k in sorted(raw, key=lambda k: raw[k] - counts[k], reverse=True)[:remainder]:
            counts[k] += 1
        out: list[str] = []
        for k in ("human", "good-bot", "bad-bot"):
            out.extend([k] * counts[k])
        return out


@dataclass
class RunConfig:
    """Everything the orchestrator needs. Built by ``main.py`` from flags and prompts."""

    base_url: str
    scenario: str
    users: int
    think_time_s: float
    duration_s: float
    ui_url: str | None = None
    baseline_s: float = 30.0
    window_s: float = 10.0
    ramp: RampConfig | None = None
    mix: MixConfig = field(default_factory=MixConfig)
    fixed_think_time: bool = False

    downloads_enabled: bool = True
    download_probability: float = 0.25
    max_download_mb: float = 50.0
    max_total_download_gb: float = 5.0
    max_inflight: int = 200

    bad_bot_open_loop: bool = False
    bad_bot_max_depth: int = 6
    bad_bot_max_urls: int = 20000
    good_bot_ua: str = DEFAULT_GOOD_BOT_UA
    bad_bot_ua: str = DEFAULT_BAD_BOT_UA
    human_ua: str = DEFAULT_HUMAN_UA
    oai_share: float = 0.2
    view_events: bool = True
    humans_per_browser: int = HUMANS_PER_BROWSER
    session_pages_mean: float = 4.0
    session_pages_max: int = 12
    action_settle_cap_s: float = 20.0
    headless: bool = True

    auto_stop: bool = True
    stop_at_onset: bool = False
    onset_factor: float = 1.5
    onset_min_increase_s: float = 0.05
    onset_confirm_windows: int = 3
    break_error_rate: float = 0.05
    break_p95_s: float = 10.0
    break_confirm_windows: int = 2

    seed: int | None = None
    output_dir: Path = field(default_factory=lambda: Path("access-load-reports"))
    requests_log: bool = True
    requests_log_max_mb: float = 200.0
    dry_run: bool = False
    skip_version_check: bool = False
    force_resources: bool = False
    max_human_users: int = MAX_HUMAN_USERS_DEFAULT
    pool_max_items: int = 5000
    http_timeout_s: float = 60.0
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # ---- derived -------------------------------------------------------------------

    @property
    def ui_base(self) -> str:
        return (self.ui_url or self.base_url).rstrip("/")

    @property
    def rest_base(self) -> str:
        """REST API root, e.g. ``https://repo.example.org/server/api``."""
        return f"{self.base_url.rstrip('/')}/server/api"

    @property
    def allowed_hosts(self) -> set[str]:
        hosts = {urlparse(self.base_url).hostname or "", urlparse(self.ui_base).hostname or ""}
        hosts.discard("")
        return hosts

    @property
    def target_host(self) -> str:
        return urlparse(self.ui_base).hostname or urlparse(self.base_url).hostname or ""

    def personas(self) -> list[str]:
        """Persona per user thread, in start order."""
        if self.scenario == "human":
            return ["human"] * self.users
        if self.scenario == "good-bots":
            return ["good-bot"] * self.users
        if self.scenario == "bad-bots":
            return ["bad-bot"] * self.users
        return self.mix.assign(self.users)

    @property
    def human_users(self) -> int:
        return sum(1 for p in self.personas() if p == "human")

    @property
    def needs_browser(self) -> bool:
        return self.human_users > 0

    def validate(self) -> None:
        if self.scenario not in SCENARIOS:
            raise ConfigError(f"scenario must be one of {', '.join(SCENARIOS)}")
        for name, url in (("base URL", self.base_url), ("UI URL", self.ui_base)):
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ConfigError(f"{name} must be an absolute http(s) URL, got {url!r}")
            check_host_allowed(parsed.hostname)
        if not 1 <= self.users <= MAX_USERS_HARD:
            raise ConfigError(f"users must be between 1 and {MAX_USERS_HARD}")
        if self.think_time_s < 0:
            raise ConfigError("think time must be >= 0")
        if self.duration_s <= 0:
            raise ConfigError("duration must be > 0")
        if self.window_s < 1:
            raise ConfigError("window must be >= 1 second")
        if self.baseline_s < 0:
            raise ConfigError("baseline seconds must be >= 0")
        if not 0 <= self.download_probability <= 1:
            raise ConfigError("download probability must be between 0 and 1")
        if not 0 <= self.oai_share <= 1:
            raise ConfigError("oai share must be between 0 and 1")
        if self.max_inflight < 1:
            raise ConfigError("max in-flight must be >= 1")
        if self.scenario == "mix":
            self.mix.validate()
        if self.ramp is not None and (
            self.ramp.start < 1 or self.ramp.step < 1 or self.ramp.every_s <= 0
        ):
            raise ConfigError("--ramp needs start >= 1, step >= 1 and every > 0")
        if self.human_users > self.max_human_users and not self.force_resources:
            raise ConfigError(
                f"{self.human_users} simulated humans exceeds the per-host cap of "
                f"{self.max_human_users}. Each human is a real browser tab. Split the run over "
                "several generator machines, or pass --force-resources if you have measured "
                "that this host can take it."
            )


def check_host_allowed(hostname: str) -> None:
    """Raise ``ConfigError`` for hosts on the built-in blocklist."""
    h = hostname.lower()
    if h in BLOCKED_HOSTS or any(h.endswith(s) for s in BLOCKED_HOST_SUFFIXES):
        raise ConfigError(
            f"Refusing to load test {hostname}: shared DSpace community infrastructure is "
            "blocked in this tool and there is no override."
        )


# ---- parsers ---------------------------------------------------------------------------


def parse_duration(text: str) -> float:
    """``90``, ``90s``, ``10m``, ``1.5h`` -> seconds."""
    t = text.strip().lower()
    if not t:
        raise ConfigError("empty duration")
    mult = 1.0
    if t.endswith("h"):
        mult, t = 3600.0, t[:-1]
    elif t.endswith("m"):
        mult, t = 60.0, t[:-1]
    elif t.endswith("s"):
        t = t[:-1]
    try:
        value = float(t)
    except ValueError as e:
        raise ConfigError(f"invalid duration {text!r}") from e
    if value <= 0:
        raise ConfigError("duration must be positive")
    return value * mult


def parse_ramp(text: str) -> RampConfig:
    """``start=2,step=2,every=60s`` or positional ``2:2:60``."""
    t = text.strip()
    if not t:
        raise ConfigError("empty --ramp")
    values: dict[str, str] = {}
    if "=" in t:
        for part in t.split(","):
            if "=" not in part:
                raise ConfigError(f"bad --ramp segment {part!r}")
            k, v = part.split("=", 1)
            values[k.strip()] = v.strip()
    else:
        parts = t.split(":")
        if len(parts) != 3:
            raise ConfigError("--ramp positional form is start:step:every")
        values = dict(zip(("start", "step", "every"), parts, strict=True))
    try:
        return RampConfig(
            start=int(values["start"]),
            step=int(values["step"]),
            every_s=parse_duration(values["every"]),
        )
    except KeyError as e:
        raise ConfigError(f"--ramp is missing {e.args[0]}") from e
    except ValueError as e:
        raise ConfigError(f"invalid --ramp value: {e}") from e


def parse_mix(text: str) -> MixConfig:
    """``human=50,good=20,bad=30`` (any order; percentages)."""
    aliases = {
        "human": "human",
        "humans": "human",
        "good": "good_bot",
        "good-bot": "good_bot",
        "good-bots": "good_bot",
        "bad": "bad_bot",
        "bad-bot": "bad_bot",
        "bad-bots": "bad_bot",
    }
    mix = MixConfig(0, 0, 0)
    for part in text.split(","):
        if "=" not in part:
            raise ConfigError(f"bad --mix segment {part!r}")
        k, v = part.split("=", 1)
        key = aliases.get(k.strip().lower())
        if key is None:
            raise ConfigError(f"unknown persona {k.strip()!r} in --mix")
        try:
            setattr(mix, key, int(v))
        except ValueError as e:
            raise ConfigError(f"invalid percentage {v!r} in --mix") from e
    mix.validate()
    return mix


# ---- generator host resources ----------------------------------------------------------


@dataclass
class MachineResources:
    cpu_count: int | None
    ram_mb: int | None


def machine_resources() -> MachineResources:
    """Best-effort CPU count and total RAM without third-party dependencies."""
    cpu = os.cpu_count()
    ram: int | None = None
    try:
        if platform.system() == "Darwin":
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=False
            )
            if out.returncode == 0:
                ram = int(out.stdout.strip()) // (1024 * 1024)
        elif Path("/proc/meminfo").exists():
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    ram = int(line.split()[1]) // 1024
                    break
    except (OSError, ValueError):
        ram = None
    return MachineResources(cpu_count=cpu, ram_mb=ram)


@dataclass
class ResourceEstimate:
    human_users: int
    browsers: int
    ram_mb: int
    cpu_cores: float
    machine: MachineResources

    @property
    def ram_share(self) -> float | None:
        if self.machine.ram_mb:
            return self.ram_mb / self.machine.ram_mb
        return None

    @property
    def cpu_share(self) -> float | None:
        if self.machine.cpu_count:
            return self.cpu_cores / self.machine.cpu_count
        return None

    @property
    def over_budget(self) -> bool:
        ram = self.ram_share
        cpu = self.cpu_share
        return (ram is not None and ram > 0.7) or (cpu is not None and cpu > 1.0)


def estimate_resources(cfg: RunConfig, machine: MachineResources | None = None) -> ResourceEstimate:
    humans = cfg.human_users
    browsers = -(-humans // max(1, cfg.humans_per_browser)) if humans else 0
    ram = humans * RAM_MB_PER_HUMAN + browsers * RAM_MB_PER_BROWSER + 150
    cpu = humans * CPU_CORES_PER_HUMAN + 0.5
    return ResourceEstimate(
        human_users=humans,
        browsers=browsers,
        ram_mb=ram,
        cpu_cores=round(cpu, 2),
        machine=machine or machine_resources(),
    )


__all__ = [
    "BLOCKED_HOST_SUFFIXES",
    "DEFAULT_BAD_BOT_UA",
    "DEFAULT_GOOD_BOT_UA",
    "DEFAULT_HUMAN_UA",
    "PERSONAS",
    "RUN_ID_HEADER",
    "SCENARIOS",
    "ConfigError",
    "MachineResources",
    "MixConfig",
    "RampConfig",
    "ResourceEstimate",
    "RunConfig",
    "check_host_allowed",
    "estimate_resources",
    "machine_resources",
    "parse_duration",
    "parse_mix",
    "parse_ramp",
]
