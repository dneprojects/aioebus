"""aioebus -- standalone async eBUS library (work in progress).

Pure, offline eBUS decoding validated against real captured telegrams:
  - codecs: datatype decoding (ebusd datatype.cpp port)
  - messages: structured definitions + ebusd-CSV ingest + decode
  - frame: on-the-wire telegram parsing, escaping, eBUS CRC-8
  - passive: raw wire bytes -> matched message -> named values (read-only)
  - transport/active/engine: enhanced-adapter I/O, active read/write, and a
    passive-first engine that caches decoded values and interleaves reads.
"""
from .active import (
    ActiveReader,
    ActiveWriter,
    ReadDriver,
    ReadError,
    WriteDriver,
    build_read,
    build_read_frame,
    build_write_frame,
)
from .codecs import Codec, decode, encode, make_codec
from .conditions import ConditionContext, context_from_scan, evaluate, evaluate_all
from .engine import EbusEngine, EngineStats, value_key
from .frame import Telegram, crc8, escape, parse_telegram, unescape
from .messages import FieldDef, MessageDef, parse_csv
from .passive import MessageRegistry, decode_raw
from .poll import PollScheduler
from .scan import Identification, ScanResult, decode_identification, decode_scan
from .transport import EnhDecoder, PassiveListener, listen

__all__ = [
    "ActiveReader",
    "ActiveWriter",
    "Codec",
    "ConditionContext",
    "EbusEngine",
    "EngineStats",
    "EnhDecoder",
    "FieldDef",
    "Identification",
    "MessageDef",
    "MessageRegistry",
    "PassiveListener",
    "PollScheduler",
    "ReadDriver",
    "ReadError",
    "ScanResult",
    "Telegram",
    "WriteDriver",
    "build_read",
    "build_read_frame",
    "build_write_frame",
    "context_from_scan",
    "crc8",
    "decode",
    "decode_identification",
    "decode_raw",
    "decode_scan",
    "encode",
    "escape",
    "evaluate",
    "evaluate_all",
    "listen",
    "make_codec",
    "parse_csv",
    "parse_telegram",
    "unescape",
    "value_key",
]
__version__ = "0.5.3"
