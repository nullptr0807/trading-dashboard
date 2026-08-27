"""Fail-closed Moomoo OpenD adapter for a real-money account.

All portfolio, order, deal and quote data in the live-account module comes from
OpenD.  The paper-trading SQLite database is never consulted here.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from core.moomoo_audit import (
    account_execution_lock, claim_preview, is_module_order, is_module_preview,
    module_preview_record, register_preview,
)
from core.live_strategy_control import ControlRejected, LiveStrategyStore
from core.live_logging import get_live_logger, log_event

_moomoo_logger = get_live_logger("live.moomoo.api", "moomoo-api.jsonl")


class MoomooUnavailable(RuntimeError):
    pass


class BrokerOutcomeUnknown(MoomooUnavailable):
    """The broker call failed after dispatch; reconciliation is mandatory."""


class LiveTradeRejected(RuntimeError):
    pass


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MoomooSettings:
    host: str = "127.0.0.1"
    port: int = 11111
    security_firm: str = "FUTUAU"
    trade_market: str = "US"
    account_id: int = 0
    currency: str = "USD"
    trading_enabled: bool = False
    auto_trading_enabled: bool = False
    trade_api_token: str = ""
    password_md5: str = ""
    minimum_nav: float = 10_000.0
    max_order_notional: float = 2_500.0
    max_daily_order_notional: float = 5_000.0
    max_limit_deviation_pct: float = 0.02
    preview_ttl_seconds: int = 90
    rth_only: bool = True
    max_quote_age_seconds: int = 120
    strategy_id: str = "B16"
    top_n: int = 6
    position_target_pct: float = 0.147
    gross_target_pct: float = 0.88
    stop_loss_pct: float = 0.08
    stop_cooldown_hours: int = 72
    min_hold_days: int = 0
    hold_band_mult: int = 4
    rebalance_hours: int = 12
    activity_lookback_days: int = 90
    read_api_token: str = ""
    control_api_token: str = ""
    account_mode: str = "UNVERIFIED"
    dedicated_account_confirmed: bool = False
    shared_account_risk_accepted: bool = False

    @classmethod
    def from_env(cls) -> "MoomooSettings":
        return cls(
            host=os.getenv("MOOMOO_OPEND_HOST", "127.0.0.1"),
            port=_int_env("MOOMOO_OPEND_PORT", 11111),
            security_firm=os.getenv("MOOMOO_SECURITY_FIRM", "FUTUAU").upper(),
            trade_market=os.getenv("MOOMOO_TRADE_MARKET", "US").upper(),
            account_id=_int_env("MOOMOO_ACCOUNT_ID", 0),
            currency=os.getenv("MOOMOO_CURRENCY", "USD").upper(),
            trading_enabled=_bool_env("MOOMOO_TRADING_ENABLED"),
            auto_trading_enabled=_bool_env("MOOMOO_AUTO_TRADING_ENABLED"),
            trade_api_token=os.getenv("MOOMOO_TRADE_API_TOKEN", ""),
            password_md5=os.getenv("MOOMOO_TRADE_PASSWORD_MD5", ""),
            minimum_nav=_float_env("MOOMOO_MINIMUM_NAV", 10_000.0),
            max_order_notional=_float_env("MOOMOO_MAX_ORDER_NOTIONAL", 2_500.0),
            max_daily_order_notional=_float_env("MOOMOO_MAX_DAILY_ORDER_NOTIONAL", 5_000.0),
            max_limit_deviation_pct=_float_env("MOOMOO_MAX_LIMIT_DEVIATION_PCT", 0.02),
            preview_ttl_seconds=_int_env("MOOMOO_PREVIEW_TTL_SECONDS", 90),
            rth_only=_bool_env("MOOMOO_RTH_ONLY", True),
            max_quote_age_seconds=_int_env("MOOMOO_MAX_QUOTE_AGE_SECONDS", 120),
            strategy_id=os.getenv("MOOMOO_STRATEGY_ID", "B16"),
            top_n=_int_env("MOOMOO_TOP_N", 6),
            position_target_pct=_float_env("MOOMOO_POSITION_TARGET_PCT", 0.147),
            gross_target_pct=_float_env("MOOMOO_GROSS_TARGET_PCT", 0.88),
            stop_loss_pct=_float_env("MOOMOO_STOP_LOSS_PCT", 0.08),
            stop_cooldown_hours=_int_env("MOOMOO_STOP_COOLDOWN_HOURS", 72),
            min_hold_days=_int_env("MOOMOO_MIN_HOLD_DAYS", 0),
            hold_band_mult=_int_env("MOOMOO_HOLD_BAND_MULT", 4),
            rebalance_hours=_int_env("MOOMOO_REBALANCE_HOURS", 12),
            activity_lookback_days=_int_env("MOOMOO_ACTIVITY_LOOKBACK_DAYS", 90),
            read_api_token=os.getenv("MOOMOO_READ_API_TOKEN", ""),
            control_api_token=os.getenv("MOOMOO_CONTROL_API_TOKEN", ""),
            account_mode=os.getenv("MOOMOO_ACCOUNT_MODE", "UNVERIFIED").strip().upper(),
            dedicated_account_confirmed=_bool_env("MOOMOO_DEDICATED_ACCOUNT_CONFIRMED", False),
            shared_account_risk_accepted=_bool_env("MOOMOO_SHARED_ACCOUNT_RISK_ACCEPTED", False),
        )

    @property
    def account_isolation_mode(self) -> str:
        mode = self.account_mode.upper()
        if (mode == "DEDICATED" and self.dedicated_account_confirmed
                and not self.shared_account_risk_accepted):
            return "dedicated"
        if (mode == "SHARED_RESTRICTED" and self.shared_account_risk_accepted
                and not self.dedicated_account_confirmed):
            return "shared_restricted"
        if (mode == "UNVERIFIED" and not self.dedicated_account_confirmed
                and not self.shared_account_risk_accepted):
            return "unverified"
        return "invalid"

    def public_policy(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "market": self.trade_market,
            "currency": self.currency,
            "minimum_nav": self.minimum_nav,
            "max_order_notional": self.max_order_notional,
            "max_daily_order_notional": self.max_daily_order_notional,
            "max_limit_deviation_pct": self.max_limit_deviation_pct,
            "limit_orders_only": True,
            "short_selling": False,
            "margin_orders": False,
            "rth_only": self.rth_only,
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "fill_outside_rth": False,
            "top_n": self.top_n,
            "position_target_pct": self.position_target_pct,
            "gross_target_pct": self.gross_target_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "stop_cooldown_hours": self.stop_cooldown_hours,
            "min_hold_days": self.min_hold_days,
            "hold_band_mult": self.hold_band_mult,
            "hold_rank_max": self.top_n * self.hold_band_mult,
            "rebalance_hours": self.rebalance_hours,
            "take_profit_pct": None,
            "trailing_stop_pct": None,
            "auto_trading_enabled": self.auto_trading_enabled,
            "activity_lookback_days": self.activity_lookback_days,
            "strategy_capital_limit": 10_000.0,
            "strategy_loss_floor": 7_500.0,
            "account_isolation_mode": self.account_isolation_mode,
            "shared_account_residual_risk": self.account_isolation_mode == "shared_restricted",

        }

    def configuration_errors(self) -> list[str]:
        errors = []
        checks = {
            "minimum_nav": (self.minimum_nav, 1_000, 100_000_000),
            "max_order_notional": (self.max_order_notional, 1, 10_000_000),
            "max_daily_order_notional": (self.max_daily_order_notional, 1, 100_000_000),
            "max_limit_deviation_pct": (self.max_limit_deviation_pct, 0.0001, 0.20),
            "position_target_pct": (self.position_target_pct, 0.001, 1.0),
            "gross_target_pct": (self.gross_target_pct, 0.01, 1.0),
            "stop_loss_pct": (self.stop_loss_pct, 0.001, 0.50),
        }
        for name, (value, low, high) in checks.items():
            if not math.isfinite(value) or not low <= value <= high:
                errors.append(f"Invalid {name}")
        if self.max_order_notional > self.max_daily_order_notional:
            errors.append("max_order_notional exceeds max_daily_order_notional")
        if self.preview_ttl_seconds < 10 or self.preview_ttl_seconds > 600:
            errors.append("Invalid preview_ttl_seconds")
        if self.max_quote_age_seconds < 10 or self.max_quote_age_seconds > 900:
            errors.append("Invalid max_quote_age_seconds")
        if self.account_id < 0:
            errors.append("Invalid account_id")
        if self.account_mode.upper() not in {"UNVERIFIED", "DEDICATED", "SHARED_RESTRICTED"}:
            errors.append("Invalid explicit account mode")
        if self.account_isolation_mode == "invalid":
            errors.append("Account mode, dedicated evidence, and shared risk acceptance must agree")
        if self.auto_trading_enabled and not self.trading_enabled:
            errors.append("Auto trading requires the real-order master switch")
        if not self.rth_only:
            errors.append("Regular-hours-only trading is immutable")
        if not 1 <= self.top_n <= 50:
            errors.append("Invalid top_n")
        if not 1 <= self.hold_band_mult <= 10:
            errors.append("Invalid hold_band_mult")
        if not 1 <= self.rebalance_hours <= 168:
            errors.append("Invalid rebalance_hours")
        if not 0 <= self.stop_cooldown_hours <= 720:
            errors.append("Invalid stop_cooldown_hours")
        return errors


class MoomooClient:
    def __init__(self, settings: MoomooSettings | None = None, sdk: Any = None, clock=None,
                 control_store: Any | None = None):
        self.settings = settings or MoomooSettings.from_env()
        self._sdk = sdk
        self._preview_secret = secrets.token_bytes(32)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._trade_lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._snapshot_cache: dict[str, Any] | None = None
        self.control = control_store or LiveStrategyStore()

    def _load_sdk(self):
        if self._sdk is None:
            try:
                import moomoo as sdk
            except ImportError as exc:
                raise MoomooUnavailable("Moomoo Python SDK is not installed") from exc
            self._sdk = sdk
        return self._sdk

    def _port_open(self) -> bool:
        try:
            with socket.create_connection((self.settings.host, self.settings.port), timeout=0.7):
                return True
        except OSError:
            return False

    def _control_sync_is_fresh(self, state: Any) -> bool:
        try:
            synced = datetime.fromisoformat(str(state.last_sync_at).replace("Z", "+00:00"))
            if synced.tzinfo is None:
                synced = synced.replace(tzinfo=timezone.utc)
            age = (self._clock().astimezone(timezone.utc) - synced.astimezone(timezone.utc)).total_seconds()
            return -60 <= age <= 7 * 60
        except (AttributeError, TypeError, ValueError):
            return False

    def current_sync_fingerprint(self) -> str:
        state = self.control.snapshot()
        payload = {
            "account_id": int(self.settings.account_id),
            "configured_account_mode": self.settings.account_mode,
            "account_isolation_mode": self.settings.account_isolation_mode,
            "shared_account_risk_accepted": self.settings.shared_account_risk_accepted,
            "trading_enabled": self.settings.trading_enabled,
            "auto_trading_enabled": self.settings.auto_trading_enabled,
            "config_version": int(state.config_version),
        }
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        self.control.observe_runtime_fingerprint(fingerprint)
        return fingerprint

    def _enum(self, group: str, value: str):
        sdk = self._load_sdk()
        obj = getattr(sdk, group)
        if not hasattr(obj, value):
            raise MoomooUnavailable(f"Unsupported {group}: {value}")
        return getattr(obj, value)

    @contextmanager
    def _trade_context(self) -> Iterator[Any]:
        sdk = self._load_sdk()
        ctx = sdk.OpenSecTradeContext(
            filter_trdmarket=self._enum("TrdMarket", self.settings.trade_market),
            host=self.settings.host,
            port=self.settings.port,
            security_firm=self._enum("SecurityFirm", self.settings.security_firm),
        )
        try:
            yield ctx
        finally:
            ctx.close()

    @contextmanager
    def _quote_context(self) -> Iterator[Any]:
        sdk = self._load_sdk()
        ctx = sdk.OpenQuoteContext(
            host=self.settings.host,
            port=self.settings.port,
            security_firm=self._enum("SecurityFirm", self.settings.security_firm),
        )
        try:
            yield ctx
        finally:
            ctx.close()

    def _result(self, result: Any, action: str):
        sdk = self._load_sdk()
        if not isinstance(result, tuple) or len(result) < 2 or result[0] != sdk.RET_OK:
            log_event(_moomoo_logger, "error", "moomoo_api_failed", action=action)
            raise MoomooUnavailable(
                f"Moomoo {action} failed; inspect protected broker diagnostics locally"
            )
        data = result[1]
        rows = len(data) if hasattr(data, "__len__") and not isinstance(data, str) else None
        log_event(_moomoo_logger, "info", "moomoo_api_ok", action=action, rows=rows)
        return data

    @staticmethod
    def _records(frame: Any) -> list[dict[str, Any]]:
        if frame is None:
            return []
        if hasattr(frame, "to_dict"):
            rows = frame.to_dict(orient="records")
        elif isinstance(frame, list):
            rows = frame
        elif isinstance(frame, dict):
            rows = [frame]
        else:
            return []
        clean = []
        for row in rows:
            item = {}
            for key, value in dict(row).items():
                if hasattr(value, "item"):
                    try:
                        value = value.item()
                    except Exception:
                        pass
                if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                    value = None
                item[str(key)] = value
            clean.append(item)
        return clean

    def _account_rows(self, ctx: Any) -> list[dict[str, Any]]:
        return self._records(self._result(ctx.get_acc_list(), "account list"))

    def _select_account_id(self, rows: list[dict[str, Any]]) -> int:
        real = [r for r in rows if str(r.get("trd_env", "")).upper() == "REAL"]
        if self.settings.account_id:
            real = [r for r in real if int(r.get("acc_id") or 0) == self.settings.account_id]
        if len(real) != 1:
            raise MoomooUnavailable(
                "Select exactly one REAL account with MOOMOO_ACCOUNT_ID" if len(real) > 1
                else "Configured REAL Moomoo account was not found"
            )
        return int(real[0]["acc_id"])

    def status(self) -> dict[str, Any]:
        fingerprint = self.current_sync_fingerprint()
        control = self.control.snapshot()
        base = {
            "sdk_installed": True,
            "opend_connected": False,
            "real_account_selected": False,
            "account_id": self.settings.account_id or None,
            "security_firm": self.settings.security_firm,
            "market": self.settings.trade_market,
            "currency": self.settings.currency,
            "trading_enabled": self.settings.trading_enabled,
            "auto_trading_enabled": self.settings.auto_trading_enabled,
            "trade_token_configured": bool(self.settings.trade_api_token),
            "read_token_configured": bool(self.settings.read_api_token),
            "unlock_secret_configured": bool(self.settings.password_md5),
            "explicit_account_configured": self.settings.account_id > 0,
            "configuration_errors": self.settings.configuration_errors(),
            "control_token_configured": bool(self.settings.control_api_token),
            "dedicated_account_confirmed": self.settings.dedicated_account_confirmed,
            "shared_account_risk_accepted": self.settings.shared_account_risk_accepted,
            "configured_account_mode": self.settings.account_mode,
            "account_isolation_mode": self.settings.account_isolation_mode,
            "control_sync_fresh": self._control_sync_is_fresh(control),
            "sync_proof_current": self.control.broker_sync_proof_matches(fingerprint),
            "control_generation": self.control.current_control_generation(),
            "strategy_control": control.__dict__,
            "place_order_ready": False,
            "message": None,
        }
        try:
            self._load_sdk()
        except MoomooUnavailable as exc:
            base.update(sdk_installed=False, message=str(exc))
            return base
        if not self._port_open():
            base["message"] = f"OpenD is not reachable at {self.settings.host}:{self.settings.port}"
            return base
        try:
            with self._trade_context() as ctx:
                rows = self._account_rows(ctx)
                account_id = self._select_account_id(rows)
            base.update(opend_connected=True, real_account_selected=True, account_id=account_id)
        except Exception as exc:
            base["opend_connected"] = True
            base["message"] = str(exc)
            return base
        base["place_order_ready"] = all([
            base["real_account_selected"], self.settings.account_id > 0,
            self.settings.trading_enabled, not base["configuration_errors"],
            bool(self.settings.read_api_token), bool(self.settings.trade_api_token),
            bool(self.settings.password_md5),
            self.settings.account_isolation_mode in {"dedicated", "shared_restricted"},
            base["control_sync_fresh"],
            base["sync_proof_current"],

            control.lifecycle == "ACTIVE",
        ])
        if self.settings.account_isolation_mode == "shared_restricted":
            base["message"] = (
                "Restricted shared-account mode: logical isolation only; "
                "manual broker activity can disturb strategy lots"
            )
        elif not base["place_order_ready"]:
            base["message"] = "Read-only mode: real-order safety gates are not fully enabled"
        return base

    def public_policy(self) -> dict[str, Any]:
        policy = self.settings.public_policy()
        try:
            runtime = self.control.config()
            policy.update(runtime.get("values") or {})
            policy["config_version"] = runtime.get("version")
        except Exception:
            policy["config_version"] = None
        policy["strategy_capital_limit"] = 10_000.0
        policy["strategy_loss_floor"] = 7_500.0
        policy["rth_only"] = True
        policy["fill_outside_rth"] = False
        return policy

    def _account_snapshot_with_ctx(self, ctx: Any, account_id: int) -> dict[str, Any]:
        sdk = self._load_sdk()
        env = sdk.TrdEnv.REAL
        accinfo = self._records(self._result(ctx.accinfo_query(
            trd_env=env, acc_id=account_id, refresh_cache=True,
            currency=self.settings.currency,
        ), "account info"))
        positions = self._records(self._result(ctx.position_list_query(
            trd_env=env, acc_id=account_id, refresh_cache=True,
            currency=self.settings.currency,
        ), "positions"))
        current_orders = self._records(self._result(ctx.order_list_query(
            trd_env=env, acc_id=account_id, refresh_cache=True,
        ), "orders"))
        current_deals = self._records(self._result(ctx.deal_list_query(
            trd_env=env, acc_id=account_id, refresh_cache=True,
        ), "deals"))
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=max(1, self.settings.activity_lookback_days))
        warnings: list[str] = []
        try:
            history_orders = self._records(self._result(ctx.history_order_list_query(
                start=start.isoformat(), end=end.isoformat(), trd_env=env, acc_id=account_id,
            ), "order history"))
        except MoomooUnavailable as exc:
            history_orders = []
            warnings.append(str(exc))
        try:
            history_deals = self._records(self._result(ctx.history_deal_list_query(
                start=start.isoformat(), end=end.isoformat(), trd_env=env, acc_id=account_id,
            ), "deal history"))
        except MoomooUnavailable as exc:
            history_deals = []
            warnings.append(str(exc))
        def merge(rows: list[dict[str, Any]], history: list[dict[str, Any]], key: str):
            merged: dict[str, dict[str, Any]] = {}
            for index, row in enumerate(history + rows):
                identity = str(row.get(key) or f"row-{index}-{row.get('code')}-{row.get('create_time')}")
                merged[identity] = row
            return list(merged.values())
        orders = merge(current_orders, history_orders, "order_id")
        deals = merge(current_deals, history_deals, "deal_id")
        module_order_ids = [str(row.get("order_id")) for row in orders
                            if row.get("order_id") is not None
                            and str(row.get("remark") or "").startswith("dashboard:")]
        order_fees: list[dict[str, Any]] = []
        if module_order_ids:
            try:
                order_fees = self._records(self._result(ctx.order_fee_query(
                    order_id_list=module_order_ids, trd_env=env, acc_id=account_id,
                ), "order fees"))
            except MoomooUnavailable as exc:
                warnings.append(str(exc))
        return {"account": accinfo[0] if accinfo else {}, "positions": positions,
                "orders": orders, "deals": deals, "order_fees": order_fees,
                "activity_warnings": warnings}

    def snapshot(self) -> dict[str, Any]:
        if not self._port_open():
            raise MoomooUnavailable("Moomoo OpenD is not connected")
        with self._trade_context() as ctx:
            account_id = self._select_account_id(self._account_rows(ctx))
            data = self._account_snapshot_with_ctx(ctx, account_id)
        data.update(account_id=account_id, source="Moomoo OpenD", fetched_at=time.time())
        log_event(_moomoo_logger, "info", "moomoo_snapshot",
                  positions=len(data.get("positions", [])), orders=len(data.get("orders", [])),
                  deals=len(data.get("deals", [])), warnings=len(data.get("activity_warnings", [])))
        return data

    def snapshot_cached(self, max_age_seconds: int = 300) -> dict[str, Any]:
        now = time.time()
        with self._snapshot_lock:
            cached = self._snapshot_cache
            if cached and now - float(cached.get("fetched_at") or 0) <= max_age_seconds:
                return cached
            fresh = self.snapshot()
            self._snapshot_cache = fresh
            return fresh

    @staticmethod
    def normalize_code(code: str) -> str:
        raw = str(code or "").strip().upper()
        if raw.startswith("US."):
            raw = raw[3:]
        if not raw or not all(ch.isalnum() or ch in {"-", "."} for ch in raw):
            raise LiveTradeRejected("Invalid US symbol")
        return "US." + raw

    def quote(self, code: str) -> dict[str, Any]:
        code = self.normalize_code(code)
        if not self._port_open():
            raise MoomooUnavailable("Moomoo OpenD is not connected")
        with self._quote_context() as ctx:
            rows = self._records(self._result(ctx.get_market_snapshot([code]), "market snapshot"))
            market_rows = self._records(self._result(ctx.get_market_state([code]), "market state"))
        if not rows:
            raise MoomooUnavailable(f"No Moomoo quote for {code}")
        row = rows[0]
        market_row = market_rows[0] if market_rows else {}
        if str(row.get("code") or "").upper() != code:
            raise MoomooUnavailable("Moomoo returned a quote for the wrong symbol")
        prices = {}
        for key in ("last_price", "bid_price", "ask_price"):
            try:
                value = float(row.get(key))
            except (TypeError, ValueError):
                value = 0.0
            if not math.isfinite(value) or value < 0:
                raise MoomooUnavailable(f"Moomoo returned invalid {key}")
            prices[key] = value
        quote = {
            "code": code,
            **prices,
            "update_time": row.get("update_time"),
            "sec_status": row.get("sec_status"),
            "market_state": market_row.get("market_state"),
            "source": "Moomoo OpenD",
        }
        log_event(_moomoo_logger, "info", "moomoo_quote", symbol=code,
                  last_price=prices["last_price"], market_state=quote["market_state"],
                  update_time=quote["update_time"])
        return quote

    @staticmethod
    def _number(row: dict[str, Any], *keys: str) -> float:
        for key in keys:
            try:
                value = row.get(key)
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                pass
        return 0.0

    @staticmethod
    def _required_number(row: dict[str, Any], key: str) -> float:
        if key not in row or row.get(key) is None:
            raise LiveTradeRejected(f"Moomoo account field {key} is missing")
        try:
            value = float(row[key])
        except (TypeError, ValueError) as exc:
            raise LiveTradeRejected(f"Moomoo account field {key} is invalid") from exc
        if not math.isfinite(value) or value < 0:
            raise LiveTradeRejected(f"Moomoo account field {key} is invalid")
        return value

    def _broker_order_is_proven_module(self, order: dict[str, Any], account_id: int) -> bool:
        order_id = str(order.get("order_id") or "")
        remark = str(order.get("remark") or "")
        preview_id = remark.rsplit(":", 1)[-1] if remark.startswith("dashboard:") else ""
        record = module_preview_record(preview_id, account_id) if preview_id else None
        if not record:
            return False
        if record.get("order_id") and str(record["order_id"]) != order_id:
            return False
        payload = record["payload"]
        return all([
            str(order.get("code") or "").upper() == str(payload.get("code") or "").upper(),
            str(order.get("trd_side") or "").upper() == str(payload.get("side") or "").upper(),
            abs(self._number(order, "qty") - self._number(payload, "qty")) <= 1e-9,
            abs(self._number(order, "price") - self._number(payload, "limit_price")) <= 1e-6,
            int(payload.get("account_id") or 0) == int(account_id),
        ])

    def preview_order(self, *, code: str, side: str, qty: int, limit_price: float,
                      _register: bool = True) -> dict[str, Any]:
        config_errors = self.settings.configuration_errors()
        if config_errors:
            raise LiveTradeRejected("Invalid live-trade configuration: " + "; ".join(config_errors))
        side = side.upper()
        if side not in {"BUY", "SELL"}:
            raise LiveTradeRejected("Only BUY and SELL are supported")
        if int(qty) != qty or qty <= 0:
            raise LiveTradeRejected("Quantity must be a positive whole number")
        if not math.isfinite(limit_price) or limit_price <= 0:
            raise LiveTradeRejected("A positive limit price is required")
        code = self.normalize_code(code)
        policy = self.public_policy()
        now = self._clock().astimezone(timezone.utc)
        control_state = self.control.snapshot()
        if self.settings.trading_enabled and not self._control_sync_is_fresh(control_state):
            raise LiveTradeRejected("A fresh Moomoo reconciliation within 7 minutes is required")
        if (self.settings.trading_enabled
                and not self.control.broker_sync_proof_matches(self.current_sync_fingerprint())):
            raise LiveTradeRejected("Current account isolation generation requires a new broker reconciliation")

        eastern = now.astimezone(ZoneInfo("America/New_York"))
        minute = eastern.hour * 60 + eastern.minute
        if eastern.weekday() >= 5 or not (570 <= minute < 960):
            raise LiveTradeRejected("Real orders are restricted to US regular trading hours")
        snap = self.snapshot()
        if snap.get("activity_warnings"):
            raise LiveTradeRejected("Moomoo order/deal history is incomplete; trading is blocked")
        quote = self.quote(code)
        if str(quote.get("sec_status") or "").upper() != "NORMAL":
            raise LiveTradeRejected("Moomoo does not report the symbol as normally tradable")
        if str(quote.get("market_state") or "").upper() not in {"MORNING", "AFTERNOON"}:
            raise LiveTradeRejected("Moomoo market state is not regular-hours trading")
        quote_time = quote.get("update_time")
        try:
            parsed = datetime.fromisoformat(str(quote_time).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
            age = (now - parsed.astimezone(timezone.utc)).total_seconds()
            if age < -60 or age > float(policy["max_quote_age_seconds"]):
                raise ValueError
        except (TypeError, ValueError):
            raise LiveTradeRejected("Moomoo quote timestamp is missing or stale")
        last = quote["last_price"]
        if last <= 0:
            raise LiveTradeRejected("Moomoo returned an invalid last price")
        deviation = abs(limit_price / last - 1)
        if deviation > float(policy["max_limit_deviation_pct"]):
            raise LiveTradeRejected(
                f"Limit price deviates {deviation:.2%} from Moomoo last price; "
                f"maximum is {float(policy['max_limit_deviation_pct']):.2%}"
            )
        notional = qty * limit_price
        if notional > float(policy["max_order_notional"]):
            raise LiveTradeRejected(
                f"Order notional ${notional:,.2f} exceeds the server limit "
                f"${float(policy['max_order_notional']):,.2f}"
            )
        account = snap["account"]
        nav = self._required_number(account, "total_assets")
        if nav < self.settings.minimum_nav:
            raise LiveTradeRejected(
                f"Account NAV ${nav:,.2f} is below the required ${self.settings.minimum_nav:,.2f}"
            )
        terminal_statuses = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED"}
        if side == "BUY":
            broker_position = next((p for p in snap["positions"] if str(p.get("code")) == code), None)
            broker_qty = self._number(broker_position or {}, "qty")
            strategy_qty = self.control.owned_quantity(code)
            if (self.settings.account_isolation_mode == "dedicated"
                    and broker_qty > strategy_qty + 1e-9):
                raise LiveTradeRejected(
                    "Symbol overlaps pre-existing/external Moomoo holdings; use a physically isolated symbol or account"
                )
            cash = self._required_number(account, "cash")
            reserved = sum(
                max(0.0, self._number(order, "qty") - self._number(order, "dealt_qty"))
                * self._number(order, "price")
                for order in snap["orders"]
                if str(order.get("trd_side") or "").upper() == "BUY"
                and str(order.get("order_status") or "").upper() not in terminal_statuses
            )
            if notional + reserved > cash:
                raise LiveTradeRejected("Insufficient available cash")
        else:
            held = next((p for p in snap["positions"] if str(p.get("code")) == code), None)
            if not held:
                raise LiveTradeRejected("No Moomoo position is available to sell")
            sellable = self._required_number(held, "can_sell_qty")
            reserved_sell = sum(
                self._number(order, "qty") - self._number(order, "dealt_qty")
                for order in snap["orders"]
                if str(order.get("code") or "") == code
                and str(order.get("trd_side") or "").upper() == "SELL"
                and str(order.get("order_status") or "").upper() not in terminal_statuses
            )
            if qty + max(0, reserved_sell) > sellable:
                raise LiveTradeRejected(f"Sell quantity exceeds available position ({sellable:g})")
        today = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        daily_notional = 0.0
        for order in snap["orders"]:
            created = str(order.get("create_time") or order.get("create_time_str") or "")
            if created[:10] == today:
                daily_notional += self._number(order, "qty") * self._number(order, "price")
        if daily_notional + notional > float(policy["max_daily_order_notional"]):
            raise LiveTradeRejected(
                f"Daily order notional would exceed ${float(policy['max_daily_order_notional']):,.2f}"
            )
        pending_buy = sum(
            max(0.0, self._number(order, "qty") - self._number(order, "dealt_qty"))
            * self._number(order, "price")
            for order in snap["orders"]
            if str(order.get("trd_side") or "").upper() == "BUY"
            and str(order.get("order_status") or "").upper() not in terminal_statuses
            and str(order.get("remark") or "").startswith("dashboard:")
        )
        pending_sell = sum(
            max(0.0, self._number(order, "qty") - self._number(order, "dealt_qty"))
            for order in snap["orders"]
            if str(order.get("code") or "") == code
            and str(order.get("trd_side") or "").upper() == "SELL"
            and str(order.get("order_status") or "").upper() not in terminal_statuses
            and str(order.get("remark") or "").startswith("dashboard:")
        )
        try:
            self.control.pretrade_guard(side, code, qty, limit_price,
                                        pending_buy_notional=pending_buy,
                                        pending_sell_qty=pending_sell)
        except ControlRejected as exc:
            raise LiveTradeRejected(str(exc)) from exc
        payload = {"code": code, "side": side, "qty": int(qty),
                   "limit_price": round(float(limit_price), 6),
                   "account_id": int(snap["account_id"]), "issued_at": int(time.time()),
                   "preview_id": secrets.token_hex(16),
                   "config_version": int(control_state.config_version),
                   "account_isolation_mode": self.settings.account_isolation_mode,
                   "sync_fingerprint": self.current_sync_fingerprint()}
        token = self._sign_preview(payload)
        # A durable ready record is required before the preview can leave the
        # server. If the audit DB is unavailable, preview fails closed.
        if _register:
            register_preview(payload, self.settings.preview_ttl_seconds)
        return {**payload, "notional": notional, "quote": quote,
                "order_type": "LIMIT", "time_in_force": "DAY",
                "fill_outside_rth": False, "preview_token": token,
                "expires_in_seconds": self.settings.preview_ttl_seconds,
                "place_order_ready": self.status()["place_order_ready"]}

    def _sign_preview(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sig = hmac.new(self._preview_secret, raw, hashlib.sha256).digest()
        encoded_raw = base64.urlsafe_b64encode(raw).decode()
        encoded_sig = base64.urlsafe_b64encode(sig).decode()
        return encoded_raw + "." + encoded_sig

    def verify_preview(self, token: str) -> dict[str, Any]:
        try:
            encoded_raw, encoded_sig = token.split(".", 1)
            raw = base64.urlsafe_b64decode(encoded_raw.encode())
            sig = base64.urlsafe_b64decode(encoded_sig.encode())
            expected = hmac.new(self._preview_secret, raw, hashlib.sha256).digest()
            if not hmac.compare_digest(sig, expected):
                raise ValueError
            payload = json.loads(raw)
        except Exception as exc:
            raise LiveTradeRejected("Invalid order preview token") from exc
        if time.time() - int(payload.get("issued_at", 0)) > self.settings.preview_ttl_seconds:
            raise LiveTradeRejected("Order preview expired; refresh the Moomoo quote")
        return payload

    def authenticate_trade_token(self, provided: str) -> None:
        expected = self.settings.trade_api_token
        if not expected or not provided or not hmac.compare_digest(expected, provided):
            raise LiveTradeRejected("Invalid live-trade authorization token")

    def place_order(self, preview_token: str, auth_token: str) -> dict[str, Any]:
        self.authenticate_trade_token(auth_token)
        if not self.settings.trading_enabled:
            raise LiveTradeRejected("Real trading is disabled on the server")
        if self.settings.account_id <= 0:
            raise LiveTradeRejected("MOOMOO_ACCOUNT_ID must explicitly select a real account")
        if self.settings.account_isolation_mode not in {"dedicated", "shared_restricted"}:
            raise LiveTradeRejected("Real trading requires an accepted account isolation mode")
        config_errors = self.settings.configuration_errors()
        if config_errors:
            raise LiveTradeRejected("Invalid live-trade configuration: " + "; ".join(config_errors))
        if not self.settings.password_md5:
            raise LiveTradeRejected("Moomoo trade unlock secret is not configured")
        payload = self.verify_preview(preview_token)
        if not claim_preview(payload["preview_id"]):
            raise LiveTradeRejected("Order preview was already used, expired, or not registered")
        sdk = self._load_sdk()
        # Account-level in-process + file locks close the concurrent BUY/SELL
        # check-to-order race across threads and local uvicorn workers. The signed
        # preview has already been claimed atomically in SQLite above.
        with self._trade_lock, account_execution_lock(self.settings.account_id):
            fresh = self.preview_order(code=payload["code"], side=payload["side"],
                                       qty=payload["qty"], limit_price=payload["limit_price"],
                                       _register=False)
            if int(payload.get("config_version", -1)) != int(fresh.get("config_version", -2)):
                raise LiveTradeRejected("Strategy configuration changed after order preview")
            if payload.get("account_isolation_mode") != fresh.get("account_isolation_mode"):
                raise LiveTradeRejected("Account isolation mode changed after order preview")
            if payload.get("sync_fingerprint") != fresh.get("sync_fingerprint"):
                raise LiveTradeRejected("Account isolation generation changed after order preview")
            if int(payload["account_id"]) != int(fresh["account_id"]):
                raise LiveTradeRejected("Moomoo account changed after order preview")
            with self._trade_context() as ctx:
                account_id = self._select_account_id(self._account_rows(ctx))
                if account_id != self.settings.account_id or account_id != int(payload["account_id"]):
                    raise LiveTradeRejected("Final Moomoo account does not match the signed preview")
                self._result(ctx.unlock_trade(password_md5=self.settings.password_md5), "trade unlock")
                try:
                    broker_result = ctx.place_order(
                        price=fresh["limit_price"], qty=fresh["qty"], code=fresh["code"],
                        trd_side=getattr(sdk.TrdSide, fresh["side"]),
                        order_type=sdk.OrderType.NORMAL, trd_env=sdk.TrdEnv.REAL,
                        acc_id=account_id, time_in_force="DAY", fill_outside_rth=False,
                        remark=f"dashboard:{self.settings.strategy_id}:{payload['preview_id']}",
                    )
                except Exception as exc:
                    raise BrokerOutcomeUnknown(
                        "Moomoo order outcome is unknown; reconcile orders before retrying"
                    ) from exc
                result = self._records(self._result(broker_result, "place order"))
        return {"accepted": True, "source": "Moomoo OpenD", "preview_id": payload["preview_id"],
                "order": result[0] if result else {}}

    def cancel_order(self, order_id: str, auth_token: str) -> dict[str, Any]:
        self.authenticate_trade_token(auth_token)
        if not self.settings.password_md5:
            raise LiveTradeRejected("Moomoo cancellation unlock secret is not configured")
        if self.settings.account_id <= 0:
            raise LiveTradeRejected("MOOMOO_ACCOUNT_ID must explicitly select a real account")
        if not str(order_id).strip():
            raise LiveTradeRejected("Order ID is required")
        sdk = self._load_sdk()
        with self._trade_lock, account_execution_lock(self.settings.account_id):
            with self._trade_context() as ctx:
                account_id = self._select_account_id(self._account_rows(ctx))
                if account_id != self.settings.account_id:
                    raise LiveTradeRejected("Final Moomoo account does not match configuration")
                snapshot = self._account_snapshot_with_ctx(ctx, account_id)
                if snapshot.get("activity_warnings"):
                    raise LiveTradeRejected("Moomoo order history is incomplete; cancellation is blocked")
                broker_order = next(
                    (row for row in snapshot["orders"] if str(row.get("order_id")) == str(order_id)),
                    None,
                )
                if not broker_order:
                    raise LiveTradeRejected("Order is not present in the configured Moomoo account")
                if not str(broker_order.get("remark") or "").startswith("dashboard:"):
                    raise LiveTradeRejected("Order was not created by this dashboard module")
                terminal = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED"}
                if str(broker_order.get("order_status") or "").upper() in terminal:
                    raise LiveTradeRejected("Moomoo order is already in a terminal state")
                self._result(ctx.unlock_trade(password_md5=self.settings.password_md5), "trade unlock")
                try:
                    broker_result = ctx.modify_order(
                        modify_order_op=sdk.ModifyOrderOp.CANCEL, order_id=str(order_id),
                        qty=0, price=0, trd_env=sdk.TrdEnv.REAL, acc_id=account_id,
                    )
                except Exception as exc:
                    raise BrokerOutcomeUnknown(
                        "Moomoo cancellation outcome is unknown; reconcile orders before retrying"
                    ) from exc
                rows = self._records(self._result(broker_result, "cancel order"))
        return {"accepted": True, "source": "Moomoo OpenD", "order": rows[0] if rows else {}}

    def cancel_all_module_orders(self, auth_token: str) -> dict[str, Any]:
        self.authenticate_trade_token(auth_token)
        snapshot = self.snapshot()
        terminal = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED"}
        candidates = [row for row in snapshot.get("orders", [])
                      if str(row.get("remark") or "").startswith("dashboard:")
                      and str(row.get("order_status") or "").upper() not in terminal]
        cancelled, errors = [], []
        for row in candidates:
            order_id = str(row.get("order_id") or "")
            remark = str(row.get("remark") or "")
            preview_id = remark.rsplit(":", 1)[-1] if remark.startswith("dashboard:") else ""
            authorized = (is_module_order(order_id, self.settings.account_id)
                          or is_module_preview(preview_id, self.settings.account_id))
            if not order_id or not authorized:
                errors.append("module_order_not_reconciled")
                continue
            try:
                self.cancel_order(order_id, auth_token)
                cancelled.append(hashlib.sha256(order_id.encode()).hexdigest()[:12])
            except Exception as exc:
                errors.append(type(exc).__name__)
        return {"requested": len(candidates), "cancelled": len(cancelled),
                "cancelled_refs": cancelled, "errors": errors}

    def module_order_authorized(self, order_id: str) -> bool:
        snapshot = self.snapshot()
        row = next((item for item in snapshot.get("orders", [])
                    if str(item.get("order_id") or "") == str(order_id)), None)
        if not row:
            return False
        remark = str(row.get("remark") or "")
        if not remark.startswith("dashboard:"):
            return False
        preview_id = remark.rsplit(":", 1)[-1]
        return (is_module_order(order_id, self.settings.account_id)
                or is_module_preview(preview_id, self.settings.account_id))
