"""Device catalog: bundled definitions, user files, the CDN with its cache."""
import asyncio
import json
import os
import time

import pytest

from aioebus import catalog as cat
from aioebus.catalog import Catalog, bundled_file, match_cdn_file, resolve_bundled
from aioebus.scan import ScanResult


def _scan(ident, hw="0000", sw="0000", mf=0xB5):
    return ScanResult(mf, "Vaillant", ident, sw, hw, int(sw) if sw.isdigit() else None,
                      int(hw) if hw.isdigit() else None)


def run(coro):
    return asyncio.run(coro)


def test_bundled_by_ident_and_hw():
    assert resolve_bundled(_scan("V32", "9802")).file == "eloblock.json"
    hp2 = resolve_bundled(_scan("V32", "6004"))  # the heat pump behind a coupler
    assert (hp2.file, hp2.profile, hp2.model) == ("hmu.json", "V32/6004", "aroTHERM plus")
    assert resolve_bundled(_scan("HMU00", "5103")).profile is None
    assert resolve_bundled(_scan("CTLV3", "8004")).model == "VRC 720"
    assert resolve_bundled(_scan("CTLV3")).text_paths == ("vaillant/15.ctlv3",)
    assert resolve_bundled(_scan("V32", "1111")) is None  # unknown HW variant
    assert resolve_bundled(_scan("UNKNOWN")) is None


def test_both_heat_pumps_share_the_full_definition():
    c = Catalog()
    run(c.load({0x08: _scan("HMU00", "5103", "0902"), 0x18: _scan("V32", "6004", "0902")}))
    reads = c.device_messages("r")
    by_zz = {zz: {m.name for m in reads if m.zz == zz} for zz in ("08", "18")}
    assert by_zz["08"] == by_zz["18"]
    assert {"EnergyGeneratedHc", "PowerConsumptionHmu", "Status01"} <= by_zz["18"]


def test_broadcasts_are_reads_of_the_broadcast_circuit():
    c = Catalog()
    run(c.load({}))
    reads, writes = c.device_messages("r"), c.device_messages("w")
    assert "Outsidetemp" in {m.name for m in reads} and all(m.zz == "fe" for m in reads)
    assert not writes
    assert c.definition(0xFE).file == "broadcast.json"


def test_conditions_choose_the_variant_per_device(tmp_path):
    defs = [
        {"direction": "r1", "name": "Foo", "pbsb": "b509", "id": "0d0500",
         "conditions": ["id_sw>=200"], "fields": [{"name": "value", "type": "UCH"}]},
        {"direction": "r", "name": "Foo", "pbsb": "b509", "id": "0d0500",
         "fields": [{"name": "value", "type": "D2C"}]},
        {"direction": "r", "name": "New", "pbsb": "b509", "id": "0d0600",
         "conditions": ["id_sw>=999"], "fields": [{"name": "value", "type": "UCH"}]},
    ]
    (tmp_path / "15.test.json").write_text(json.dumps(defs), encoding="utf-8")
    c = Catalog(user_dir=tmp_path)
    run(c.load({0x15: _scan("TEST", sw="808"), 0x16: _scan("TEST", sw="100")}))
    hi = {m.name: m for m in c.messages(0x15)}
    assert hi["Foo"].fields[0].type == "UCH" and "New" not in hi and hi["Foo"].zz == "15"
    assert {m.name: m for m in c.messages(0x16)}["Foo"].fields[0].type == "D2C"
    assert c.definition(0x15).source == "user"


def test_user_file_by_ident_prefix_and_variant(tmp_path):
    for name in ("15.ctlv3.json", "08.hmu.json", "08.hmu.HW5103.json", "26.vr70.csv"):
        (tmp_path / name).write_text("[]", encoding="utf-8")
    assert cat.resolve_user_file(_scan("HMU00", "5103"), tmp_path).name == "08.hmu.HW5103.json"
    assert cat.resolve_user_file(_scan("HMU00", "9999"), tmp_path).name == "08.hmu.json"
    assert cat.resolve_user_file(_scan("VR70"), tmp_path).name == "26.vr70.csv"
    assert cat.resolve_user_file(_scan("NOPE"), tmp_path) is None


