"""The developer tools: a capture replays through the real engine."""
import asyncio
import importlib.util
import sqlite3
import sys
from pathlib import Path

from aioebus.engine import EbusEngine
from aioebus.messages import parse_csv
from aioebus.passive import MessageRegistry

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def _tool(name: str):
    spec = importlib.util.spec_from_file_location(f"tool_{name}", TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FLOW = "3138b509030d1800740003910100bb00"  # FlowTemp 25.0625 from 0x38
CSV = ("type,circuit,level,name,comment,qq,zz,pbsb,id\n"
       "r1,,,FlowTemp,,,38,b509,0d1800,value,,D2C,,,,ign,,IGN:1,,,\n")


def test_replay_decodes_through_the_engine_and_skips_bad_rows():
    replay = _tool("replay")
    engine = EbusEngine(registry=MessageRegistry(parse_csv(CSV)))
    asyncio.run(replay.replay(engine, iter(["zz", FLOW, "3138b5", FLOW])))
    assert engine.cache == {("38", "FlowTemp", "FlowTemp"): 25.0625}
    assert engine.stats.telegrams == 2  # the truncated frame is no telegram


def test_replay_main_reports_a_capture(tmp_path, capsys):
    db = sqlite3.connect(tmp_path / "t.db")
    db.execute("CREATE TABLE telegram (ts, status, zz, pb, sb, s_dd, raw)")
    db.execute("INSERT INTO telegram VALUES (1, 'ok', 99, 0, 0, '', ?)", (FLOW,))
    db.commit()
    db.close()
    assert _tool("replay").main([str(tmp_path / "t.db"), "--scan", "38"]) == 0
    assert "devices scanned: 0" in capsys.readouterr().out


def test_frame_validation_of_an_empty_capture_fails(tmp_path, capsys):
    db = sqlite3.connect(tmp_path / "e.db")
    db.execute("CREATE TABLE telegram (type, qq, zz, pb, sb, nn, dd, crc, s_dd, s_crc, raw,"
               " status)")
    db.commit()
    db.close()
    assert _tool("validate_frame_db").main(str(tmp_path / "e.db")) == 1
    assert "nothing to check" in capsys.readouterr().out
    assert sys.modules.get("tool_replay") is None  # tools load without registering
