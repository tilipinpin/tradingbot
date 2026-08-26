from __future__ import annotations

from typing import Sequence

from eth_abi import decode, encode
from eth_utils import keccak

from src.polygon_split import CTF_ADDRESS, JsonRpcReader


class PolygonResolutionError(RuntimeError):
    pass


class PolygonResolutionReader:
    """Reads finalized Conditional Tokens payout vectors from Polygon."""

    def __init__(self, rpc_url: str, *, timeout: int = 5) -> None:
        self.rpc = JsonRpcReader(rpc_url, timeout=timeout)

    @staticmethod
    def _call_data(signature: str, argument_types: list[str], arguments: list[object]) -> str:
        selector = keccak(text=signature)[:4]
        return "0x" + (selector + encode(argument_types, arguments)).hex()

    def _uint_call(self, signature: str, types: list[str], arguments: list[object]) -> int:
        raw = self.rpc.call(CTF_ADDRESS, self._call_data(signature, types, arguments))
        if not raw or raw == "0x":
            raise PolygonResolutionError(f"empty Polygon response for {signature}")
        return int(decode(["uint256"], bytes.fromhex(raw.removeprefix("0x")))[0])

    def winner(self, condition_id: str, outcomes: Sequence[str]) -> str | None:
        try:
            condition = bytes.fromhex(condition_id.removeprefix("0x"))
        except ValueError as exc:
            raise PolygonResolutionError(f"invalid condition id: {condition_id}") from exc
        if len(condition) != 32:
            raise PolygonResolutionError(f"condition id must be bytes32: {condition_id}")
        denominator = self._uint_call(
            "payoutDenominator(bytes32)", ["bytes32"], [condition]
        )
        if denominator == 0:
            return None
        numerators = [
            self._uint_call(
                "payoutNumerators(bytes32,uint256)",
                ["bytes32", "uint256"],
                [condition, index],
            )
            for index in range(len(outcomes))
        ]
        winners = [index for index, value in enumerate(numerators) if value == denominator]
        if len(winners) != 1:
            raise PolygonResolutionError(
                f"ambiguous payout vector for {condition_id}: {numerators}/{denominator}"
            )
        outcome = str(outcomes[winners[0]]).strip().upper()
        if outcome not in {"UP", "DOWN"}:
            raise PolygonResolutionError(f"unsupported winning outcome: {outcome}")
        return outcome