def test_user_csv_and_broken_user_file(tmp_path, caplog):
    (tmp_path / "26.vr70.csv").write_text(
        "type,circuit,level,name,comment,qq,zz,pbsb,id\n"
        "r,,,MixerTemp,,,,b509,0d0100,value,,D2C,,°C,\n", encoding="utf-8")
    (tmp_path / "27.broken.json").write_text("{not json", encoding="utf-8")
    c = Catalog(user_dir=tmp_path)
    run(c.load({0x26: _scan("VR70"), 0x27: _scan("BROKEN")}))
    assert [m.name for m in c.messages(0x26)] == ["MixerTemp"]
    assert c.messages(0x27) == [] and "27.broken.json" in caplog.text


def test_cdn_file_matches_like_ebusd():
    names = ["08.hmu.csv", "08.hmu.HW5103.csv", "15.ctlv3.csv", "15.ctlv2.csv",
             "0a.pmw.hwc.csv", "15.350.HW7102.csv", "26.vr_71.csv", "scan.csv"]
    assert match_cdn_file(names, 0x08, _scan("HMU00", "5103")) == "08.hmu.HW5103.csv"
    assert match_cdn_file(names, 0x08, _scan("HMU00", "9999")) == "08.hmu.csv"
    assert match_cdn_file(names, 0x15, _scan("CTLV3")) == "15.ctlv3.csv"
    assert match_cdn_file(names, 0x0A, _scan("PMW01")) == "0a.pmw.hwc.csv"
    assert match_cdn_file(names, 0x15, _scan("35000", "0000")) is None  # HW7102 only
    assert match_cdn_file(names, 0x18, _scan("HMU00", "5103")) is None  # other address


HDR = "type,circuit,level,name,comment,qq,zz,pbsb,id,*name,part,type,divisor/values,unit,comment\n"
VR70_CSV = HDR + "r,,,MixerTemp,,,,b509,0d0100,value,,D2C,,°C,\n!load,vr70.inc,,,\n"
VR70_INC = HDR + "r,,,PumpState,,,,b509,0d0200,value,,UCH,0=off;1=on,,\n"


class FakeCdn:
    def __init__(self, files):
        self.files, self.calls = files, []

    async def __call__(self, url):
        self.calls.append(url)
        return self.files.get(url)


def _cdn(**extra):
    base = "https://ebus.github.io/en/vaillant/"
    return FakeCdn({base: json.dumps("26.vr_70.csv\n52.vr_70.csv\nscan.csv"),
                    base + "52.vr_70.csv": VR70_CSV, base + "vr70.inc": VR70_INC, **extra})


def test_unknown_device_comes_from_the_cdn_and_is_cached(tmp_path):
    fetch = _cdn()
    c = Catalog(cache_dir=tmp_path, fetch=fetch)
    run(c.load({0x52: _scan("VR_70")}))
    assert {m.name for m in c.messages(0x52)} == {"MixerTemp", "PumpState"}  # with the include
    d = c.definition(0x52)
    assert (d.source, d.file, d.text_paths) == ("cdn", "vaillant/52.vr_70.csv",
                                                ("vaillant/52.vr_70",))
    assert (tmp_path / "en" / "vaillant" / "52.vr_70.csv").read_text(encoding="utf-8") == VR70_CSV
    fetched = len(fetch.calls)
    c2 = Catalog(cache_dir=tmp_path, fetch=fetch)  # the next start: from the cache
    run(c2.load({0x52: _scan("VR_70")}))
    assert len(fetch.calls) == fetched
    assert {m.name for m in c2.messages(0x52)} == {"MixerTemp", "PumpState"}


def test_stale_cache_is_refreshed_and_stands_in_offline(tmp_path):
    run(Catalog(cache_dir=tmp_path, fetch=_cdn()).load({0x52: _scan("VR_70")}))
    old = time.time() - cat.CACHE_MAX_AGE - 10
    for p in (tmp_path / "en" / "vaillant").iterdir():
        os.utime(p, (old, old))
    offline = FakeCdn({})
    c = Catalog(cache_dir=tmp_path, fetch=offline)
    run(c.load({0x52: _scan("VR_70")}))
    assert offline.calls  # tried to refresh
    assert {m.name for m in c.messages(0x52)} == {"MixerTemp", "PumpState"}  # stale copy


