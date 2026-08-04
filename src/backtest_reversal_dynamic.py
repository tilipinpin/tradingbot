from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from src.reversal_runtime import dynamic_recovery_decision, _marketable_buy_size
from src.reversal_v11 import Direction, ReversalSettings


SNAPSHOT = re.compile(
    r"(?P<slug>btc-updown-5m-(?P<epoch>\d+)) seconds_left=(?P<left>\d+) "
    r"spot=(?P<spot>[0-9.]+) start=(?P<open>[0-9.]+) "
    r"p_up=(?P<p_up>[0-9.]+) "
    r"up=OrderBookQuote\(bid=Decimal\('(?P<up_bid>[0-9.]+)'\), "
    r"ask=Decimal\('(?P<up>[0-9.]+)'\)\) "
    r"down=OrderBookQuote\(bid=Decimal\('(?P<down_bid>[0-9.]+)'\), "
    r"ask=Decimal\('(?P<down>[0-9.]+)'\)\)"
)
RESULT = re.compile(
    r"REVERSAL_CHAINLINK_RESULT slug=(?P<slug>btc-updown-5m-\d+) "
    r"result=(?P<result>UP|DOWN)"
)


@dataclass(frozen=True)
class Snapshot:
    seconds_left: int
    spot: Decimal
    open_price: Decimal
    probability_up: Decimal
    up_bid: Decimal
    up_ask: Decimal
    down_bid: Decimal
    down_ask: Decimal


@dataclass(frozen=True)
class Window:
    slug: str
    epoch: int
    result: Direction
    snapshots: tuple[Snapshot, ...]


@dataclass
class Outcome:
    policy: str
    windows: int = 0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    stage4_trades: int = 0
    stage4_wins: int = 0
    stage4_skips: int = 0
    stage4_net_profit: Decimal = Decimal("0")
    locked_windows: int = 0
    cost: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    net_profit: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("0")
    worst_round_loss: Decimal = Decimal("0")
    triggered_rounds: int = 0
    forced_exits: int = 0
    filter_skips: int = 0


def load_windows(path: Path) -> list[Window]:
    snapshots: dict[str, dict[int, dict[str, str]]] = {}
    explicit_results: dict[str, Direction] = {}
    with path.open(errors="replace") as handle:
        for line in handle:
            result_match = RESULT.search(line)
            if result_match:
                explicit_results[result_match["slug"]] = Direction(result_match["result"])
            match = SNAPSHOT.search(line)
            if not match:
                continue
            payload = match.groupdict()
            snapshots.setdefault(payload["slug"], {})[int(payload["left"])] = payload

    ordered = sorted(
        snapshots.items(),
        key=lambda item: int(next(iter(item[1].values()))["epoch"]),
    )
    opens = {
        int(next(iter(values.values()))["epoch"]): Decimal(
            next(iter(values.values()))["open"]
        )
        for _, values in ordered
    }
    windows: list[Window] = []
    for slug, values_by_second in ordered:
        value = next(iter(values_by_second.values()))
        epoch = int(value["epoch"])
        result = explicit_results.get(slug)
        next_open = opens.get(epoch + 300)
        if result is None and next_open is not None:
            current_open = Decimal(value["open"])
            result = Direction.UP if next_open >= current_open else Direction.DOWN
        if result is None:
            continue
        frames = tuple(
            Snapshot(
                seconds_left=int(frame["left"]),
                spot=Decimal(frame["spot"]),
                open_price=Decimal(frame["open"]),
                probability_up=Decimal(frame["p_up"]),
                up_bid=Decimal(frame["up_bid"]),
                up_ask=Decimal(frame["up"]),
                down_bid=Decimal(frame["down_bid"]),
                down_ask=Decimal(frame["down"]),
            )
            for frame in sorted(
                values_by_second.values(),
                key=lambda item: int(item["left"]),
                reverse=True,
            )
        )
        windows.append(
            Window(
                slug=slug,
                epoch=epoch,
                result=result,
                snapshots=frames,
            )
        )
    return windows


