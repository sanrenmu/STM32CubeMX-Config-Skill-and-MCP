"""Pure functions for development-time cross-checks: read .ioc, compare key/value, scan the log.

A cross-check = run the same target down both paths and compare the results (not just what the command returns). The release path does not do this; development/CI runs it in a long-lived session.

Parsing goes through `iocc/props.py` (Java Properties semantics, values kept faithfully without strip); normalization happens only in `compare`, never in parsing.
"""

from __future__ import annotations

from ..iocc import props


def unescape(text: str) -> str:
    """Unescape a single-line value (kept for old callers; full semantics in `iocc/props.py`)."""
    return props.unescape_value(text)


def parse_ioc(text: str) -> dict:
    """`.ioc` text -> {key: value}. Faithful: no strip, comment lines skipped."""
    return props.loads(text).as_dict()


def parse_vector(value: str):
    """First three fields of the interrupt vector value: enabled, preemption priority, sub priority."""
    parts = value.split(":")
    try:
        return (parts[0].strip().lower() == "true", int(parts[1]), int(parts[2]))
    except (IndexError, ValueError):
        return None


#: values of these keys depend on the file name used when saving, not on the configuration, so they are not compared.
IGNORED_KEYS = ("ProjectManager.ProjectFileName", "ProjectManager.ProjectName")

#: these key families are lists: CubeMX appends entries it discovers itself (for example VirtualMode).
#: the comparison rule is 'everything we wrote is there', not exact equality.
LIST_SUFFIXES = (".IPParameters", ".GPIOParameters")

#: generated function-call order table: after loading, CubeMX appends entries for newly enabled peripherals.
#: measured: loading and re-saving one of CubeMX's own projects turns 3 entries into 7 (the 4 extra are FMC/LTDC/
#: SPI1/USART1), while the first 3 entries keep their text and order. So the rule is 'our entries are all present in the original order',
#: and entries it appends go to the normalized list, not counted as problems.
FUNCTION_LIST_KEY = "ProjectManager.functionlistsort"

#: known normalizations that CubeMX itself produces (only these two classes; evidence in the development docs):
#:   - `PCC.*`: board seeds are old-version files; after loading the device name/Vdd are rewritten and old-version keys are dropped;
#:   - `PLLSourceVirtual` vs `PLLSource`: in the DB the latter is derived from the former, and CubeMX keeps only one key name.
#: any other dropped key or changed value still counts as a problem - only these two classes are allowed here, and they are listed separately in the result.
NORMALIZED_PREFIX = "PCC."
PLL_SOURCE_PAIR = {"PLLSourceVirtual": "PLLSource", "PLLSource": "PLLSourceVirtual"}


def _pll_source_alias(key: str) -> str | None:
    """`RCC.PLLSourceVirtual` -> `RCC.PLLSource` (or the other way round)."""
    head, _sep, leaf = key.rpartition(".")
    other = PLL_SOURCE_PAIR.get(leaf)
    return (head + "." + other) if other else None


def _function_rows(value: str) -> list:
    """One row of the order table is `index-function-IP-false-HAL-true`, split by comma into entries."""
    return [row.strip() for row in (value or "").split(",") if row.strip()]


def _function_identity(row: str) -> str:
    """Drop the leading index (CubeMX renumbers rows); what remains is the identity of the row."""
    head, sep, rest = row.partition("-")
    if sep and head.strip().isdigit():
        return rest
    return row


def _compare_function_list(want: str, got: str, normalized) -> list:
    """Our entries must all be present unchanged and in order; appended entries only go to normalized."""
    expected = [_function_identity(row) for row in _function_rows(want)]
    actual = [_function_identity(row) for row in _function_rows(got)]
    missing = [row for row in expected if row not in actual]
    if missing:
        return ["missing from the list: %s -> %s" % (FUNCTION_LIST_KEY, missing)]
    position = -1
    for row in expected:
        try:
            at = actual.index(row, position + 1)
        except ValueError:
            return ["order mismatch: %s (%s was moved after other entries)"
                    % (FUNCTION_LIST_KEY, row)]
        position = at
    extra = [row for row in actual if row not in expected]
    if extra and normalized is not None:
        normalized.append("%s: CubeMX appended %d entries (%s)"
                          % (FUNCTION_LIST_KEY, len(extra), ", ".join(extra)))
    return []


