from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


STATUS_FILE = "active_learning_committee.json"
EVENTS_FILE = "active_learning_committee_events.jsonl"
RUNTIME_FILE = "active_learning_committee_runtime.json"


MEMBERS = [
    {
        "key": "phoenix_parser",
        "name": "Phoenix Parser Auditor",
        "role": "Checks direction, entry range, SL, TP ladder and edited messages.",
    },
    {
        "key": "phoenix_entry",
        "name": "Phoenix Entry Auditor",
        "role": "Checks market probes, range pending orders and late-entry decisions.",
    },
    {
        "key": "phoenix_protection",
        "name": "Phoenix Protection Auditor",
        "role": "Checks TP milestones, BE attempts, pending expiry and correlated exposure.",
    },
    {
        "key": "telegram_learning",
        "name": "Telegram Learning Auditor",
        "role": "Reviews recent channel outcomes and listener learning state.",
    },
    {
        "key": "xau_scalper",
        "name": "XAU Scalper Auditor",
        "role": "Reviews the main XAU scalper heartbeat, PnL and current gate reason.",
    },
    {
        "key": "evening_scalper",
        "name": "Evening Scalper Auditor",
        "role": "Reviews the independent evening scalper and its current setup state.",
    },
    {
        "key": "nasdaq_team",
        "name": "Nasdaq Team Auditor",
        "role": "Reviews agent consensus, control-team decisions and realized results.",
    },
    {
        "key": "execution_health",
        "name": "Execution Health Auditor",
        "role": "Detects stale module heartbeats, missing files and execution anomalies.",
    },
    {
        "key": "risk_chair",
        "name": "Correlated Exposure Chair",
        "role": "Measures how many legs express the same market thesis at once.",
    },
    {
        "key": "learning_chair",
        "name": "Active Learning Chair",
        "role": "Combines evidence and publishes recommendations without changing live settings.",
    },
]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2), encoding="utf-8")
    temporary.replace(path)


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=True) + "\n")


def _parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_seconds(value: Any, now: datetime) -> float | None:
    parsed = _parse_utc(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds())


def _tail_jsonl(path: Path, max_lines: int = 5000, max_bytes: int = 8_000_000) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes), os.SEEK_SET)
            raw = handle.read()
    except OSError:
        return []
    if size > max_bytes:
        first_break = raw.find(b"\n")
        raw = raw[first_break + 1 :] if first_break >= 0 else b""
    result: list[dict[str, Any]] = []
    for line in raw.splitlines()[-max_lines:]:
        try:
            item = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


def _is_phoenix(event: dict[str, Any]) -> bool:
    signal = event.get("signal") if isinstance(event.get("signal"), dict) else {}
    title = str(event.get("chat_title") or signal.get("chat_title") or "").lower()
    chat_id = event.get("chat_id", signal.get("chat_id"))
    return "phoenix" in title or str(chat_id) == "-1002864291293"


def _event_message_id(event: dict[str, Any]) -> int:
    signal = event.get("signal") if isinstance(event.get("signal"), dict) else {}
    try:
        return int(event.get("message_id", signal.get("message_id", 0)) or 0)
    except (TypeError, ValueError):
        return 0


