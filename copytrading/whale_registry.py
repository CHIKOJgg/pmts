from __future__ import annotations

from typing import Optional

from copytrading.models import WhaleProfile

BUILTIN_WHALES: list[tuple[str, str, str, Optional[float], Optional[float]]] = [
    ("0xf8831548531d56ad6a4331493243c447a827cd1f", "Inaccuratestake", "Politics+Macro", None, 3.99e6),
    ("0x5e4c3b5b81171e2ca4ab776ac0d6bba787f9dba2", "endlessFate", "Multi-category", None, 7.41e6),
    ("0xbc11a64ab34a03a043fbe80598fa065ee87eeec6", "frostrizz", "Crypto", None, 3.02e6),
    ("0x96cfcb0c30942cfcd1cdf76c7d408794d66b1acb", "mintblade", "Multi-category", None, 9.24e6),
    ("0x36a3f17401e395ef4cb1b7f42bcdb8ab8e15fafb", "gfjoigfsjoigsjoi", "Crypto", None, 2.93e6),
    ("0x03e8a544e97eeff5753bc1e90d46e5ef22af1697", "weflyhigh", "Sports", None, 2.40e6),
    ("0x5bffcf561bcae83af680ad600cb99f1184d6ffbe", "0943", "Multi-category", None, 2.36e6),
    ("0xc2e7800b5af46e6093872b177b7a5e7f0563be51", "beachboy4", "Sports", None, 4.67e6),
    ("0xed64a7bf029040aa331abc87902434d815ef217d", "fishalive", "Crypto", None, 9.06e6),
    ("0x0b89e6c79decff0365855c828c73caa1ccd0d710", "Slickvenom", "Politics", None, 1.59e6),
    ("0x9f2fe025f84839ca81dd8e0338892605702d2ca8", "surfandturf", "Macro", None, 3.13e6),
    ("0x5966db1fe50763c9e3c014d756369bad07e1f804", "0x5966…f804", "Multi-category", None, 2.04e6),
    ("0x8cb4ca5af7d9361322340bb307a828d288c91057", "Whale50", "Multi-category", None, 2.40e6),
    ("0x3f87d51f27ba6e19ec52aaeebb68559a839c742c", "GRIMDRIP", "Multi-category", None, 7.60e6),
    ("0xee00ba338c59557141789b127927a55f5cc5cea1", "S-Works", "Sports", None, 2.91e6),
    ("0x26437896ed9dfeb2f69765edcafe8fdceaab39ae", "Latina", "Politics", None, 1.59e6),
    ("0xf0318c32136c2db7fec88b84869aee6a1106c80c", "BreakTheBank", "Multi-category", None, 1.75e6),
    ("0x94a428cfa4f84b264e01f70d93d02bc96cb36356", "GCottrell93", "Politics", None, 3.44e6),
    ("0x84dbb7103982e3617704a2ed7d5b39691952aeeb", "Soarin22", "Crypto", None, 1.81e6),
    ("0x241f846866c2de4fb67cdb0ca6b963d85e56ef50", "Pestle", "Multi-category", None, 1.91e6),
]

_ADDRESS_SET: set[str] = {entry[0].lower() for entry in BUILTIN_WHALES}


def get_whale_profiles(custom_addresses: Optional[list[str]] = None) -> list[WhaleProfile]:
    profiles = [
        WhaleProfile(
            address=addr,
            name=name,
            category=cat,
            total_pnl_usdc=pnl,
            win_rate=wr,
        )
        for addr, name, cat, wr, pnl in BUILTIN_WHALES
    ]
    if custom_addresses:
        known = {p.address.lower() for p in profiles}
        for addr in custom_addresses:
            if addr.lower() not in known:
                profiles.append(WhaleProfile(address=addr, name=addr[:10] + "…"))
    return profiles


def is_known_whale(address: str) -> bool:
    return address.lower() in _ADDRESS_SET
