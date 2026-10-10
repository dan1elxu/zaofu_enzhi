import copy
import ast
from zf.runtime.channel_provider_prompt import _intern_cross_review_coverage as intern, build_channel_provider_prompt

def test_intern_roundtrips_without_mutating_source_or_dropping_semantics():
    coverage=[{"message_id":"m", "message_body_digest":"a"*64,"message_body_ref":"b", "kind":"requirement"}]*20
    source={"cross_review_index":[{"request_id":"one","source_coverage":coverage,"answer":"Keep every answer"},{"request_id":"two","source_coverage":coverage,"answer":"And all dissent","dissent":"Remain independent"}], "semantic_source_documents":[{"content":"Full original source"}]}
    original=copy.deepcopy(source);result=intern(source)
    assert source==original
    for row in result["cross_review_index"]:
        ref=row.pop("source_coverage_manifest_ref");row["source_coverage"]=result["cross_review_source_coverage_manifests"][ref]
    assert result["cross_review_index"]==original["cross_review_index"]
    assert result["semantic_source_documents"]==original["semantic_source_documents"]

def test_unique_or_missing_coverage_keeps_original_view():
    for source in [{}, {"cross_review_index":[]}, {"cross_review_index":[{"source_coverage":[{"message_id":"m"}]}]}, {"cross_review_index":[{"source_coverage":[1]},{"source_coverage":[2]}]}]:
        assert intern(source) is source

def test_mixed_rows_keep_each_independent_manifest():
    source={"cross_review_index":[{"source_coverage":[1]},{"source_coverage":[1]},{"source_coverage":[2]},None]}
    result=intern(source)
    assert result["cross_review_index"][2:]==source["cross_review_index"][2:]
    assert len(result["cross_review_source_coverage_manifests"])==1


def test_provider_prompt_uses_interned_view_without_changing_bound_pack(monkeypatch):
    import zf.runtime.channel_provider_prompt as prompts
    coverage=[{"message_id":"requirement", "message_body_ref":"source.json", "message_body_digest":"abc"}]
    pack={"cross_review_index":[{"source_coverage":coverage},{"source_coverage":coverage}], "semantic_source_documents":[{"content":"Every original field and rule"}], "cross_review_index_digest":"bound-original-index"}
    original=copy.deepcopy(pack)
    monkeypatch.setattr(prompts,"channel_context_pack_by_id",lambda *a,**k: pack)
    prompt=build_channel_provider_prompt(channel={"channel_id":"ch"},member={"member_id":"synthesizer"},message={"refs":{"synthesis_request_id":"s1"}},request={})
    projected=ast.literal_eval(next(line.removeprefix("context_pack: ") for line in prompt.splitlines() if line.startswith("context_pack: ")))
    assert projected["semantic_source_documents"]==original["semantic_source_documents"]
    assert len(projected["cross_review_source_coverage_manifests"])==1
    assert projected["cross_review_index_digest"]=="bound-original-index"
    assert pack==original