def analyze_phoenix(events: list[dict[str, Any]]) -> dict[str, Any]:
    phoenix_events = [event for event in events if _is_phoenix(event)]
    signals = [event for event in phoenix_events if event.get("type") == "signal"]
    signals.sort(key=lambda event: str(event.get("timestamp_utc", "")))
    ranges: dict[int, list[dict[str, Any]]] = {}
    brains: dict[int, dict[str, Any]] = {}
    protection_events: dict[int, list[dict[str, Any]]] = {}
    unbound_protection_events: list[dict[str, Any]] = []
    for event in events:
        message_id = _event_message_id(event)
        event_type = str(event.get("type", ""))
        if not _is_phoenix(event) and not event_type.startswith("phoenix_"):
            continue
        if message_id and event_type in {
            "phoenix_range_trigger_attempt",
            "phoenix_range_optimized_extra_attempt",
        }:
            ranges.setdefault(message_id, []).append(event)
        if message_id and event_type == "phoenix_entry_brain":
            brains[message_id] = event
        if message_id and any(token in event_type for token in ("be_", "protect", "pending_cancel", "pending_expir")):
            protection_events.setdefault(message_id, []).append(event)
        elif any(token in event_type for token in ("be_", "protect", "pending_cancel", "pending_expir")):
            unbound_protection_events.append(event)

    recent: list[dict[str, Any]] = []
    for signal_event in signals[-4:]:
        signal = signal_event.get("signal", {})
        signal_id = _event_message_id(signal_event)
        signal_time = _parse_utc(signal_event.get("timestamp_utc"))
        side = str(signal.get("side", ""))
        matching_range_id = 0
        matching_range_events: list[dict[str, Any]] = []
        for range_id, attempts in ranges.items():
            if not attempts:
                continue
            attempt_time = _parse_utc(attempts[0].get("timestamp_utc"))
            if signal_time is None or attempt_time is None:
                continue
            age = (signal_time - attempt_time).total_seconds()
            attempt_side = str(attempts[0].get("side", ""))
            if 0 <= age <= 30 * 60 and attempt_side == side and range_id > matching_range_id:
                matching_range_id = range_id
                matching_range_events = attempts

        market_attempts = [item for item in matching_range_events if item.get("order_kind") == "market"]
        pending_attempts = [item for item in matching_range_events if item.get("order_kind") == "limit"]
        extras = [item for item in matching_range_events if item.get("type") == "phoenix_range_optimized_extra_attempt"]
        successful = [item for item in matching_range_events if int(item.get("retcode", 0) or 0) in {10008, 10009, 10010}]
        total_volume = sum(float(item.get("volume", 0.0) or 0.0) for item in successful)
        brain_event = brains.get(signal_id, {})
        brain = brain_event.get("brain", {}) if isinstance(brain_event.get("brain"), dict) else {}
        range_time = _parse_utc(matching_range_events[0].get("timestamp_utc")) if matching_range_events else None
        unbound_count = 0
        if range_time is not None and signal_time is not None:
            for event in unbound_protection_events:
                event_time = _parse_utc(event.get("timestamp_utc"))
                if event_time is not None and range_time <= event_time <= signal_time:
                    unbound_count += 1
        recent.append(
            {
                "message_id": signal_id,
                "timestamp_utc": signal_event.get("timestamp_utc"),
                "side": side,
                "entries": signal.get("entries", []),
                "sl": signal.get("sl"),
                "tps": signal.get("tps", []),
                "entry_brain": brain.get("decision", "unknown"),
                "range_message_id": matching_range_id or None,
                "range_attempts": len(matching_range_events),
                "range_market_attempts": len(market_attempts),
                "range_pending_attempts": len(pending_attempts),
                "range_extra_attempts": len(extras),
                "successful_range_legs": len(successful),
                "successful_range_volume": round(total_volume, 2),
                "protection_events": len(protection_events.get(signal_id, []))
                + len(protection_events.get(matching_range_id, []))
                + unbound_count,
            }
        )

    latest = recent[-1] if recent else None
    anomalies: list[dict[str, Any]] = []
    for signal in recent:
        if signal["range_market_attempts"] >= 6 or signal["successful_range_volume"] >= 0.8:
            anomalies.append(
                {
                    "severity": "high",
                    "code": "phoenix_correlated_pre_signal_exposure",
                    "message_id": signal["message_id"],
                    "detail": (
                        f"Pre-signal range opened {signal['successful_range_legs']} legs "
                        f"with {signal['successful_range_volume']:.2f} total lot before full confirmation."
                    ),
                }
            )
        if signal["entry_brain"] == "skip_too_late_after_tp" and signal["successful_range_legs"] == 0:
            anomalies.append(
                {
                    "severity": "medium",
                    "code": "phoenix_signal_arrived_after_tp_without_coverage",
                    "message_id": signal["message_id"],
                    "detail": "Full signal arrived after TP progress and had no preliminary-range coverage.",
                }
            )
    return {
        "latest_signal": latest,
        "recent_signals": recent,
        "anomalies": anomalies,
        "events_reviewed": len(phoenix_events),
    }


def _module_status(path: Path, now: datetime, stale_after: float) -> dict[str, Any]:
    data = _read_json(path, {})
    heartbeat = data.get("heartbeat_utc") if isinstance(data, dict) else None
    age = _age_seconds(heartbeat, now)
    active = age is not None and age <= stale_after
    return {
        "name": str(data.get("strategy") or path.parent.name),
        "path": str(path),
        "active": active,
        "heartbeat_utc": heartbeat,
        "heartbeat_age_seconds": round(age, 1) if age is not None else None,
        "day_realized_profit": float(data.get("day_realized_profit", 0.0) or 0.0),
        "open_profit": float(data.get("open_profit", 0.0) or 0.0),
        "positions_count": int(data.get("positions_count", 0) or 0),
        "last_reason": data.get("last_reason", ""),
        "settings": data.get("settings", {}),
    }


def _discover_scalpers(project_dir: Path, now: datetime, stale_after: float) -> tuple[list[dict[str, Any]], int]:
    statuses: list[dict[str, Any]] = []
    stale_count = 0
    for path in sorted(project_dir.glob("data_vantage*/xau_scalp_status.json")):
        status = _module_status(path, now, stale_after)
        if status["active"]:
            statuses.append(status)
        else:
            stale_count += 1
    return statuses, stale_count


