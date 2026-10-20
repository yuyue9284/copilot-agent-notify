import hashlib
import io
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import transcript_stream as reader
from activity import Activity, Transcript
from session_probe import ActivityRevision


class StreamTests(unittest.TestCase):
    def test_known_offsets_do_not_query_native_position_per_record(self):
        class WithoutTell(io.BytesIO):
            def tell(self):
                raise AssertionError("the caller already knows the binary position")

        prefix = b"earlier record\n"
        raw = json.dumps({"type": "user.message", "data": {
            "delivery": "idle", "content": "\u03bb"}}, ensure_ascii=False).encode() + b"\r\n"
        for limit in (reader.FAST_RECORD, 8):
            with self.subTest(limit=limit), patch.object(reader, "FAST_RECORD", limit):
                stream = WithoutTell(prefix + raw)
                stream.seek(len(prefix))
                end, event = reader.read_record(stream, len(prefix))
                self.assertEqual(end, len(prefix) + len(raw))
                self.assertEqual(event["data"]["delivery"], "idle")

    def decode(self, raw):
        with patch.object(reader, "FAST_RECORD", 8):
            stream = io.BytesIO(raw + b"\n")
            return stream, reader.read_record(stream)[1]

    def compare(self, raw):
        expected = json.loads(raw)
        stream, actual = self.decode(raw)
        digest = hashlib.sha256(json.dumps(
            expected, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
        position = stream.tell()
        self.assertEqual(actual.canonical_digest(), digest)
        self.assertEqual(stream.tell(), position)
        for cls in (Activity, ActivityRevision):
            a, b = cls("synthetic-root"), cls("synthetic-root")
            a.event(expected)
            b.event(actual)
            self.assertEqual(a.counts(), b.counts())
            if cls is ActivityRevision:
                self.assertEqual(a.activity_revision, b.activity_revision)
        return actual

    def test_unicode_escapes_scalars_duplicate_keys_and_nested_values(self):
        values = [
            b'{"type":"user.message","data":{"content":"a\\n\\t\\u0000\\ud83d\\ude00\\ud800"}}',
            '{"type":"user.message","data":{"content":"\u03bb\U0001f600"}}'.encode(),
            b'{"z":1,"a":2,"z":3,"type":"user.message","data":{"n":-0.0,"f":1e30,"a":[true,false,null,NaN,Infinity,-Infinity,1e999]}}',
            b'{"type":"user.message","data":{"x":{"b":4,"a":5},"a":[{},[],{"d":2,"c":1}]}}',
            b'{"type":"assistant.message","data":{"toolRequests":[{"arguments":{"large":"ignored"}}]}}',
            b'{"type":"hook.start","data":{"hookType":"notification","input":{"notification_type":"permission_prompt"}}}',
            b'{"type":"assistant.turn_start","type":"user.message","data":{"delivery":"idle"}}',
        ]
        for raw in values:
            with self.subTest(raw=raw):
                self.compare(raw)
        self.compare(b'\xef\xbb\xbf{"type":"user.message","data":{"content":"BOM"}}')

    def test_string_escapes_cross_chunk_boundaries(self):
        raw = b'{"type":"user.message","data":{"content":"' + (
            b'abc\\ud83d\\ude00\\n\\"\\\\\\ud800' + "\u03bb\U0001f600".encode()) * 30 + b'"}}'
        with patch.object(reader, "CHUNK", 17):
            self.compare(raw)

    def test_wide_objects_use_bounded_sort_batches_and_last_duplicate(self):
        raw = ('{"type":"user.message","data":{'
               + ",".join(json.dumps(str(i)) + ":" + str(i) for i in range(400, -1, -1))
               + ',"200":-1}}').encode()
        with patch.object(reader, "OBJECT_BATCH", 7):
            self.compare(raw)

    def test_large_keys_use_streaming_comparison(self):
        data = {"x" * 8000 + suffix: suffix for suffix in ("a", "b", "c")}
        data["x" * 8000 + "\U0001f600"] = "unicode"
        raw = json.dumps({"type": "user.message", "data": data}).encode()
        self.compare(raw)

    def test_irrelevant_content_is_not_retained(self):
        actual = self.compare(json.dumps({
            "type": "user.message", "data": {"delivery": "idle", "content": "x" * 1024 ** 2}
        }).encode())
        self.assertEqual(actual, {"type": "user.message", "data": {"delivery": "idle"}})

    def test_projected_lifecycle_preserves_all_reducer_state(self):
        events = [
            {"type": "session.start", "data": {"sessionId": "synthetic-root"}},
            {"type": "user.message", "data": {"delivery": "idle", "source": "user"}},
            {"type": "subagent.started", "data": {"agentId": "child", "toolCallId": "spawn"}},
            {"type": "tool.execution_start", "data": {"toolName": "ask_user", "toolCallId": "ask"}},
            {"type": "tool.execution_complete", "data": {"toolCallId": "ask"}},
            {"type": "hook.start", "data": {"hookType": "notification", "input": {
                "sessionId": "child", "notification_type": "permission_prompt", "toolCallId": "permit"}}},
            {"type": "assistant.turn_start", "agentId": "child", "data": {}},
            {"type": "assistant.message", "agentId": "child", "data": {
                "turnId": "child-turn", "toolRequests": [{"arguments": {"payload": "ignored"}}]}},
            {"type": "assistant.message", "agentId": "child", "data": {
                "turnId": "child-turn", "phase": "final_answer", "toolRequests": []}},
            {"type": "hook.start", "data": {"hookType": "agentStop", "hookInvocationId": "child-stop",
                                          "input": {"sessionId": "child"}}},
            {"type": "hook.end", "data": {"hookInvocationId": "child-stop", "success": True}},
            {"type": "assistant.turn_end", "agentId": "child", "data": {"turnId": "child-turn"}},
            {"type": "subagent.completed", "data": {"toolCallId": "spawn"}},
            {"type": "assistant.message", "data": {
                "turnId": "root-turn", "phase": "final_answer", "toolRequests": []}},
            {"type": "hook.start", "data": {"hookType": "agentStop", "hookInvocationId": "stop",
                                          "input": {"sessionId": "synthetic-root", "stopReason": "abort"}}},
            {"type": "hook.end", "data": {"hookInvocationId": "stop", "success": True}},
            {"type": "assistant.turn_end", "data": {"turnId": "root-turn"}},
            {"type": "session.shutdown", "data": {}},
            {"type": "assistant.turn_start", "data": {}},
        ]
        for cls in (Activity, ActivityRevision):
            expected, actual = cls("synthetic-root"), cls("synthetic-root")
            for i, event in enumerate(events):
                event = dict(event, id="synthetic-event-%d" % i,
                             timestamp="2025-01-01T00:00:00Z")
                stream, projected = self.decode(json.dumps(event).encode())
                expected.event(event)
                actual.event(projected)
                self.assertEqual(actual.__dict__, expected.__dict__, event["type"])

    def test_partial_large_line_waits_and_malformed_record_advances(self):
        raw = b'{"type":"user.message","data":{"content":"' + b"x" * 100 + b'"}}'
        with patch.object(reader, "FAST_RECORD", 8):
            stream = io.BytesIO(raw)
            self.assertIsNone(reader.read_record(stream))
            self.assertEqual(stream.tell(), 0)
            stream.seek(0, 2)
            stream.write(b"\n")
            stream.seek(0)
            self.assertEqual(reader.read_record(stream)[0], len(raw) + 1)
        for raw in (b'{"extra":"trailing comma",}', b'{"x":"bad\\q"}',
                    b'{"x":"raw\x00"}', b'{"x":[1,]}', b'{"x":"\xff"}', b'{"x":tru}',
                    b'{"x":1} garbage', b'{"x":"\\u123x"}', b'{"x":"\\u000"}'):
            with self.subTest(raw=raw), patch.object(reader, "FAST_RECORD", 1):
                stream = io.BytesIO(raw + b"\n")
                with self.assertRaises((ValueError, UnicodeDecodeError)):
                    reader.read_record(stream)
                self.assertEqual(stream.tell(), len(raw) + 1)

    def test_deterministic_generated_differential_fixtures(self):
        rng = random.Random(42)

        def value(depth):
            scalar = rng.choice([None, True, False, rng.randrange(-10000, 10000),
                                 rng.random() * 1e40, 'quote" slash\\ \n \u03bb \U0001f600'])
            if depth == 0:
                return scalar
            return rng.choice([scalar, [value(depth - 1) for _ in range(3)],
                               {str(i): value(depth - 1) for i in range(3)}])
        for _ in range(80):
            self.compare(json.dumps({"type": "user.message", "data": {"content": value(3)}},
                                    ensure_ascii=rng.choice([True, False])).encode())

    def test_large_record_partial_tail_budget_and_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            header = b'{"type":"session.start","data":{"sessionId":"synthetic-root"}}\n'
            record = b'{"type":"user.message","data":{"delivery":"idle","content":"' + b"x" * 300000
            path.write_bytes(header + record)
            transcript = Transcript(path, "synthetic-root")
            self.assertTrue(transcript.poll())
            self.assertEqual(transcript.offset, len(header))
            with path.open("ab") as stream:
                stream.write(b'"}}\n{"type":"assistant.message","data":{"turnId":"t"}}\n')
                stream.write(b'{"type":"assistant.turn_end","data":{"turnId":"t"}}\n')
            self.assertFalse(transcript.poll(budget=1024))
            self.assertEqual(transcript.display_counts, (0, 0))
            self.assertTrue(transcript.poll())
            self.assertEqual(transcript.display_counts, (0, 0))
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(header + b'{"type":"assistant.turn_start","data":{}}\n')
            replacement.replace(path)
            self.assertTrue(transcript.poll())
            self.assertEqual(transcript.display_counts, (1, 0))

    def test_replacement_recovers_from_malformed_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            header = b'{"type":"session.start","data":{"sessionId":"synthetic-root"}}\n'
            path.write_bytes(header + b'{"invalid":}\n')
            transcript = Transcript(path, "synthetic-root")
            with self.assertLogs("copilot-notify-icons", level="WARNING"):
                transcript.poll()
            self.assertTrue(transcript.malformed)
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(header)
            replacement.replace(path)
            self.assertTrue(transcript.poll())
            self.assertFalse(transcript.malformed)

    def test_invalid_shapes_are_malformed_not_successful_idle(self):
        for record in (b'[]', b'null', b'{"type":"user.message","data":[]}'):
            with self.subTest(record=record), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "events.jsonl"
                path.write_bytes(
                    b'{"type":"session.start","data":{"sessionId":"synthetic-root"}}\n'
                    + record + b"\n")
                transcript = Transcript(path, "synthetic-root")
                with self.assertLogs("copilot-notify-icons", level="WARNING"):
                    self.assertTrue(transcript.poll())
                self.assertTrue(transcript.malformed)


if __name__ == "__main__":
    unittest.main()