def simulate(windows: list[Window], *, policy: str) -> Outcome:
    if policy not in {
        "unfiltered_fixed",
        "stop_after_2_4_8",
        "dynamic_stage_4_6",
        "current_stage_5_6",
        "recommended_tiered_cap",
        "all_stage_live_edge",
        "all_stage_live_edge_no_cross",
        "tiered_all_stage_risk",
        "stage_2_3_fair_value",
        "first_attempt_only",
    }:
        raise ValueError(f"unknown policy: {policy}")
    settings = ReversalSettings()
    outcome = Outcome(policy=policy)
    recent: list[Direction] = []
    active_trend: Direction | None = None
    failures = 0
    round_loss = Decimal("0")
    blocked: Direction | None = None
    equity = Decimal("0")
    peak = Decimal("0")
    previous_epoch: int | None = None
    consecutive_filter_skips = 0

    for window in windows:
        outcome.windows += 1
        if previous_epoch is not None and window.epoch != previous_epoch + 300:
            recent = []
            active_trend = None
            failures = 0
            round_loss = Decimal("0")
            blocked = None
        previous_epoch = window.epoch

        if active_trend is None and len(recent) == 2 and recent[0] == recent[1]:
            if blocked != recent[-1]:
                active_trend = recent[-1]
                failures = 0
                round_loss = Decimal("0")
                consecutive_filter_skips = 0
                outcome.triggered_rounds += 1

        traded = False
        if active_trend is not None:
            target = active_trend.opposite
            nominal = settings.stakes[failures]
            attempt = failures + 1
            selected: Snapshot | None = None
            dynamic_shares: Decimal | None = None

            def signal_values(frame: Snapshot) -> tuple[Decimal, Decimal, Decimal]:
                frame_ask = frame.up_ask if target is Direction.UP else frame.down_ask
                probability = (
                    frame.probability_up
                    if target is Direction.UP
                    else Decimal("1") - frame.probability_up
                )
                fee_per_share = (
                    Decimal("0.07") * frame_ask * (Decimal("1") - frame_ask)
                )
                distance = (
                    frame.spot - frame.open_price
                    if target is Direction.UP
                    else frame.open_price - frame.spot
                )
                return frame_ask, probability - frame_ask - fee_per_share, distance

            if policy == "stop_after_2_4_8" and attempt >= 4:
                outcome.stage4_skips += 1
                outcome.filter_skips += 1
                blocked = active_trend
                active_trend = None
            elif policy == "current_stage_5_6" and attempt >= 5:
                frame = window.snapshots[0]
                ask, _, _ = signal_values(frame)
                retained_probability = (
                    frame.probability_up
                    if target is Direction.UP
                    else Decimal("1") - frame.probability_up
                )
                decision = dynamic_recovery_decision(
                    cumulative_loss=round_loss,
                    entry_price=ask,
                    retained_side=target,
                    retained_probability=retained_probability,
                    spot_price=frame.spot,
                    open_price=frame.open_price,
                    settings=settings,
                )
                if decision.allowed:
                    selected = frame
                    dynamic_shares = decision.shares
                else:
                    outcome.filter_skips += 1
                    blocked = active_trend
                    active_trend = None
                    failures = 0
                    round_loss = Decimal("0")
            elif policy == "recommended_tiered_cap":
                min_edges = {
                    1: Decimal("0.03"),
                    2: Decimal("0.05"),
                    3: Decimal("0.07"),
                    4: Decimal("0.10"),
                    5: Decimal("0.05"),
                    6: Decimal("0.05"),
                }
                max_asks = {
                    1: Decimal("0.65"),
                    2: Decimal("0.60"),
                    3: Decimal("0.58"),
                    4: Decimal("0.55"),
                    5: Decimal("0.62"),
                    6: Decimal("0.62"),
                }
                for frame in window.snapshots:
                    # Opening-only decision: do not wait more than 15 seconds.
                    if frame.seconds_left < 285:
                        break
                    ask, edge, cross_distance = signal_values(frame)
                    bid = frame.up_bid if target is Direction.UP else frame.down_bid
                    if (
                        ask > max_asks[attempt]
                        or ask - bid > Decimal("0.05")
                        or edge < min_edges[attempt]
                        or cross_distance < Decimal("2")
                    ):
                        continue
                    candidate_shares = nominal
                    if attempt >= 5:
                        retained_probability = (
                            frame.probability_up
                            if target is Direction.UP
                            else Decimal("1") - frame.probability_up
                        )
                        decision = dynamic_recovery_decision(
                            cumulative_loss=round_loss,
                            entry_price=ask,
                            retained_side=target,
                            retained_probability=retained_probability,
                            spot_price=frame.spot,
                            open_price=frame.open_price,
                            settings=settings,
                        )
                        if not decision.allowed:
                            continue
                        candidate_shares = decision.shares
                    executable_shares = _marketable_buy_size(
                        nominal_shares=candidate_shares,
                        price=ask,
                    )
                    candidate_fee = (
                        executable_shares
                        * Decimal("0.07")
                        * ask
                        * (Decimal("1") - ask)
                    )
                    if (
                        round_loss
                        + executable_shares * ask
                        + candidate_fee
                        > Decimal("4")
                    ):
                        continue
                    selected = frame
                    dynamic_shares = candidate_shares if attempt >= 5 else None
                    break
                if selected is None:
                    outcome.filter_skips += 1
                    blocked = active_trend
                    active_trend = None
                    failures = 0
                    round_loss = Decimal("0")
            elif policy in {
                "all_stage_live_edge",
                "all_stage_live_edge_no_cross",
                "tiered_all_stage_risk",
            }:
                threshold = (
                    {
                        1: Decimal("0.02"),
                        2: Decimal("0.03"),
                        3: Decimal("0.04"),
                    }.get(attempt, settings.recovery_min_expected_value)
                    if policy == "tiered_all_stage_risk"
                    else (
                        Decimal("0.03")
                        if attempt <= 3
                        else settings.recovery_min_expected_value
                    )
                )
                for frame in window.snapshots:
                    ask, edge, cross_distance = signal_values(frame)
                    tiered_cross_ok = (
                        attempt == 1
                        or (attempt == 2 and cross_distance >= Decimal("-2"))
                        or (attempt == 3 and cross_distance >= Decimal("0"))
                        or (
                            attempt >= 4
                            and cross_distance
                            >= settings.recovery_min_open_cross_usd
                        )
                    )
                    tiered_price_ok = attempt <= 3 or (
                        settings.recovery_min_entry_price
                        <= ask
                        <= settings.recovery_max_entry_price
                    )
                    if (
                        (
                            policy == "tiered_all_stage_risk"
                            and (not tiered_price_ok or not tiered_cross_ok)
                        )
                        or (
                            policy != "tiered_all_stage_risk"
                            and not settings.recovery_min_entry_price
                            <= ask
                            <= settings.recovery_max_entry_price
                        )
                        or edge < threshold
                        or (
                            policy == "all_stage_live_edge"
                            and cross_distance < settings.recovery_min_open_cross_usd
                        )
                    ):
                        continue
                    if attempt >= 4:
                        retained_probability = (
                            frame.probability_up
                            if target is Direction.UP
                            else Decimal("1") - frame.probability_up
                        )
                        decision = dynamic_recovery_decision(
                            cumulative_loss=round_loss,
                            entry_price=ask,
                            retained_side=target,
                            retained_probability=retained_probability,
                            spot_price=frame.spot,
                            open_price=frame.open_price,
                            settings=settings,
                        )
                        if not decision.allowed:
                            continue
                        dynamic_shares = decision.shares
                    selected = frame
                    break
                if selected is None:
                    outcome.filter_skips += 1
                    if policy == "tiered_all_stage_risk":
                        blocked = active_trend
                        active_trend = None
                        failures = 0
                        round_loss = Decimal("0")
                        consecutive_filter_skips = 0
                    else:
                        consecutive_filter_skips += 1
                        if window.result is target or consecutive_filter_skips >= 2:
                            if window.result is not target:
                                blocked = active_trend
                            active_trend = None
                            failures = 0
                            round_loss = Decimal("0")
                            consecutive_filter_skips = 0
            elif policy == "stage_2_3_fair_value" and attempt in {2, 3}:
                for frame in window.snapshots:
                    ask, _, _ = signal_values(frame)
                    probability = (
                        frame.probability_up
                        if target is Direction.UP
                        else Decimal("1") - frame.probability_up
                    )
                    if probability - ask >= Decimal("0.02"):
                        selected = frame
                        break
                if selected is None:
                    outcome.filter_skips += 1
                    blocked = active_trend
                    active_trend = None
                    failures = 0
                    round_loss = Decimal("0")
            elif policy in {"dynamic_stage_4_6", "stage_2_3_fair_value"} and attempt >= 4:
                frame = window.snapshots[0]
                ask, _, _ = signal_values(frame)
                retained_probability = (
                    frame.probability_up
                    if target is Direction.UP
                    else Decimal("1") - frame.probability_up
                )
                decision = dynamic_recovery_decision(
                    cumulative_loss=round_loss,
                    entry_price=ask,
                    retained_side=target,
                    retained_probability=retained_probability,
                    spot_price=frame.spot,
                    open_price=frame.open_price,
                    settings=settings,
                )
                if decision.allowed:
                    selected = frame
                    dynamic_shares = decision.shares
                else:
                    outcome.stage4_skips += 1
                    outcome.filter_skips += 1
                    blocked = active_trend
                    active_trend = None
            else:
                selected = next(
                    (
                        frame
                        for frame in window.snapshots
                        if Decimal("0")
                        < (frame.up_ask if target is Direction.UP else frame.down_ask)
                        < Decimal("1")
                    ),
                    None,
                )

            if active_trend is not None and selected is not None:
                ask = selected.up_ask if target is Direction.UP else selected.down_ask
                if dynamic_shares is not None:
                    nominal = dynamic_shares
                consecutive_filter_skips = 0
                is_stage4 = attempt >= 4
                if is_stage4:
                    outcome.stage4_trades += 1
                shares = _marketable_buy_size(nominal_shares=nominal, price=ask)
                fee = shares * Decimal("0.07") * ask * (Decimal("1") - ask)
                cost = shares * ask
                won = window.result is target
                pnl = shares - cost - fee if won else -cost - fee
                outcome.trades += 1
                outcome.cost += cost
                outcome.fees += fee
                outcome.net_profit += pnl
                if is_stage4:
                    outcome.stage4_net_profit += pnl
                    if won:
                        outcome.stage4_wins += 1
                equity += pnl
                peak = max(peak, equity)
                outcome.max_drawdown = max(outcome.max_drawdown, peak - equity)
                traded = True
                if won:
                    outcome.wins += 1
                    outcome.worst_round_loss = max(outcome.worst_round_loss, round_loss)
                    active_trend = None
                    failures = 0
                    round_loss = Decimal("0")
                    blocked = None
                else:
                    outcome.losses += 1
                    round_loss += cost + fee
                    failures += 1
                    if failures >= len(settings.stakes):
                        outcome.worst_round_loss = max(outcome.worst_round_loss, round_loss)
                        outcome.forced_exits += 1
                        blocked = active_trend
                        active_trend = None
                        failures = 0
                        round_loss = Decimal("0")
                if policy == "first_attempt_only" and active_trend is not None:
                    outcome.worst_round_loss = max(outcome.worst_round_loss, round_loss)
                    blocked = active_trend
                    active_trend = None
                    failures = 0
                    round_loss = Decimal("0")

        # The current result is only known after this window's decision.  Never
        # use it to clear a lock before simulating the entry (look-ahead bias).
        if blocked is not None:
            if window.result != blocked:
                blocked = None
            elif not traded:
                outcome.locked_windows += 1
        recent = (recent + [window.result])[-2:]
    return outcome


