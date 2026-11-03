"""Native structured format (JSON) + CSV->structured converter.

This is the "something structured" alternative to ebusd-CSV for OUR OWN curated
definitions: a nested, schema-able, comment-tolerant-when-YAML format with real
field objects instead of positional columns. JSON here keeps the lib dependency
free; swap `json` for `yaml` if you want comments in authored files.

`to_dicts` / `from_dicts` round-trip MessageDef <-> plain dicts; `csv_to_json`
converts an ebusd-CSV file into the native format (we convert what we curate,
not the whole upstream library).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .messages import FieldDef, MessageDef, parse_csv, usable_fields


def to_dicts(msgs: list[MessageDef]) -> list[dict]:
    out = []
    for m in msgs:
        fields = []
        for f in m.fields:
            fd: dict[str, Any] = {"name": f.name, "type": f.type}
            if f.part:
                fd["part"] = f.part
            if f.divisor is not None:
                fd["divisor"] = f.divisor
            if f.values:
                fd["values"] = {str(k): v for k, v in f.values.items()}
            if f.unit:
                fd["unit"] = f.unit
            if f.comment:
                fd["comment"] = f.comment
            if f.tr:
                fd["tr"] = f.tr
            if f.const is not None:
                fd["const"] = f.const
                if f.verify:
                    fd["verify"] = True
            fields.append(fd)
        out.append(
            {
                "direction": m.direction, "circuit": m.circuit, "name": m.name,
                "pbsb": m.pbsb, "id": m.msgid,
                **({"qq": m.qq} if m.qq else {}),
                **({"zz": m.zz} if m.zz else {}),
                **({"level": m.level} if m.level else {}),
                **({"comment": m.comment} if m.comment else {}),
                **({"conditions": m.conditions} if m.conditions else {}),
                **({"tr": m.tr} if m.tr else {}),
                "fields": fields,
            }
        )
    return out


def from_dicts(data: list[dict]) -> list[MessageDef]:
    msgs = []
    for m in data:
        fields = [
            FieldDef(
                name=f.get("name", ""), part=f.get("part", ""), type=f["type"],
                divisor=f.get("divisor"),
                values={int(k): v for k, v in f["values"].items()} if f.get("values") else None,
                unit=f.get("unit", ""), comment=f.get("comment", ""), tr=f.get("tr"),
                const=f.get("const"), verify=bool(f.get("verify")),
            )
            for f in m.get("fields", [])
        ]
        if (usable := usable_fields(m["name"], fields)) is None:
            continue
        fields = usable
        msgs.append(
            MessageDef(
                direction=m["direction"], circuit=m.get("circuit", ""),
                level=m.get("level", ""), name=m["name"], comment=m.get("comment", ""),
                qq=m.get("qq", ""), zz=m.get("zz", ""), pbsb=m["pbsb"],
                msgid=m["id"], fields=fields, conditions=m.get("conditions"),
                tr=m.get("tr"),
            )
        )
    return msgs


def csv_to_json(csv_text: str) -> str:
    return json.dumps(to_dicts(parse_csv(csv_text)), indent=2, ensure_ascii=False)


def _msg_key(m: MessageDef) -> tuple[str, str, str, str]:
    return (m.direction, m.pbsb, m.msgid, m.name)


def attach_translations(
    defs: list[MessageDef], by_lang: dict[str, list[MessageDef]]
) -> list[MessageDef]:
    """Set ``tr`` on each def from the same message in every provided language.

    ``by_lang`` maps a language code to that language's parsed definitions of the
    *same device* (the per-language CSVs are structurally identical). Messages are
    matched by ``(direction, pbsb, id, name)`` and fields by position (guarded by
    the field name), so order differences do not matter. Only non-empty comments
    are stored; a message/field with no translation anywhere keeps ``tr=None``.
    Mutates and returns ``defs``."""
    # conditional variants share a key: each takes its own variant's texts
    # (same conditions), else the first one's
    index: dict[str, dict] = {}
    for lang, msgs in by_lang.items():
        for m in msgs:
            idx = index.setdefault(lang, {})
            idx.setdefault(_msg_key(m), m)
            idx.setdefault((*_msg_key(m), tuple(m.conditions or ())), m)
    for m in defs:
        key = (*_msg_key(m), tuple(m.conditions or ()))
        m_tr = {lang: o.comment for lang, idx in index.items()
                if (o := idx.get(key)) is not None and o.comment}
        m.tr = m_tr or None
        for i, f in enumerate(m.fields):
            f_tr = {
                lang: o.fields[i].comment
                for lang, idx in index.items()
                if (o := idx.get(key)) is not None
                and i < len(o.fields) and o.fields[i].name == f.name and o.fields[i].comment
            }
            f.tr = f_tr or None
    return defs


def convert_dir(src_dir: Path, out_dir: Path) -> int:
    """Batch-convert every ebusd CSV in a directory to native JSON (same base
    name). Used to generate device definitions locally from your own copy of
    ebusd-configuration -- the GPL-3.0 derivative stays on your machine."""
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for csv_file in sorted(src_dir.glob("*.csv")):
        text = csv_file.read_text(encoding="utf-8", errors="replace")
        (out_dir / (csv_file.stem + ".json")).write_text(
            csv_to_json(text) + "\n", encoding="utf-8")
        n += 1
    return n


if __name__ == "__main__":  # python -m aioebus.convert <in.csv [out.json]> | <in_dir out_dir>
    src = Path(sys.argv[1])
    if src.is_dir():
        if len(sys.argv) < 3:
            raise SystemExit("usage: python -m aioebus.convert <in_dir> <out_dir>")
        count = convert_dir(src, Path(sys.argv[2]))
        print(f"converted {count} file(s) to {sys.argv[2]}")
    else:
        out = csv_to_json(src.read_text(encoding="utf-8"))
        if len(sys.argv) > 2:
            Path(sys.argv[2]).write_text(out, encoding="utf-8")
        else:
            print(out)
