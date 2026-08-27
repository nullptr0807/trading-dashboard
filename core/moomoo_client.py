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

from core.moomoo_audit import account_execution_lock, claim_preview, register_preview


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
        )

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
    def __init__(self, settings: MoomooSettings | None = None, sdk: Any = None, clock=None):
        self.settings = settings or MoomooSettings.from_env()
        self._sdk = sdk
        self._preview_secret = secrets.token_bytes(32)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._trade_lock = threading.Lock()

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
            detail = result[1] if isinstance(result, tuple) and len(result) > 1 else result
            raise MoomooUnavailable(f"Moomoo {action} failed: {detail}")
        return result[1]

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
        ])
        if not base["place_order_ready"]:
            base["message"] = "Read-only mode: real-order safety gates are not fully enabled"
        return base

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
        return {"account": accinfo[0] if accinfo else {}, "positions": positions,
                "orders": orders, "deals": deals, "activity_warnings": warnings}

    def snapshot(self) -> dict[str, Any]:
        if not self._port_open():
            raise MoomooUnavailable("Moomoo OpenD is not connected")
        with self._trade_context() as ctx:
            account_id = self._select_account_id(self._account_rows(ctx))
            data = self._account_snapshot_with_ctx(ctx, account_id)
        data.update(account_id=account_id, source="Moomoo OpenD", fetched_at=time.time())
        return data

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
        if not rows:
            raise MoomooUnavailable(f"No Moomoo quote for {code}")
        row = rows[0]
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
        return {
            "code": code,
            **prices,
            "update_time": row.get("update_time"),
            "sec_status": row.get("sec_status"),
            "source": "Moomoo OpenD",
        }

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
        now = self._clock().astimezone(timezone.utc)
        if self.settings.rth_only:
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
        quote_time = quote.get("update_time")
        try:
            parsed = datetime.fromisoformat(str(quote_time).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
            age = (now - parsed.astimezone(timezone.utc)).total_seconds()
            if age < -60 or age > self.settings.max_quote_age_seconds:
                raise ValueError
        except (TypeError, ValueError):
            raise LiveTradeRejected("Moomoo quote timestamp is missing or stale")
        last = quote["last_price"]
        if last <= 0:
            raise LiveTradeRejected("Moomoo returned an invalid last price")
        deviation = abs(limit_price / last - 1)
        if deviation > self.settings.max_limit_deviation_pct:
            raise LiveTradeRejected(
                f"Limit price deviates {deviation:.2%} from Moomoo last price; "
                f"maximum is {self.settings.max_limit_deviation_pct:.2%}"
            )
        notional = qty * limit_price
        if notional > self.settings.max_order_notional:
            raise LiveTradeRejected(
                f"Order notional ${notional:,.2f} exceeds the server limit "
                f"${self.settings.max_order_notional:,.2f}"
            )
        account = snap["account"]
        nav = self._required_number(account, "total_assets")
        if nav < self.settings.minimum_nav:
            raise LiveTradeRejected(
                f"Account NAV ${nav:,.2f} is below the required ${self.settings.minimum_nav:,.2f}"
            )
        if side == "BUY":
            cash = self._required_number(account, "cash")
            terminal_statuses = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED"}
            reserved = sum(
                self._number(order, "qty") * self._number(order, "price")
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
            terminal_statuses = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DISABLED", "DELETED"}
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
        if daily_notional + notional > self.settings.max_daily_order_notional:
            raise LiveTradeRejected(
                f"Daily order notional would exceed ${self.settings.max_daily_order_notional:,.2f}"
            )
        payload = {"code": code, "side": side, "qty": int(qty),
                   "limit_price": round(float(limit_price), 6),
                   "account_id": int(snap["account_id"]), "issued_at": int(time.time()),
                   "preview_id": secrets.token_hex(16)}
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
        if not self.settings.trading_enabled or not self.settings.password_md5:
            raise LiveTradeRejected("Real trading cancellation is disabled")
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