def serializable(outcome: Outcome) -> dict[str, object]:
    payload = asdict(outcome)
    for key, value in tuple(payload.items()):
        if isinstance(value, Decimal):
            payload[key] = f"{value:.4f}"
    payload["win_rate"] = (
        f"{Decimal(outcome.wins) / Decimal(outcome.trades):.2%}"
        if outcome.trades
        else "0.00%"
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    args = parser.parse_args()
    windows = load_windows(args.log)
    print(
        json.dumps(
            {
                "sample_windows": len(windows),
                "unfiltered_fixed": serializable(
                    simulate(windows, policy="unfiltered_fixed")
                ),
                "stop_after_2_4_8": serializable(
                    simulate(windows, policy="stop_after_2_4_8")
                ),
                "dynamic_stage_4_6": serializable(
                    simulate(windows, policy="dynamic_stage_4_6")
                ),
                "current_stage_5_6": serializable(
                    simulate(windows, policy="current_stage_5_6")
                ),
                "recommended_tiered_cap": serializable(
                    simulate(windows, policy="recommended_tiered_cap")
                ),
                "all_stage_live_edge": serializable(
                    simulate(windows, policy="all_stage_live_edge")
                ),
                "all_stage_live_edge_no_cross": serializable(
                    simulate(windows, policy="all_stage_live_edge_no_cross")
                ),
                "tiered_all_stage_risk": serializable(
                    simulate(windows, policy="tiered_all_stage_risk")
                ),
                "stage_2_3_fair_value": serializable(
                    simulate(windows, policy="stage_2_3_fair_value")
                ),
                "first_attempt_only": serializable(
                    simulate(windows, policy="first_attempt_only")
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
