"""Unit tests for the LLM runner's amount parser (_parse_llm_amount).

Run directly:  python3 agents/test_llm_amount.py

Regression coverage for the Round-3 N1 finding: the old parser
regex-grabbed the first digit run of the model's answer, so a
cautious JSON number like 0.00005 (stringified by Python as
'5e-05') became 5 NEAR and was clamped to the maximum legal take,
a negative answer silently lost its sign, and a yocto-denominated
answer became a capped maximum. The parser must read the value
the model actually meant, and reject anything else so the runner
falls back to committing 0.
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm_runner import _parse_llm_amount  # noqa: E402

OK_CASES = {
    "0.0005": Decimal("0.0005"),
    "5e-05": Decimal("0.00005"),      # str(0.00005), the N1 case
    "2.5e-05": Decimal("0.000025"),
    "1e-3": Decimal("0.001"),
    "0": Decimal("0"),
    "12": Decimal("12"),
    "0.000000000000000000000001": Decimal("0.000000000000000000000001"),
}
for raw, want in OK_CASES.items():
    got = _parse_llm_amount(raw)
    assert got == want, f"_parse_llm_amount({raw!r}) = {got}, want {want}"

# A JSON number arrives via str(): 0.00005 -> '5e-05' (covered
# above); a plain float like 0.0005 -> '0.0005'.
assert _parse_llm_amount(str(0.0005)) == Decimal("0.0005")
assert _parse_llm_amount(0) == Decimal("0")

BAD_CASES = [
    "-0.5",                            # sign must not be dropped
    "-1",
    "250000000000000000000",           # yocto-denominated: unit error
    "2000000",                         # over the plausibility bound
    "abc",
    "",
    "NaN",
    "Infinity",
]
for raw in BAD_CASES:
    try:
        _parse_llm_amount(raw)
    except ValueError:
        pass
    else:
        raise AssertionError(f"_parse_llm_amount accepted {raw!r}")

print("llm amount parser tests passed")
