"""
octagon_setup.py — first-run setup wizard for Doctor Octogon.

Run:  python -m octagon.setup
      python -m octagon.octagon_setup
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import platform
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ── Paths ─────────────────────────────────────────────────────────────────────

_REPO = Path(__file__).parent.parent
ENV_PATH         = _REPO / ".env"
FREQ_LEVER_PATH  = _REPO / "freq_lever.json"
_DEFAULT_DB_PATH = Path.home() / "Desktop" / "octagon" / "octagon.db"
_DEFAULT_LOG_DIR = _REPO / "logs"

# ── ANSI palette ──────────────────────────────────────────────────────────────

_G  = "\033[92m"   # bright green
_GD = "\033[32m"   # dim green
_W  = "\033[97m"   # bright white
_DM = "\033[2m"    # dim
_RD = "\033[91m"   # red
_YL = "\033[93m"   # amber
_BL = "\033[1m"    # bold
_RS = "\033[0m"    # reset


def _g(t: str)  -> str: return f"{_G}{t}{_RS}"
def _gd(t: str) -> str: return f"{_GD}{t}{_RS}"
def _w(t: str)  -> str: return f"{_W}{t}{_RS}"
def _dm(t: str) -> str: return f"{_DM}{t}{_RS}"
def _rd(t: str) -> str: return f"{_RD}{t}{_RS}"
def _yl(t: str) -> str: return f"{_YL}{t}{_RS}"
def _b(t: str)  -> str: return f"{_BL}{t}{_RS}"


TICK  = _g("✓")
CROSS = _rd("✗")
DOT   = _dm("·")
ARROW = _g("➤")

# ── ASCII banner ──────────────────────────────────────────────────────────────

def _banner() -> None:
    G, W, DM, GD, BL, RS = _G, _W, _DM, _GD, _BL, _RS
    print(f"""
{G}        ╱▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔╲{RS}
{G}      ╱  ┌──────────────────────────────────┐  ╲{RS}
{G}     │   │                                  │   │{RS}
{G}     │   │   {W}▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓{G}   │   │{RS}
{G}     │   │  {W}▓▓▓{G}  ╔══════╗   ╔══════╗  {W}▓▓▓{G}  │   │{RS}
{G}     │   │  {W}▓▓▓{G}  ║ {W}◉  ◉{G} ║   ║ {W}◉  ◉{G} ║  {W}▓▓▓{G}  │   │{RS}
{G}     │   │  {W}▓▓▓{G}  ╚══════╝   ╚══════╝  {W}▓▓▓{G}  │   │{RS}
{G}     │   │  {W}▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓{G}   │   │{RS}
{G}     │   │  {W}▓▓{G} ┌───────────────────────┐ {W}▓▓{G} │   │{RS}
{G}     │   │  {W}▓▓{G} │{DM}  ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒  {RS}{G}│ {W}▓▓{G} │   │{RS}
{G}     │   │  {W}▓▓{G} │{DM}  ▒ R E S P I R A T O R ▒  {RS}{G}│ {W}▓▓{G} │   │{RS}
{G}     │   │  {W}▓▓{G} │{DM}  ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒  {RS}{G}│ {W}▓▓{G} │   │{RS}
{G}     │   │  {W}▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓{G}   │   │{RS}
{G}     │   │   {W}▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓{G}   │   │{RS}
{G}     │   │                                  │   │{RS}
{G}      ╲  └──────────────────────────────────┘  ╱{RS}
{G}        ╲▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁╱{RS}

