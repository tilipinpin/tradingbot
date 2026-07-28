from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable, Protocol

import requests
from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address


POLYGON_CHAIN_ID = 137
PUSD_ADDRESS = to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
CTF_ADDRESS = to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
NEG_RISK_ADAPTER = to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
CTF_COLLATERAL_ADAPTER = to_checksum_address(
    "0xAdA100Db00Ca00073811820692005400218FcE1f"
)
NEG_RISK_CTF_COLLATERAL_ADAPTER = to_checksum_address(
    "0xadA2005600Dec949baf300f4C6120000bDB6eAab"
)
ZERO_BYTES32 = bytes(32)
MAX_UINT256 = 2**256 - 1
TOKEN_DECIMALS = 6


class SplitExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContractCall:
    to: str
    data: str
    value: str = "0"


@dataclass(frozen=True)
class SplitPreflight:
    wallet: str
    adapter: str
    amount: Decimal
    amount_units: int
    collateral_balance_units: int
    collateral_allowance_units: int
    approval_required: bool
    up_balance_before: int
    down_balance_before: int


@dataclass(frozen=True)
class SplitReceipt:
    transaction_id: str | None
    transaction_hash: str
    state: str
    approval_included: bool
    amount: Decimal
    amount_units: int
    up_received_units: int
    down_received_units: int


@dataclass(frozen=True)
class MergeReceipt:
    transaction_id: str | None
    transaction_hash: str
    state: str
    approval_included: bool
    amount: Decimal
    amount_units: int
    collateral_received_units: int


class RpcReader(Protocol):
    def chain_id(self) -> int: ...

    def code(self, address: str) -> str: ...

    def call(self, to: str, data: str) -> str: ...


class CallSubmitter(Protocol):
    def submit(self, calls: list[ContractCall], metadata: str) -> tuple[str | None, str, str]: ...


