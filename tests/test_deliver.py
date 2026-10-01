from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from support import CALLBACK_TOKEN, COMPLETED_AT, INBOX_ORIGIN, JOB_ID, NOW, ok_body

from codex_grokbot_mcp.deliver import (
    PacketError,
    build_ask_packet,
    build_x_query_packet,
    callback_url,
    clean_goal,
    require_callback_target,
    validate_result,
)


def packet(job_type: str = "x_query", **overrides):
    builder = {"x_query": build_x_query_packet, "ask": build_ask_packet}[job_type]
    options = {
        "job_id": JOB_ID,
        "goal": "What is being discussed?",
        "origin": INBOX_ORIGIN,
        "callback_token": CALLBACK_TOKEN,
    }
    options.update(overrides)
    return builder(**options)


class PacketTests(unittest.TestCase):
    def test_x_query_packet_is_callback_only(self) -> None:
        built = packet()

        self.assertEqual(built["schema_version"], "v3")
        self.assertEqual(built["job_type"], "x_query")
        self.assertEqual(built["deliver"], "callback")
        self.assertEqual(
            built["context"]["callback_url"], callback_url(INBOX_ORIGIN, JOB_ID, "result")
        )
        self.assertNotIn("control_repo", built["context"])
        self.assertNotIn("github_token", built["context"])
        self.assertEqual(set(built["context"]), {"callback_url", "callback_token"})
        self.assertTrue(built["constraints"]["read_only"])
        self.assertIn("read-only research", " ".join(built["instructions"]))

    def test_ask_packet_uses_the_ask_job_type(self) -> None:
        built = packet("ask", goal="Summarise the release notes.")

        self.assertEqual(built["job_type"], "ask")
        self.assertEqual(built["deliver"], "callback")
        self.assertEqual(built["goal"], "Summarise the release notes.")
        self.assertEqual(built["schema_version"], "v3")
        instructions = " ".join(built["instructions"])
        self.assertIn("Answer the question directly", instructions)
        self.assertNotIn("GitHub", built["context"])

    def test_both_builders_trim_the_goal(self) -> None:
        for job_type in ("x_query", "ask"):
            with self.subTest(job_type=job_type):
                self.assertEqual(packet(job_type, goal="  padded  ")["goal"], "padded")

    def test_builders_reject_control_repository_authority(self) -> None:
        forbidden = (
            {"control_repo": "example/private-control"},
            {"github_token": CALLBACK_TOKEN},
            {"head_ref": "grokbot/job-coding-1234abcd"},
            {"answer_path": "answers/answer-1234abcd.json"},
        )
        for job_type in ("x_query", "ask"):
            for extra in forbidden:
                with self.subTest(job_type=job_type, extra=extra):
                    with self.assertRaises(PacketError):
                        packet(job_type, **extra)

    def test_builders_reject_any_other_delivery(self) -> None:
        for job_type in ("x_query", "ask"):
            with self.subTest(job_type=job_type):
                with self.assertRaises(PacketError):
                    packet(job_type, deliver="github_pr")

    def test_goal_bounds_are_enforced(self) -> None:
        for goal in ("", "   ", "x" * 2001, "with\x00null"):
            with self.subTest(goal=goal[:12]):
                with self.assertRaises(PacketError):
                    clean_goal(goal)

    def test_callback_origin_rules_are_enforced(self) -> None:
        rejected = (
            "http://inbox.example.invalid",
            "https://user@inbox.example.invalid",
            "https://inbox.example.invalid/extra",
            "https://inbox.example.invalid/?q=1",
            "https://192.0.2.10",
        )
        for origin in rejected:
            with self.subTest(origin=origin):
                with self.assertRaises(PacketError):
                    packet(origin=origin)

    def test_callback_token_must_be_bounded_and_unsafe_free(self) -> None:
        for token in ("short", CALLBACK_TOKEN + " space"):
            with self.subTest(token=token[:8]):
                with self.assertRaises(PacketError):
                    packet(callback_token=token)

    def test_foreign_host_or_job_id_does_not_match_the_configured_callback(self) -> None:
        expected = callback_url(INBOX_ORIGIN, JOB_ID, "result")
        foreign = expected.replace("inbox.example.invalid", "other.example.invalid")
        other_job = "ffffffff-ffff-4fff-8fff-ffffffffffff"

        with self.assertRaises(PacketError):
            require_callback_target(foreign, origin=INBOX_ORIGIN, job_id=JOB_ID, kind="result")
        with self.assertRaises(PacketError):
            require_callback_target(
                callback_url(INBOX_ORIGIN, other_job, "result"),
                origin=INBOX_ORIGIN,
                job_id=JOB_ID,
                kind="result",
            )
        with self.assertRaises(PacketError):
            callback_url(INBOX_ORIGIN, JOB_ID, "patch")

    def test_job_id_must_be_a_canonical_uuid(self) -> None:
        for job_id in ("not-a-uuid", JOB_ID.upper(), ""):
            with self.subTest(job_id=job_id):
                with self.assertRaises(PacketError):
                    packet(job_id=job_id)


