"""Unit tests for Accept-Language parsing and negotiation core."""

import pytest

from app.negotiation import (
    AcceptLanguageError,
    GroupParticipantInput,
    LocaleFallbackError,
    negotiate,
    negotiate_group,
    normalize_fallbacks,
    parse_accept_language,
    truncation_chain,
    validate_fallback_chains,
)


class TestParse:
    def test_none_and_empty(self):
        assert parse_accept_language(None) == []
        assert parse_accept_language("   ") == []

    def test_basic_q_weights_and_order(self):
        ranges = parse_accept_language("zh-CN,zh;q=0.9,en;q=0.8")
        assert [(r.range, r.q, r.position) for r in ranges] == [
            ("zh-cn", 1.0, 1),
            ("zh", 0.9, 2),
            ("en", 0.8, 3),
        ]

    def test_wildcard(self):
        ranges = parse_accept_language("*;q=0.5")
        assert ranges[0].range == "*"
        assert ranges[0].q == 0.5

    def test_duplicate_ranges_keep_first(self):
        ranges = parse_accept_language("en;q=0.8,EN;q=0.2, fr")
        assert [(r.range, r.q) for r in ranges] == [("en", 0.8), ("fr", 1.0)]

    def test_q_zero_preserved(self):
        ranges = parse_accept_language("en;q=0")
        assert ranges[0].q == 0.0

    @pytest.mark.parametrize(
        "header",
        [
            "en;q=",  # missing q value
            "en;q=2",  # out of range
            "en;q=1.0001",  # too many decimals
            "en;q=.5",  # leading dot forbidden
            "en;r=0.5",  # unknown parameter
            "en;q;q",  # q without value
            "not a tag",  # whitespace in tag
            "12345",  # not BCP 47
            "en,,fr",  # empty element
            "en-",  # dangling separator
            "en;;fr",  # empty parameter
        ],
    )
    def test_invalid_syntax(self, header):
        with pytest.raises(AcceptLanguageError):
            parse_accept_language(header)

    def test_ows_around_elements_and_params_allowed(self):
        # RFC 9110 allows OWS around elements, ";" and "=".
        ranges = parse_accept_language(" en , fr ; q = 0.5 ")
        assert [(r.range, r.q) for r in ranges] == [("en", 1.0), ("fr", 0.5)]


class TestTruncationChain:
    def test_basic(self):
        assert truncation_chain("zh-Hant-TW") == [
            "zh-hant-tw",
            "zh-hant",
            "zh",
        ]

    def test_singleton_prefixes_skipped_but_intermediate_kept(self):
        # RFC 4647 truncates one subtag at a time and only skips prefixes
        # whose trailing subtag is a singleton.
        assert truncation_chain("zh-CN-u-ca-chinese") == [
            "zh-cn-u-ca-chinese",
            "zh-cn-u-ca",
            "zh-cn",
            "zh",
        ]

    def test_privateuse_singleton(self):
        assert truncation_chain("en-US-x-foo") == [
            "en-us-x-foo",
            "en-us",
            "en",
        ]

    def test_pure_privateuse_tag(self):
        assert truncation_chain("x-foo-bar") == ["x-foo-bar", "x-foo"]