def compare(expected: dict, saved: dict, normalized: list | None = None) -> list:
    """Compare 'what we wrote' with 'what CubeMX saved'. Only keys relevant to the target are looked at.

    - interrupt vectors compare only the first three fields: it normalizes the whole string to 9..11 fields and fills the rest itself;
    - list keys compare by containment: it may append items;
    - keys tied to the save file name are not compared.

    Value normalization (strip) happens only here: props parsing is faithful, comparison is by field semantics.
    """
    def _norm(value):
        return value.strip() if isinstance(value, str) else value

    problems = []
    for key, want in expected.items():
        if key in IGNORED_KEYS:
            continue
        if key.startswith(NORMALIZED_PREFIX):
            if normalized is not None and saved.get(key) != want:
                normalized.append("normalized: %s (expected %r, got %r)"
                                  % (key, want, saved.get(key)))
            continue
        if key not in saved:
            alias = _pll_source_alias(key)
            if alias is not None and saved.get(alias) == want:
                if normalized is not None:
                    normalized.append("normalized: %s replaced by equivalent key %s (same value)"
                                      % (key, alias))
                continue
            problems.append("missing key: %s (expected %r)" % (key, want))
            continue
        got = saved[key]
        want, got = _norm(want), _norm(got)
        if key.startswith("NVIC.") and key != "NVIC.PriorityGroup":
            left, right = parse_vector(want), parse_vector(got)
            if left and right and left != right:
                problems.append("interrupt parameter mismatch: %s expected %s got %s"
                                % (key, left, right))
        elif key == FUNCTION_LIST_KEY:
            problems.extend(_compare_function_list(want, got, normalized))
        elif key.endswith(LIST_SUFFIXES):
            present = set(got.split(","))
            missing = []
            aliased = []
            for name in want.split(","):
                if not name or name in present:
                    continue
                alias = PLL_SOURCE_PAIR.get(name)
                if alias is not None and alias in present:
                    aliased.append(name)
                    continue
                missing.append(name)
            if aliased and normalized is not None:
                normalized.append("normalized: %s in %s replaced by an equivalent key name (same value)"
                                  % (key, ", ".join(aliased)))
            if missing:
                problems.append("missing from the list: %s -> %s" % (key, missing))
        elif got != want:
            problems.append("value mismatch: %s expected %r got %r" % (key, want, got))
    return problems


#: known harmless load notices: some devices have no RIF virtual pin, so CubeMX reports one line every time.
ALLOWED_LOG_HINTS = (
    "VP_RIF_VS_RIF1",
    "Unable to find key (Mcu.ThirdPartyNb)",
    # CubeMX's own saved DMA fixture prints this group on load too (measured:
    # evidence/fixture_dma_usart1tx.ioc has the same 8 lines); generation is correct, so it is noise.
    "IP (DMA) : Parameter (",
    # CubeMX's network component (AdobePublisher) prints one line when offline or on a slow network
    # "Exception while opening the connection: SocketTimeoutException".
    # and it is unrelated to the project configuration (measured: the same .ioc stays quiet with network, prints when it flaps), so it is noise;
    # only this one source is allowed (not by exception name), so real connectivity problems are not hidden.
    "AdobePublisher",
    # event group parameters (Events01) are created by the FreeRTOS plugin at runtime and are not declared in the DB;
    # after we write them, loading reports 'Invalid parameter (Events01)', but the value is kept and generation works (measured).
    "Invalid parameter (Events01)",
)


def errors_in_log(log_text: str, allow=ALLOWED_LOG_HINTS) -> list:
    """Lines in the log that really are problems (dropped on load, errors). Allowed lines are not."""
    out = []
    for line in (log_text or "").splitlines():
        if "OptionalMessage_ERROR" not in line and "[ERROR]" not in line:
            continue
        if any(hint in line for hint in allow):
            continue
        out.append(line.strip())
    return out