def test_bundled_wins_and_no_definition_means_no_messages(tmp_path):
    fetch = _cdn()
    c = Catalog(cache_dir=tmp_path, fetch=fetch)
    run(c.load({0x15: _scan("CTLV3", "8004"), 0x99: _scan("XYZ", mf=0x77)}))
    assert c.definition(0x15).source == "bundled"
    assert c.definition(0x99) is None and c.messages(0x99) == []
    assert not [u for u in fetch.calls if "ctlv3" in u]  # never fetched for a bundled one


def test_zones_and_bounds_from_the_bundled_catalog():
    c = Catalog()
    run(c.load({0x15: _scan("CTLV3", "8004", "808"), 0x08: _scan("HMU00", "5103")}))
    zones = {z.n: z for z in c.zones()}
    assert set(zones) == {1, 2, 3}
    z1 = zones[1]
    assert (z1.name, z1.zz, z1.target_write.name, z1.current.name, z1.mode_read.name) == (
        "Heizkreis 1", "15", "Z1DayTemp", "Z1RoomTemp", "Z1OpMode")
    assert z1.target_bounds == (5.0, 30.0, 0.5)
    assert c.bounds(0x15, "HwcLockTime") == (0.0, 120.0, 10.0)
    assert c.bounds(0x08, "Z1DayTemp") is None


def test_answers_come_from_memory_after_load(tmp_path, monkeypatch):
    (tmp_path / "26.vr71x.json").write_text("[]", encoding="utf-8")
    c = Catalog(user_dir=tmp_path)
    run(c.load({0x15: _scan("CTLV3", "8004", "808"), 0x26: _scan("VR71X")}))

    def no_io(*_a, **_k):
        raise AssertionError("file I/O after load")

    from pathlib import Path
    monkeypatch.setattr(Path, "read_text", no_io)
    monkeypatch.setattr(Path, "glob", no_io)
    c.all_messages()
    c.device_messages("w")
    c.zones()


def test_hmu_energy_counters_decode_kwh():
    defs = {m.name: m for m in bundled_file("hmu.json")}
    energy = {n for n, m in defs.items() if n.startswith("Energy") and m.pbsb == "b516"}
    assert energy == {f"Energy{s}{u}" for s in ("Environment", "Generated", "Electric")
                      for u in ("Hc", "Hwc", "Cool")}
    assert defs["EnergyEnvironmentHc"].decode_labeled(
        "1000ffff42030000", "00000142035e35372f9f4b") == {"EnergyEnvironmentHc": 20864.622}
    reg = [(m.pbsb, m.msgid, m.direction, tuple(m.conditions or ())) for m in defs.values()]
    assert len(reg) == len(set(reg))  # no register defined twice under other names


@pytest.mark.parametrize("name", cat.bundled_files())
def test_every_bundled_file_loads(name):
    assert isinstance(bundled_file(name), tuple)


def test_user_files_are_read_again_on_each_load(tmp_path):
    dev = {0x26: _scan("VR71X")}
    c = Catalog(user_dir=tmp_path)
    run(c.load(dev))
    assert c.messages(0x26) == [] and c.definition(0x26) is None
    (tmp_path / "26.vr71x.json").write_text(
        '[{"direction": "r", "name": "Foo", "pbsb": "b509", "id": "0d0100",'
        ' "fields": [{"name": "value", "type": "UCH"}]}]', encoding="utf-8")
    run(c.load(dev))  # the next setup (e.g. after a reload)
    assert [m.name for m in c.messages(0x26)] == ["Foo"]


def test_names_from_the_cdn_never_leave_the_cache(tmp_path):
    base = "https://ebus.github.io/en/vaillant/"
    evil_csv = VR70_CSV.replace("!load,vr70.inc", "!load,../../../outside.inc")
    fetch = FakeCdn({base: json.dumps("52.vr_70.csv\n52.a/../../x.csv"),
                     base + "52.vr_70.csv": evil_csv})
    c = Catalog(cache_dir=tmp_path / "cache", fetch=fetch)
    run(c.load({0x52: _scan("VR_70")}))
    assert {m.name for m in c.messages(0x52)} == {"MixerTemp"}  # the include was refused
    assert not any(".." in u for u in fetch.calls)
    assert match_cdn_file(["52.a/b.csv"], 0x52, _scan("A/B")) is None
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == [".listing",
                                                                           "52.vr_70.csv"]


