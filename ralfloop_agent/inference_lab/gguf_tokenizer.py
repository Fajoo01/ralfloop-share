from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
import unicodedata
from typing import Any, BinaryIO


GGUF_TYPES = {
    0: ("B", 1),
    1: ("b", 1),
    2: ("H", 2),
    3: ("h", 2),
    4: ("I", 4),
    5: ("i", 4),
    6: ("f", 4),
    7: ("?", 1),
    10: ("Q", 8),
    11: ("q", 8),
    12: ("d", 8),
}


class GGUFError(ValueError):
    pass


def _read_exact(handle: BinaryIO, size: int) -> bytes:
    raw = handle.read(size)
    if len(raw) != size:
        raise GGUFError("truncated_gguf")
    return raw


def _read_u32(handle: BinaryIO) -> int:
    return struct.unpack("<I", _read_exact(handle, 4))[0]


def _read_u64(handle: BinaryIO) -> int:
    return struct.unpack("<Q", _read_exact(handle, 8))[0]


def _read_string(handle: BinaryIO) -> str:
    length = _read_u64(handle)
    if length > 128 * 1024 * 1024:
        raise GGUFError("gguf_string_too_large")
    return _read_exact(handle, length).decode("utf-8", "strict")


def _read_value(handle: BinaryIO, value_type: int) -> Any:
    if value_type == 8:
        return _read_string(handle)
    if value_type == 9:
        item_type = _read_u32(handle)
        count = _read_u64(handle)
        if count > 2_000_000:
            raise GGUFError("gguf_array_too_large")
        return [_read_value(handle, item_type) for _ in range(count)]
    spec = GGUF_TYPES.get(value_type)
    if spec is None:
        raise GGUFError(f"unsupported_gguf_value_type:{value_type}")
    fmt, size = spec
    return struct.unpack("<" + fmt, _read_exact(handle, size))[0]


def read_gguf_metadata(path: str | Path) -> dict[str, Any]:
    with Path(path).open("rb") as handle:
        if _read_exact(handle, 4) != b"GGUF":
            raise GGUFError("not_gguf")
        version = _read_u32(handle)
        if version not in {2, 3}:
            raise GGUFError(f"unsupported_gguf_version:{version}")
        tensor_count = _read_u64(handle)
        metadata_count = _read_u64(handle)
        if metadata_count > 100_000:
            raise GGUFError("gguf_metadata_count_too_large")
        metadata: dict[str, Any] = {
            "general.gguf_version": version,
            "general.tensor_count": tensor_count,
        }
        for _ in range(metadata_count):
            key = _read_string(handle)
            metadata[key] = _read_value(handle, _read_u32(handle))
        return metadata


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _bytes_to_unicode() -> dict[int, str]:
    values = list(range(ord("!"), ord("~") + 1))
    values += list(range(ord("¡"), ord("¬") + 1))
    values += list(range(ord("®"), ord("ÿ") + 1))
    chars = list(values)
    extra = 0
    for value in range(256):
        if value not in values:
            values.append(value)
            chars.append(256 + extra)
            extra += 1
    return dict(zip(values, (chr(value) for value in chars)))


BYTE_ENCODER = _bytes_to_unicode()
BYTE_DECODER = {value: key for key, value in BYTE_ENCODER.items()}


def _kind(char: str) -> str:
    category = unicodedata.category(char)
    if category.startswith("L"):
        return "letter"
    if category.startswith("N"):
        return "number"
    if char.isspace():
        return "space"
    return "other"


def qwen2_pretokenize(text: str) -> list[str]:
    """Stdlib implementation of the Qwen2 pre-tokenizer regex."""
    pieces: list[str] = []
    index = 0
    contractions = ("'re", "'ve", "'ll", "'s", "'t", "'m", "'d")
    while index < len(text):
        lower = text[index:].lower()
        contraction = next((item for item in contractions if lower.startswith(item)), None)
        if contraction:
            pieces.append(text[index : index + len(contraction)])
            index += len(contraction)
            continue
        char = text[index]
        kind = _kind(char)
        if char not in "\r\n" and kind not in {"letter", "number"} and index + 1 < len(text) and _kind(text[index + 1]) == "letter":
            end = index + 2
            while end < len(text) and _kind(text[end]) == "letter":
                end += 1
            pieces.append(text[index:end])
            index = end
            continue
        if kind == "letter":
            end = index + 1
            while end < len(text) and _kind(text[end]) == "letter":
                end += 1
            pieces.append(text[index:end])
            index = end
            continue
        if kind == "number":
            pieces.append(char)
            index += 1
            continue
        if char == " " and index + 1 < len(text) and _kind(text[index + 1]) == "other":
            end = index + 2
            while end < len(text) and _kind(text[end]) == "other":
                end += 1
            while end < len(text) and text[end] in "\r\n":
                end += 1
            pieces.append(text[index:end])
            index = end
            continue
        if kind == "other":
            end = index + 1
            while end < len(text) and _kind(text[end]) == "other":
                end += 1
            while end < len(text) and text[end] in "\r\n":
                end += 1
            pieces.append(text[index:end])
            index = end
            continue
        run_end = index + 1
        while run_end < len(text) and _kind(text[run_end]) == "space":
            run_end += 1
        newline_positions = [offset for offset in range(index, run_end) if text[offset] in "\r\n"]
        if newline_positions:
            end = newline_positions[-1] + 1
        elif run_end < len(text) and run_end - index >= 2:
            # Qwen2's `\s+(?!\S)` leaves one whitespace character for the
            # following optional-prefix alternative.
            end = run_end - 1
        else:
            end = run_end
        pieces.append(text[index:end])
        index = end
    return pieces


