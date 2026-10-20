"""Bounded-payload JSONL reads and compatible canonical event fingerprints."""

import bisect
import codecs
import hashlib
import json
import re


CHUNK = 65536
FAST_RECORD = 256 * 1024
OBJECT_BATCH = 128
SPECIAL = re.compile(br'["\\\x00-\x1f]')
WHITESPACE = re.compile(br"[ \t\r\n]*")
TOKEN = re.compile(br'[^ \t\r\n,\]}]+')
KEEP = object()
TRUTH = object()
FIELDS = {
    "type": KEEP, "agentId": KEEP, "timestamp": KEEP,
    "data": {
        "sessionId": KEEP, "context": {"cwd": KEEP}, "agentId": KEEP,
        "toolCallId": KEEP, "delivery": KEEP, "source": KEEP, "toolRequests": TRUTH,
        "turnId": KEEP, "phase": KEEP, "hookType": KEEP, "hookInvocationId": KEEP,
        "success": KEEP, "toolName": KEEP,
        "input": {"sessionId": KEEP, "stopReason": KEEP, "notification_type": KEEP,
                  "toolCallId": KEEP},
    },
}


class Cursor:
    def __init__(self, stream, start, end):
        self.stream = stream
        self.position = start
        self.end = end
        self.base = start
        self.buffer = b""

    def chunk(self):
        if not self.base <= self.position < self.base + len(self.buffer):
            self.stream.seek(self.position)
            self.base = self.position
            self.buffer = self.stream.read(min(CHUNK, self.end - self.position))
        return self.buffer[self.position - self.base:]

    def peek(self, size=1):
        if self.position + size > self.end:
            return b""
        value = self.chunk()[:size]
        if len(value) < size:
            self.stream.seek(self.position)
            self.base = self.position
            self.buffer = self.stream.read(min(CHUNK, self.end - self.position))
            value = self.buffer[:size]
        return value

    def expect(self, value):
        if self.peek(len(value)) != value:
            raise ValueError("invalid JSON token")
        self.position += len(value)

    def whitespace(self):
        while self.position < self.end:
            data = self.chunk()
            size = WHITESPACE.match(data).end()
            self.position += size
            if size < len(data):
                break


def string_parts(cursor):
    cursor.expect(b'"')
    decoder = codecs.getincrementaldecoder("utf-8")()
    while cursor.position < cursor.end:
        data = cursor.chunk()
        special = SPECIAL.search(data)
        size = special.start() if special else len(data)
        if size:
            cursor.position += size
            yield decoder.decode(data[:size])
        if special is None:
            continue
        yield decoder.decode(b"", final=True)
        decoder.reset()
        kind = cursor.peek()
        if kind == b'"':
            cursor.position += 1
            return
        if kind != b"\\":
            raise ValueError("control character in JSON string")
        cursor.position += 1
        escape = cursor.peek()
        if escape == b"u":
            cursor.position += 1
            digits = cursor.peek(4)
            if len(digits) != 4 or re.fullmatch(br"[0-9a-fA-F]{4}", digits) is None:
                raise ValueError("invalid JSON Unicode escape")
            cursor.position += 4
            code = int(digits, 16)
            following = cursor.peek(6)
            if 0xD800 <= code <= 0xDBFF and re.fullmatch(br"\\u[dD][c-fC-F][0-9a-fA-F]{2}", following):
                low = int(following[2:], 16)
                code = 0x10000 + ((code - 0xD800) << 10) + low - 0xDC00
                cursor.position += 6
            yield chr(code)
        else:
            escapes = {b'"': '"', b"\\": "\\", b"/": "/", b"b": "\b",
                       b"f": "\f", b"n": "\n", b"r": "\r", b"t": "\t"}
            if escape not in escapes:
                raise ValueError("invalid JSON string escape")
            cursor.position += 1
            yield escapes[escape]
    raise ValueError("unterminated JSON string")


class Span:
    def __init__(self, kind, start, end, nonempty=True):
        self.kind, self.start, self.end, self.nonempty = kind, start, end, nonempty


class Key:
    def __init__(self, parser, span):
        self.parser, self.span = parser, span
        self.text = parser.load(span) if span.end - span.start <= 4096 else None

    def compare(self, other):
        if self.text is not None and other.text is not None:
            return (self.text > other.text) - (self.text < other.text)
        left = iter(string_parts(self.parser.cursor(self.span)))
        right = iter(string_parts(other.parser.cursor(other.span)))
        a = b = ""
        while True:
            if not a:
                a = next((part for part in left if part), "")
            if not b:
                b = next((part for part in right if part), "")
            if not a or not b:
                return bool(a) - bool(b)
            size = min(len(a), len(b))
            if a[:size] != b[:size]:
                return (a[:size] > b[:size]) - (a[:size] < b[:size])
            a, b = a[size:], b[size:]

    def __lt__(self, other):
        return self.compare(other) < 0

    def __eq__(self, other):
        return self.compare(other) == 0