class JsonRpcReader:
    def __init__(self, rpc_url: str, timeout: int = 20) -> None:
        if not rpc_url.startswith(("http://", "https://")):
            raise ValueError("Polygon RPC URL must use HTTP or HTTPS")
        self.rpc_url = rpc_url
        self.timeout = timeout
        self._request_id = 0

    def _rpc(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        response = requests.post(
            self.rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise SplitExecutionError(f"Polygon RPC {method} failed: {payload['error']}")
        return payload.get("result")

    def chain_id(self) -> int:
        return int(self._rpc("eth_chainId", []), 16)

    def code(self, address: str) -> str:
        return str(self._rpc("eth_getCode", [to_checksum_address(address), "latest"]) or "0x")

    def call(self, to: str, data: str) -> str:
        return str(
            self._rpc(
                "eth_call",
                [{"to": to_checksum_address(to), "data": data}, "latest"],
            )
        )


class EoaSubmitter:
    """Pays Polygon gas directly; only valid when the pUSD holder is the EOA."""

    def __init__(self, rpc_url: str, private_key: str, wallet: str, timeout: int = 120) -> None:
        from web3 import Web3

        self.web3 = Web3(Web3.HTTPProvider(rpc_url))
        self.account = Account.from_key(private_key)
        if self.account.address.lower() != wallet.lower():
            raise SplitExecutionError("EOA signer does not own the configured collateral wallet")
        self.timeout = timeout

    def submit(self, calls: list[ContractCall], metadata: str) -> tuple[str | None, str, str]:
        del metadata
        last_hash = ""
        for call in calls:
            nonce = self.web3.eth.get_transaction_count(self.account.address, "pending")
            transaction = {
                "chainId": POLYGON_CHAIN_ID,
                "from": self.account.address,
                "to": to_checksum_address(call.to),
                "data": call.data,
                "value": int(call.value),
                "nonce": nonce,
            }
            transaction["gas"] = self.web3.eth.estimate_gas(transaction)
            transaction["gasPrice"] = self.web3.eth.gas_price
            signed = self.account.sign_transaction(transaction)
            raw_transaction = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            tx_hash = self.web3.eth.send_raw_transaction(raw_transaction)
            receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.timeout)
            if int(receipt.status) != 1:
                raise SplitExecutionError(f"Polygon transaction reverted: {tx_hash.hex()}")
            last_hash = tx_hash.hex()
        return None, last_hash, "STATE_CONFIRMED"


class SecureRelayerSubmitter:
    """Submits wallet batches through the unified SDK's Relayer API-key flow."""

    def __init__(
        self,
        *,
        relayer_url: str,
        private_key: str,
        signature_type: int,
        wallet: str,
        rpc_url: str,
        relayer_api_key: str,
        relayer_api_key_address: str,
        clob_url: str = "https://clob.polymarket.com",
        client_factory: Callable[..., Any] | None = None,
        transaction_call_factory: Callable[..., Any] | None = None,
    ) -> None:
        if signature_type not in {1, 2, 3}:
            raise ValueError("relayer supports signature types 1, 2, and 3")
        if not relayer_api_key or not relayer_api_key_address:
            raise SplitExecutionError(
                "RELAYER_API_KEY and RELAYER_API_KEY_ADDRESS are required"
            )
        self.relayer_url = relayer_url
        self.private_key = private_key
        self.signature_type = signature_type
        self.wallet = to_checksum_address(wallet)
        self.rpc_url = rpc_url
        self.clob_url = clob_url
        self.relayer_api_key = relayer_api_key
        self.relayer_api_key_address = to_checksum_address(relayer_api_key_address)
        self._client_factory = client_factory or _create_secure_client
        self._transaction_call_factory = (
            transaction_call_factory or _create_sdk_transaction_call
        )
        self._client: Any | None = None

    def read_only_self_check(self) -> dict[str, Any]:
        signer = Account.from_key(self.private_key).address
        transaction_type = {1: "PROXY", 2: "SAFE", 3: "WALLET"}[
            self.signature_type
        ]
        headers = {
            "RELAYER_API_KEY": self.relayer_api_key,
            "RELAYER_API_KEY_ADDRESS": self.relayer_api_key_address,
        }
        deployed_response = requests.get(
            f"{self.relayer_url.rstrip('/')}/deployed",
            params={"address": self.wallet, "type": transaction_type},
            headers=headers,
            timeout=20,
        )
        deployed_response.raise_for_status()
        deployed = bool(deployed_response.json().get("deployed"))
        if not deployed:
            raise SplitExecutionError("configured Polymarket wallet is not deployed")
        params_response = requests.get(
            f"{self.relayer_url.rstrip('/')}/v1/account/transactions/params",
            params={"address": signer, "type": transaction_type},
            headers=headers,
            timeout=20,
        )
        params_response.raise_for_status()
        nonce = str(params_response.json().get("nonce") or "")
        if not nonce.isdigit():
            raise SplitExecutionError("relayer credential check returned no numeric nonce")
        return {
            "wallet": self.wallet,
            "wallet_type": transaction_type,
            "deployed": True,
            "nonce_available": True,
        }

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory(
                relayer_url=self.relayer_url,
                rpc_url=self.rpc_url,
                clob_url=self.clob_url,
                private_key=self.private_key,
                wallet=self.wallet,
                relayer_api_key=self.relayer_api_key,
                relayer_api_key_address=self.relayer_api_key_address,
            )
            expected_wallet_type = {
                1: "POLY_PROXY",
                2: "GNOSIS_SAFE",
                3: "DEPOSIT_WALLET",
            }[self.signature_type]
            actual_wallet = str(self._client.wallet)
            actual_wallet_type = str(self._client.wallet_type)
            if actual_wallet.lower() != self.wallet.lower():
                raise SplitExecutionError(
                    f"SDK wallet {actual_wallet} does not match configured wallet {self.wallet}"
                )
            if actual_wallet_type != expected_wallet_type:
                raise SplitExecutionError(
                    f"SDK classified wallet as {actual_wallet_type}, expected {expected_wallet_type}"
                )
        return self._client

    def submit(self, calls: list[ContractCall], metadata: str) -> tuple[str | None, str, str]:
        client = self._get_client()
        handle = client.execute_transaction(
            calls=[
                self._transaction_call_factory(
                    to=call.to, data=call.data, value=int(call.value)
                )
                for call in calls
            ],
            metadata=metadata,
        )
        result = handle.wait()
        if result is None:
            raise SplitExecutionError("relayed split did not reach a confirmed state")
        transaction_id = getattr(result, "transaction_id", None) or getattr(
            handle, "transaction_id", None
        )
        tx_hash = str(
            getattr(result, "transaction_hash", None)
            or getattr(handle, "transaction_hash", None)
            or ""
        )
        if not tx_hash:
            raise SplitExecutionError("relayer returned no Polygon transaction hash")
        return transaction_id, tx_hash, "STATE_CONFIRMED"


def _create_secure_client(
    *,
    relayer_url: str,
    rpc_url: str,
    clob_url: str,
    private_key: str,
    wallet: str,
    relayer_api_key: str,
    relayer_api_key_address: str,
) -> Any:
    try:
        from polymarket import PRODUCTION, RelayerApiKey, SecureClient
    except ImportError as exc:
        raise SplitExecutionError(
            "polymarket-client is required for Deposit Wallet relayer execution"
        ) from exc

    environment = replace(
        PRODUCTION,
        relayer_url=relayer_url.rstrip("/"),
        rpc_url=rpc_url,
        clob_url=clob_url.rstrip("/"),
    )
    return SecureClient.create(
        private_key=private_key,
        wallet=wallet,
        environment=environment,
        api_key=RelayerApiKey(
            key=relayer_api_key,
            address=relayer_api_key_address,
        ),
    )


def _create_sdk_transaction_call(*, to: str, data: str, value: int) -> Any:
    try:
        from polymarket import TransactionCall
    except ImportError as exc:
        raise SplitExecutionError(
            "polymarket-client is required for Deposit Wallet relayer execution"
        ) from exc
    return TransactionCall(to=to, data=data, value=value)


class CompleteSetSplitter:
    def __init__(self, rpc: RpcReader, submitter: CallSubmitter, wallet: str) -> None:
        self.rpc = rpc
        self.submitter = submitter
        self.wallet = to_checksum_address(wallet)

    def preflight(
        self,
        *,
        condition_id: str,
        up_token_id: str,
        down_token_id: str,
        amount: Decimal,
        neg_risk: bool,
    ) -> SplitPreflight:
        condition = _condition_bytes(condition_id)
        del condition
        amount_units = to_token_units(amount)
        adapter = adapter_for_market(neg_risk)
        position_token = position_token_for_market(neg_risk)
        if self.rpc.chain_id() != POLYGON_CHAIN_ID:
            raise SplitExecutionError("RPC is not connected to Polygon mainnet chain 137")
        for address, name in (
            (PUSD_ADDRESS, "pUSD"),
            (CTF_ADDRESS, "CTF"),
            (adapter, "CTF collateral adapter"),
            (position_token, "position token"),
        ):
            if self.rpc.code(address) in {"0x", "0x0", ""}:
                raise SplitExecutionError(f"{name} has no contract code on Polygon")
        balance = erc20_balance(self.rpc, PUSD_ADDRESS, self.wallet)
        if balance < amount_units:
            raise SplitExecutionError(
                f"insufficient pUSD: required {amount_units}, available {balance} base units"
            )
        allowance = erc20_allowance(self.rpc, PUSD_ADDRESS, self.wallet, adapter)
        return SplitPreflight(
            wallet=self.wallet,
            adapter=adapter,
            amount=amount,
            amount_units=amount_units,
            collateral_balance_units=balance,
            collateral_allowance_units=allowance,
            approval_required=allowance < amount_units,
            up_balance_before=erc1155_balance(
                self.rpc, position_token, self.wallet, int(up_token_id)
            ),
            down_balance_before=erc1155_balance(
                self.rpc, position_token, self.wallet, int(down_token_id)
            ),
        )

    def split(
        self,
        *,
        condition_id: str,
        up_token_id: str,
        down_token_id: str,
        amount: Decimal,
        neg_risk: bool,
    ) -> SplitReceipt:
        preflight = self.preflight(
            condition_id=condition_id,
            up_token_id=up_token_id,
            down_token_id=down_token_id,
            amount=amount,
            neg_risk=neg_risk,
        )
        calls: list[ContractCall] = []
        if preflight.approval_required:
            calls.append(build_approval_call(preflight.adapter))
        calls.append(
            build_split_call(
                preflight.adapter,
                condition_id,
                preflight.amount_units,
            )
        )
        transaction_id, transaction_hash, state = self.submitter.submit(
            calls,
            f"reversal-v11 complete-set split {amount} pUSD",
        )
        position_token = position_token_for_market(neg_risk)
        up_after = erc1155_balance(self.rpc, position_token, self.wallet, int(up_token_id))
        down_after = erc1155_balance(
            self.rpc, position_token, self.wallet, int(down_token_id)
        )
        up_received = up_after - preflight.up_balance_before
        down_received = down_after - preflight.down_balance_before
        if up_received < preflight.amount_units or down_received < preflight.amount_units:
            raise SplitExecutionError(
                "split transaction confirmed but equal UP/DOWN balance deltas were not observed"
            )
        return SplitReceipt(
            transaction_id=transaction_id,
            transaction_hash=transaction_hash,
            state=state,
            approval_included=preflight.approval_required,
            amount=amount,
            amount_units=preflight.amount_units,
            up_received_units=up_received,
            down_received_units=down_received,
        )

    def merge(
        self,
        *,
        condition_id: str,
        up_token_id: str,
        down_token_id: str,
        amount: Decimal,
        neg_risk: bool,
    ) -> MergeReceipt:
        _condition_bytes(condition_id)
        amount_units = to_token_units(amount)
        adapter = adapter_for_market(neg_risk)
        position_token = position_token_for_market(neg_risk)
        if self.rpc.chain_id() != POLYGON_CHAIN_ID:
            raise SplitExecutionError("RPC is not connected to Polygon mainnet chain 137")
        up_before = erc1155_balance(self.rpc, position_token, self.wallet, int(up_token_id))
        down_before = erc1155_balance(
            self.rpc, position_token, self.wallet, int(down_token_id)
        )
        if up_before < amount_units or down_before < amount_units:
            raise SplitExecutionError(
                "insufficient equal UP/DOWN balances for complete-set merge"
            )
        collateral_before = erc20_balance(self.rpc, PUSD_ADDRESS, self.wallet)
        approved = erc1155_is_approved_for_all(
            self.rpc, position_token, self.wallet, adapter
        )
        calls: list[ContractCall] = []
        if not approved:
            calls.append(build_operator_approval_call(position_token, adapter))
        calls.append(build_merge_call(adapter, condition_id, amount_units))
        transaction_id, transaction_hash, state = self.submitter.submit(
            calls,
            f"reversal-v11 complete-set merge {amount} pUSD",
        )
        up_after = erc1155_balance(self.rpc, position_token, self.wallet, int(up_token_id))
        down_after = erc1155_balance(
            self.rpc, position_token, self.wallet, int(down_token_id)
        )
        collateral_after = erc20_balance(self.rpc, PUSD_ADDRESS, self.wallet)
        if (
            up_before - up_after < amount_units
            or down_before - down_after < amount_units
            or collateral_after - collateral_before < amount_units
        ):
            raise SplitExecutionError(
                "merge transaction confirmed but token burn/collateral deltas were not observed"
            )
        return MergeReceipt(
            transaction_id=transaction_id,
            transaction_hash=transaction_hash,
            state=state,
            approval_included=not approved,
            amount=amount,
            amount_units=amount_units,
            collateral_received_units=collateral_after - collateral_before,
        )


def splitter_from_config(config: dict[str, str | None]) -> CompleteSetSplitter:
    private_key = str(config.get("PRIVATE_KEY") or "").strip()
    wallet = str(config.get("DEPOSIT_WALLET") or config.get("FUNDER_ADDRESS") or "").strip()
    rpc_url = str(
        config.get("POLYGON_RPC_URL")
        or config.get("RPC_URL")
        or "https://polygon.drpc.org"
    ).strip()
    signature_type = int(str(config.get("SIGNATURE_TYPE") or "0"))
    if not private_key:
        raise SplitExecutionError("PRIVATE_KEY is required for Polygon split execution")
    if not wallet:
        if signature_type == 0:
            wallet = Account.from_key(private_key).address
        else:
            raise SplitExecutionError("DEPOSIT_WALLET is required for smart-wallet splitting")
    rpc = JsonRpcReader(rpc_url)
    if signature_type == 0:
        submitter: CallSubmitter = EoaSubmitter(rpc_url, private_key, wallet)
    elif signature_type in {1, 2, 3}:
        submitter = SecureRelayerSubmitter(
            relayer_url=str(
                config.get("RELAYER_URL")
                or config.get("POLYMARKET_RELAYER_URL")
                or "https://relayer-v2.polymarket.com/"
            ),
            private_key=private_key,
            signature_type=signature_type,
            wallet=wallet,
            rpc_url=rpc_url,
            clob_url=str(config.get("CLOB_HOST") or "https://clob.polymarket.com"),
            relayer_api_key=str(config.get("RELAYER_API_KEY") or ""),
            relayer_api_key_address=str(config.get("RELAYER_API_KEY_ADDRESS") or ""),
        )
    else:
        raise SplitExecutionError(f"unsupported Polymarket signature type: {signature_type}")
    return CompleteSetSplitter(rpc, submitter, wallet)


def adapter_for_market(neg_risk: bool) -> str:
    return NEG_RISK_CTF_COLLATERAL_ADAPTER if neg_risk else CTF_COLLATERAL_ADAPTER


def position_token_for_market(neg_risk: bool) -> str:
    return NEG_RISK_ADAPTER if neg_risk else CTF_ADDRESS


def to_token_units(amount: Decimal) -> int:
    if amount <= 0:
        raise ValueError("split amount must be positive")
    scaled = amount * Decimal(10**TOKEN_DECIMALS)
    integral = scaled.to_integral_value(rounding=ROUND_DOWN)
    if scaled != integral:
        raise ValueError("split amount supports at most six decimal places")
    return int(integral)


def build_approval_call(spender: str) -> ContractCall:
    return ContractCall(
        to=PUSD_ADDRESS,
        data=_encode_function("approve(address,uint256)", ["address", "uint256"], [spender, MAX_UINT256]),
    )


def build_split_call(adapter: str, condition_id: str, amount_units: int) -> ContractCall:
    if amount_units <= 0:
        raise ValueError("split amount units must be positive")
    return ContractCall(
        to=to_checksum_address(adapter),
        data=_encode_function(
            "splitPosition(address,bytes32,bytes32,uint256[],uint256)",
            ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
            [PUSD_ADDRESS, ZERO_BYTES32, _condition_bytes(condition_id), [1, 2], amount_units],
        ),
    )


def build_merge_call(adapter: str, condition_id: str, amount_units: int) -> ContractCall:
    if amount_units <= 0:
        raise ValueError("merge amount units must be positive")
    return ContractCall(
        to=to_checksum_address(adapter),
        data=_encode_function(
            "mergePositions(address,bytes32,bytes32,uint256[],uint256)",
            ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
            [PUSD_ADDRESS, ZERO_BYTES32, _condition_bytes(condition_id), [1, 2], amount_units],
        ),
    )


def build_operator_approval_call(token: str, operator: str) -> ContractCall:
    return ContractCall(
        to=to_checksum_address(token),
        data=_encode_function(
            "setApprovalForAll(address,bool)",
            ["address", "bool"],
            [to_checksum_address(operator), True],
        ),
    )


def erc20_balance(rpc: RpcReader, token: str, account: str) -> int:
    return _decode_uint(
        rpc.call(token, _encode_function("balanceOf(address)", ["address"], [account]))
    )


def erc20_allowance(rpc: RpcReader, token: str, owner: str, spender: str) -> int:
    return _decode_uint(
        rpc.call(
            token,
            _encode_function("allowance(address,address)", ["address", "address"], [owner, spender]),
        )
    )


def erc1155_balance(rpc: RpcReader, token: str, account: str, token_id: int) -> int:
    return _decode_uint(
        rpc.call(
            token,
            _encode_function("balanceOf(address,uint256)", ["address", "uint256"], [account, token_id]),
        )
    )


def erc1155_is_approved_for_all(
    rpc: RpcReader, token: str, owner: str, operator: str
) -> bool:
    return bool(
        _decode_uint(
            rpc.call(
                token,
                _encode_function(
                    "isApprovedForAll(address,address)",
                    ["address", "address"],
                    [owner, operator],
                ),
            )
        )
    )




def _condition_bytes(condition_id: str) -> bytes:
    raw = condition_id.removeprefix("0x")
    if len(raw) != 64:
        raise ValueError("condition ID must be exactly 32 bytes")
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError("condition ID must be hexadecimal") from exc


def _encode_function(signature: str, types: list[str], values: list[Any]) -> str:
    selector = keccak(text=signature)[:4]
    return "0x" + (selector + encode(types, values)).hex()


def _decode_uint(value: str) -> int:
    raw = bytes.fromhex(value.removeprefix("0x"))
    if len(raw) < 32:
        raise SplitExecutionError("contract call returned an invalid uint256")
    return int(decode(["uint256"], raw[-32:])[0])