class TestNegotiate:
    SUPPORTED = ["en-US", "zh-CN", "zh-TW", "ja-JP"]
    DEFAULT = "en-US"

    def test_weights_and_truncation(self):
        result = negotiate(
            "zh-TW;q=0.95,zh;q=0.9,en;q=0.8,*;q=0.5",
            self.SUPPORTED,
            self.DEFAULT,
        )
        assert result["selected_language"] == "zh-TW"
        assert result["fallback"] is False
        # zh-CN truncates to zh -> matched by 'zh' range at q 0.9.
        zh_cn = next(c for c in result["candidates"] if c["tag"] == "zh-CN")
        assert zh_cn["matched_range"] == "zh"
        assert zh_cn["q"] == 0.9
        # ja-JP only matches wildcard.
        ja = next(c for c in result["candidates"] if c["tag"] == "ja-JP")
        assert ja["matched_range"] == "*"
        assert ja["q"] == 0.5
        match_events = [e for e in ja["events"] if e["outcome"] == "match"]
        assert match_events[-1]["truncated_to"] == "*"

    def test_q_zero_excludes_tag(self):
        result = negotiate(
            "en;q=0,zh-CN;q=0.9", self.SUPPORTED, self.DEFAULT
        )
        en = result["candidates"][0]
        assert en["status"] == "excluded"
        assert en["q"] == 0
        assert result["selected_language"] == "zh-CN"

    def test_q_zero_blocks_wildcard_excluded_candidates(self):
        # All explicit ranges q=0 -> default fallback even with wildcard q=1
        # concrete en q=0 excludes en; ja has wildcard q=1 though...
        result = negotiate(
            "en;q=0,ja;q=0,*;q=0.1", self.SUPPORTED, self.DEFAULT
        )
        assert result["selected_language"] == "zh-CN"
        assert result["fallback"] is False

    def test_all_excluded_falls_back_to_default(self):
        result = negotiate("*;q=0", self.SUPPORTED, self.DEFAULT)
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is True
        assert result["fallback_reason"] == "no-acceptable-candidate"
        assert "q=0" in result["message"]

    def test_missing_header_falls_back(self):
        for header in (None, "", "   "):
            result = negotiate(header, self.SUPPORTED, self.DEFAULT)
            assert result["selected_language"] == "en-US"
            assert result["fallback_reason"] == "no-valid-range"
            assert all(c["status"] == "no-range" for c in result["candidates"])

    def test_tie_breaks_by_server_order(self):
        # en and zh both match * at q=0.1 -> earliest in supported list wins.
        result = negotiate("de;q=0.9,*;q=0.1", self.SUPPORTED, self.DEFAULT)
        assert result["selected_language"] == "en-US"

    def test_exact_match_beats_prefix_and_wildcard(self):
        supported = ["en", "en-US"]
        result = negotiate("en-US;q=0.1,en;q=0.9,*;q=0.99", supported, "en")
        # en-US exact q=0.1 vs en exact q=0.9: highest q wins -> en.
        assert result["selected_language"] == "en"
        en_us = result["candidates"][1]
        assert en_us["matched_range"] == "en-us"
        # wildcard event skipped because concrete match exists
        assert all(
            e["outcome"] != "match" or e["truncated_to"] != "*"
            for e in en_us["events"]
        )

    def test_duplicate_header_ranges_reported(self):
        result = negotiate(
            "en;q=0.8,en;q=0.2", self.SUPPORTED, self.DEFAULT
        )
        assert len(result["parsed_ranges"]) == 1
        assert result["ignored_duplicates"] == [
            {"range": "en", "position": 2}
        ]
        assert result["candidates"][0]["q"] == 0.8

    def test_wildcard_only_selects_first_supported(self):
        result = negotiate("*", self.SUPPORTED, self.DEFAULT)
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is False


class TestFallbackValidation:
    def test_normalize_lowercases_and_accepts_valid(self):
        assert normalize_fallbacks({"DE": "EN-gb", "en-gb": "en"}) == {
            "de": "en-gb",
            "en-gb": "en",
        }

    @pytest.mark.parametrize(
        "raw",
        [
            [("de", "en")],  # not a mapping
            {"de": "en", "DE": "fr"},  # duplicate source case-insensitive
            {"12345": "en"},  # bad source tag
            {"de": "not a tag"},  # bad target tag
        ],
    )
    def test_normalize_rejects_bad_shape(self, raw):
        with pytest.raises(LocaleFallbackError):
            normalize_fallbacks(raw)

    def test_chain_reaching_supported_is_valid(self):
        validate_fallback_chains(
            {"de": "gsw", "gsw": "zh-cn"}, ["zh-CN", "en-US"]
        )

    def test_dangling_chain_rejected(self):
        with pytest.raises(LocaleFallbackError, match="悬空"):
            validate_fallback_chains({"de": "gsw"}, ["zh-CN"])

    def test_cycle_rejected(self):
        with pytest.raises(LocaleFallbackError, match="环路"):
            validate_fallback_chains(
                {"de": "fr", "fr": "it", "it": "fr"}, ["zh-CN"]
            )

    def test_self_loop_before_supported_rejected(self):
        with pytest.raises(LocaleFallbackError, match="环路"):
            validate_fallback_chains({"de": "de"}, ["zh-CN"])

    def test_self_loop_on_supported_tag_ok(self):
        validate_fallback_chains({"en": "en", "de": "en"}, ["en"])


