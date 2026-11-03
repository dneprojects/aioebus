"""aioebus -- standalone async eBUS library (no ebusd daemon).

  - link: the TCP connection to an enhanced adapter, and a liveness probe
  - engine: one connection, passive decoding plus interleaved active reads and
    writes, a value cache and a poll scheduler (poll)
  - discovery: which devices are on the bus and what they are (scan, identification)
  - catalog: each device's definitions (bundled, user files, ebusd CSVs from the CDN)
  - values: what a message's values are (keys, labels, numeric, the control a write allows)
  - messages / codecs: message definitions, ebusd-CSV ingest, datatype decoding
  - frame / passive / transport / active: telegrams, CRC, the adapter protocol and
    the transaction state machine
  - translations: stable keys for display texts
"""
from .active import (
    DEFAULT_SOURCE,
    ReadDriver,
    ReadError,
    WriteDriver,
    WriteUncertain,
    build_read_frame,
    build_write_frame,
)
from .catalog import BROADCAST_SCAN, Catalog, ClimateZone, Definition, Texts
from .codecs import Codec, decode, encode, make_codec
from .conditions import ConditionContext, context_from_scan, evaluate, evaluate_all
from .discovery import BusSilent, Device, Discovery, discover, identify_later, remember
from .engine import EbusEngine, EngineStats
from .frame import BROADCAST, Telegram, crc8, escape, is_master, is_slave, parse_telegram, unescape
from .link import Link, connect, probe
from .messages import FieldDef, MessageDef, label_of, parse_csv
from .passive import MessageRegistry, decode_raw
from .poll import PollScheduler
from .scan import MANUFACTURERS, Identification, ScanResult, decode_identification, decode_scan
from .translations import slug, translation_key
from .values import (
    Value,
    closes_period,
    control_of,
    has_numeric_value,
    is_float_type,
    is_number_type,
    value_key,
    values_of,
)

__all__ = [
    "BROADCAST",
    "BROADCAST_SCAN",
    "DEFAULT_SOURCE",
    "MANUFACTURERS",
    "BusSilent",
    "Catalog",
    "ClimateZone",
    "Codec",
    "ConditionContext",
    "Definition",
    "Device",
    "Discovery",
    "EbusEngine",
    "EngineStats",
    "FieldDef",
    "Identification",
    "Link",
    "MessageDef",
    "MessageRegistry",
    "PollScheduler",
    "ReadDriver",
    "ReadError",
    "ScanResult",
    "Telegram",
    "Texts",
    "Value",
    "WriteDriver",
    "WriteUncertain",
    "build_read_frame",
    "build_write_frame",
    "closes_period",
    "connect",
    "context_from_scan",
    "control_of",
    "crc8",
    "decode",
    "decode_identification",
    "decode_raw",
    "decode_scan",
    "discover",
    "encode",
    "escape",
    "evaluate",
    "evaluate_all",
    "has_numeric_value",
    "identify_later",
    "is_float_type",
    "is_master",
    "is_number_type",
    "is_slave",
    "label_of",
    "make_codec",
    "parse_csv",
    "parse_telegram",
    "probe",
    "remember",
    "slug",
    "translation_key",
    "unescape",
    "value_key",
    "values_of",
]
__version__ = "0.7.4"
