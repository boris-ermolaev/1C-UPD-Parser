#!/usr/bin/env python3
"""
Парсер УПД для командной строки — парсинг русских УПД документов из командной строки.

Примеры использования:
    python cli.py <path_to_upd.pdf>
    python cli.py <path_to_upd.pdf> --output result.json
    python cli.py <path_to_upd.pdf> --compact
    python cli.py <path_to_upd.pdf> --summary
"""

import argparse
import json
import sys
from pathlib import Path

from upd_parser import parse_upd


def main():
    # Инициализация парсера аргументов командной строки
    parser = argparse.ArgumentParser(
        description="Парсинг русских УПД (Универсальный Передаточный Документ) PDF-файлов в структурированный JSON."
    )
    parser.add_argument("pdf", help="Путь к PDF файлу УПД")
    parser.add_argument("-o", "--output", help="Путь выходного JSON файла (по умолчанию: stdout)")
    parser.add_argument("--compact", action="store_true", help="Компактный вывод JSON (без отступов)")
    parser.add_argument("--summary", action="store_true", help="Вывести удобочитаемое резюме вместо JSON")
    args = parser.parse_args()

    # Проверка существования файла PDF
    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"Error: file not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    # Парсинг УПД документа
    doc = parse_upd(pdf_path)

    # Вывод резюме или JSON в зависимости от аргументов
    if args.summary:
        _print_summary(doc)
    else:
        # Определение параметров форматирования JSON
        indent = None if args.compact else 2
        data = doc.to_dict()
        json_str = json.dumps(data, ensure_ascii=False, indent=indent)

        # Запись в файл или вывод на экран
        if args.output:
            Path(args.output).write_text(json_str, encoding="utf-8")
            print(f"Written to {args.output}")
        else:
            print(json_str)

    # Вывод статуса валидации на stderr
    v = doc.validation
    if not v.is_valid:
        print(f"\n  VALIDATION FAILED ({len(v.errors)} error(s))", file=sys.stderr)
        for e in v.errors:
            print(f"    ERROR: {e}", file=sys.stderr)
    for w in v.warnings:
        print(f"    WARNING: {w}", file=sys.stderr)
    if v.is_valid and not v.warnings:
        print(f"\n  VALIDATION PASSED", file=sys.stderr)


def _print_summary(doc):
    """Вывести удобочитаемое резюме проанализированного документа."""
    print(f"{'=' * 60}")
    print(f"  УПД (Status {doc.status}) #{doc.invoice_number}")
    print(f"  Date: {doc.invoice_date} ({doc.invoice_date_iso})")
    print(f"  Pages: {doc.page_count}")
    print(f"  Generator: {doc.generator}")
    print(f"{'=' * 60}")
    print()
    print(f"  Seller:  {doc.seller.name}")
    print(f"           INN {doc.seller.inn} / KPP {doc.seller.kpp}")
    print(f"           {doc.seller.address}")
    print()
    print(f"  Buyer:   {doc.buyer.name}")
    print(f"           INN {doc.buyer.inn} / KPP {doc.buyer.kpp}")
    print(f"           {doc.buyer.address}")
    print()
    print(f"  Currency: {doc.currency} ({doc.currency_code})")
    print()

    # Режим НДС
    vat = doc.vat
    mode_labels = {
        "none": "NO VAT (БезНДС / НДСНеВыделять)",
        "ontop": "VAT ON TOP (НДС сверху / СуммаВключаетНДС=false)",
        "included": "VAT INCLUDED (НДС в сумме / СуммаВключаетНДС=true)",
    }
    mode_label = mode_labels.get(vat.vat_mode, vat.vat_mode)
    rates_str = ", ".join(f"{r}%" for r in vat.vat_rates) if vat.vat_rates else "none"
    print(f"  VAT Mode:  {mode_label}")
    print(f"  VAT Rates: {rates_str}")
    print(f"  Confidence: {vat.detection_confidence} — {vat.detection_reason}")
    print()

    # Строки товаров со столбцом НДС
    has_vat = vat.vat_mode != "none"
    if has_vat:
        print(f"  {'#':<4} {'Code':<14} {'Description':<34} {'Qty':>5} {'Price':>11} {'VAT%':>5} {'VAT':>10} {'Total':>11}")
        print(f"  {'-'*4} {'-'*14} {'-'*34} {'-'*5} {'-'*11} {'-'*5} {'-'*10} {'-'*11}")
        for item in doc.items:
            # Усечение названия товара до максимальной длины
            name = item.name[:34] if len(item.name) > 34 else item.name
            rate_str = f"{item.vat_rate_percent}%" if item.vat_rate_percent else "--"
            print(
                f"  {item.row_number:<4} {item.product_code:<14} {name:<34} "
                f"{float(item.quantity):>5.0f} {float(item.unit_price):>11,.2f} "
                f"{rate_str:>5} {float(item.vat_amount):>10,.2f} {float(item.total):>11,.2f}"
            )
        print(f"  {'-'*4} {'-'*14} {'-'*34} {'-'*5} {'-'*11} {'-'*5} {'-'*10} {'-'*11}")
    else:
        print(f"  {'#':<4} {'Code':<14} {'Description':<40} {'Qty':>6} {'Price':>12} {'Total':>12}")
        print(f"  {'-'*4} {'-'*14} {'-'*40} {'-'*6} {'-'*12} {'-'*12}")
        for item in doc.items:
            # Усечение названия товара до максимальной длины
            name = item.name[:40] if len(item.name) > 40 else item.name
            print(
                f"  {item.row_number:<4} {item.product_code:<14} {name:<40} "
                f"{float(item.quantity):>6.0f} {float(item.unit_price):>12,.2f} {float(item.total):>12,.2f}"
            )
        print(f"  {'-'*4} {'-'*14} {'-'*40} {'-'*6} {'-'*12} {'-'*12}")

    # Итоговые суммы
    print(f"  {'Subtotal (excl. VAT):':<66} {float(doc.totals.subtotal):>12,.2f}")
    if has_vat:
        print(f"  {'VAT:':<66} {float(doc.totals.vat):>12,.2f}")
    print(f"  {'TOTAL:':<66} {float(doc.totals.total):>12,.2f}")
    print()

    # Информация о передаче товара
    if doc.transfer.transfer_basis:
        print(f"  Transfer basis: {doc.transfer.transfer_basis}")
    if doc.transfer.entity_shipper:
        print(f"  Shipper entity: {doc.transfer.entity_shipper}")
    if doc.transfer.entity_receiver:
        print(f"  Receiver entity: {doc.transfer.entity_receiver}")


if __name__ == "__main__":
    main()