class TestLocaleChainFallback:
    SUPPORTED = ["en-US", "zh-CN", "zh-TW", "ja-JP"]
    DEFAULT = "en-US"

    def test_chain_walked_from_positive_q_range(self):
        result = negotiate(
            "gsw;q=0.9",
            self.SUPPORTED,
            self.DEFAULT,
            {"gsw": "de", "de": "zh-CN"},
        )
        assert result["selected_language"] == "zh-CN"
        assert result["fallback"] is False
        report = result["locale_fallback"]
        assert report["triggered"] is True
        assert report["source_range"] == "gsw"
        assert report["source_q"] == 0.9
        assert report["source_position"] == 1
        assert report["path"] == ["gsw", "de", "zh-cn"]
        assert report["selected_language"] == "zh-CN"
        winner = next(
            c for c in result["candidates"] if c["tag"] == "zh-CN"
        )
        assert winner["status"] == "selected"

    def test_sources_ordered_by_descending_q(self):
        # Both ranges miss; de (higher q) is tried first even though fr
        # appears earlier in the header.
        result = negotiate(
            "fr;q=0.3,de;q=0.9",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "zh-TW", "fr": "ja-JP"},
        )
        assert result["selected_language"] == "zh-TW"
        assert result["locale_fallback"]["source_range"] == "de"

    def test_equal_q_breaks_by_header_order(self):
        result = negotiate(
            "fr;q=0.5,de;q=0.5",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "zh-TW", "fr": "ja-JP"},
        )
        assert result["selected_language"] == "ja-JP"
        assert result["locale_fallback"]["source_range"] == "fr"

    def test_q_zero_supported_node_skipped_walk_continues(self):
        result = negotiate(
            "de,zh-tw;q=0",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "zh-TW", "zh-tw": "zh-CN"},
        )
        assert result["selected_language"] == "zh-CN"
        assert result["locale_fallback"]["path"] == [
            "de",
            "zh-tw",
            "zh-cn",
        ]

    def test_q_zero_prefix_excludes_supported_node(self):
        # zh;q=0 excludes zh-CN via lookup prefix as well.
        result = negotiate(
            "de,zh;q=0",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "zh-CN", "zh-cn": "ja-JP"},
        )
        assert result["selected_language"] == "ja-JP"
        assert result["locale_fallback"]["path"] == [
            "de",
            "zh-cn",
            "ja-jp",
        ]

    def test_wildcard_q_zero_does_not_block_chain(self):
        # Only concrete q=0 ranges disqualify chain candidates.
        result = negotiate(
            "de,*;q=0",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "zh-CN"},
        )
        assert result["selected_language"] == "zh-CN"
        assert result["locale_fallback"]["path"] == ["de", "zh-cn"]

    def test_q_zero_range_never_starts_a_chain(self):
        result = negotiate(
            "de;q=0,fr;q=0.5",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "zh-CN", "fr": "ja-JP"},
        )
        assert result["selected_language"] == "ja-JP"
        assert result["locale_fallback"]["source_range"] == "fr"

    def test_dead_end_source_then_next_source_tried(self):
        result = negotiate(
            "de,ja-jp;q=0,fr",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "ja-JP", "ja-jp": "ko", "fr": "zh-CN"},
        )
        assert result["selected_language"] == "zh-CN"
        assert result["locale_fallback"]["source_range"] == "fr"

    def test_chains_not_consulted_when_standard_lookup_eligible(self):
        result = negotiate(
            "en;q=0.1,*;q=0.05",
            self.SUPPORTED,
            self.DEFAULT,
            {"zz": "zh-TW"},
        )
        assert result["selected_language"] == "en-US"
        assert result["locale_fallback"] is None

    def test_no_reachable_chain_falls_back_to_default(self):
        result = negotiate(
            "de;q=0",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "zh-CN"},
        )
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is True
        assert result["fallback_reason"] == "no-acceptable-candidate"
        assert result["locale_fallback"] is None
        assert "locale_fallbacks" in result["message"]

    def test_without_fallbacks_key_behavior_unchanged(self):
        result = negotiate("de", self.SUPPORTED, self.DEFAULT)
        assert "locale_fallback" in result
        assert result["locale_fallback"] is None


