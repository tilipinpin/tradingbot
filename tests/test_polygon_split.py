from decimal import Decimal

import pytest
from eth_abi import decode

from src.polygon_split import (
    CTF_ADDRESS,
    CTF_COLLATERAL_ADAPTER,
    MAX_UINT256,
    NEG_RISK_CTF_COLLATERAL_ADAPTER,
    NEG_RISK_ADAPTER,
    PUSD_ADDRESS,
    CompleteSetSplitter,
    ContractCall,
    SecureRelayerSubmitter,
    SplitExecutionError,
    adapter_for_market,
    build_approval_call,
    build_merge_call,
    build_split_call,
    position_token_for_market,
    splitter_from_config,
    to_token_units,
)


WALLET = "0x1111111111111111111111111111111111111111"
CONDITION = "0x" + "22" * 32
UP_TOKEN = "101"
DOWN_TOKEN = "202"


class FakeRpc:
    def __init__(self, allowance: int = 0, chain_id: int = 137) -> None:
        self.allowance = allowance
        self._chain_id = chain_id
        self.split_done = False
        self.merge_done = False
        self.operator_approved = False

    def chain_id(self) -> int:
        return self._chain_id

    def code(self, address: str) -> str:
        return "0x6001"

    def call(self, to: str, data: str) -> str:
        selector = data[:10]
        if to.lower() == PUSD_ADDRESS.lower() and selector == "0x70a08231":
            return _uint(50_000_000 + (2_000_000 if self.merge_done else 0))
        if to.lower() == PUSD_ADDRESS.lower() and selector == "0xdd62ed3e":
            return _uint(self.allowance)
        if to.lower() in {CTF_ADDRESS.lower(), NEG_RISK_ADAPTER.lower()} and selector == "0x00fdd58e":
            token_id = decode(["address", "uint256"], bytes.fromhex(data[10:]))[1]
            base = 7_000_000 if token_id == int(UP_TOKEN) else 9_000_000
            delta = 2_000_000 if self.split_done and not self.merge_done else 0
            return _uint(base + delta)
        if to.lower() in {CTF_ADDRESS.lower(), NEG_RISK_ADAPTER.lower()} and selector == "0xe985e9c5":
            return _uint(1 if self.operator_approved else 0)
        raise AssertionError(f"unexpected eth_call to={to} data={data}")


class FakeSubmitter:
    def __init__(self, rpc: FakeRpc) -> None:
        self.rpc = rpc
        self.calls = []

    def submit(self, calls, metadata):
        self.calls = calls
        if "complete-set split" in metadata:
            self.rpc.split_done = True
        elif "complete-set merge" in metadata:
            self.rpc.operator_approved = True
            self.rpc.merge_done = True
        else:
            raise AssertionError(metadata)
        return "relay-id", "0xtransaction", "STATE_CONFIRMED"


def _uint(value: int) -> str:
    return "0x" + value.to_bytes(32, "big").hex()


def test_amount_conversion_is_exact_to_six_decimals() -> None:
    assert to_token_units(Decimal("2")) == 2_000_000
    assert to_token_units(Decimal("16.000001")) == 16_000_001
    with pytest.raises(ValueError):
        to_token_units(Decimal("1.0000001"))


def test_split_calldata_uses_pusd_zero_parent_binary_partition_and_amount() -> None:
    call = build_split_call(CTF_COLLATERAL_ADAPTER, CONDITION, 2_000_000)
    decoded = decode(
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        bytes.fromhex(call.data[10:]),
    )

    assert call.to == CTF_COLLATERAL_ADAPTER
    assert decoded[0].lower() == PUSD_ADDRESS.lower()
    assert decoded[1] == bytes(32)
    assert decoded[2] == bytes.fromhex("22" * 32)
    assert decoded[3] == (1, 2)
    assert decoded[4] == 2_000_000


def test_merge_calldata_is_inverse_binary_operation() -> None:
    call = build_merge_call(CTF_COLLATERAL_ADAPTER, CONDITION, 2_000_000)
    decoded = decode(
        ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
        bytes.fromhex(call.data[10:]),
    )

    assert call.to == CTF_COLLATERAL_ADAPTER
    assert decoded[0].lower() == PUSD_ADDRESS.lower()
    assert decoded[3] == (1, 2)
    assert decoded[4] == 2_000_000


def test_approval_targets_selected_adapter_for_max_amount() -> None:
    call = build_approval_call(NEG_RISK_CTF_COLLATERAL_ADAPTER)
    spender, amount = decode(["address", "uint256"], bytes.fromhex(call.data[10:]))
    assert call.to == PUSD_ADDRESS
    assert spender.lower() == NEG_RISK_CTF_COLLATERAL_ADAPTER.lower()
    assert amount == MAX_UINT256


def test_market_type_selects_distinct_v2_adapter() -> None:
    assert adapter_for_market(False) == CTF_COLLATERAL_ADAPTER
    assert adapter_for_market(True) == NEG_RISK_CTF_COLLATERAL_ADAPTER
    assert position_token_for_market(False) == CTF_ADDRESS
    assert position_token_for_market(True) == NEG_RISK_ADAPTER


def test_split_batches_approval_and_verifies_equal_balance_deltas() -> None:
    rpc = FakeRpc(allowance=0)
    submitter = FakeSubmitter(rpc)
    splitter = CompleteSetSplitter(rpc, submitter, WALLET)

    receipt = splitter.split(
        condition_id=CONDITION,
        up_token_id=UP_TOKEN,
        down_token_id=DOWN_TOKEN,
        amount=Decimal("2"),
        neg_risk=False,
    )

    assert len(submitter.calls) == 2
    assert receipt.approval_included is True
    assert receipt.up_received_units == 2_000_000
    assert receipt.down_received_units == 2_000_000
    assert receipt.transaction_hash == "0xtransaction"


