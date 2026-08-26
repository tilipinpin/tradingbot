from eth_abi import encode

from src.polygon_resolution import PolygonResolutionReader


class FakeRpc:
    def __init__(self, values: list[int]) -> None:
        self.values = iter(values)

    def call(self, to: str, data: str) -> str:
        del to, data
        return "0x" + encode(["uint256"], [next(self.values)]).hex()


def test_unresolved_condition_returns_none() -> None:
    reader = PolygonResolutionReader("https://polygon.example")
    reader.rpc = FakeRpc([0])
    assert reader.winner("0x" + "11" * 32, ("Up", "Down")) is None


def test_resolved_condition_maps_payout_index_to_outcome() -> None:
    reader = PolygonResolutionReader("https://polygon.example")
    reader.rpc = FakeRpc([1, 0, 1])
    assert reader.winner("0x" + "11" * 32, ("Up", "Down")) == "DOWN"
