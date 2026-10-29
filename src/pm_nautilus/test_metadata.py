"""Bounded sharing of immutable public metadata decoded from TEST ledgers."""

from collections import namedtuple
from dataclasses import fields
from sys import getsizeof
from threading import RLock
from weakref import ref

from .rules import Fees, Token


CACHE_SCHEMA_VERSION = 1
MAX_CACHE_ENTRIES = 32_768
MAX_CACHE_BYTES = 32 * 1024 * 1024
CacheInfo = namedtuple("CacheInfo", "hits misses entries bytes admission_rejects")


def _immutable(value):
    if type(value) in (str, bytes, bool, int, float, type(None)):
        return True
    if type(value) is tuple:
        return all(_immutable(item) for item in value)
    if type(value) in (Token, Fees):
        if not type(value).__dataclass_params__.frozen:
            return False
        return all(_immutable(getattr(value, field.name)) for field in fields(value))
    return False


def _weight(value, seen):
    identity = id(value)
    if identity in seen:
        return 0
    seen.add(identity)
    size = getsizeof(value)
    if type(value) is tuple:
        size += sum(_weight(item, seen) for item in value)
    elif type(value) in (Token, Fees):
        size += getsizeof(value.__dict__)
        size += sum(_weight(getattr(value, field.name), seen) for field in fields(value))
    return size


def _decode(token_id, payload, decoder):
    # Keep the original Store.tokens validation/construction order. A decoder
    # failure, malformed identity or otherwise invalid row is never memoized.
    data = decoder(payload)
    if data["token_id"] != token_id:
        raise ValueError("代币元数据身份与主键不一致")
    data["fees"] = Fees(**data["fees"])
    data["category_ids"] = tuple(data["category_ids"])
    data["category_labels"] = tuple(data["category_labels"])
    return Token(**data)


class TestMetadataMemo:
    __test__ = False

    def __init__(self, max_entries=MAX_CACHE_ENTRIES, max_bytes=MAX_CACHE_BYTES):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries = {}
        self._bytes = 0
        self._hits = self._misses = self._admission_rejects = 0
        # Admission allocates Python objects; GC can release another cached
        # value here and synchronously reenter _release on this same thread.
        self._lock = RLock()
        self._reference = ref(self)

    def _release(self, key, reference):
        with self._lock:
            saved = self._entries.get(key)
            if saved is not None and saved[0] is reference:
                del self._entries[key]
                self._bytes -= saved[1]

    def decode(self, token_id, payload, decoder):
        # SQLite supplies strings. Unknown input/callables retain the original
        # validation path instead of acquiring new cache-specific requirements.
        if type(token_id) is not str or type(payload) is not str:
            return _decode(token_id, payload, decoder)
        key = (CACHE_SCHEMA_VERSION, decoder, token_id, payload)
        try:
            hash(key)
        except TypeError:
            return _decode(token_id, payload, decoder)
        with self._lock:
            saved = self._entries.get(key)
            if saved is not None:
                token = saved[0]()
                if token is not None:
                    self._hits += 1
                    return token
                del self._entries[key]
                self._bytes -= saved[1]
            self._misses += 1
            # The caller already owns the Token. Budget only the complete key,
            # weak reference, callback/defaults and dict entry. When full, fall
            # straight back to the original decoder rather than recursively
            # inspecting values which cannot be admitted anyway.
            weight = _weight((token_id, payload), set()) + getsizeof(key) + 512
            admit = (
                self.max_entries > 0
                and len(self._entries) < self.max_entries
                and self._bytes + weight <= self.max_bytes
            )
            if not admit:
                self._admission_rejects += 1
        token = _decode(token_id, payload, decoder)
        # A frozen dataclass alone is insufficient: legacy payloads may contain
        # mutable values in otherwise loosely typed descriptive fields.
        if not admit or not _immutable(token):
            return token
        with self._lock:
            # Two readers may decode a miss concurrently; publish only one
            # immutable result and never serialize the actual decoding work.
            saved = self._entries.get(key)
            if saved is not None:
                shared = saved[0]()
                if shared is not None:
                    return shared
                del self._entries[key]
                self._bytes -= saved[1]
            if len(self._entries) >= self.max_entries or self._bytes + weight > self.max_bytes:
                # A long catalog scan must not evict the live subset just shared
                # by another account. Dead values free their own admission room.
                self._admission_rejects += 1
                return token

            def release(reference, owner=self._reference, identity=key):
                memo = owner()
                if memo is not None:
                    memo._release(identity, reference)

            self._entries[key] = (ref(token, release), weight)
            self._bytes += weight
        return token

    def info(self):
        with self._lock:
            return CacheInfo(
                self._hits, self._misses, len(self._entries), self._bytes, self._admission_rejects
            )


_MEMO = TestMetadataMemo()


def decode_test_token(token_id, payload, decoder):
    return _MEMO.decode(token_id, payload, decoder)