{BL}{G}           D O C T O R   O C T O G O N{RS}
{GD}         THE MIND BEHIND THE MACHINE{RS}
{DM}         ─────────────────────────────────────{RS}
""")


# ── Print helpers ─────────────────────────────────────────────────────────────

_W70 = 70

def _section(title: str) -> None:
    pad = _W70 - len(title) - 6
    print(f"\n{_GD}  ─── {_RS}{_W}{title}{_RS}{_GD} {'─' * pad}{_RS}")


def _ok(label: str, detail: str = "") -> None:
    detail_str = f"  {_dm(detail)}" if detail else ""
    print(f"  {TICK}  {label}{detail_str}")


def _fail(label: str, detail: str = "") -> None:
    detail_str = f"  {_dm(detail)}" if detail else ""
    print(f"  {CROSS}  {_rd(label)}{detail_str}")


def _info(label: str, value: str = "") -> None:
    if value:
        print(f"  {DOT}  {_dm(label)}: {_w(value)}")
    else:
        print(f"  {DOT}  {_dm(label)}")


def _warn(msg: str) -> None:
    print(f"  {_yl('⚠')}  {_yl(msg)}")


def _prompt(msg: str) -> str:
    try:
        return input(f"  {ARROW}  {_g(msg)} ")
    except EOFError:
        return ""


def _yn(msg: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = _prompt(f"{msg} {hint}:").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def _mask_key(key: str) -> str:
    if len(key) < 12:
        return "****"
    return key[:7] + "···" + key[-4:]


# ── System check ──────────────────────────────────────────────────────────────

def _check_system() -> bool:
    _section("SYSTEM CHECK")
    ok = True

    # Python version
    vi = sys.version_info
    if vi >= (3, 11):
        _ok(f"Python {vi.major}.{vi.minor}.{vi.micro}")
    else:
        _fail(f"Python {vi.major}.{vi.minor}.{vi.micro}", "3.11+ required")
        ok = False

    # Disk space
    free_gb = shutil.disk_usage(_REPO).free / 1e9
    if free_gb >= 1.0:
        _ok(f"Disk free: {free_gb:.1f} GB")
    else:
        _warn(f"Low disk space: {free_gb:.1f} GB")

    # Platform
    _info("Platform", platform.system() + " " + platform.release())

    # httpx (required for key validation)
    try:
        import httpx as _httpx
        _ok(f"httpx {_httpx.__version__}")
    except ImportError:
        _fail("httpx not found", "pip install httpx")
        ok = False

    return ok


# ── .env helpers ──────────────────────────────────────────────────────────────

def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _write_env(path: Path, env: dict[str, str]) -> None:
    lines = []
    for k, v in env.items():
        lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


# ── Key validation ────────────────────────────────────────────────────────────

async def _test_cerebras(key: str) -> tuple[bool, str]:
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama3.1-8b",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.cerebras.ai/v1/chat/completions",
                headers=headers,
                json=payload,
            )
        if r.status_code == 200:
            return True, ""
        if r.status_code == 401:
            return False, "invalid key (401 Unauthorized)"
        return False, f"API returned {r.status_code}"
    except Exception as exc:
        return False, str(exc)


async def _test_anthropic(key: str) -> tuple[bool, str]:
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
            )
        if r.status_code in (200, 400):  # 400 = bad params but auth ok
            return True, ""
        if r.status_code == 401:
            return False, "invalid key (401 Unauthorized)"
        return False, f"API returned {r.status_code}"
    except Exception as exc:
        return False, str(exc)


# ── Cerebras key setup ────────────────────────────────────────────────────────

def _setup_cerebras(env: dict[str, str]) -> dict[str, str]:
    _section("CEREBRAS API KEY")

    existing = env.get("CEREBRAS_API_KEY", "").strip()
    if existing and existing != "csk-xxx":
        _ok(f"Existing key detected: {_mask_key(existing)}", "skipping validation")
        return env

    print(f"  {DOT}  {_dm('Get a free key at')} {_w('cloud.cerebras.ai')}")
    print()

    for attempt in range(3):
        try:
            key = getpass.getpass(
                f"  {ARROW}  {_g('Paste your Cerebras API key')} {_dm('(input hidden)')}: "
            )
        except KeyboardInterrupt:
            print()
            raise
        except EOFError:
            _warn("Non-interactive mode — skipping Cerebras key prompt")
            return env

        key = key.strip()
        if not key:
            _warn("No key entered — skipping Cerebras setup")
            return env

        print(f"  {DOT}  {_dm('Validating key...')}", end="", flush=True)
        valid, err = asyncio.run(_test_cerebras(key))

        if valid:
            print(f"\r  {TICK}  Key validated {_dm(_mask_key(key))}")
            env["CEREBRAS_API_KEY"] = key
            return env
        else:
            print(f"\r  {CROSS}  {_rd(err)}")
            if attempt < 2 and _yn("    Try a different key?", default=True):
                continue
            _warn("Skipping Cerebras key — you can add it to .env manually later")
            return env

    return env


# ── Anthropic key setup (optional) ───────────────────────────────────────────

def _setup_anthropic(env: dict[str, str]) -> dict[str, str]:
    _section("ANTHROPIC API KEY  (optional — for swap-back forecasting)")

    existing = env.get("ANTHROPIC_API_KEY", "").strip()
    if existing:
        _ok(f"Existing key detected: {_mask_key(existing)}", "skipping")
        return env

    if not _yn("  Add an Anthropic key now?", default=False):
        _info("Skipped", "set ANTHROPIC_API_KEY in .env to enable later")
        return env

    try:
        key = getpass.getpass(
            f"  {ARROW}  {_g('Paste your Anthropic API key')} {_dm('(input hidden)')}: "
        )
    except KeyboardInterrupt:
        print()
        raise
    except EOFError:
        _info("Skipped", "non-interactive mode")
        return env

    key = key.strip()
    if not key:
        _info("Skipped")
        return env

    print(f"  {DOT}  {_dm('Validating...')}", end="", flush=True)
    valid, err = asyncio.run(_test_anthropic(key))
    if valid:
        print(f"\r  {TICK}  Key validated {_dm(_mask_key(key))}")
        env["ANTHROPIC_API_KEY"] = key
    else:
        print(f"\r  {CROSS}  {_rd(err)}")
        _warn("Key not saved — add it to .env manually if needed")

    return env


# ── .env write ────────────────────────────────────────────────────────────────

def _persist_env(env: dict[str, str]) -> bool:
    _section("WRITING .env")
    try:
        _write_env(ENV_PATH, env)
        _ok(f".env written to {ENV_PATH}", "mode 600")
        return True
    except PermissionError:
        _fail("Permission denied writing .env")
        print(f"  {DOT}  {_dm('Try:')} {_w(f'chmod 755 {_REPO}')}")
        return False
    except Exception as exc:
        _fail(f"Write failed: {exc}")
        return False


# ── Database init ─────────────────────────────────────────────────────────────

def _setup_database() -> None:
    _section("DATABASE")

    try:
        from octagon.octagon_config import CONFIG
        db_path = Path(CONFIG.db_path).expanduser()
    except Exception:
        db_path = _DEFAULT_DB_PATH

    if db_path.exists():
        try:
            con = sqlite3.connect(str(db_path))
            n_pred  = con.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
            n_trade = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            n_res   = con.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]
            con.close()
            _ok(f"Existing database: {db_path.name}")
            _info("predictions", str(n_pred))
            _info("trades", str(n_trade))
            _info("resolutions", str(n_res))
        except Exception as exc:
            _warn(f"Database exists but couldn't query it: {exc}")
        return

    # Fresh init
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from octagon.octagon_ledger import OctagonLedger
        OctagonLedger()
        _ok("Database initialized", str(db_path))
        _info("Schema created", "markets · predictions · trades · resolutions")
    except Exception as exc:
        _fail(f"Database init failed: {exc}")


# ── freq_lever.json init ──────────────────────────────────────────────────────

def _setup_freq_lever() -> None:
    _section("FREQ LEVER")

    if FREQ_LEVER_PATH.exists():
        try:
            data = json.loads(FREQ_LEVER_PATH.read_text())
            mode = data.get("mode", "auto")
            pos  = data.get("manual_position", 3)
            _ok("freq_lever.json found")
            _info("mode", mode)
            _info("manual_position", str(pos))
        except Exception as exc:
            _warn(f"Couldn't parse freq_lever.json: {exc}")
        return

    defaults = {
        "mode": "auto",
        "manual_position": 3,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    FREQ_LEVER_PATH.write_text(json.dumps(defaults, indent=2))
    _ok("freq_lever.json created with defaults")
    _info("mode", "auto — tier determined by bankroll")
    _info("manual_position", "3 — P03_Default (change via HUD)")


# ── Log directory ─────────────────────────────────────────────────────────────

def _setup_dirs() -> None:
    for d in (_DEFAULT_LOG_DIR, _REPO / "traces"):
        d.mkdir(parents=True, exist_ok=True)


# ── Final status panel ────────────────────────────────────────────────────────

def _final_status(env_ok: bool) -> None:
    _section("READY")

    print(f"""
  {_b(_g('Launch the daemon'))}
  {_dm('$')} {_w('python -m octagon.main')}

  {_b(_g('Open the HUD'))}
  {_w('http://127.0.0.1:7711')}      {_dm('(start octagon_hud_server first)')}
  {_dm('$')} {_w('python -m octagon.octagon_hud_server &')}

  {_b(_g('Tail the logs'))}
  {_dm('$')} {_w('tail -f logs/octagon.log')}

  {_b(_g('Adjust trading aggression'))}
  {_dm('Use the freq lever slider in the HUD panel')}
""")

    if not env_ok:
        _warn(".env was not written — add keys manually before launching")

    print(_dm("  ─" * 35))
    print(f"  {_gd('Doctor Octogon is ready.')}")
    print(_dm("  ─" * 35))
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        _banner()
        sys_ok = _check_system()
        if not sys_ok:
            print()
            _warn("System check failed — fix issues above and re-run setup")

        # Load or start fresh .env
        env = _read_env(ENV_PATH)
        if ENV_PATH.exists():
            _section("EXISTING .env DETECTED")
            _ok(f"{ENV_PATH}")
            keys_present = [k for k in ("CEREBRAS_API_KEY", "ANTHROPIC_API_KEY") if env.get(k)]
            if keys_present:
                _info("Keys already set", ", ".join(keys_present))
        else:
            _section("NO .env FOUND — creating one")

        env = _setup_cerebras(env)
        env = _setup_anthropic(env)
        env_ok = _persist_env(env)

        _setup_database()
        _setup_freq_lever()
        _setup_dirs()
        _final_status(env_ok)

    except KeyboardInterrupt:
        print(f"\n\n  {_yl('Setup interrupted.')} No partial .env was written.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
