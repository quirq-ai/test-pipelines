"""Verdict policy, from infra-config's `flakes.toml` [verdict] (read with qqcfg.load).

    retry_failed       how many times failed tests are rerun with the change
    compare_with_base  then run the still-failing tests without the change
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path

from qqresults.errors import Error


class PolicyError(Error):
    pass


@dataclass(frozen=True)
class Policy:
    retry_failed: int = 1
    compare_with_base: bool = True


def from_infra_config(root: Path) -> Policy:
    """Read the policy from an infra-config checkout through its own loader, qqcfg.load."""
    path = Path(root) / "tools" / "qqcfg.py"
    spec = importlib.util.spec_from_file_location("qqcfg", path)
    if spec is None or not path.is_file():
        raise PolicyError(f"{path}: not an infra-config checkout (no tools/qqcfg.py)")
    qqcfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qqcfg)
    try:
        v = qqcfg.load(Path(root))["flakes"]["verdict"]
    except getattr(qqcfg, "ConfigError", ValueError) as e:
        raise PolicyError(f"{root}: {e}") from None
    try:
        return Policy(retry_failed=int(v["retry_failed"]),
                      compare_with_base=bool(v["compare_with_base"]))
    except (KeyError, TypeError, ValueError) as e:
        raise PolicyError(f"{root}: config/flakes.toml has no usable [verdict]: {e}") from None