class Parser:
    def __init__(self, stream, start, end):
        self.stream, self.start, self.end = stream, start, end

    def cursor(self, span):
        return Cursor(self.stream, span.start, span.end)

    def load(self, span):
        self.stream.seek(span.start)
        return json.loads(self.stream.read(span.end - span.start))

    def value(self, cursor):
        cursor.whitespace()
        start = cursor.position
        kind = cursor.peek()
        if kind == b'"':
            for _ in string_parts(cursor):
                pass
            return Span("string", start, cursor.position, cursor.position - start > 2)
        if kind in (b"{", b"["):
            cursor.position += 1
            cursor.whitespace()
            closing = b"}" if kind == b"{" else b"]"
            nonempty = cursor.peek() != closing
            if nonempty:
                while True:
                    if kind == b"{":
                        self.value_key(cursor)
                        cursor.whitespace()
                        cursor.expect(b":")
                    self.value(cursor)
                    cursor.whitespace()
                    if cursor.peek() != b",":
                        break
                    cursor.position += 1
                    cursor.whitespace()
            cursor.expect(closing)
            return Span("object" if kind == b"{" else "array", start, cursor.position, nonempty)
        parts = []
        while cursor.position < cursor.end:
            data = cursor.chunk()
            match = TOKEN.match(data)
            if match is None:
                break
            parts.append(match.group())
            cursor.position += match.end()
            if match.end() < len(data):
                break
        token = b"".join(parts)
        if not token:
            raise ValueError("missing JSON value")
        scalar = json.loads(token)
        if isinstance(scalar, (dict, list, str)):
            raise ValueError("invalid JSON scalar")
        return Span("scalar", start, cursor.position, bool(scalar))

    def value_key(self, cursor):
        start = cursor.position
        for _ in string_parts(cursor):
            pass
        return Span("string", start, cursor.position)

    def pairs(self, span):
        cursor = self.cursor(span)
        cursor.expect(b"{")
        cursor.whitespace()
        if cursor.peek() == b"}":
            return
        while True:
            key = self.value_key(cursor)
            cursor.whitespace()
            cursor.expect(b":")
            value = self.value(cursor)
            yield key, value
            cursor.whitespace()
            if cursor.peek() == b"}":
                return
            cursor.expect(b",")
            cursor.whitespace()

    def project(self, span, fields):
        if fields is TRUTH:
            return [True] if span.nonempty else []
        if fields is KEEP or span.kind != "object":
            return self.load(span)
        result = {}
        for key, value in self.pairs(span):
            # Oversized keys cannot match a reducer field.
            if key.end - key.start > 256:
                continue
            name = self.load(key)
            if name in fields:
                result[name] = self.project(value, fields[name])
        return result

    def canonical(self, span, emit):
        if span.kind == "string":
            emit(b'"')
            for part in string_parts(self.cursor(span)):
                if part:
                    emit(json.dumps(part, ensure_ascii=True)[1:-1].encode("ascii"))
            emit(b'"')
        elif span.kind == "scalar":
            emit(json.dumps(self.load(span), ensure_ascii=True).encode("ascii"))
        elif span.kind == "array":
            emit(b"[")
            cursor = self.cursor(span)
            cursor.position += 1
            cursor.whitespace()
            first = True
            while cursor.peek() != b"]":
                value = self.value(cursor)
                if not first:
                    emit(b", ")
                self.canonical(value, emit)
                first = False
                cursor.whitespace()
                if cursor.peek() == b"]":
                    break
                cursor.expect(b",")
                cursor.whitespace()
            emit(b"]")
        else:
            emit(b"{")
            after = None
            first = True
            while True:
                keys, values = [], []
                for key_span, value in self.pairs(span):
                    key = Key(self, key_span)
                    if after is not None and not after < key:
                        continue
                    position = bisect.bisect_left(keys, key)
                    if position < len(keys) and keys[position] == key:
                        # json.loads retains the last value for a duplicate key.
                        values[position] = value
                    elif position < OBJECT_BATCH:
                        keys.insert(position, key)
                        values.insert(position, value)
                        if len(keys) > OBJECT_BATCH:
                            keys.pop()
                            values.pop()
                for key, value in zip(keys, values):
                    if not first:
                        emit(b", ")
                    self.canonical(key.span, emit)
                    emit(b": ")
                    self.canonical(value, emit)
                    first = False
                if len(keys) < OBJECT_BATCH:
                    break
                after = keys[-1]
            emit(b"}")


class StreamEvent(dict):
    def canonical_digest(self):
        position = self.parser.stream.tell()
        try:
            digest = hashlib.sha256()
            self.parser.canonical(self.span, digest.update)
            return digest.hexdigest()
        finally:
            self.parser.stream.seek(position)


def read_record(stream, start=None):
    """Return (end offset, event), or None for EOF/an incomplete JSONL tail."""
    # A known binary offset avoids an expensive native tell() on every event.
    if start is None:
        start = stream.tell()
    line = stream.readline(FAST_RECORD + 1)
    if line.endswith(b"\n"):
        return start + len(line), json.loads(line)
    if len(line) <= FAST_RECORD:
        stream.seek(start)
        return None
    end = start + len(line)
    while True:
        part = stream.readline(CHUNK)
        end += len(part)
        if part.endswith(b"\n"):
            break
        if not part:
            stream.seek(start)
            return None
    parser = Parser(stream, start, end)
    try:
        cursor = Cursor(stream, start, end)
        if cursor.peek(3) == b"\xef\xbb\xbf":
            cursor.position += 3
        span = parser.value(cursor)
        cursor.whitespace()
        if cursor.position != end:
            raise ValueError("extra data after JSON value")
        if span.kind != "object":
            raise ValueError("invalid transcript event shape")
        event = StreamEvent(parser.project(span, FIELDS))
        event.parser, event.span = parser, span
        return end, event
    finally:
        stream.seek(end)