BAI = HDR + "r,,,Base,,,,b509,0d0100,value,,UCH,,,\n" \
    "[Scan_id_product='0010043897']!load,bai.a.inc,,,\n" \
    "[id_hw=7603]!load,bai.a.inc,,,\n" \
    "[id_hw=7401]!load,bai.b.inc,,,\n" \
    "!load,bai.b.inc,,,\n"
INC_A = HDR + "r,,,OnlyA,,,,b509,0d0200,value,,UCH,,,\n!load,common.inc,,,\n"
INC_B = HDR + "r,,,OnlyB,,,,b509,0d0300,value,,UCH,,,\n"
COMMON = HDR + "r,,,Common,,,,b509,0d0400,value,,UCH,,,\n"


def _bai_cdn():
    base = "https://ebus.github.io/en/vaillant/"
    return FakeCdn({base: json.dumps("08.bai.csv"), base + "08.bai.csv": BAI,
                    base + "bai.a.inc": INC_A, base + "bai.b.inc": INC_B,
                    base + "common.inc": COMMON})


def test_conditional_includes_pick_the_product_part(tmp_path):
    from aioebus.scan import Identification
    c = Catalog(cache_dir=tmp_path, fetch=_bai_cdn())
    run(c.load({0x08: _scan("BAI00", "7603")}))  # by HW, and the include's own include
    assert {m.name for m in c.messages(0x08)} == {"Base", "OnlyA", "Common"}
    c = Catalog(cache_dir=tmp_path / "b", fetch=_bai_cdn())
    ident = Identification("0010043897", "x", 2020, 1)
    run(c.load({0x08: _scan("BAI00", "1111")}, {0x08: ident}))  # by product number
    assert {m.name for m in c.messages(0x08)} == {"Base", "OnlyA", "Common"}
    c = Catalog(cache_dir=tmp_path / "c", fetch=_bai_cdn())
    run(c.load({0x08: _scan("BAI00", "1111")}))  # nothing matches: the default
    assert {m.name for m in c.messages(0x08)} == {"Base", "OnlyB"}


def test_user_csv_follows_its_includes(tmp_path):
    (tmp_path / "08.bai.csv").write_text(BAI, encoding="utf-8")
    (tmp_path / "bai.a.inc").write_text(INC_A, encoding="utf-8")
    c = Catalog(user_dir=tmp_path)
    run(c.load({0x08: _scan("BAI00", "7603")}))
    assert {m.name for m in c.messages(0x08)} == {"Base", "OnlyA"}  # common.inc absent


def test_user_file_variant_rules(tmp_path):
    for name in ("08.bai.csv", "08.bai.SW0500.HW7603.csv", "08.bai.SW0400.csv"):
        (tmp_path / name).write_text(HDR, encoding="utf-8")
    assert cat.resolve_user_file(_scan("BAI00", "7603", "0500"), tmp_path).name == (
        "08.bai.SW0500.HW7603.csv")
    assert cat.resolve_user_file(_scan("BAI00", "7603", "0600"), tmp_path).name == "08.bai.csv"


class Broken(FakeCdn):
    async def __call__(self, url):
        self.calls.append(url)
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")


def test_a_broken_cdn_answer_costs_only_its_device(tmp_path, caplog):
    c = Catalog(cache_dir=tmp_path, fetch=Broken({}))
    run(c.load({0x52: _scan("VR_70"), 0x15: _scan("CTLV3", "8004")}))
    assert c.definition(0x15).source == "bundled" and c.messages(0x15)
    assert c.definition(0x52) is None


def test_no_html_in_the_cache_and_one_listing_per_load(tmp_path):
    base = "https://ebus.github.io/en/vaillant/"
    fetch = FakeCdn({base: json.dumps("52.vr_70.csv\n53.vr_70.csv"),
                     base + "52.vr_70.csv": "<html>login</html>",
                     base + "53.vr_70.csv": VR70_CSV})
    c = Catalog(cache_dir=tmp_path, fetch=fetch)
    run(c.load({0x52: _scan("VR_70"), 0x53: _scan("VR_70")}))
    assert c.definition(0x52) is None  # a portal page is no definition
    assert not (tmp_path / "en" / "vaillant" / "52.vr_70.csv").exists()
    assert fetch.calls.count(base) == 1  # the listing once, not per device


