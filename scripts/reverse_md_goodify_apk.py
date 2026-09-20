#!/usr/bin/env python3
"""Targeted static extractor for the MD / Goodify Android integration.

The script intentionally reports only protocol-relevant metadata:
- Goodify endpoint references;
- methods touching Costanti$staticBeans.accessToken;
- Gson SerializedName mappings for itemDonazione.

It does not print generic string tables or unrelated embedded credentials.
Requires androguard and an already-extracted directory containing classes*.dex.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from loguru import logger
from androguard.core.dex import DEX

logger.remove()

GOODIFY_NEEDLES = ("/api/goodify/getdonation", "/api/goodify/purchasedonation")
TOKEN_NEEDLE = "Costanti$staticBeans;->accessToken"
DONATION_CLASS = "Lcom/agora/md/app/Beans/itemDonazione;"
SERIALIZED_NAME = "Lcom/google/gson/annotations/SerializedName;"


def method_lines(code) -> list[str]:
    if code is None:
        return []
    return [ins.get_output() for ins in code.get_bc().get_instructions()]


def print_method_hits(dex_path: Path, dex: DEX) -> None:
    for cls in dex.get_classes():
        for method in cls.get_methods():
            lines = method_lines(method.get_code())
            if not lines:
                continue
            goodify = sorted({needle for needle in GOODIFY_NEEDLES if any(needle in line for line in lines)})
            token = any(TOKEN_NEEDLE in line for line in lines)
            if not goodify and not token:
                continue
            tags = []
            if goodify:
                tags.append("goodify=" + ",".join(goodify))
            if token:
                tags.append("accessToken")
            print(f"METHOD {dex_path.name} {cls.get_name()}->{method.get_name()} {method.get_descriptor()} [{' '.join(tags)}]")


def print_donation_serialized_names(dex_path: Path, dex: DEX) -> None:
    for cls in dex.get_classes():
        if cls.get_name() != DONATION_CLASS:
            continue
        directory = cls.annotations_directory_item
        if not directory:
            return
        cm = cls.CM
        print(f"MODEL {dex_path.name} {DONATION_CLASS}")
        for field_annotation in directory.get_field_annotations():
            _, descriptor, field_name = cm.get_field(field_annotation.get_field_idx())
            annotation_set = cm.get_annotation_set_item(field_annotation.get_annotations_off())
            json_name = None
            for off in annotation_set.get_annotation_off_item():
                annotation = off.get_annotation_item().get_annotation()
                if cm.get_type(annotation.get_type_idx()) != SERIALIZED_NAME:
                    continue
                for element in annotation.get_elements():
                    if cm.get_string(element.get_name_idx()) == "value":
                        json_name = element.get_value().get_value()
            if json_name is not None:
                print(f"  {field_name}: {descriptor} <- {json_name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dex-dir", type=Path, default=Path("/tmp/md-apk"))
    args = parser.parse_args()

    dex_files = sorted(args.dex_dir.glob("classes*.dex"))
    if not dex_files:
        parser.error(f"no classes*.dex under {args.dex_dir}")

    for dex_path in dex_files:
        dex = DEX(dex_path.read_bytes())
        print_method_hits(dex_path, dex)
        print_donation_serialized_names(dex_path, dex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