class TestGroupNegotiation:
    SUPPORTED = ["en-US", "zh-CN", "zh-TW", "ja-JP"]
    DEFAULT = "en-US"

    def _group(self, *pairs):
        return negotiate_group(
            [GroupParticipantInput(id=pid, accept_language=header) for pid, header in pairs],
            self.SUPPORTED,
            self.DEFAULT,
        )

    def _candidate(self, result, tag):
        return next(c for c in result["candidates"] if c["tag"] == tag)

    def test_basic_selection_by_min_and_total_q(self):
        result = self._group(
            ("a", "zh-CN;q=0.8,en;q=0.5"),
            ("b", "zh;q=0.9,en;q=0.4,*;q=0.1"),
        )
        assert result["selected_language"] == "zh-CN"
        assert result["selected_supported_index"] == 1
        assert result["fallback"] is False
        zh_cn = self._candidate(result, "zh-CN")
        assert zh_cn["status"] == "selected"
        assert zh_cn["min_q"] == 0.8
        assert zh_cn["total_q"] == 1.7
        en_us = self._candidate(result, "en-US")
        assert en_us["status"] == "eligible"
        assert en_us["min_q"] == 0.4
        assert en_us["total_q"] == 0.9

    def test_min_q_outranks_total_q(self):
        # en-US: (0.5, 0.9) -> min 0.5; zh-CN: (0.9, 0.4) -> min 0.4,
        # even though zh-CN has the larger sum (1.3).
        result = self._group(
            ("a", "zh-CN;q=0.9,en;q=0.5"),
            ("b", "zh-CN;q=0.4,en;q=0.9"),
        )
        assert result["selected_language"] == "en-US"

    def test_full_tie_breaks_by_supported_order(self):
        # Both zh-CN and zh-TW match prefix zh at q=0.5 for everyone.
        result = self._group(
            ("a", "zh;q=0.5"),
            ("b", "zh;q=0.5,en;q=0.1"),
        )
        assert result["selected_language"] == "zh-CN"
        assert self._candidate(result, "zh-CN")["status"] == "selected"
        assert self._candidate(result, "zh-TW")["status"] == "eligible"

    def test_concrete_q_zero_is_global_veto(self):
        result = self._group(
            ("a", "zh-CN;q=0.9"),
            ("b", "zh;q=0,en;q=0.5"),
        )
        assert result["fallback"] is True
        assert result["fallback_reason"] == "no-common-acceptable-candidate"
        assert result["selected_language"] == "en-US"
        zh_cn = self._candidate(result, "zh-CN")
        assert zh_cn["status"] == "vetoed"
        assert zh_cn["min_q"] is None
        assert zh_cn["total_q"] is None
        assert zh_cn["vetoed_by"] == [
            {
                "participant_id": "b",
                "matched_range": "zh",
                "q": 0,
                "header_position": 1,
            }
        ]
        # Prefix q=0 also vetoes zh-TW even though a never asked for it.
        assert self._candidate(result, "zh-TW")["status"] == "vetoed"

    def test_wildcard_q_zero_is_not_a_veto(self):
        result = self._group(
            ("a", "zh-CN;q=0.9"),
            ("b", "*;q=0"),
        )
        zh_cn = self._candidate(result, "zh-CN")
        assert zh_cn["status"] == "not-common"
        assert zh_cn["vetoed_by"] == []
        rejection = zh_cn["rejected_by"][0]
        assert rejection["participant_id"] == "b"
        assert rejection["status"] == "excluded"
        assert rejection["matched_range"] == "*"
        assert result["fallback"] is True

    def test_unmatched_participant_makes_candidate_not_common(self):
        result = self._group(
            ("a", "zh-CN;q=0.9"),
            ("b", "en;q=0.8"),
        )
        zh_cn = self._candidate(result, "zh-CN")
        assert zh_cn["status"] == "not-common"
        assert zh_cn["rejected_by"][0] == {
            "participant_id": "b",
            "matched_range": None,
            "q": None,
            "status": "unmatched",
        }
        assert result["fallback"] is True

    def test_missing_or_empty_header_imposes_no_preference(self):
        result = self._group(
            ("a", "zh-CN;q=0.9"),
            ("b", None),
            ("c", "   "),
        )
        assert result["fallback"] is False
        assert result["selected_language"] == "zh-CN"
        cells = self._candidate(result, "zh-CN")["cells"]
        assert [cell["status"] for cell in cells] == [
            "eligible",
            "no-range",
            "no-range",
        ]
        assert cells[1]["matched_range"] is None
        assert cells[1]["q"] is None

    def test_single_participant_matches_single_negotiation(self):
        result = self._group(("a", "zh-TW;q=0.95,zh;q=0.9,en;q=0.8,*;q=0.5"))
        assert result["selected_language"] == "zh-TW"
        zh_cn = self._candidate(result, "zh-CN")
        assert zh_cn["status"] == "eligible"
        assert zh_cn["cells"][0]["matched_range"] == "zh"
        assert zh_cn["cells"][0]["q"] == 0.9
        assert zh_cn["cells"][0]["matched_via"] == "concrete"
        ja = self._candidate(result, "ja-JP")
        assert ja["cells"][0]["matched_range"] == "*"
        assert ja["cells"][0]["matched_via"] == "wildcard"

    def test_participant_reports_carry_actual_hits(self):
        result = self._group(
            ("a", "zh-CN;q=0.8,en;q=0.5"),
            ("b", "zh;q=0.9"),
        )
        reports = {report["id"]: report for report in result["participants"]}
        assert reports["a"]["accept_language"] == "zh-CN;q=0.8,en;q=0.5"
        assert [(r["range"], r["q"]) for r in reports["b"]["parsed_ranges"]] == [
            ("zh", 0.9)
        ]
        # Participant b's zh-CN cell (supported_index 1) hits prefix zh.
        b_zh = next(
            cell
            for cell in reports["b"]["cells"]
            if cell["supported_index"] == 1
        )
        assert b_zh["matched_range"] == "zh"
        assert b_zh["q"] == 0.9
        # Participant b has no wildcard, so ja-JP is unmatched.
        b_ja = next(
            cell
            for cell in reports["b"]["cells"]
            if cell["supported_index"] == 3
        )
        assert b_ja["status"] == "unmatched"

    def test_ignored_duplicate_ranges_reported_per_participant(self):
        result = self._group(("a", "en;q=0.8,en;q=0.2"), ("b", "en"))
        reports = {report["id"]: report for report in result["participants"]}
        assert reports["a"]["ignored_duplicates"] == [
            {"range": "en", "position": 2}
        ]
        assert reports["b"]["ignored_duplicates"] == []
        en_us = self._candidate(result, "en-US")
        # First occurrence (q=0.8) decides the weight.
        assert en_us["cells"][0]["q"] == 0.8

    def test_all_participants_no_ranges_selects_first_supported(self):
        result = self._group(("a", None), ("b", ""))
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is False
        for candidate in result["candidates"]:
            assert candidate["status"] in ("selected", "eligible")
            assert candidate["min_q"] == 1.0
            assert candidate["total_q"] == 1.0

    def test_fallback_message_mentions_veto_when_default_vetoed(self):
        result = self._group(
            ("a", "zh-CN"),
            ("b", "en;q=0"),
        )
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is True
        assert "否决" in result["message"]

    def test_malformed_header_raises_same_error_as_single_endpoint(self):
        with pytest.raises(AcceptLanguageError):
            self._group(("a", "en;q=2"), ("b", "en"))

    def test_wildcard_makes_wildcard_only_candidates_acceptable(self):
        # b wildcard accepts ja-JP, a does not match it -> not common;
        # en-US is acceptable to both via prefix/wildcard -> selected.
        result = self._group(
            ("a", "en;q=0.7"),
            ("b", "*;q=0.3"),
        )
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is False
        ja = self._candidate(result, "ja-JP")
        assert ja["status"] == "not-common"
