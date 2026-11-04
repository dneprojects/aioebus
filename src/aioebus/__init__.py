"""aioebus -- standalone async eBUS library (no ebusd daemon).

  - link: the TCP connection to an enhanced adapter, and a liveness probe
  - engine: one connection, passive decoding plus interleaved active reads and
    writes (a write read back), a value cache and a poll scheduler (poll)
  - runner: keeps an engine connected, reconnecting when the link ends
  - discovery: which devices are on the bus (scan: the 0704 answer and the
    Vaillant identification)
  - catalog: each device's definitions (bundled, user files, ebusd CSVs from
    the CDN, see cdn), with conditions evaluated (conditions)
  - values: what a message's values are (keys, labels, the control a write allows)
  - messages / codecs: message definitions, ebusd-CSV ingest, datatypes;
    convert: the native JSON format
  - frame / passive / transport / active: telegrams, CRC, the adapter protocol and
    the transaction state machine
  - translations: stable keys for display texts, and the texts per language

Everything listed in ``__all__`` is the public API; the submodule paths it comes
from are public as well.
"""
from .active import (
    DEFAULT_SOURCE,
    ReadDriver,
    ReadError,
    WriteDriver,
    WriteUncertain,
    build_master_frame,
    build_read_frame,
)
from .catalog import (
    BROADCAST_SCAN,
    Catalog,
    ClimateZone,
    Definition,
    Texts,
    bundled_file,
    bundled_file_of,
    bundled_files,
    resolve_bundled,
)
from .cdn import Fetch
from .codecs import (
    Codec,
    decode,
    encode,
    has_numeric_value,
    is_float,
    is_number,
    is_numeric,
    make_codec,
    value_range,
)
from .conditions import ConditionContext, context_from_scan, evaluate, evaluate_all
from .discovery import (
    BASE_ADDRESSES,
    BusSilent,
    Device,
    Discovery,
    discover,
    identify_later,
    remember,
)
from .engine import EbusEngine, EngineStats, WriteOutcome
from .frame import BROADCAST, Telegram, crc8, escape, is_master, is_slave, parse_telegram, unescape
from .link import Link, connect, probe
from .messages import FieldDef, MessageDef, label_of, parse_csv
from .passive import MessageRegistry, decode_raw
from .poll import PollScheduler
from .runner import EngineRunner
from .scan import MANUFACTURERS, Identification, ScanResult, decode_identification, decode_scan
from .translations import attach_translations, slug, translation_key
from .values import (
    CacheKey,
    MessageKey,
    Value,
    closes_period,
    control_of,
    message_key,
    value_key,
    values_of,
)

__all__ = [
    "BASE_ADDRESSES",
    "BROADCAST",
    "BROADCAST_SCAN",
    "DEFAULT_SOURCE",
    "MANUFACTURERS",
    "BusSilent",
    "CacheKey",
    "Catalog",
    "ClimateZone",
    "Codec",
    "ConditionContext",
    "Definition",
    "Device",
    "Discovery",
    "EbusEngine",
    "EngineRunner",
    "EngineStats",
    "Fetch",
    "FieldDef",
    "Identification",
    "Link",
    "MessageDef",
    "MessageKey",
    "MessageRegistry",
    "PollScheduler",
    "ReadDriver",
    "ReadError",
    "ScanResult",
    "Telegram",
    "Texts",
    "Value",
    "WriteDriver",
    "WriteOutcome",
    "WriteUncertain",
    "attach_translations",
    "build_master_frame",
    "build_read_frame",
    "bundled_file",
    "bundled_file_of",
    "bundled_files",
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
    "is_float",
    "is_master",
    "is_number",
    "is_numeric",
    "is_slave",
    "label_of",
    "make_codec",
    "message_key",
    "parse_csv",
    "parse_telegram",
    "probe",
    "remember",
    "resolve_bundled",
    "slug",
    "translation_key",
    "unescape",
    "value_key",
    "value_range",
    "values_of",
]
__version__ = "0.7.6"