@dataclass(frozen=True)
class TokenizerIdentity:
    tokenizer_hash: str
    vocabulary_hash: str
    vocabulary_size: int
    model: str
    pre_tokenizer: str
    bos_token_id: int | None
    eos_token_id: int | None


class GGUFTokenizer:
    def __init__(self, metadata: dict[str, Any]) -> None:
        tokens = metadata.get("tokenizer.ggml.tokens")
        merges = metadata.get("tokenizer.ggml.merges")
        if not isinstance(tokens, list) or not all(isinstance(item, str) for item in tokens):
            raise GGUFError("tokenizer_tokens_missing")
        if not isinstance(merges, list) or not all(isinstance(item, str) for item in merges):
            raise GGUFError("tokenizer_merges_missing")
        self.tokens = tokens
        self.token_to_id = {token: index for index, token in enumerate(tokens)}
        self.merge_ranks = {
            tuple(merge.split(" ", 1)): rank
            for rank, merge in enumerate(merges)
            if " " in merge
        }
        self.special = {
            token: index
            for index, token in enumerate(tokens)
            if token.startswith("<|") and token.endswith("|>")
        }
        self.special_by_id = {value: key for key, value in self.special.items()}
        model = str(metadata.get("tokenizer.ggml.model") or "")
        pre = str(metadata.get("tokenizer.ggml.pre") or "")
        if model != "gpt2" or pre != "qwen2":
            raise GGUFError(f"unsupported_tokenizer:{model}:{pre}")
        identity_payload = {
            "model": model,
            "pre": pre,
            "tokens": tokens,
            "merges": merges,
            "bos": metadata.get("tokenizer.ggml.bos_token_id"),
            "eos": metadata.get("tokenizer.ggml.eos_token_id"),
            "add_bos": metadata.get("tokenizer.ggml.add_bos_token"),
        }
        self.identity = TokenizerIdentity(
            tokenizer_hash=_stable_hash(identity_payload),
            vocabulary_hash=_stable_hash(tokens),
            vocabulary_size=len(tokens),
            model=model,
            pre_tokenizer=pre,
            bos_token_id=_optional_int(metadata.get("tokenizer.ggml.bos_token_id")),
            eos_token_id=_optional_int(metadata.get("tokenizer.ggml.eos_token_id")),
        )
        self._bpe_cache: dict[str, tuple[str, ...]] = {}

    @classmethod
    def from_file(cls, path: str | Path) -> "GGUFTokenizer":
        return cls(read_gguf_metadata(path))

    def _bpe(self, token: str) -> tuple[str, ...]:
        cached = self._bpe_cache.get(token)
        if cached is not None:
            return cached
        word = tuple(token)
        while len(word) > 1:
            ranked = [
                (self.merge_ranks[pair], pair)
                for pair in zip(word, word[1:])
                if pair in self.merge_ranks
            ]
            if not ranked:
                break
            _, selected = min(ranked, key=lambda item: item[0])
            merged: list[str] = []
            index = 0
            while index < len(word):
                if index + 1 < len(word) and (word[index], word[index + 1]) == selected:
                    merged.append(word[index] + word[index + 1])
                    index += 2
                else:
                    merged.append(word[index])
                    index += 1
            word = tuple(merged)
        self._bpe_cache[token] = word
        return word

    def encode(self, text: str) -> list[int]:
        output: list[int] = []
        index = 0
        special_tokens = sorted(self.special, key=len, reverse=True)
        while index < len(text):
            matched = next((item for item in special_tokens if text.startswith(item, index)), None)
            if matched is not None:
                output.append(self.special[matched])
                index += len(matched)
                continue
            end = index + 1
            while end < len(text) and not any(text.startswith(item, end) for item in special_tokens):
                end += 1
            for piece in qwen2_pretokenize(text[index:end]):
                encoded = "".join(BYTE_ENCODER[value] for value in piece.encode("utf-8"))
                for bpe_token in self._bpe(encoded):
                    try:
                        output.append(self.token_to_id[bpe_token])
                    except KeyError as exc:
                        raise GGUFError("token_not_in_vocabulary") from exc
            index = end
        return output

    def decode(self, token_ids: list[int]) -> str:
        chunks: list[str] = []
        byte_buffer = bytearray()

        def flush() -> None:
            if byte_buffer:
                chunks.append(bytes(byte_buffer).decode("utf-8", "replace"))
                byte_buffer.clear()

        for token_id in token_ids:
            if not isinstance(token_id, int) or isinstance(token_id, bool) or not 0 <= token_id < len(self.tokens):
                raise GGUFError("invalid_token_id")
            if token_id in self.special_by_id:
                flush()
                chunks.append(self.special_by_id[token_id])
                continue
            token = self.tokens[token_id]
            if token.startswith("<0x") and token.endswith(">") and len(token) == 6:
                byte_buffer.append(int(token[3:5], 16))
                continue
            for char in token:
                value = BYTE_DECODER.get(char)
                if value is None:
                    flush()
                    chunks.append(char)
                else:
                    byte_buffer.append(value)
        flush()
        return "".join(chunks)


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None