def _member_vote(key: str, status: str, finding: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    member = next(item for item in MEMBERS if item["key"] == key)
    return {
        **member,
        "status": status,
        "finding": finding,
        "evidence": evidence or {},
    }


def build_snapshot(project_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or _utc_now()
    data_dir = project_dir / os.getenv("ACTIVE_LEARNING_COMMITTEE_DATA_DIR", "data_vantage")
    stale_after = max(10.0, float(os.getenv("ACTIVE_LEARNING_COMMITTEE_STALE_SECONDS", "45")))
    events = _tail_jsonl(data_dir / "telegram_signal_events.jsonl")
    phoenix = analyze_phoenix(events)
    listener_state_path = data_dir / "telegram_signal_state.json"
    listener_state = _read_json(listener_state_path, {})
    listener_age = max(0.0, now.timestamp() - listener_state_path.stat().st_mtime) if listener_state_path.exists() else None
    adaptive = listener_state.get("adaptive_learning", {}) if isinstance(listener_state, dict) else {}
    phoenix_learning = adaptive.get("channels", {}).get("phoenixvip", {}) if isinstance(adaptive, dict) else {}
    scalpers, stale_scalpers = _discover_scalpers(project_dir, now, stale_after)
    main_scalper = next((item for item in scalpers if Path(item["path"]).parent.name == "data_vantage"), None)
    evening_scalper = next((item for item in scalpers if "evening" in Path(item["path"]).parent.name), None)
    agent_path = project_dir / "data_vantage_agent_teams" / "agent_teams_status.json"
    agent_data = _read_json(agent_path, {})
    agent_age = _age_seconds(agent_data.get("heartbeat_utc"), now) if isinstance(agent_data, dict) else None
    agent_active = agent_age is not None and agent_age <= stale_after
    agent_summary = agent_data.get("summary", {}) if isinstance(agent_data, dict) else {}

    latest_signal = phoenix.get("latest_signal") or {}
    high_exposure = any(item.get("severity") == "high" for item in phoenix.get("anomalies", []))
    parser_ok = bool(latest_signal.get("side") in {"buy", "sell"} and latest_signal.get("sl") and latest_signal.get("tps"))
    listener_ok = listener_age is not None and listener_age <= stale_after
    votes = [
        _member_vote(
            "phoenix_parser",
            "pass" if parser_ok else "warning",
            "Latest Phoenix direction, SL and TP ladder are complete." if parser_ok else "Latest Phoenix structure is incomplete or unavailable.",
            {"message_id": latest_signal.get("message_id"), "side": latest_signal.get("side")},
        ),
        _member_vote(
            "phoenix_entry",
            "warning" if high_exposure else "pass",
            "Pre-signal entry fan-out is too concentrated." if high_exposure else "No excessive recent pre-signal fan-out detected.",
            {
                "range_legs": latest_signal.get("successful_range_legs", 0),
                "range_volume": latest_signal.get("successful_range_volume", 0.0),
                "brain": latest_signal.get("entry_brain", "unknown"),
            },
        ),
        _member_vote(
            "phoenix_protection",
            "review" if high_exposure else "pass",
            "BE cannot protect a trade that never moves favorably; confirmation must precede scale-in."
            if high_exposure
            else "Protection events and range coverage are being observed.",
            {"protection_events": latest_signal.get("protection_events", 0)},
        ),
        _member_vote(
            "telegram_learning",
            "pass" if listener_ok else "critical",
            "Listener state is fresh and adaptive outcomes are available." if listener_ok else "Listener state is stale or missing.",
            {"state_age_seconds": round(listener_age, 1) if listener_age is not None else None, "phoenix": phoenix_learning},
        ),
        _member_vote(
            "xau_scalper",
            "pass" if main_scalper else "critical",
            "Main XAU scalper is active." if main_scalper else "Main XAU scalper heartbeat is stale or missing.",
            main_scalper or {},
        ),
        _member_vote(
            "evening_scalper",
            "pass" if evening_scalper else "inactive",
            "Evening scalper is active." if evening_scalper else "Evening scalper is not currently active.",
            evening_scalper or {},
        ),
        _member_vote(
            "nasdaq_team",
            "pass" if agent_active else "critical",
            "Nasdaq agents and control team are reporting." if agent_active else "Nasdaq agent heartbeat is stale or missing.",
            {"heartbeat_age_seconds": round(agent_age, 1) if agent_age is not None else None, "summary": agent_summary},
        ),
        _member_vote(
            "execution_health",
            "pass" if listener_ok and main_scalper and agent_active else "critical",
            "Core monitored modules are fresh." if listener_ok and main_scalper and agent_active else "At least one core module is stale.",
            {"active_scalpers": len(scalpers), "stale_scalper_profiles": stale_scalpers},
        ),
        _member_vote(
            "risk_chair",
            "warning" if high_exposure else "pass",
            "Reduce correlated pre-signal market legs until direction is confirmed."
            if high_exposure
            else "No high correlated-exposure anomaly in the recent Phoenix sample.",
            {"anomalies": phoenix.get("anomalies", [])},
        ),
    ]
    critical = sum(1 for item in votes if item["status"] == "critical")
    warnings = sum(1 for item in votes if item["status"] in {"warning", "review"})
    verdict = "intervention_required" if critical else "review_required" if warnings else "healthy"
    recommendations = [
        {
            "priority": 1,
            "code": "phoenix_confirm_then_scale",
            "text": "Use one small market probe on the preliminary direction; release extra market legs only after favorable confirmation. Keep range pending orders independent.",
        },
        {
            "priority": 2,
            "code": "phoenix_same_thesis_reentry_lock",
            "text": "After a preliminary Phoenix basket closes at SL without a favorable move, block another market entry in the same direction for 20 minutes unless a materially new range appears.",
        },
        {
            "priority": 3,
            "code": "phoenix_preserve_provider_sl",
            "text": "Keep the provider SL for the full signal. Treat inferred pre-signal SL as provisional and do not multiply it across a full-size market basket.",
        },
    ]
    votes.append(
        _member_vote(
            "learning_chair",
            verdict,
            "Committee published evidence-based recommendations; live strategy mutation remains disabled.",
            {"critical": critical, "warnings": warnings, "recommendations": [item["code"] for item in recommendations]},
        )
    )
    return {
        "schema": 1,
        "heartbeat_utc": now.isoformat(),
        "enabled": True,
        "mode": "observation_only",
        "verdict": verdict,
        "members": votes,
        "phoenix": phoenix,
        "modules": {
            "telegram_listener": {
                "active": listener_ok,
                "state_age_seconds": round(listener_age, 1) if listener_age is not None else None,
            },
            "scalpers": scalpers,
            "nasdaq_agents": {
                "active": agent_active,
                "heartbeat_age_seconds": round(agent_age, 1) if agent_age is not None else None,
                "summary": agent_summary,
            },
        },
        "recommendations": recommendations,
        "automatic_changes_applied": [],
    }


def run_once(project_dir: Path) -> dict[str, Any]:
    data_dir = project_dir / os.getenv("ACTIVE_LEARNING_COMMITTEE_DATA_DIR", "data_vantage")
    snapshot = build_snapshot(project_dir)
    status_path = data_dir / STATUS_FILE
    runtime_path = data_dir / RUNTIME_FILE
    events_path = data_dir / EVENTS_FILE
    runtime = _read_json(runtime_path, {})
    fingerprint_source = {
        "verdict": snapshot["verdict"],
        "phoenix": snapshot["phoenix"].get("latest_signal"),
        "member_status": {item["key"]: item["status"] for item in snapshot["members"]},
    }
    fingerprint = hashlib.sha256(json.dumps(fingerprint_source, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    _write_json(status_path, snapshot)
    if runtime.get("last_fingerprint") != fingerprint:
        _append_jsonl(
            events_path,
            {
                "timestamp_utc": snapshot["heartbeat_utc"],
                "type": "committee_verdict",
                "fingerprint": fingerprint,
                "verdict": snapshot["verdict"],
                "latest_phoenix": snapshot["phoenix"].get("latest_signal"),
                "recommendations": snapshot["recommendations"],
            },
        )
    _write_json(runtime_path, {"last_fingerprint": fingerprint, "last_run_utc": snapshot["heartbeat_utc"]})
    return snapshot


def run() -> None:
    project_dir = Path(os.getenv("ACTIVE_LEARNING_COMMITTEE_PROJECT_DIR", Path.cwd())).resolve()
    interval = max(1.0, float(os.getenv("ACTIVE_LEARNING_COMMITTEE_INTERVAL_SECONDS", "15")))
    while True:
        try:
            run_once(project_dir)
        except Exception as exc:  # pragma: no cover - process resilience
            data_dir = project_dir / os.getenv("ACTIVE_LEARNING_COMMITTEE_DATA_DIR", "data_vantage")
            _append_jsonl(
                data_dir / EVENTS_FILE,
                {"timestamp_utc": _utc_now().isoformat(), "type": "committee_error", "error": repr(exc)},
            )
        time.sleep(interval)


if __name__ == "__main__":
    run()
