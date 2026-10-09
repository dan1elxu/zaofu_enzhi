"""Only an absent outer envelope brace may be recovered mechanically."""
import json
from types import SimpleNamespace

import pytest

from zf.runtime.channel_reply_parsing import structured_reply_payload_with_error
from zf.runtime.channel_provider_completion import _continuation_reason


def test_complete_value_without_outer_brace_does_not_regenerate_document():
    body = {"prd_markdown": "# Full source\n" + "{literal field}\n" * 3000,
            "nested": {"value": "quoted \"text\""}, "next_round": {"action": "finalize"}}
    reply = "Visible summary\n<!-- zf:channel-contract -->\n" + json.dumps({"channel_synthesis": body})[:-1]
    parsed, error = structured_reply_payload_with_error(reply, "channel_synthesis")
    assert not error and parsed == body
    assert _continuation_reason(SimpleNamespace(ok=True, reply=reply), expected_contract_key="channel_synthesis") == (False, "")


@pytest.mark.parametrize("reply", [
    '{"channel_synthesis":{"prd_markdown":"unfinished',
    '{"channel_synthesis":{"field":',
    '{"channel_synthesis":{"field":"value",',
    '{"channel_synthesis":{"nested":{"field":"value"}',
    '{"channel_synthesis":{"field":"value"} trailing material',
    '{"channel_synthesis":{"field":"value"},"another":',
    '{"channel_synthesis":[{"field":"value"}]',
])
def test_inner_truncation_or_trailing_material_still_requires_repair(reply):
    body, error = structured_reply_payload_with_error(reply, "channel_synthesis")
    assert not body and error
