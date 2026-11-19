# HRS execution basis serialization proof

## Confirmed failure and contract

The complete ETNUSDT SELL execution for order 320067 contains trade 8898
(0.07 at 338.72) and trade 8899 (0.22 at 338.46). The existing Decimal fill
arithmetic yields 338.5227586206896551724137931 for quantity -0.29. The signed
positionRisk response reports entryPrice as the raw string `338.5227586207`.
Exact basis equality therefore rejects a unique owner with the correct quantity.

`execution_basis_matches_reported(computed, reported)` is asymmetric. Valid,
positive, finite exact equality keeps the existing fast path. For nonexact
values it quantizes only the computed execution basis with Decimal ROUND_HALF_UP
to `10 ** min(reported_decimal_exponent, -8)`, then requires equality to the
reported value. Thus the ETN example uses 1e-10; coarse or integer displays
cannot admit more than half of 1e-8 quote units. More trailing zeros can only
narrow admission. HALF_UP resolves exact half-quantum ties upward for these
positive prices. No tick-size, relative tolerance or float comparison applies.

Nonexact quantization supports at most 256 coefficient digits per input and
Decimal exponents from -256 through 256 inclusive; unsupported representations
fail closed. A private Decimal context retains all integer and fractional digits
needed for quantization, independent of ambient context precision or traps.
Nonexact work is bounded; exact finite equality does not allocate a quantization
context and retains its previous numerical range. Invalid, boolean, zero,
negative and nonfinite prices fail closed.

## Scope and invariants

The matcher is used only for reconstructed execution basis versus reported
entryPrice in `prove_positions` (both foreign and HRS owners) and
`certify_opening_inventory`. Independent actual inventory bases still compare
exactly, as do current versus captured inventories in entry occupancy. Local
protection prices are outside this change.

Unique economic owner, full signed fill evidence, exact quantities, independent
inventory, HRS local identity/quantity, marks, claims, symbol coverage and flat
bootstrap requirements remain intact. Reconstructed fills, raw payloads and
proof hashes are never quantized or rewritten. PositionMark and certified
opening inventory retain the reported entryPrice. Existing certified zero
anchors and financial history continue without resetting or reanchoring.

## Verification

The actual ETN regression failed first at the two existing exact comparison
sites for both HRS and foreign ownership (four failing cases). The new focused
suite covers the real sample; HALF_UP ties and either side of the half-quantum
boundary; coarse, integer and trailing-zero displays; asymmetric comparison;
invalid values; large coefficients and low ambient precision; supported bounds;
missing/conflicting/multiple owners; exact fill/independent/local quantities;
strict independent basis equality; bootstrap, coverage and claims; unchanged
source hashes and pre-existing zero-anchor continuation.

Run with `/tmp/tuner-fact-lag-venv/bin/python -m pytest` and the isolated local
`HRS_TEST_POSTGRES_DSN` for the equity service, reconciliation, risk ledger,
loss gates, ownership scope, acceptance and evidence export/archive suites.
Validation on 2026-11-19: 356 passed across those suites, including the runtime
permissions checks; the new serialization file independently passes 43 cases.
No schema, ACL, production ledger or deployment change is part of this fix.