def test_listing_is_fetched_once_per_load_without_a_cache():
    base = "https://ebus.github.io/en/vaillant/"
    fetch = FakeCdn({base: json.dumps("52.vr_70.csv\n53.vr_70.csv"),
                     base + "52.vr_70.csv": VR70_CSV, base + "53.vr_70.csv": VR70_CSV})
    run(Catalog(fetch=fetch).load({0x52: _scan("VR_70"), 0x53: _scan("VR_70")}))
    assert fetch.calls.count(base) == 1


CTL_EN = HDR + "r,,,Hc1HeatCurve,heat curve,,,b524,020002000f00,ign,,IGN:4,,,,value,,EXP,,,\n"
CTL_DE = HDR + "r,,,Hc1HeatCurve,Heizkurve,,,b524,020002000f00,ign,,IGN:4,,,,value,,EXP,,,\n"


class TextCdn(FakeCdn):
    """Serves the controller's text files; '' (not there) for French."""

    async def __call__(self, url):
        self.calls.append(url)
        if "/fr/" in url:
            return ""
        return self.files.get(url)


def test_display_texts_per_language_are_cached(tmp_path):
    files = {"https://ebus.github.io/en/vaillant/15.ctlv3.csv": CTL_EN,
             "https://ebus.github.io/de/vaillant/15.ctlv3.csv": CTL_DE}
    fetch = TextCdn(files)
    c = Catalog(cache_dir=tmp_path, fetch=fetch)
    run(c.load({0x15: _scan("CTLV3", "8004", "808")}))
    assert "vaillant/15.ctlv3" in c.text_paths
    texts = run(c.load_texts(["en", "de", "fr"]))
    curve = next(m for m in texts.messages if m.name == "Hc1HeatCurve" and m.is_read)
    assert curve.tr == {"en": "heat curve", "de": "Heizkurve"}
    assert texts.languages == {"en", "de"}
    assert not texts.complete  # the broadcast texts were not reachable here
    assert all(m.tr is None for m in c.all_messages())  # the catalog's own stay clean
    before = len(fetch.calls)
    run(c.load_texts(["de"]))  # from the cache now
    assert not [u for u in fetch.calls[before:] if "15.ctlv3" in u]


def test_a_language_the_cdn_lacks_is_final():
    c = Catalog(fetch=TextCdn({}))
    run(c.load({}))
    texts = run(c.load_texts(["fr"]))
    assert texts.complete and texts.languages == frozenset()


def test_stale_texts_offline_are_not_complete(tmp_path):
    files = {"https://ebus.github.io/en/vaillant/15.ctlv3.csv": CTL_EN,
             "https://ebus.github.io/en/broadcast.csv": HDR,
             "https://ebus.github.io/en/vaillant/broadcast.csv": HDR}
    c = Catalog(cache_dir=tmp_path, fetch=TextCdn(files))
    run(c.load({0x15: _scan("CTLV3", "8004", "808")}))
    assert run(c.load_texts(["en"])).complete
    old = time.time() - cat.CACHE_MAX_AGE - 10
    for p in (tmp_path / "en").rglob("*.csv"):
        os.utime(p, (old, old))
    c2 = Catalog(cache_dir=tmp_path, fetch=FakeCdn({}))  # offline now
    run(c2.load({0x15: _scan("CTLV3", "8004", "808")}))
    texts = run(c2.load_texts(["en"]))
    curve = next(m for m in texts.messages if m.name == "Hc1HeatCurve" and m.is_read)
    assert curve.tr == {"en": "heat curve"}  # the stale copy still names it
    assert not texts.complete  # but the next start tries again


class SlowFirst(FakeCdn):
    async def __call__(self, url):
        self.calls.append(url)
        await asyncio.sleep(0.15)  # every text file is slow
        return self.files.get(url)


def test_texts_of_one_language_are_fetched_together():
    files = {"https://ebus.github.io/en/vaillant/15.ctlv3.csv": CTL_EN}
    fetch = SlowFirst(files)
    c = Catalog(fetch=fetch)
    run(c.load({0x15: _scan("CTLV3", "8004", "808")}))

    async def timed():
        start = asyncio.get_running_loop().time()
        await c.load_texts(["en"])
        return asyncio.get_running_loop().time() - start, len(fetch.calls)

    elapsed, calls = run(timed())
    assert len(c.text_paths) >= 3 and calls == len(c.text_paths)
    assert elapsed < 0.3  # together, not 3 x 0.15 s one after another