def test_split_skips_approval_when_allowance_is_sufficient() -> None:
    rpc = FakeRpc(allowance=20_000_000)
    submitter = FakeSubmitter(rpc)
    splitter = CompleteSetSplitter(rpc, submitter, WALLET)

    receipt = splitter.split(
        condition_id=CONDITION,
        up_token_id=UP_TOKEN,
        down_token_id=DOWN_TOKEN,
        amount=Decimal("2"),
        neg_risk=True,
    )

    assert len(submitter.calls) == 1
    assert submitter.calls[0].to == NEG_RISK_CTF_COLLATERAL_ADAPTER
    assert receipt.approval_included is False


def test_merge_batches_erc1155_operator_approval_and_verifies_deltas() -> None:
    rpc = FakeRpc(allowance=20_000_000)
    rpc.split_done = True
    submitter = FakeSubmitter(rpc)
    splitter = CompleteSetSplitter(rpc, submitter, WALLET)

    receipt = splitter.merge(
        condition_id=CONDITION,
        up_token_id=UP_TOKEN,
        down_token_id=DOWN_TOKEN,
        amount=Decimal("2"),
        neg_risk=False,
    )

    assert len(submitter.calls) == 2
    assert submitter.calls[0].to == CTF_ADDRESS
    assert submitter.calls[1].to == CTF_COLLATERAL_ADAPTER
    assert receipt.approval_included is True
    assert receipt.collateral_received_units == 2_000_000


def test_preflight_rejects_wrong_chain_before_any_submission() -> None:
    rpc = FakeRpc(chain_id=1)
    submitter = FakeSubmitter(rpc)
    splitter = CompleteSetSplitter(rpc, submitter, WALLET)

    with pytest.raises(SplitExecutionError, match="chain 137"):
        splitter.preflight(
            condition_id=CONDITION,
            up_token_id=UP_TOKEN,
            down_token_id=DOWN_TOKEN,
            amount=Decimal("2"),
            neg_risk=False,
        )


def test_config_factory_rejects_smart_wallet_without_relayer_credentials() -> None:
    with pytest.raises(SplitExecutionError, match="RELAYER_API_KEY"):
        splitter_from_config(
            {
                "PRIVATE_KEY": "0x" + "11" * 32,
                "DEPOSIT_WALLET": WALLET,
                "SIGNATURE_TYPE": "3",
                "POLYGON_RPC_URL": "https://polygon.example",
            }
        )


def test_secure_relayer_uses_existing_api_key_and_batches_calls() -> None:
    observed = {}

    class Outcome:
        transaction_id = "new-relay-id"
        transaction_hash = "0xnewtransaction"

    class Handle:
        transaction_id = "new-relay-id"
        transaction_hash = None

        def wait(self):
            return Outcome()

    class Client:
        wallet = WALLET
        wallet_type = "DEPOSIT_WALLET"

        def execute_transaction(self, *, calls, metadata):
            observed["calls"] = calls
            observed["metadata"] = metadata
            return Handle()

    def factory(**kwargs):
        observed["config"] = kwargs
        return Client()

    submitter = SecureRelayerSubmitter(
        relayer_url="https://relayer-v2.polymarket.com",
        private_key="0x" + "11" * 32,
        signature_type=3,
        wallet=WALLET,
        rpc_url="https://polygon.example",
        relayer_api_key="existing-key",
        relayer_api_key_address=WALLET,
        client_factory=factory,
        transaction_call_factory=lambda **kwargs: ContractCall(
            to=kwargs["to"], data=kwargs["data"], value=str(kwargs["value"])
        ),
    )

    transaction_id, transaction_hash, state = submitter.submit(
        [ContractCall(to=CTF_COLLATERAL_ADAPTER, data="0x1234")],
        "complete-set split",
    )

    assert observed["config"]["relayer_api_key"] == "existing-key"
    assert observed["config"]["relayer_api_key_address"] == WALLET
    assert observed["calls"][0].to == CTF_COLLATERAL_ADAPTER
    assert observed["metadata"] == "complete-set split"
    assert (transaction_id, transaction_hash, state) == (
        "new-relay-id",
        "0xnewtransaction",
        "STATE_CONFIRMED",
    )


def test_secure_relayer_read_only_check_verifies_deployment_and_nonce(monkeypatch) -> None:
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/deployed"):
            return Response({"deployed": True})
        return Response({"nonce": "7", "address": WALLET})

    monkeypatch.setattr("src.polygon_split.requests.get", fake_get)
    submitter = SecureRelayerSubmitter(
        relayer_url="https://relayer-v2.polymarket.com",
        private_key="0x" + "11" * 32,
        signature_type=3,
        wallet=WALLET,
        rpc_url="https://polygon.example",
        relayer_api_key="existing-key",
        relayer_api_key_address=WALLET,
    )

    report = submitter.read_only_self_check()

    assert report["deployed"] is True
    assert calls[0][1]["params"]["type"] == "WALLET"
    assert calls[0][1]["params"]["address"] == WALLET
    assert calls[0][1]["headers"]["RELAYER_API_KEY"] == "existing-key"
    assert calls[1][0].endswith("/v1/account/transactions/params")
