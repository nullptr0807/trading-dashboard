#!/usr/bin/env python3
"""Archive a complete sanitized EOD evidence packet for live-system review."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.live_logging import redact

ROOT = Path(__file__).resolve().parents[1]
STRATEGY_DB = ROOT / "data" / "live_strategy.db"
AUDIT_DB = ROOT / "data" / "moomoo_live_audit.db"
LOG_DIR = ROOT / "logs" / "live_account"
CRON_DIR = Path.home() / ".hermes" / "cron"
ARCHIVE_ROOT = ROOT / "data" / "live_eod_reports"
NY = ZoneInfo("America/New_York")
SENSITIVE_KEY = re.compile(r"password|token|secret|credential|authorization|account.?id|phone|email", re.I)
REFERENCE_KEY = re.compile(r"^(?:order_id|preview_id|account_ref|process_id|pid)$", re.I)
TOKEN_TEXT = re.compile(r"(?i)(?:gh[opusr]_[A-Za-z0-9]{12,}|sk-[A-Za-z0-9_-]{12,}|bearer\s+\S+)")
LONG_NUMBER = re.compile(r"(?<![\d.])\d{7,}(?![\d.])")
TERMINAL_INTENTS = {"FILLED", "CANCELLED", "FAILED"}


def ref(value: Any) -> str | None:
    text = str(value or "").strip()
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:16] if text else None


def sanitize(value: Any, key: str = "") -> Any:
    if SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if REFERENCE_KEY.match(key):
        return ref(value)
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, tuple):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        text = str(redact(TOKEN_TEXT.sub("[REDACTED]", value)))
        if key not in {"ts", "created_at", "updated_at", "applied_at", "started_at", "finished_at", "claimed_at"}:
            text = LONG_NUMBER.sub(lambda m: "[NUMREF:" + hashlib.sha256(m.group().encode()).hexdigest()[:10] + "]", text)
        return text
    return value


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def in_window(value: Any, start: datetime, end: datetime) -> bool:
    parsed = parse_ts(value)
    return bool(parsed and start <= parsed < end)


def ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def rows(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in con.execute(sql, params).fetchall()]


def json_value(value: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return str(value or "")


def collect_strategy(start: datetime, end: datetime) -> dict[str, Any]:
    with ro(STRATEGY_DB) as con:
        state = dict(con.execute("SELECT * FROM strategy_state WHERE id=1").fetchone())
        config_row = con.execute("SELECT * FROM strategy_config WHERE active=1").fetchone()
        config = dict(config_row) if config_row else None
        if config:
            config["config"] = json_value(config.pop("config_json"))
        positions = rows(con, "SELECT * FROM owned_positions ORDER BY symbol")
        fills = [r for r in rows(con, "SELECT * FROM applied_fills ORDER BY applied_at")
                 if in_window(r["applied_at"], start, end)]
        all_fills = rows(con, "SELECT * FROM applied_fills ORDER BY applied_at,fill_hash")
        fee_accounts = rows(con, "SELECT * FROM order_fee_accounts ORDER BY order_hash")
        fee_adjustments = rows(
            con, "SELECT * FROM order_fee_adjustments ORDER BY applied_at,id"
        )
        equity = [r for r in rows(con, "SELECT * FROM strategy_equity ORDER BY ts")
                  if in_window(r["ts"], start, end)]
        events = [r for r in rows(con, "SELECT * FROM strategy_events ORDER BY ts,id")
                  if in_window(r["ts"], start, end)]
        for row in events:
            row["details"] = json_value(row.pop("details_json"))
        intents = []
        for row in rows(con, "SELECT * FROM auto_order_intents ORDER BY created_at,intent_id"):
            if (in_window(row["created_at"], start, end) or in_window(row["updated_at"], start, end)
                    or str(row["status"]) not in TERMINAL_INTENTS):
                row["preview_id"] = ref(row.get("preview_id"))
                intents.append(row)
        quick_check = con.execute("PRAGMA quick_check").fetchone()[0]
    for row in [*fills, *all_fills]:
        row["fill_hash"] = ref(row.get("fill_hash"))
        row["order_hash"] = ref(row.get("order_hash"))
    for row in fee_accounts:
        row["order_hash"] = ref(row.get("order_hash"))
    for row in fee_adjustments:
        row["order_hash"] = ref(row.get("order_hash"))
        row.pop("adjustment_hash", None)
    fill_fee_total = sum(float(row["fee"]) for row in all_fills)
    adjustment_delta_total = sum(float(row["delta"]) for row in fee_adjustments)
    fill_fees_by_order: dict[str | None, float] = {}
    for row in all_fills:
        key = row.get("order_hash")
        fill_fees_by_order[key] = fill_fees_by_order.get(key, 0.0) + float(row["fee"])
    adjustment_by_order: dict[str | None, float] = {}
    for row in fee_adjustments:
        key = row.get("order_hash")
        adjustment_by_order[key] = adjustment_by_order.get(key, 0.0) + float(row["delta"])
    fee_reconciliation = []
    for account in fee_accounts:
        key = account.get("order_hash")
        fill_total = fill_fees_by_order.get(key, 0.0)
        delta_total = adjustment_by_order.get(key, 0.0)
        current_total = float(account["cumulative_fee"])
        fee_reconciliation.append({
            "order_hash": key, "revision": int(account["revision"]),
            "fill_fee_total": fill_total, "adjustment_delta_total": delta_total,
            "reconstructed_total": fill_total + delta_total,
            "current_total": current_total,
            "matches_current_total": abs(fill_total + delta_total - current_total) <= 1e-9,
        })
    return sanitize({
        "quick_check": quick_check,
        "state_at_collection": {k: v for k, v in state.items() if k != "id"},
        "active_config": config,
        "owned_positions_at_collection": positions,
        "fills_during_window": fills,
        "applied_fills_at_collection": all_fills,
        "order_fee_accounts_at_collection": fee_accounts,
        "order_fee_adjustments_at_collection": fee_adjustments,
        "fee_accounting": {
            "identity": "total_fees = all applied fill fees + all order adjustment deltas",
            "fill_fee_total": fill_fee_total,
            "adjustment_delta_total": adjustment_delta_total,
            "reconstructed_total_fees": fill_fee_total + adjustment_delta_total,
            "current_order_cumulative_fee_total": sum(
                float(row["cumulative_fee"]) for row in fee_accounts
            ),
            "by_order": fee_reconciliation,
        },
        "equity_samples_during_window": equity,
        "events_during_window": events,
        "auto_intents_touched_or_unresolved": intents,
    })


def safe_payload(payload: Any) -> dict[str, Any]:
    raw = json_value(payload)
    if not isinstance(raw, dict):
        return {"unparsed": sanitize(raw)}
    allowed = {
        "code", "side", "qty", "limit_price", "session", "fill_outside_rth",
        "config_version", "auto_intent_id", "strategy_id", "created_at",
    }
    result = {key: raw.get(key) for key in allowed if key in raw}
    if "auto_intent_id" in result:
        result["auto_intent_id"] = ref(result["auto_intent_id"])
    return sanitize(result)


def collect_audit(start: datetime, end: datetime) -> dict[str, Any]:
    with ro(AUDIT_DB) as con:
        audit = [r for r in rows(con, "SELECT * FROM live_order_audit ORDER BY ts,id")
                 if in_window(r["ts"], start, end)]
        for row in audit:
            row.pop("account_id", None)
            row["order_id"] = ref(row.get("order_id"))
            row["detail"] = sanitize(json_value(row.get("detail")))
        previews = [r for r in rows(con, "SELECT * FROM live_order_previews ORDER BY created_at")
                    if in_window(r["created_at"], start, end)]
        for row in previews:
            row.pop("account_id", None)
            row["preview_id"] = ref(row.get("preview_id"))
            row["order_id"] = ref(row.get("order_id"))
            row["payload"] = safe_payload(row.get("payload"))
            row.pop("expires_at", None)
        quick_check = con.execute("PRAGMA quick_check").fetchone()[0]
    return sanitize({
        "quick_check": quick_check,
        "order_audit": audit,
        "order_previews": previews,
        "excluded": ["live_nav_snapshots (shared-account personal NAV/cash is outside strategy scope)"],
    })


def collect_json_logs(start: datetime, end: datetime) -> dict[str, Any]:
    by_file: dict[str, list[dict[str, Any]]] = {}
    parse_errors: list[dict[str, Any]] = []
    for path in sorted(LOG_DIR.glob("*.jsonl*")):
        matched = []
        try:
            with path.open(errors="replace") as handle:
                for line_no, line in enumerate(handle, 1):
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        parse_errors.append({"file": path.name, "line": line_no})
                        continue
                    if in_window(item.get("ts"), start, end):
                        matched.append(sanitize(item))
        except OSError as exc:
            parse_errors.append({"file": path.name, "error": type(exc).__name__})
        if matched:
            by_file[path.name] = matched
    counts = {
        name: {
            "records": len(items),
            "levels": dict(Counter(str(i.get("level", "UNKNOWN")) for i in items)),
            "messages": dict(Counter(str(i.get("message", "")) for i in items)),
        }
        for name, items in by_file.items()
    }
    return {"files": by_file, "counts": counts, "parse_errors": parse_errors}


def journal(unit: str, start: datetime, end: datetime) -> dict[str, Any]:
    command = [
        "journalctl", "--no-pager", "-u", unit,
        "--since", start.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "--until", end.strftime("%Y-%m-%d %H:%M:%S UTC"), "-o", "json",
    ]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=45)
    entries = []
    for line in proc.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        micros = item.get("__REALTIME_TIMESTAMP")
        ts = None
        try:
            ts = datetime.fromtimestamp(int(micros) / 1_000_000, timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            pass
        entries.append(sanitize({
            "ts": ts, "priority": item.get("PRIORITY"),
            "message": item.get("MESSAGE", ""),
        }))
    return {
        "return_code": proc.returncode,
        "stderr": sanitize(proc.stderr.strip()),
        "records": entries,
        "priority_counts": dict(Counter(str(row.get("priority")) for row in entries)),
    }


def collect_cron(start: datetime, end: datetime) -> dict[str, Any]:
    jobs_doc = json.loads((CRON_DIR / "jobs.json").read_text())
    all_jobs = jobs_doc.get("jobs", [])
    relevant = [j for j in all_jobs if any(word in str(j.get("name", "")).lower()
                for word in ("moomoo", "live", "b16"))]
    job_ids = {str(j.get("id")) for j in relevant}
    safe_jobs = [{
        "job_id_hash": ref(j.get("id")), "name": j.get("name"),
        "schedule": j.get("schedule"), "enabled": j.get("enabled"),
        "state": j.get("state"), "last_status": j.get("last_status"),
        "last_run_at": j.get("last_run_at"), "next_run_at": j.get("next_run_at"),
        "script": j.get("script"),
    } for j in relevant]
    executions = []
    with ro(CRON_DIR / "executions.db") as con:
        for row in rows(con, "SELECT * FROM executions ORDER BY claimed_at,id"):
            if str(row.get("job_id")) in job_ids and in_window(
                    row.get("started_at") or row.get("claimed_at"), start, end):
                row["job_id_hash"] = ref(row.pop("job_id", None))
                row.pop("id", None)
                row.pop("process_id", None)
                row.pop("pid", None)
                executions.append(sanitize(row))
    outputs = []
    for job_id in job_ids:
        directory = CRON_DIR / "output" / job_id
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if start <= modified < end:
                text = path.read_text(errors="replace")
                outputs.append({
                    "job_id_hash": ref(job_id), "file": path.name,
                    "mtime": modified.isoformat(), "bytes": path.stat().st_size,
                    "content": sanitize(text),
                })
    return {"jobs_at_collection": safe_jobs, "executions": executions, "outputs": outputs}


def compact_review_packet(evidence: dict[str, Any]) -> dict[str, Any]:
    """Reduce every collected record deterministically while retaining all material records."""
    app = evidence["application_logs"]
    app_anomalies = []
    app_samples = {}
    for filename, records in app["files"].items():
        app_anomalies.extend(
            {"file": filename, **row} for row in records
            if str(row.get("level", "")).upper() in {"WARNING", "ERROR", "CRITICAL"}
        )
        by_message: dict[str, list[dict[str, Any]]] = {}
        for row in records:
            by_message.setdefault(str(row.get("message", "")), []).append(row)
        app_samples[filename] = {
            message: {"first": items[0], "last": items[-1], "count": len(items)}
            for message, items in by_message.items()
        }

    journal_summary = {}
    for unit, block in evidence["systemd_journal"].items():
        material = []
        for row in block["records"]:
            message = str(row.get("message", ""))
            try:
                priority = int(row.get("priority"))
            except (TypeError, ValueError):
                priority = 7
            if priority <= 4 or re.search(
                    r"(?i)error|fail|fatal|panic|traceback|restart|start(?:ed|ing)|stop(?:ped|ping)|disconnect|timeout|killed",
                    message):
                material.append(row)
        journal_summary[unit] = {
            "return_code": block["return_code"],
            "stderr": block["stderr"],
            "total_records": len(block["records"]),
            "priority_counts": block["priority_counts"],
            "material_records": material,
        }

    previews = evidence["order_audit"]["order_previews"]
    material_previews = [row for row in previews if str(row.get("status")) != "ready"]
    preview_groups = Counter(
        (str(row.get("status")), str(row.get("outcome")),
         str(row.get("payload", {}).get("code")), str(row.get("payload", {}).get("side")))
        for row in previews
    )
    preview_summary = [
        {"status": key[0], "outcome": key[1], "code": key[2], "side": key[3], "count": count}
        for key, count in sorted(preview_groups.items())
    ]
    strategy = evidence["strategy"]
    return {
        "schema_version": 2,
        "trading_day_ny": evidence["trading_day_ny"],
        "window": evidence["window"],
        "collected_at": evidence["collected_at"],
        "scope": evidence["scope"],
        "strategy": strategy,
        "order_audit": {
            "quick_check": evidence["order_audit"]["quick_check"],
            "order_audit": evidence["order_audit"]["order_audit"],
            "preview_summary_over_all_records": preview_summary,
            "material_non_ready_previews": material_previews,
            "excluded": evidence["order_audit"]["excluded"],
        },
        "application_logs": {
            "counts_over_all_records": app["counts"],
            "message_first_last_samples": app_samples,
            "all_warning_error_critical_records": app_anomalies,
            "parse_errors": app["parse_errors"],
        },
        "systemd_journal": journal_summary,
        "cron": evidence["cron"],
        "reduction_policy": {
            "full_evidence_preserved": True,
            "all_trades_intents_fills_events_order_audits_retained": True,
            "all_application_records_counted": True,
            "all_application_warning_error_critical_retained": True,
            "repeated_info_logs_reduced_to_count_plus_first_last": True,
            "all_journal_records_counted": True,
            "journal_material_records_retained": True,
        },
    }


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n")
    os.chmod(temp, 0o600)
    temp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="New York trading date YYYY-MM-DD; default today in New York")
    args = parser.parse_args()
    trading_day = date.fromisoformat(args.date) if args.date else datetime.now(NY).date()
    start_local = datetime.combine(trading_day, time.min, NY)
    end_local = datetime.combine(trading_day + timedelta(days=1), time.min, NY)
    start, end = start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
    collected_at = datetime.now(timezone.utc)
    archive = ARCHIVE_ROOT / trading_day.isoformat()
    evidence_path = archive / "evidence.json"
    review_path = archive / "review_packet.json"
    report_path = archive / "analysis.md"
    candidates_path = archive / "optimization_candidates.json"
    evidence = {
        "schema_version": 2,
        "trading_day_ny": trading_day.isoformat(),
        "window": {"start_utc": start.isoformat(), "end_utc": end.isoformat()},
        "collected_at": collected_at.isoformat(),
        "scope": {
            "live_strategy_only": True,
            "personal_broker_positions_cash_nav_excluded": True,
            "production_mutation_allowed": False,
            "recommendations_target": "paper-trading experiments only",
        },
        "strategy": collect_strategy(start, end),
        "order_audit": collect_audit(start, end),
        "application_logs": collect_json_logs(start, end),
        "systemd_journal": {
            "trading-dashboard.service": journal("trading-dashboard.service", start, end),
            "moomoo-opend.service": journal("moomoo-opend.service", start, end),
        },
        "cron": collect_cron(start, end),
    }
    atomic_json(evidence_path, evidence)
    atomic_json(review_path, compact_review_packet(evidence))
    digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    manifest = {
        "status": "EVIDENCE_ARCHIVED",
        "trading_day_ny": trading_day.isoformat(),
        "collected_at": collected_at.isoformat(),
        "evidence_path": str(evidence_path),
        "evidence_sha256": digest,
        "evidence_bytes": evidence_path.stat().st_size,
        "review_packet_path": str(review_path),
        "review_packet_bytes": review_path.stat().st_size,
        "report_path": str(report_path),
        "optimization_candidates_path": str(candidates_path),
        "record_counts": {
            "fills": len(evidence["strategy"]["fills_during_window"]),
            "order_fee_accounts": len(
                evidence["strategy"]["order_fee_accounts_at_collection"]
            ),
            "order_fee_adjustments": len(
                evidence["strategy"]["order_fee_adjustments_at_collection"]
            ),
            "events": len(evidence["strategy"]["events_during_window"]),
            "intents": len(evidence["strategy"]["auto_intents_touched_or_unresolved"]),
            "order_audit": len(evidence["order_audit"]["order_audit"]),
            "previews": len(evidence["order_audit"]["order_previews"]),
            "application_logs": sum(len(v) for v in evidence["application_logs"]["files"].values()),
            "journal_records": sum(len(v["records"]) for v in evidence["systemd_journal"].values()),
            "cron_executions": len(evidence["cron"]["executions"]),
        },
        "instructions": "Read review_packet_path completely. It is a deterministic reduction over every full-evidence record. Use evidence_path only for drill-down. Write both required artifacts and verify them.",
    }
    atomic_json(archive / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
