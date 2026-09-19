"""API-level tests via FastAPI TestClient."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SUPPORTED = ["en-US", "zh-CN", "zh-TW", "ja-JP"]
DEFAULT = "en-US"


def _post(payload):
    return client.post("/api/v1/negotiate", json=payload)


def _post_group(payload):
    return client.post("/api/v1/negotiate/group", json=payload)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "language-negotiator",
    }


def test_negotiate_success():
    response = _post(
        {
            "accept_language": "zh-CN,zh;q=0.9,en;q=0.8,*;q=0.5",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["selected_language"] == "zh-CN"
    assert body["selected_supported_index"] == 1
    assert body["fallback"] is False
    assert len(body["candidates"]) == 4
    zh_cn = body["candidates"][1]
    assert zh_cn["status"] == "selected"
    # Truncation trace: full tag matches directly.
    assert zh_cn["events"][0] == {
        "truncated_to": "zh-cn",
        "outcome": "match",
        "matched_range": "zh-cn",
        "q": 1.0,
        "header_position": 1,
        "detail": None,
    }


def test_missing_header_default_fallback():
    response = _post(
        {
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["selected_language"] == "en-US"
    assert body["fallback"] is True
    assert body["fallback_reason"] == "no-valid-range"
    assert body["parsed_ranges"] == []


def test_invalid_accept_language_returns_422():
    response = _post(
        {
            "accept_language": "en;q=2",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
        }
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "accept_language"]
    assert detail[0]["type"] == "accept_language.syntax_error"


def test_default_not_in_supported_returns_422():
    response = _post(
        {
            "accept_language": "en",
            "supported_languages": SUPPORTED,
            "default_language": "fr-FR",
        }
    )
    assert response.status_code == 422


def test_duplicate_supported_tags_returns_422():
    response = _post(
        {
            "supported_languages": ["en", "EN"],
            "default_language": "en",
        }
    )
    assert response.status_code == 422


def test_empty_supported_returns_422():
    response = _post(
        {"supported_languages": [], "default_language": "en"}
    )
    assert response.status_code == 422


def test_extra_field_rejected_422():
    response = _post(
        {
            "supported_languages": ["en"],
            "default_language": "en",
            "unexpected": 1,
        }
    )
    assert response.status_code == 422


def test_q_zero_trace_and_fallback():
    response = _post(
        {
            "accept_language": "*;q=0",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["selected_language"] == "en-US"
    assert body["fallback"] is True
    assert all(c["status"] == "excluded" for c in body["candidates"])
    assert "q=0" in body["message"]


def test_wildcard_and_duplicates():
    response = _post(
        {
            "accept_language": "de;q=0.9,en;q=0.1,en;q=0.5,*;q=0.05",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
        }
    )
    body = response.json()
    # en wins among wildcard candidates; duplicate second en ignored.
    assert body["selected_language"] == "en-US"
    assert body["ignored_duplicates"] == [
        {"range": "en", "position": 3}
    ]


def test_locale_fallback_chain_success_omission_rule():
    response = _post(
        {
            "accept_language": "gsw;q=0.9,fr;q=0.5",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
            "locale_fallbacks": {
                "gsw": "de",
                "de": "zh-TW",
                "fr": "ja-JP",
            },
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["selected_language"] == "zh-TW"
    assert body["fallback"] is False
    assert body["locale_fallback"] == {
        "triggered": True,
        "source_range": "gsw",
        "source_q": 0.9,
        "source_position": 1,
        "path": ["gsw", "de", "zh-tw"],
        "selected_language": "zh-TW",
    }


def test_locale_fallback_key_absent_in_legacy_responses():
    # Field not provided -> the response shape must stay exactly legacy.
    response = _post(
        {
            "accept_language": "de,*;q=0.05",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
        }
    )
    assert response.status_code == 200
    assert "locale_fallback" not in response.json()

    # Field provided but chain not triggered -> still omitted.
    response = _post(
        {
            "accept_language": "de;q=0",
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
            "locale_fallbacks": {"de": "zh-CN"},
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["fallback"] is True
    assert "locale_fallback" not in body


def test_locale_fallback_q_zero_concrete_blocks_selection():
    # zh-TW is reached but excluded by a concrete q=0 prefix range.
    response = _post(
        {
            "accept_language": "de,zh;q=0",
            "supported_languages": ["zh-TW", "ja-JP"],
            "default_language": "ja-JP",
            "locale_fallbacks": {"de": "zh-TW", "zh-TW": "ja-JP"},
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["selected_language"] == "ja-JP"
    assert body["locale_fallback"]["path"] == ["de", "zh-tw", "ja-jp"]


def test_locale_fallback_duplicate_sources_422():
    response = _post(
        {
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
            "locale_fallbacks": {"DE": "zh-CN", "de": "zh-TW"},
        }
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "locale_fallbacks"]
    assert "重复" in detail[0]["msg"]


def test_locale_fallback_illegal_tag_422():
    response = _post(
        {
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
            "locale_fallbacks": {"de": "not a tag"},
        }
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "locale_fallbacks"]


def test_locale_fallback_cycle_422():
    response = _post(
        {
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
            "locale_fallbacks": {"de": "fr", "fr": "de"},
        }
    )
    assert response.status_code == 422
    assert "环路" in response.json()["detail"][0]["msg"]


def test_locale_fallback_dangling_chain_422():
    response = _post(
        {
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
            "locale_fallbacks": {"de": "xx", "xx": "yy"},
        }
    )
    assert response.status_code == 422
    assert "悬空" in response.json()["detail"][0]["msg"]


def test_locale_fallback_must_be_object_422():
    response = _post(
        {
            "supported_languages": SUPPORTED,
            "default_language": DEFAULT,
            "locale_fallbacks": [["de", "fr"]],
        }
    )
    assert response.status_code == 422


class TestGroupNegotiationAPI:
    def test_group_success_weights_and_order(self):
        response = _post_group(
            {
                "participants": [
                    {"id": "a", "accept_language": "zh-CN;q=0.8,en;q=0.5"},
                    {
                        "id": "b",
                        "accept_language": "zh;q=0.9,en;q=0.4,*;q=0.1",
                    },
                ],
                "supported_languages": SUPPORTED,
                "default_language": DEFAULT,
            }
        )
        assert response.status_code == 200
        body = response.json()
        assert body["selected_language"] == "zh-CN"
        assert body["selected_supported_index"] == 1
        assert body["fallback"] is False
        assert body["fallback_reason"] is None
        assert len(body["candidates"]) == 4
        zh_cn = body["candidates"][1]
        assert zh_cn["status"] == "selected"
        assert zh_cn["min_q"] == 0.8
        assert zh_cn["total_q"] == 1.7
        assert zh_cn["vetoed_by"] == []
        assert zh_cn["rejected_by"] == []
        # Cells align with participant order.
        assert [cell["matched_range"] for cell in zh_cn["cells"]] == [
            "zh-cn",
            "zh",
        ]
        assert [cell["matched_via"] for cell in zh_cn["cells"]] == [
            "concrete",
            "concrete",
        ]

    def test_group_min_q_beats_total_q(self):
        response = _post_group(
            {
                "participants": [
                    {"id": "a", "accept_language": "zh-CN;q=0.9,en;q=0.5"},
                    {"id": "b", "accept_language": "zh-CN;q=0.4,en;q=0.9"},
                ],
                "supported_languages": SUPPORTED,
                "default_language": DEFAULT,
            }
        )
        assert response.json()["selected_language"] == "en-US"

    def test_group_full_tie_breaks_by_supported_order(self):
        response = _post_group(
            {
                "participants": [
                    {"id": "a", "accept_language": "zh;q=0.5"},
                    {"id": "b", "accept_language": "zh;q=0.5,en;q=0.1"},
                ],
                "supported_languages": SUPPORTED,
                "default_language": DEFAULT,
            }
        )
        assert response.json()["selected_language"] == "zh-CN"

    def test_group_concrete_q_zero_global_veto_and_default_fallback(self):
        response = _post_group(
            {
                "participants": [
                    {"id": "a", "accept_language": "zh-CN;q=0.9"},
                    {"id": "b", "accept_language": "zh;q=0,en;q=0.5"},
                ],
                "supported_languages": SUPPORTED,
                "default_language": DEFAULT,
            }
        )
        assert response.status_code == 200
        body = response.json()
        assert body["selected_language"] == "en-US"
        assert body["fallback"] is True
        assert body["fallback_reason"] == "no-common-acceptable-candidate"
        zh_cn = body["candidates"][1]
        assert zh_cn["status"] == "vetoed"
        assert zh_cn["min_q"] is None
        assert zh_cn["total_q"] is None
        assert zh_cn["vetoed_by"] == [
            {
                "participant_id": "b",
                "matched_range": "zh",
                "q": 0.0,
                "header_position": 1,
            }
        ]

    def test_group_wildcard_q_zero_is_rejection_not_veto(self):
        response = _post_group(
            {
                "participants": [
                    {"id": "a", "accept_language": "zh-CN;q=0.9"},
                    {"id": "b", "accept_language": "*;q=0"},
                ],
                "supported_languages": SUPPORTED,
                "default_language": DEFAULT,
            }
        )
        body = response.json()
        zh_cn = body["candidates"][1]
        assert zh_cn["status"] == "not-common"
        assert zh_cn["vetoed_by"] == []
        assert zh_cn["rejected_by"][0]["participant_id"] == "b"
        assert zh_cn["rejected_by"][0]["status"] == "excluded"
        assert body["fallback"] is True

    def test_group_null_or_missing_accept_language_is_no_preference(self):
        response = _post_group(
            {
                "participants": [
                    {"id": "a", "accept_language": "zh-CN;q=0.9"},
                    {"id": "b", "accept_language": None},
                    {"id": "c"},
                ],
                "supported_languages": SUPPORTED,
                "default_language": DEFAULT,
            }
        )
        body = response.json()
        assert body["selected_language"] == "zh-CN"
        assert body["fallback"] is False
        cells = body["candidates"][1]["cells"]
        assert [cell["status"] for cell in cells] == [
            "eligible",
            "no-range",
            "no-range",
        ]
        # Participant report echoes the raw header and shows actual hits.
        b_report = body["participants"][1]
        assert b_report["accept_language"] is None
        assert b_report["parsed_ranges"] == []
        assert b_report["cells"][1]["status"] == "no-range"

    def test_group_participant_cells_show_actual_matched_ranges(self):
        response = _post_group(
            {
                "participants": [
                    {"id": "a", "accept_language": "zh-Hant;q=0.7,*;q=0.2"},
                    {"id": "b", "accept_language": "zh-TW;q=0.6"},
                ],
                "supported_languages": SUPPORTED,
                "default_language": DEFAULT,
            }
        )
        body = response.json()
        # zh-TW truncates zh-tw -> zh; a matches via concrete prefix?
        # a has zh-hant only, so zh-tw misses concrete and uses wildcard.
        zh_tw = body["candidates"][2]
        assert [cell["matched_range"] for cell in zh_tw["cells"]] == [
            "*",
            "zh-tw",
        ]
        assert [cell["matched_via"] for cell in zh_tw["cells"]] == [
            "wildcard",
            "concrete",
        ]

    def test_group_ignored_duplicates_per_participant(self):
        response = _post_group(
            {
                "participants": [
                    {"id": "a", "accept_language": "en;q=0.8,EN;q=0.2"},
                    {"id": "b", "accept_language": "en"},
                ],
                "supported_languages": SUPPORTED,
                "default_language": DEFAULT,
            }
        )
        body = response.json()
        assert body["participants"][0]["ignored_duplicates"] == [
            {"range": "en", "position": 2}
        ]
        assert body["participants"][1]["ignored_duplicates"] == []
        # First occurrence keeps q=0.8.
        assert body["candidates"][0]["cells"][0]["q"] == 0.8

    def test_group_empty_participants_422(self):
        response = _post_group(
            {
                "participants": [],
                "supported_languages": SUPPORTED,
                "default_language": DEFAULT,
            }
        )
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "participants"]

    def test_group_missing_participants_422(self):
        response = _post_group(
            {"supported_languages": SUPPORTED, "default_language": DEFAULT}
        )
        assert response.status_code == 422

    def test_group_duplicate_ids_422(self):
        response = _post_group(
            {
                "participants": [
                    {"id": "u-1", "accept_language": "en"},
                    {"id": "u-1", "accept_language": "zh"},
                ],
                "supported_languages": SUPPORTED,
                "default_language": DEFAULT,
            }
        )
        assert response.status_code == 422
        detail = response.json()["detail"][0]
        assert detail["loc"] == ["body", "participants"]
        assert "u-1" in detail["msg"]

    def test_group_empty_id_422(self):
        response = _post_group(
            {
                "participants": [{"id": "", "accept_language": "en"}],
                "supported_languages": SUPPORTED,
                "default_language": DEFAULT,
            }
        )
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"][-1] == "id"

    def test_group_malformed_accept_language_422(self):
        response = _post_group(
            {
                "participants": [
                    {"id": "a", "accept_language": "en"},
                    {"id": "b", "accept_language": "zh;q=2"},
                ],
                "supported_languages": SUPPORTED,
                "default_language": DEFAULT,
            }
        )
        assert response.status_code == 422
        detail = response.json()["detail"][0]
        assert detail["loc"] == ["body", "participants", 1, "accept_language"]
        assert detail["type"] == "accept_language.syntax_error"

    def test_group_shared_request_validation_with_single_endpoint(self):
        # default_language not in supported_languages
        response = _post_group(
            {
                "participants": [{"id": "a", "accept_language": "en"}],
                "supported_languages": SUPPORTED,
                "default_language": "fr-FR",
            }
        )
        assert response.status_code == 422

        # duplicate supported tags
        response = _post_group(
            {
                "participants": [{"id": "a"}],
                "supported_languages": ["en", "EN"],
                "default_language": "en",
            }
        )
        assert response.status_code == 422

        # extra field forbidden
        response = _post_group(
            {
                "participants": [{"id": "a", "unexpected": 1}],
                "supported_languages": ["en"],
                "default_language": "en",
            }
        )
        assert response.status_code == 422
