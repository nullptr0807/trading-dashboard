from __future__ import annotations

import re
from pathlib import Path


def test_live_module_contains_no_committed_broker_credentials_or_real_identifiers():
    root = Path(__file__).resolve().parents[1]
    files = [
        root / "api/live_account.py",
        root / "core/moomoo_client.py",
        root / "core/moomoo_audit.py",
        root / "core/live_strategy_control.py",
        root / "core/live_logging.py",
        root / "core/live_auto_executor.py",
        root / "core/live_auto_planner.py",
        root / "core/live_order_service.py",
        root / "core/live_signal_adapter.py",
        root / "static/js/live_account.js",
        root / "docs/moomoo-live-account.md",
    ] + list((root / "scripts").glob("live_*.py")) + list((root / "scripts").glob("live_*.sh"))
    text = "\n".join(path.read_text(errors="ignore") for path in files)
    forbidden = [
        r"gh[opusr]_[A-Za-z0-9]{20,}",
        r"sk-[A-Za-z0-9_-]{20,}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"MOOMOO_ACCOUNT_ID\s*=\s*[0-9]+",
        r"MOOMOO_(?:READ|TRADE|CONTROL)_API_TOKEN\s*=\s*[^<\s][^\s]*",
        r"MOOMOO_TRADE_PASSWORD_MD5\s*=\s*[a-fA-F0-9]{16,}",
    ]
    for pattern in forbidden:
        assert re.search(pattern, text) is None, pattern


def test_runtime_sensitive_directories_are_gitignored():
    root = Path(__file__).resolve().parents[1]
    ignore = (root / ".gitignore").read_text().splitlines()
    assert "data/" in ignore
    assert "logs/" in ignore


def test_auto_wrapper_uses_new_york_window_and_never_passes_token_in_argv():
    root = Path(__file__).resolve().parents[1]
    shell = (root / "scripts/live_auto_cycle.sh").read_text()
    assert "TZ=America/New_York" in shell
    for window in ("0935", "0950", "1005", "1020"):
        assert window in shell
    assert "MOOMOO_TRADE_API_TOKEN" not in shell
    assert 'python scripts/live_auto_cycle.py "$MODE" --quiet' in shell
    cycle = (root / "scripts/live_auto_cycle.py").read_text()
    assert "executor.execute_serial() if args.execute" in cycle
    assert "broker_outcome_unknown_held" in cycle
    assert "broker_outcome_unknown_frozen" not in cycle