class ResultValidationTests(unittest.TestCase):
    def test_ok_and_error_bodies_are_accepted_for_both_types(self) -> None:
        for job_type in ("x_query", "ask"):
            with self.subTest(job_type=job_type, status="ok"):
                body = ok_body(JOB_ID, job_type)
                self.assertEqual(validate_result(JOB_ID, job_type, body, now=NOW)["status"], "ok")
            with self.subTest(job_type=job_type, status="error"):
                error = ok_body(
                    JOB_ID,
                    job_type,
                    answer=None,
                    sources=[],
                    status="error",
                    error={"code": "source_unavailable", "message": "The read failed."},
                )
                self.assertIsNone(validate_result(JOB_ID, job_type, error, now=NOW)["answer"])

    def test_ask_allows_an_empty_source_list(self) -> None:
        result = validate_result(JOB_ID, "ask", ok_body(JOB_ID, "ask", sources=[]), now=NOW)

        self.assertEqual(result["sources"], [])

    def test_multiline_answers_are_accepted(self) -> None:
        plain = "Highlights:\n\n- First point\n- Second point"
        structured = {"top_themes": [{"theme": "First line\ncontinued"}]}

        self.assertEqual(
            validate_result(JOB_ID, "ask", ok_body(JOB_ID, "ask", answer=plain), now=NOW)["answer"],
            plain,
        )
        self.assertEqual(
            validate_result(JOB_ID, "ask", ok_body(JOB_ID, "ask", answer=structured), now=NOW)[
                "answer"
            ],
            structured,
        )

    def test_wrong_job_type_or_job_id_is_rejected(self) -> None:
        with self.assertRaises(PacketError):
            validate_result(JOB_ID, "x_query", ok_body(JOB_ID, "ask"), now=NOW)
        with self.assertRaises(PacketError):
            validate_result(JOB_ID, "ask", ok_body(JOB_ID, "x_query"), now=NOW)
        with self.assertRaises(PacketError):
            validate_result(
                JOB_ID,
                "ask",
                ok_body("ffffffff-ffff-4fff-8fff-ffffffffffff", "ask"),
                now=NOW,
            )
        with self.assertRaises(PacketError):
            validate_result(JOB_ID, "coding", ok_body(JOB_ID, "x_query"), now=NOW)
        with self.assertRaises(PacketError):
            validate_result(JOB_ID, "ask", ["not", "an", "object"], now=NOW)

    def test_future_completion_time_is_rejected(self) -> None:
        future = (NOW + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self.assertRaisesRegex(PacketError, "future"):
            validate_result(JOB_ID, "ask", ok_body(JOB_ID, "ask", completed_at=future), now=NOW)

    def test_naive_completion_time_is_rejected(self) -> None:
        with self.assertRaises(PacketError):
            validate_result(
                JOB_ID, "ask", ok_body(JOB_ID, "ask", completed_at="2026-09-26T00:00:30"), now=NOW
            )

    def test_unsafe_text_fails_closed(self) -> None:
        changes = (
            {"summary": "token=abc"},
            {"answer": "I posted an update"},
            {"answer": "safe\x00unsafe"},
            {"read_only_attestation": False},
            {"query": "x" * 2001},
            {"summary": " padded "},
        )
        for change in changes:
            with self.subTest(change=list(change)):
                with self.assertRaises(PacketError):
                    validate_result(JOB_ID, "ask", ok_body(JOB_ID, "ask", **change), now=NOW)

    def test_structured_answers_require_themes(self) -> None:
        with self.assertRaises(PacketError):
            validate_result(JOB_ID, "ask", ok_body(JOB_ID, "ask", answer={"other": 1}), now=NOW)
        with self.assertRaises(PacketError):
            validate_result(JOB_ID, "ask", ok_body(JOB_ID, "ask", answer=12), now=NOW)

    def test_oversized_bodies_are_rejected(self) -> None:
        cases = (
            {"answer": "x" * 4001},
            {"answer": {"top_themes": [{"theme": "x" * 20001}]}},
            {"sources": [f"https://example.invalid/{index}" for index in range(33)]},
            {"sources": ["x" * 513]},
        )
        for change in cases:
            with self.subTest(change=list(change)):
                with self.assertRaises(PacketError):
                    validate_result(JOB_ID, "ask", ok_body(JOB_ID, "ask", **change), now=NOW)

    def test_error_bodies_cannot_carry_an_answer(self) -> None:
        with self.assertRaises(PacketError):
            validate_result(
                JOB_ID,
                "ask",
                ok_body(
                    JOB_ID,
                    "ask",
                    status="error",
                    answer="I still answered",
                    error={"code": "x", "message": "y"},
                ),
                now=NOW,
            )
        with self.assertRaises(PacketError):
            validate_result(
                JOB_ID,
                "ask",
                ok_body(JOB_ID, "ask", status="error", answer=None, sources=[], error=None),
                now=NOW,
            )
        with self.assertRaises(PacketError):
            validate_result(
                JOB_ID,
                "ask",
                ok_body(JOB_ID, "ask", status="ok", error={"code": "a", "message": "b"}),
                now=NOW,
            )
        with self.assertRaises(PacketError):
            validate_result(JOB_ID, "ask", ok_body(JOB_ID, "ask", status="maybe"), now=NOW)

    def test_completed_at_must_be_utc(self) -> None:
        self.assertEqual(COMPLETED_AT, "2026-09-26T00:00:30Z")
        with self.assertRaises(PacketError):
            validate_result(
                JOB_ID,
                "ask",
                ok_body(JOB_ID, "ask", completed_at="2026-09-26T00:00:30+02:00"),
                now=datetime(2026, 9, 26, 1, tzinfo=UTC),
            )


if __name__ == "__main__":
    unittest.main()
