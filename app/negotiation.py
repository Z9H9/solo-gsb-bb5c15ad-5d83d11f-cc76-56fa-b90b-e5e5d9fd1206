"""Accept-Language parsing and language-range negotiation.

Implements the negotiation rules required by the service:

* q-weight parsing per RFC 9110 (``Accept-Language``), including ``q=0``
  exclusion, the wildcard range ``*`` and duplicated ranges;
* lookup style matching (RFC 4647 section 3.4): a candidate tag is truncated
  one subtag at a time (``zh-Hant-TW`` -> ``zh-Hant`` -> ``zh``) until a
  configured language range matches;
* concrete ranges always take precedence over the wildcard;
* candidates are ranked by the client q weight and ties are broken by the
  stable order of the server-supported list;
* empty / absent header falls back to the configured default language;
* malformed header syntax raises :class:`AcceptLanguageError`;
* when standard lookup yields no acceptable candidate, optional directed
  ``locale_fallbacks`` chains (source tag -> next tag) are walked starting
  from the client's positive-q concrete ranges, in descending q / header
  order; a chain node that is a supported tag wins, unless it is excluded
  by any concrete q=0 range, in which case the walk continues.

Language tag grammar follows RFC 5646 (BCP 47) primary tag grammar, which
covers every ordinary language tag (the legacy "grandfathered" irregular
tags from the RFC registry are intentionally rejected).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# RFC 5646 section 2.1 grammar (main tag grammar, no grandfathered tags).
_PRIVATEUSE = r"x(?:-[0-9a-z]{1,8})+"
_LANGUAGE_TAG = (
    r"(?:[a-z]{2,3}(?:-[a-z]{3}){0,3}"  # 2/3 letter language + extlangs
    r"|[a-z]{4,8})"  # 4+ letter language
    r"(?:-[a-z]{4})?"  # script
    r"(?:-(?:[a-z]{2}|[0-9]{3}))?"  # region
    r"(?:-(?:[0-9a-z]{5,8}|[0-9][0-9a-z]{3}))*"  # variants
    r"(?:-[0-9a-wy-z](?:-[0-9a-z]{2,8})+)*"  # extensions
    r"(?:-x(?:-[0-9a-z]{1,8})+)?"  # private use
)
TAG_RE = re.compile(rf"(?:{_LANGUAGE_TAG}|{_PRIVATEUSE})", re.IGNORECASE)
_RANGE_RE = re.compile(rf"^(?:\*|{_LANGUAGE_TAG}|{_PRIVATEUSE})$", re.IGNORECASE)

# RFC 9110 quality value: 0 / 1 / three-decimal fractions ("q=.5" is invalid).
_QVALUE_RE = re.compile(r"^(?:0(?:\.\d{1,3})?|1(?:\.0{1,3})?)$")


class AcceptLanguageError(ValueError):
    """Raised when an Accept-Language header value is syntactically invalid."""


class LocaleFallbackError(ValueError):
    """Raised when a ``locale_fallbacks`` mapping is structurally invalid."""


def normalize_fallbacks(
    raw: Any,
) -> dict[str, str]:
    """Validate the shape of a locale_fallbacks mapping and lowercase it.

    Keys/values must be well-formed BCP 47 tags (case-insensitive); keys
    may not repeat (case-insensitive). Raises
    :class:`LocaleFallbackError` otherwise.
    """

    if not isinstance(raw, dict):
        raise LocaleFallbackError("locale_fallbacks 必须是对象（源标签 -> 下一标签）")
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not TAG_RE.fullmatch(key):
            raise LocaleFallbackError(f"不是合法的 BCP 47 源标签: {key!r}")
        if not isinstance(value, str) or not TAG_RE.fullmatch(value):
            raise LocaleFallbackError(
                f"源标签 {key!r} 的下一标签不是合法的 BCP 47 标签: {value!r}"
            )
        source = key.lower()
        if source in normalized:
            raise LocaleFallbackError(
                f"locale_fallbacks 存在重复源标签（大小写不敏感）: {key!r}"
            )
        normalized[source] = value.lower()
    return normalized


def validate_fallback_chains(
    chains: dict[str, str], supported: list[str]
) -> None:
    """Reject cycles and dangling chains that never reach a supported tag.

    Every source key must, by repeatedly following the directed edges,
    terminate at one of ``supported`` (case-insensitive). A loop on the
    way (including a self-loop before reaching a supported node) is a
    cycle; walking off the map without hitting a supported tag is a
    dangling chain.
    """

    supported_lower = {tag.lower() for tag in supported}
    for start in chains:
        seen: set[str] = set()
        node = start
        while True:
            if node in supported_lower:
                break
            if node in seen:
                raise LocaleFallbackError(
                    f"locale_fallbacks 从 {start!r} 出发存在环路"
                    f"（节点 {node!r} 被重复访问）"
                )
            seen.add(node)
            if node not in chains:
                raise LocaleFallbackError(
                    f"locale_fallbacks 从 {start!r} 出发的链在 {node!r} 处悬空："
                    "无法到达任何受支持语言"
                )
            node = chains[node]


@dataclass(frozen=True)
class LanguageRange:
    """A single ``language-range [ ";" "q=" qvalue ]`` entry."""

    range: str
    q: float
    position: int  # 1-based position in the header, used for diagnostics

    def to_dict(self) -> dict[str, Any]:
        return {"range": self.range, "q": self.q, "position": self.position}


def _parse_qvalue(raw: str, position: int) -> float:
    # OWS around ";" and "=" is allowed per RFC 9110, but the parameter
    # itself must be non-empty and of the form q=value.
    parameter = raw.strip()
    if not parameter:
        raise AcceptLanguageError(f"第 {position} 个语言范围含有空参数")
    if "=" not in parameter:
        raise AcceptLanguageError(
            f"第 {position} 个语言范围的 q 参数缺少取值"
        )
    key, _, value = parameter.partition("=")
    key, value = key.strip(), value.strip()
    if key != "q":
        raise AcceptLanguageError(
            f"第 {position} 个语言范围含有非法参数: {key!r}"
        )
    if not value:
        raise AcceptLanguageError(f"第 {position} 个语言范围的 q 参数缺少取值")
    if not _QVALUE_RE.match(value):
        raise AcceptLanguageError(
            f"第 {position} 个语言范围的 q 值非法: {value!r}"
        )
    return float(value)


def parse_accept_language(header: str | None) -> list[LanguageRange]:
    """Parse an Accept-Language header into ordered language ranges.

    Returns an empty list when the header is missing or whitespace-only.
    Duplicate ranges (same range, case-insensitive) keep their first
    occurrence; later occurrences are ignored.

    Raises :class:`AcceptLanguageError` on malformed syntax.
    """

    if header is None or not header.strip():
        return []

    ranges: list[LanguageRange] = []
    seen: set[str] = set()
    for position, element in enumerate(header.split(","), start=1):
        token = element.strip()
        if not token:
            # Trailing/empty element such as "en,," is a syntax error.
            raise AcceptLanguageError(f"第 {position} 个语言范围为空")

        # Split off parameters; OWS around ";" is permitted, so each
        # part is stripped. A space *inside* the tag token itself is not.
        parts = [part.strip() for part in element.split(";")]
        tag = parts[0]
        if not tag or " " in tag:
            raise AcceptLanguageError(
                f"第 {position} 个语言范围的标签非法: {tag!r}"
            )
        if not _RANGE_RE.match(tag):
            raise AcceptLanguageError(
                f"第 {position} 个语言范围不是合法的 BCP 47 标签: {tag!r}"
            )

        q = 1.0
        for parameter in parts[1:]:
            q = _parse_qvalue(parameter, position)

        normalized = tag.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        ranges.append(
            LanguageRange(range=normalized, q=q, position=position)
        )

    return ranges


def truncation_chain(tag: str) -> list[str]:
    """RFC 4647 section 3.4 lookup chain, always lowercase.

    Subtags are removed one at a time from the right; prefixes ending at
    or after a singleton subtag (extension markers like ``u`` or the
    privateuse marker ``x``) are skipped, because a singleton must never
    be the trailing subtag of a lookup prefix::

        zh-Hant-TW -> [zh-hant-tw, zh-hant, zh]
        zh-CN-u-ca-chinese -> [zh-cn-u-ca-chinese, zh-cn-u-ca, zh-cn, zh]
        en-US-x-foo -> [en-us-x-foo, en-us, en]
        x-foo-bar -> [x-foo-bar, x-foo]
    """

    subtags = tag.lower().split("-")
    chain: list[str] = []
    for end in range(len(subtags), 0, -1):
        last = subtags[end - 1]
        # Never stop on a singleton (extension / privateuse marker);
        # continue removing subtags from the right.
        if len(last) == 1:
            continue
        chain.append("-".join(subtags[:end]))
    return chain


def _find_concrete_range(
    chain: list[str], ranges: list[LanguageRange]
) -> tuple[LanguageRange | None, str | None]:
    """Return the first configured range matching any chain prefix.

    The chain is tried longest-first; within a prefix the first range in
    header order wins. Returns ``(range, matched_prefix)``.
    """

    by_range = {entry.range: entry for entry in ranges}
    for prefix in chain:
        entry = by_range.get(prefix)
        if entry is not None:
            return entry, prefix
    return None, None


def _wildcard_range(ranges: list[LanguageRange]) -> LanguageRange | None:
    for entry in ranges:
        if entry.range == "*":
            return entry
    return None


def _concrete_q_zero_excluded(
    tag_lower: str, zero_ranges: list[str]
) -> str | None:
    """Return a concrete q=0 range that excludes ``tag_lower``, if any.

    Exclusion uses the same lookup truncation as acceptance: the tag is
    excluded when one of its truncation prefixes is a concrete q=0 range.
    The wildcard ``*`` is deliberately not considered here; the
    fallbacks contract only forbids candidates excluded by concrete
    q=0 ranges.
    """

    excluded = set(zero_ranges)
    for prefix in truncation_chain(tag_lower):
        if prefix in excluded:
            return prefix
    return None


def _chain_fallback(
    parsed: list[LanguageRange],
    chains: dict[str, str],
    supported_lower_to_index: dict[str, int],
    zero_concrete: list[str],
) -> dict[str, Any] | None:
    """Walk locale_fallbacks chains from positive-q concrete ranges.

    Concrete ranges with strictly positive q are tried in descending q
    order, ties broken by header order. For each one whose label has an
    outgoing chain edge, the directed chain is followed: the first chain
    node that is a supported tag and is not excluded by a concrete q=0
    range wins. Returns a report dict, or ``None`` when no chain reaches
    an acceptable supported tag.
    """

    positive = [
        entry
        for entry in parsed
        if entry.range != "*" and entry.q > 0 and entry.range in chains
    ]
    positive.sort(key=lambda entry: (-entry.q, entry.position))

    for source in positive:
        path: list[str] = [source.range]
        visited: set[str] = {source.range}
        node = source.range
        while True:
            index = supported_lower_to_index.get(node)
            if index is not None:
                blocking = _concrete_q_zero_excluded(node, zero_concrete)
                if blocking is None:
                    return {
                        "source_range": source.range,
                        "source_q": source.q,
                        "source_position": source.position,
                        "path": path,
                        "selected_supported_index": index,
                    }
                # Supported but explicitly excluded: keep walking.
            nxt = chains.get(node)
            if nxt is None or nxt in visited:
                break
            visited.add(nxt)
            path.append(nxt)
            node = nxt
    return None


def negotiate(
    header: str | None,
    supported: list[str],
    default: str,
    locale_fallbacks: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the full negotiation and build the explanation payload.

    Parameters mirror the API request: ``header`` is the raw
    Accept-Language value, ``supported`` the server-supported BCP 47 tags
    in priority order, ``default`` the fallback tag (must be in
    ``supported``), ``locale_fallbacks`` an optional source-tag ->
    next-tag directed-chain mapping (already normalized to lowercase).
    """

    chains = (
        {source.lower(): target.lower() for source, target in locale_fallbacks.items()}
        if locale_fallbacks
        else {}
    )

    parsed = parse_accept_language(header)

    ignored_duplicates: list[dict[str, Any]] = []
    if header and header.strip():
        # Second pass purely for reporting dropped duplicate elements.
        seen: set[str] = set()
        for position, element in enumerate(header.split(","), start=1):
            token = element.strip()
            tag = token.split(";", 1)[0].strip().lower()
            if tag in seen:
                ignored_duplicates.append(
                    {"range": tag, "position": position}
                )
            else:
                seen.add(tag)

    candidates: list[dict[str, Any]] = []
    for index, candidate in enumerate(supported):
        chain = truncation_chain(candidate)
        events: list[dict[str, Any]] = []

        matched, matched_prefix = _find_concrete_range(chain, parsed)
        for prefix in chain:
            if matched is not None and prefix == matched_prefix:
                events.append(
                    {
                        "truncated_to": prefix,
                        "outcome": "match",
                        "matched_range": matched.range,
                        "q": matched.q,
                        "header_position": matched.position,
                    }
                )
            else:
                events.append(
                    {"truncated_to": prefix, "outcome": "no-match"}
                )

        wildcard = _wildcard_range(parsed)
        if matched is None and wildcard is not None:
            events.append(
                {
                    "truncated_to": "*",
                    "outcome": "match",
                    "matched_range": "*",
                    "q": wildcard.q,
                    "header_position": wildcard.position,
                }
            )
        elif wildcard is not None:
            events.append(
                {
                    "truncated_to": "*",
                    "outcome": "skipped",
                    "detail": "存在更具体的语言范围匹配，通配符未参与",
                }
            )

        effective = matched if matched is not None else wildcard
        q_value = effective.q if effective is not None else None

        if not parsed:
            status = "no-range"
            detail = "请求头中没有有效语言范围"
        elif effective is None:
            status = "unmatched"
            detail = "所有逐级截断均未命中任何语言范围（含通配符）"
        elif effective.q == 0:
            status = "excluded"
            detail = f"匹配到的范围 {effective.range!r} q=0，候选被显式排除"
        else:
            status = "eligible"
            detail = "候选可接受"

        candidates.append(
            {
                "tag": candidate,
                "supported_index": index,
                "truncation_chain": chain,
                "events": events,
                "matched_range": (
                    effective.range if effective is not None else None
                ),
                "q": q_value,
                "status": status,
                "detail": detail,
            }
        )

    eligible = [c for c in candidates if c["status"] == "eligible"]
    selected_index: int | None = None
    chain_report: dict[str, Any] | None = None
    if eligible:
        # Highest client weight first; server list order breaks ties
        # (min() keeps the earliest supported_index among equal q).
        winner = min(eligible, key=lambda c: (-c["q"], c["supported_index"]))
        selected_index = winner["supported_index"]
        winner["status"] = "selected"
        winner["detail"] = "在所有可接受候选中权重最高（同权重按服务端列表顺序最早）"
    elif chains:
        # Standard lookup found nothing acceptable: try the directed
        # locale fallback chains, starting from the client's positive-q
        # concrete ranges in descending q / header order.
        zero_concrete = [
            entry.range
            for entry in parsed
            if entry.range != "*" and entry.q == 0
        ]
        supported_lower_to_index = {
            tag.lower(): index for index, tag in enumerate(supported)
        }
        chain_report = _chain_fallback(
            parsed, chains, supported_lower_to_index, zero_concrete
        )

    used_fallback = selected_index is None and chain_report is None
    if chain_report is not None:
        selected_index = chain_report["selected_supported_index"]
        winner = candidates[selected_index]
        winner["status"] = "selected"
        winner["detail"] = (
            f"标准 lookup 无可用候选，沿客户端范围 "
            f"{chain_report['source_range']!r} 的 locale_fallbacks 链 "
            f"{' -> '.join(chain_report['path'])} 找到该受支持标签"
        )
        message = (
            f"已按 locale_fallbacks 链从客户端范围 "
            f"{chain_report['source_range']!r} 沿 "
            f"{' -> '.join(chain_report['path'])} 选出语言"
        )
        reason = None
    elif used_fallback:
        selected_index = next(
            i for i, tag in enumerate(supported) if tag == default
        )
        if not parsed:
            reason = "no-valid-range"
            message = "Accept-Language 缺失或没有有效范围，回退到默认语言"
        elif not eligible:
            reason = "no-acceptable-candidate"
            message = "没有可接受的候选语言（均被 q=0 排除或未命中），回退到默认语言"
        else:  # pragma: no cover - defensive, eligible is non-empty above
            reason = "no-acceptable-candidate"
            message = "回退到默认语言"
        if chains and chain_report is None:
            message += "；locale_fallbacks 链也未找到未被 q=0 具体范围排除的受支持语言"
        default_report = candidates[selected_index]
        if default_report["status"] == "excluded":
            message += "；注意默认语言本身被客户端 q=0 显式排除"
    else:
        reason = None
        message = "已按客户端权重选出最佳语言"

    return {
        "selected_language": supported[selected_index],
        "selected_supported_index": selected_index,
        "fallback": used_fallback,
        "fallback_reason": reason,
        "message": message,
        "parsed_ranges": [entry.to_dict() for entry in parsed],
        "ignored_duplicates": ignored_duplicates,
        "candidates": candidates,
        "locale_fallback": (
            {
                "triggered": True,
                "source_range": chain_report["source_range"],
                "source_q": chain_report["source_q"],
                "source_position": chain_report["source_position"],
                "path": chain_report["path"],
                "selected_language": supported[
                    chain_report["selected_supported_index"]
                ],
            }
            if chain_report is not None
            else None
        ),
    }
