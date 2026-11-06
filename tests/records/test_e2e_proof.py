"""REC-AUTO-11: the e2e-proof replay runs every stage with no manual step and sends nothing."""
import json

import pytest

from campaign_tool.records import e2e_proof
from tests.records.test_run_slice import synthetic_email


@pytest.fixture()
def campaign(tmp_path):
    content = tmp_path / "campaign" / "content"
    content.mkdir(parents=True)
    (content / "agencies.json").write_text(json.dumps(
        [{"agency_id": "a-1", "name": "Agency", "profile": {"status": "Records requested", "summary": "s"}}]))
    return tmp_path / "campaign"


def test_replay_passes_every_stage_twice_without_manual_steps(tmp_path, campaign):
    message = tmp_path / "m.eml"
    message.write_bytes(synthetic_email())
    first = e2e_proof.replay(message, tmp_path / "out", campaign, label="one")
    second = e2e_proof.replay(message, tmp_path / "out", campaign, label="two")
    for report in (first, second):
        assert report["result"] == "PASS" and report["manual_steps"] == []
        assert report["sent"] is False and report["published"] is False
        assert [s["stage"] for s in report["stages"]] == [
            "arrival_trigger", "save_mail", "extract", "digest_gates", "outputs", "next_steps",
            "telegram_text", "console"]
    assert (tmp_path / "out" / "one" / "telegram-summary.txt").is_file()
    assert (tmp_path / "out" / "one" / "proof-report.md").is_file()


def test_replay_refuses_existing_output_dir(tmp_path, campaign):
    message = tmp_path / "m.eml"
    message.write_bytes(synthetic_email())
    e2e_proof.replay(message, tmp_path / "out", campaign, label="one")
    with pytest.raises(e2e_proof.ProofError) as caught:
        e2e_proof.replay(message, tmp_path / "out", campaign, label="one")
    assert caught.value.code == "OUT_EXISTS"


def test_message_required(tmp_path):
    with pytest.raises(e2e_proof.ProofError) as caught:
        e2e_proof.pick_message(None, None, 1)
    assert caught.value.code == "MESSAGE_REQUIRED"
