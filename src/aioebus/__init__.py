"""aioebus -- standalone async eBUS library (work in progress).

Pure, offline eBUS decoding validated against real captured telegrams:
  - codecs: datatype decoding (ebusd datatype.cpp port)
  - messages: structured definitions + ebusd-CSV ingest + decode
  - frame: on-the-wire telegram parsing, escaping, eBUS CRC-8
  - passive: raw wire bytes -> matched message -> named values (read-only)

No bus I/O yet (the enhanced-adapter transport is next) -- see README.
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
from .engine import EbusEngine, value_key
from .frame import Telegram, crc8, escape, parse_telegram, unescape
from .messages import FieldDef, MessageDef, parse_csv
from .passive import MessageRegistry, decode_raw
from .poll import PollScheduler
from .scan import ScanResult, decode_scan
from .transport import EnhDecoder, PassiveListener, listen

__all__ = [
    "Codec", "decode", "encode", "make_codec",
    "FieldDef", "MessageDef", "parse_csv",
    "Telegram", "crc8", "escape", "unescape", "parse_telegram",
    "MessageRegistry", "decode_raw",
    "EnhDecoder", "PassiveListener", "listen",
    "build_read", "build_read_frame", "build_write_frame", "PollScheduler",
    "ActiveReader", "ActiveWriter", "ReadDriver", "WriteDriver", "ReadError",
    "ScanResult", "decode_scan",
    "ConditionContext", "context_from_scan", "evaluate", "evaluate_all",
    "EbusEngine", "value_key",
]
__version__ = "0.0.1"
