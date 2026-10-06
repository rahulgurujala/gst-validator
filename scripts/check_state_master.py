#!/usr/bin/env -S uv run python
"""Diff the hardcoded state table against the GST portal's own master list.

The package decodes a GSTIN's state offline, from a table baked into
``gstin.py``. That table goes stale when the portal changes: Ladakh (38) was
added in 2019, and Dadra and Nagar Haveli merged with Daman and Diu into 26 in
2020. This fetches the portal's master and reports any drift.

It is a maintainer tool, not part of the package, and deliberately not a test:
the suite never touches the network. Run it before a release.

    uv run scripts/check_state_master.py

Exits 0 when the table agrees with the portal, 1 when it does not.
"""

import sys
from typing import Any

import httpx

# Reading these private tables is this script's entire job: it exists to
# check them against the portal, so it is the one caller that should.
from gst_validator.gstin import (
    _STATE_NAMES,  # pyright: ignore[reportPrivateUsage]
    _UNION_TERRITORIES,  # pyright: ignore[reportPrivateUsage]
)

MASTER_URL = "https://services.gst.gov.in/master/allstates"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Codes we keep that the portal's dropdown does not list, each for a reason.
EXPECTED_EXTRA = {
    "28": "retired on the Telangana split; older registrations still carry it",
    "96": "'Other Countries', from the NIC e-invoice master codes",
    "99": "the non-resident prefix; the portal labels it 'CBIC' in this dropdown",
}


def fetch_master() -> list[dict[str, Any]]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
        response = client.get(MASTER_URL, params={"includeCbic": "true"})
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
    rows: list[dict[str, Any]] = payload["data"]
    return rows


def main() -> int:
    rows = fetch_master()
    portal: dict[str, str] = {str(row["c"]): str(row["n"]) for row in rows}
    portal_ut: frozenset[str] = frozenset(str(row["c"]) for row in rows if row.get("u") == "Y")
    problems: list[str] = []

    for code in sorted(portal.keys() - _STATE_NAMES.keys()):
        problems.append(f"MISSING  {code}  portal has {portal[code]!r}, we have nothing")

    for code in sorted(_STATE_NAMES.keys() - portal.keys()):
        if code not in EXPECTED_EXTRA:
            problems.append(f"EXTRA    {code}  we have {_STATE_NAMES[code]!r}, portal does not")

    for code in sorted(portal.keys() & _STATE_NAMES.keys()):
        if code in EXPECTED_EXTRA:
            continue
        if portal[code].strip().casefold() != _STATE_NAMES[code].strip().casefold():
            problems.append(
                f"RENAMED  {code}  portal {portal[code]!r} != ours {_STATE_NAMES[code]!r}"
            )

    if portal_ut != _UNION_TERRITORIES:
        for code in sorted(portal_ut - _UNION_TERRITORIES):
            problems.append(f"UT+      {code}  {portal[code]!r} is flagged a union territory")
        for code in sorted(_UNION_TERRITORIES - portal_ut):
            problems.append(f"UT-      {code}  we flag it a union territory, the portal does not")

    print(f"portal codes: {len(portal)}   ours: {len(_STATE_NAMES)}")
    print(f"union territories: portal {len(portal_ut)}   ours {len(_UNION_TERRITORIES)}")
    if problems:
        print("\ndrift found:")
        for line in problems:
            print(" ", line)
        print("\nupdate _STATE_NAMES / _UNION_TERRITORIES in src/gst_validator/gstin.py")
        return 1
    print("\nno drift: the hardcoded table matches the portal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
