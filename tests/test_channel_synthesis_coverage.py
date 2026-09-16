"""Contribution and semantic-source coverage gate digest matching."""

import hashlib
import json

from zf.runtime.channel_semantic_sources import (
    validate_semantic_source_coverage,
)
from zf.runtime.channel_synthesis_coverage import contract_coverage_error

_REF_A = "channels/ch/contracts/contribution/reply-a.json"
_REF_B = "channels/ch/contracts/contribution/reply-b.json"

_DIGEST_A = "0a95d28076e8e4840354f21880d5387334eae8592c7915631c8c76ef1bbac700"
# Same 16-char prefix as _DIGEST_A but divergent tail: LLM hand-transcription.
_DIGEST_A_TYPO = "0a95d28076e8e4840354f21880d5387334eae8592c7915631c8c76ef1bbac70f"
_DIGEST_B = "d1c08a5ea3b97b1b4c6eef9654dec64fed83b6144be7156e3b08970bda7190db"


def test_exact_match_passes() -> None:
    assert contract_coverage_error(
        required_refs=[_REF_A], required_digests=[_DIGEST_A],
        consumed_refs=[_REF_A], consumed_digests=[_DIGEST_A],
    ) == ""


def test_digest_prefix_match_tolerates_transcription_tail_errors() -> None:
    # Real failure ch-auto-ade5849896: consumed digest had two wrong chars
    # past the 16th hex position and the gate rejected the whole synthesis.
    assert contract_coverage_error(
        required_refs=[_REF_A], required_digests=[_DIGEST_A],
        consumed_refs=[_REF_A], consumed_digests=[_DIGEST_A_TYPO],
    ) == ""


def test_digest_prefix_mismatch_still_missing() -> None:
    error = contract_coverage_error(
        required_refs=[_REF_A], required_digests=[_DIGEST_A],
        consumed_refs=[_REF_A], consumed_digests=[_DIGEST_B],
    )
    assert error == "contribution_coverage_missing_digests:0a95d28076e8e484"


def test_unknown_digest_detected_by_prefix() -> None:
    error = contract_coverage_error(
        required_refs=[_REF_A], required_digests=[_DIGEST_A],
        consumed_refs=[_REF_A],
        consumed_digests=[_DIGEST_A, _DIGEST_B],
    )
    assert error == "contribution_coverage_unknown_digests:d1c08a5ea3b97b1b"


def test_refs_still_require_exact_match() -> None:
    error = contract_coverage_error(
        required_refs=[_REF_A], required_digests=[],
        consumed_refs=[_REF_B], consumed_digests=[],
    )
    assert "contribution_coverage_missing_refs:" in error


_MSG_DIGEST = "41894766e30bb8de532f3d4664e8d5af317b8471c25e3fb3e82c8a2c50f1a3e6"
_MSG_DIGEST_TYPO = "41894766e30bb8de5323d4664e8d5af317b8471c25e3fb3e82c8a2c50f1a3e6"
_MSG_DIGEST_OTHER = "ffffffffe30bb8de532f3d4664e8d5af317b8471c25e3fb3e82c8a2c50f1a3e6"


def _semantic_pack_channel(*, required: list[str]) -> dict:
    manifest = [
        {
            "round": "contribution:1",
            "message_id": "msg-1",
            "message_body_digest": digest,
        }
        for digest in required
    ]
    manifest_digest = hashlib.sha256(
        json.dumps(
            manifest,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "context_packs": {
            "pack-1": {
                "context_pack_id": "pack-1",
                "semantic_source_required": True,
                "semantic_source_complete": True,
                "semantic_source_manifest_digest": manifest_digest,
                "semantic_source_required_digests": required,
                "semantic_source_manifest": manifest,
            },
        },
    }


def _semantic_error(declared: list[str], *, required: list[str]) -> str:
    _, error = validate_semantic_source_coverage(
        _semantic_pack_channel(required=required),
        {"context_pack_id": "pack-1"},
        declared,
    )
    return error


def test_semantic_source_prefix_tolerates_transcription_tail_errors() -> None:
    # Real failure ch-auto-4f1edcc4eb: one char dropped at hex position 20.
    assert _semantic_error(
        [_MSG_DIGEST_TYPO], required=[_MSG_DIGEST]
    ) == ""


def test_semantic_source_prefix_mismatch_still_missing() -> None:
    assert _semantic_error(
        [_MSG_DIGEST_OTHER], required=[_MSG_DIGEST]
    ) == "semantic_source_coverage_missing:41894766e30bb8de"


def test_semantic_source_unknown_detected_by_prefix() -> None:
    assert _semantic_error(
        [_MSG_DIGEST, _MSG_DIGEST_OTHER], required=[_MSG_DIGEST]
    ) == "semantic_source_coverage_unknown:ffffffffe30bb8de"
