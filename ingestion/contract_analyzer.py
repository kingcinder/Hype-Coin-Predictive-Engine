"""Contract Analyzer — on-chain honeypot detection and deployer history.

Analyzes token contracts for suspicious patterns: honeypot functions,
hidden mint, ownership not renounced, and deployer track record.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from common.http import HttpClient
from common.logging import get_logger

log = get_logger(__name__)

# Honeypot indicator patterns in contract bytecode (simplified heuristics)
HONEYPOT_PATTERNS = [
    # Function selectors for known honeypot patterns
    ("approve", "allowance-based sell block"),
    ("transferFrom", "selective transfer restriction"),
    # Ownership patterns that prevent selling
    ("onlyOwner", "owner-only restriction"),
    ("_isExcluded", "exclusion list (may block sells)"),
]

# Deployer rug-check loaded lazily from database
_known_rug_deployers: set[str] | None = None


@dataclass
class ContractAnalysis:
    """Results of analyzing a token contract."""
    suspicious_flags: int = 0
    reasons: list[str] = field(default_factory=list)
    is_honeypot: bool = False
    ownership_renounced: bool | None = None
    deployer_known_rug: bool = False
    has_mint_function: bool = False
    has_pause_function: bool = False
    contract_age_hours: float | None = None
    deployer_tx_count: int | None = None


def _get_rug_deployers() -> set[str]:
    """Lazily load known rug deployers from database."""
    global _known_rug_deployers
    if _known_rug_deployers is None:
        try:
            from sqlalchemy import text

            from storage.database import SessionLocal
            with SessionLocal() as session:
                rows = session.execute(
                    text(
                        "SELECT DISTINCT deployer_wallet FROM contracts "
                        "WHERE deployer_wallet IS NOT NULL"
                    )
                ).fetchall()
                _known_rug_deployers = {r[0].lower() for r in rows if r[0]}
        except Exception:  # noqa: BLE001
            _known_rug_deployers = set()
    return _known_rug_deployers


def analyze_contract(
    contract_address: str,
    chain: str = "ethereum",
    *,
    http: HttpClient | None = None,
) -> ContractAnalysis:
    """Analyze a token contract for suspicious patterns.

    Uses Etherscan/Solana FM bytecode inspection and deployer history.
    """
    result = ContractAnalysis()

    if chain in ("ethereum", "base"):
        _analyze_evm_contract(contract_address, chain, result, http)
    elif chain == "solana":
        _analyze_solana_contract(contract_address, result, http)

    # Check deployer reputation
    rug_deployers = _get_rug_deployers()
    if result.deployer_known_rug or any(
        r.lower() in rug_deployers for r in result.reasons if r.startswith("deployer:")
    ):
        result.deployer_known_rug = True
        result.suspicious_flags += 3
        if not any("prior rug-pull" in r for r in result.reasons):
            result.reasons.append("Deployer wallet has prior rug-pull history")

    return result


def _analyze_evm_contract(
    address: str,
    chain: str,
    result: ContractAnalysis,
    http: HttpClient | None = None,
) -> None:
    """Analyze an EVM contract using Etherscan API."""
    client = http or HttpClient(base_url="https://api.etherscan.io")
    try:
        # Fetch contract source code for pattern matching
        chain_id = "1" if chain == "ethereum" else "8453"
        data = client.get_json(
            "/v2/api",
            params={
                "chainid": chain_id,
                "module": "contract",
                "action": "getsourcecode",
                "address": address,
            },
        )

        source_code = ""
        if isinstance(data, dict) and data.get("result"):
            result_list = data["result"]
            if isinstance(result_list, list) and result_list:
                source_code = result_list[0].get("SourceCode", "") or ""

        if source_code:
            # Check for honeypot patterns
            for pattern, description in HONEYPOT_PATTERNS:
                if pattern.lower() in source_code.lower():
                    result.suspicious_flags += 1
                    result.reasons.append(f"Contract contains {description}")

            # Check for mint function
            if re.search(r"function\s+mint\s*\(", source_code):
                result.has_mint_function = True
                result.suspicious_flags += 1
                result.reasons.append("Contract has mint function (supply can be inflated)")

            # Check for pause function
            if re.search(r"function\s+pause\s*\(", source_code):
                result.has_pause_function = True
                result.suspicious_flags += 1
                result.reasons.append("Contract has pause function (trading can be halted)")

            # Check ownership
            if "Ownable" in source_code:
                # Check if ownership was transferred to zero address (renounced)
                if re.search(r"transferOwnership\s*\(\s*0x0+", source_code):
                    result.ownership_renounced = True
                else:
                    result.ownership_renounced = False
                    result.suspicious_flags += 1
                    result.reasons.append("Contract ownership not renounced")
        else:
            # No source code available — proxy or unverified contract
            result.suspicious_flags += 1
            result.reasons.append("Contract source code not verified")

        # Fetch deployer info
        tx_data = client.get_json(
            "/v2/api",
            params={
                "chainid": chain_id,
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": 1,
                "sort": "asc",
            },
        )
        if isinstance(tx_data, dict) and tx_data.get("result"):
            txs = tx_data["result"]
            if isinstance(txs, list) and txs:
                deployer = txs[0].get("from", "")
                result.contract_age_hours = _estimate_age_hours(txs[0].get("timeStamp"))
                # Check deployer history
                _check_deployer_history(deployer, client, chain_id, result)

    except Exception as exc:  # noqa: BLE001
        log.debug("contract_analysis_error", address=address, error=str(exc))
    finally:
        if http is None:
            client.close()


def _analyze_solana_contract(
    address: str,
    result: ContractAnalysis,
    http: HttpClient | None = None,
) -> None:
    """Analyze a Solana token using on-chain data."""
    client = http or HttpClient(base_url="https://api.mainnet-beta.solana.com")
    try:
        # Get token metadata
        data = client.post_json(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [address, {"encoding": "jsonParsed"}],
            },
        )
        account_info = (data or {}).get("result", {}).get("value")
        if not account_info:
            result.suspicious_flags += 1
            result.reasons.append("Token account not found")
            return

        # Check for mint authority (can mint more tokens)
        program_data = account_info.get("data", {})
        if isinstance(program_data, dict):
            parsed = program_data.get("parsed", {})
            info = parsed.get("info", {})
            if info.get("mintAuthority"):
                result.has_mint_function = True
                result.suspicious_flags += 1
                result.reasons.append("Solana token has mint authority (supply can be inflated)")
            else:
                result.ownership_renounced = True  # No mint authority

    except Exception as exc:  # noqa: BLE001
        log.debug("solana_analysis_error", address=address, error=str(exc))
    finally:
        if http is None:
            client.close()


def _check_deployer_history(
    deployer: str,
    client: HttpClient,
    chain_id: str,
    result: ContractAnalysis,
) -> None:
    """Check if deployer has prior rug-pull history."""
    try:
        tx_data = client.get_json(
            "/v2/api",
            params={
                "chainid": chain_id,
                "module": "account",
                "action": "txlist",
                "address": deployer,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": 5,
                "sort": "desc",
            },
        )
        if isinstance(tx_data, dict) and tx_data.get("result"):
            txs = tx_data["result"]
            if isinstance(txs, list):
                result.deployer_tx_count = len(txs)
                # Check if deployer is in known rug list
                rug_deployers = _get_rug_deployers()
                if deployer.lower() in rug_deployers:
                    result.deployer_known_rug = True
    except Exception:  # noqa: BLE001
        pass


def _estimate_age_hours(timeStamp: str | int | None) -> float | None:
    """Estimate contract age in hours from deployment timestamp."""
    if not timeStamp:
        return None
    try:
        ts = int(timeStamp)
        return (time.time() - ts) / 3600.0
    except (ValueError, TypeError):
        return None
