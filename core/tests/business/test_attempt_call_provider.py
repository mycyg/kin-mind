"""Each call in the attempt ledger says whether it went straight to DeepSeek or through the native session's
review, so the usage ledger counts the direct ones once: the native ones are the gateway's rows already."""
from kin_mind import attempts
from kin_mind.appraisal import DeepSeek, NativeReview


def test_a_call_names_the_provider_that_made_it():
    calls = []
    for provider in (object.__new__(DeepSeek), object.__new__(NativeReview)):
        provider.attempt_calls = calls
        attempts.record_call(provider, "submit_appraisal", outcome="ok", model="synthetic",
                             usage={"input_tokens": 10, "output_tokens": 2})
    assert [call["provider"] for call in calls] == ["deepseek", "native"]
    assert "provider" in attempts.CALL_FIELDS
