import json
import unittest

from memory_v2.conversation_source import ConversationMessage
from memory_v2.encoding import (
    EncodingContractError,
    build_encoding_messages,
    parse_encoding_output,
)
from memory_v2.models import BatchSourceRef, EncodingBatch
from memory_v2.replay import ReplayBatchPlan


def _fixture():
    messages = [
        ConversationMessage(
            row_id=1,
            active_date="2000-01-02",
            calendar_date="2000-01-02",
            session_id="main",
            timestamp="2000-01-02T10:00:00+08:00",
            role="assistant",
            content="我把明天的提醒设好了。",
            message_id="m1",
        )
    ]
    sources = tuple(
        BatchSourceRef(
            message_row_id=message.row_id,
            message_id=message.message_id,
            session_id=message.session_id,
            active_date=message.active_date,
            calendar_date=message.calendar_date,
            source_ts=message.timestamp,
            source_role=message.role,
            content_digest="d",
        )
        for message in messages
    )
    batch = EncodingBatch(
        source_namespace="test",
        session_id="main",
        active_date="2000-01-02",
        from_message_row_id=1,
        to_message_row_id=1,
        from_message_id="m1",
        to_message_id="m1",
        source_count=1,
        source_digest="digest",
        encoder_version="test",
        prompt_version="test",
        batch_id="batch-1",
    )
    plan = ReplayBatchPlan(
        batch=batch,
        batch_sources=sources,
        estimated_tokens=10,
        content_chars=10,
        boundary_reason="test",
        oversized_single_message=False,
    )
    return plan, messages


def _output(subject_id: str, epistemic_status: str) -> str:
    return json.dumps(
        {
            "episodes": [
                {
                    "source_refs": ["s1"],
                    "primary_subject_id": subject_id,
                    "participant_ids": [subject_id],
                    "event_type": "agent_action",
                    "aspects": [],
                    "summary": "Agent 设好了明天的提醒。",
                    "occurred_at": "",
                    "calendar_date": "",
                    "temporal_basis": "contemporaneous_report",
                    "time_precision": "reported_at",
                    "time_expression": "",
                    "importance": 0.4,
                    "emotional_weight": 0.0,
                    "confidence": 0.9,
                    "epistemic_status": epistemic_status,
                }
            ],
            "overflow": False,
            "overflow_source_refs": [],
            "non_event_source_refs": [],
        }
    )


class EncodingSubjectIdTests(unittest.TestCase):
    def test_prompt_names_only_canonical_agent_id(self):
        plan, messages = _fixture()
        user_prompt = build_encoding_messages(plan, messages)[1]["content"]
        self.assertIn("human for U/Human and agent for Agent", user_prompt)
        self.assertIn("primary_subject_id must be agent", user_prompt)
        self.assertNotIn("k for Agent", user_prompt)

    def test_agent_rows_require_direct_observation(self):
        plan, messages = _fixture()
        with self.assertRaises(EncodingContractError):
            parse_encoding_output(_output("agent", "explicit_report"), plan, messages)

    def test_unknown_primary_subject_cannot_bypass_agent_checks(self):
        plan, messages = _fixture()
        with self.assertRaises(EncodingContractError):
            parse_encoding_output(_output("k", "explicit_report"), plan, messages)

    def test_agent_event_with_direct_observation_is_accepted(self):
        plan, messages = _fixture()
        parsed = parse_encoding_output(
            _output("assistant", "direct_observation"), plan, messages
        )
        self.assertEqual(parsed.events[0].subject_id, "agent")


if __name__ == "__main__":
    unittest.main()
