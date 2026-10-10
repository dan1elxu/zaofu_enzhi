"""History search keeps evidence discoverable without rendering provider protocols."""

import json

from zf.core.events import EventLog, ZfEvent
from zf.runtime.channel_projection import search_channel_history


def test_history_hides_contract_but_preserves_evidence_hit_and_user_json(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    contract = {"channel_cross_review": {"answer": "RTK 30 天", "outcome": "resolved"}}
    machine = "<!-- zf:channel-contract -->\n" + json.dumps(contract, ensure_ascii=False)
    texts = {
        "provider": "已完成来源审查。\n\n" + machine,
        "user": "我想查看协议示例：\n" + machine,
        "plain-json": '{"business_field": "RTK 示例"}',
        "legacy": "已核证。\n```json\n" + json.dumps(contract, ensure_ascii=False) + "\n```",
    }
    for message_id, text in texts.items():
        log.append(ZfEvent(type="channel.message.posted", actor="test", payload={
            "channel_id": "ch-history", "thread_id": "origin", "message_id": message_id,
            "role": "user" if message_id == "user" else "assistant", "text": text,
            "refs": {"request_id": "a-request"},
        }))
    before = (tmp_path / "events.jsonl").read_bytes()
    result = search_channel_history(tmp_path, "ch-history", q="RTK", thread_id="origin")
    rows = {row["message_id"]: row for row in result["results"]}
    assert set(rows) == set(texts)
    for message_id in ("provider", "legacy"):
        excerpt = rows[message_id]["text_excerpt"]
        assert "结构化审查证据" in excerpt
        assert "channel_cross_review" not in excerpt
        assert "<!--" not in excerpt
        assert "已" in excerpt
    assert rows["user"]["text_excerpt"] == texts["user"]
    assert rows["plain-json"]["text_excerpt"] == texts["plain-json"]
    assert (tmp_path / "events.jsonl").read_bytes() == before
    prose = search_channel_history(tmp_path, "ch-history", q="来源审查")
    assert prose["results"][0]["text_excerpt"] == "已完成来源审查。"
