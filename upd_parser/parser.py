"""
Основной парсер УПД.

Стратегия:
  - Страница 1: извлечение шапки на основе regex + извлечение табличной части с помощью pdfplumber
  - Страница 2+: определение страниц-продолжений (без блока продавца/покупателя), извлечение только строк товаров
  - Последняя страница: извлечение строки итогов + метаданные подписи/передачи [8]-[19]
  - Валидация: перекрестная проверка математики строк товаров против объявленных итогов
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import pdfplumber


# ---------------------------------------------------------------------------
# Классы данных
# ---------------------------------------------------------------------------

@dataclass
class Party:
    """Сведения о продавце или покупателе."""
    name: str = ""
    inn: str = ""
    kpp: str = ""
    address: str = ""


@dataclass
class LineItem:
    """Строка товара из таблицы УПД."""
    row_number: int = 0
    product_code: str = ""           # Колонка А  — Код товара/работ, услуг
    name: str = ""                   # Колонка 1а — Наименование товара
    product_type_code: str = ""      # Колонка 1б — Код вида товара
    unit_code: str = ""              # Колонка 2  — Код единицы измерения
    unit_name: str = ""              # Колонка 2а — Условное обозначение
    quantity: Decimal = Decimal("0")  # Колонка 3
    unit_price: Decimal = Decimal("0")  # Колонка 4
    subtotal: Decimal = Decimal("0")  # Колонка 5  — Стоимость без налога
    excise: str = ""                 # Колонка 6  — Акциз
    vat_rate: str = ""               # Колонка 7  — Налоговая ставка (строка)
    vat_rate_percent: int = 0        # Распарсенная ставка НДС: 0, 10, 20 и т.д.
    vat_amount: Decimal = Decimal("0")  # Колонка 8
    total: Decimal = Decimal("0")    # Колонка 9  — Стоимость с налогом
    country_code: str = ""           # Колонка 10
    country_name: str = ""           # Колонка 10а
    customs_declaration: str = ""    # Колонка 11


@dataclass
class Totals:
    """Агрегированные итоги из 'Всего к оплате'."""
    subtotal: Decimal = Decimal("0")
    vat: Decimal = Decimal("0")
    total: Decimal = Decimal("0")


@dataclass
class TransferInfo:
    """Поля [8]–[19] из раздела передачи/приемки."""
    transfer_basis: str = ""         # [8]  Основание передачи
    transport_data: str = ""         # [9]  Данные о транспортировке
    shipped_by: str = ""             # [10] Товар передал
    shipment_date: str = ""          # [11] Дата отгрузки
    shipment_notes: str = ""         # [12] Иные сведения об отгрузке
    responsible_shipper: str = ""    # [13] Ответственный (отправитель)
    entity_shipper: str = ""         # [14] Наименование субъекта-составителя
    received_by: str = ""            # [15] Товар получил
    receipt_date: str = ""           # [16] Дата получения
    receipt_notes: str = ""          # [17] Иные сведения о получении
    responsible_receiver: str = ""   # [18] Ответственный (получатель)
    entity_receiver: str = ""        # [19] Наименование субъекта-составителя


@dataclass
class VATInfo:
    """
    Режим НДС и ставки, определенные из документа.

    Согласуется с семантикой полей 1C:
      - "none"     → БезНДС / НДСНеВыделять=true   — в документе нет НДС
      - "ontop"    → СуммаВключаетНДС=false          — НДС добавляется к цене
      - "included" → СуммаВключаетНДС=true           — цена уже включает НДС

    Логика определения:
      - Если все строки товаров имеют ставку НДС "--" или "без НДС" → "none"
      - Если subtotal = цена × кол-во (цена нетто) и total = subtotal + НДС → "ontop"
      - Если total = цена × кол-во (цена брутто) и subtotal = total - НДС → "included"
    """
    vat_mode: str = ""               # "none", "ontop" или "included"
    vat_rates: list[int] = field(default_factory=list)  # Найденные уникальные ставки: [20], [10, 20], и т.д.
    detection_confidence: str = ""   # "high", "medium" или "low"
    detection_reason: str = ""       # Человекочитаемое объяснение


@dataclass
class ValidationResult:
    """Результаты перекрестной проверки извлеченных данных."""
    is_valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class UPDDocument:
    """Полностью распарсенный УПД."""
    status: int = 0                  # 1 или 2
    invoice_number: str = ""
    invoice_date: str = ""           # Исходная строка
    invoice_date_iso: str = ""       # YYYY-MM-DD
    correction_number: str = ""
    seller: Party = field(default_factory=Party)
    buyer: Party = field(default_factory=Party)
    consigner: str = ""              # Грузоотправитель
    consignee: str = ""              # Грузополучатель
    payment_document: str = ""       # К платежно-расчетному документу
    shipping_document: str = ""      # Документ об отгрузке
    currency: str = ""
    currency_code: str = ""
    government_contract_id: str = ""
    items: list[LineItem] = field(default_factory=list)
    totals: Totals = field(default_factory=Totals)
    vat: VATInfo = field(default_factory=VATInfo)
    transfer: TransferInfo = field(default_factory=TransferInfo)
    page_count: int = 0
    source_file: str = ""
    generator: str = ""              # Создатель PDF (например, 1C:Enterprise)
    validation: ValidationResult = field(default_factory=ValidationResult)

    def to_dict(self) -> dict:
        """Преобразовать в JSON-совместимый словарь."""
        d = asdict(self)
        _convert_decimals(d)
        return d


def _convert_decimals(obj):
    """Рекурсивно преобразовать значения Decimal в float для JSON-сериализации."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, Decimal):
                obj[k] = float(v)
            elif isinstance(v, (dict, list)):
                _convert_decimals(v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, Decimal):
                obj[i] = float(v)
            elif isinstance(v, (dict, list)):
                _convert_decimals(v)


# ---------------------------------------------------------------------------
# Вспомогательные функции парсинга
# ---------------------------------------------------------------------------

_RUSSIAN_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def _parse_russian_date(text: str) -> tuple[str, str]:
    """Распарсить '17 июня 2025 г.' -> ('17 июня 2025 г.', '2025-06-17')."""
    text = text.strip().rstrip(".")
    m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", text)
    if m:
        day, month_str, year = m.group(1), m.group(2).lower(), m.group(3)
        month_num = _RUSSIAN_MONTHS.get(month_str, 0)
        if month_num:
            return text, f"{year}-{month_num:02d}-{int(day):02d}"
    return text, ""


def _parse_decimal(text: str) -> Decimal:
    """Распарсить число в русском формате: '10 500,00' -> Decimal('10500.00')."""
    if not text or text.strip() in ("--", "-", "", "Х", "X"):
        return Decimal("0")
    cleaned = text.strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    cleaned = re.sub(r"[^\d.\-]", "", cleaned)
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return Decimal("0")


def _clean(text: Optional[str]) -> str:
    """Очистить извлеченный текст: свернуть пробелы, обрезать."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _parse_vat_rate_percent(rate_str: str) -> int:
    """
    Распарсить строку ставки НДС в целый процент.

    '20%' → 20, '10%' → 10, 'без НДС' → 0, '--' → 0, '10/110' → 10, '20/120' → 20
    """
    if not rate_str:
        return 0
    rate_str = rate_str.strip()
    if rate_str in ("--", "-", "", "без НДС", "Без НДС"):
        return 0
    # Прямой процент: "20%", "10%"
    m = re.search(r"(\d+)\s*%", rate_str)
    if m:
        return int(m.group(1))
    # Дробная форма: "10/110", "20/120"
    m = re.search(r"(\d+)\s*/\s*\d+", rate_str)
    if m:
        return int(m.group(1))
    return 0


def _extract_between(text: str, start: str, end: str) -> str:
    """Извлечь подстроку между двумя маркерами."""
    idx_s = text.find(start)
    if idx_s == -1:
        return ""
    idx_s += len(start)
    idx_e = text.find(end, idx_s)
    if idx_e == -1:
        return text[idx_s:].strip()
    return text[idx_s:idx_e].strip()


# ---------------------------------------------------------------------------
# Парсер шапки документа (Страница 1, на основе regex)
# ---------------------------------------------------------------------------

def _parse_header_addresses(page) -> tuple[str, str]:
    """
    Использовать пространственное обрезание для отдельного извлечения адресов продавца и покупателя.

    Шапка УПД имеет двухколонный макет — продавец слева, покупатель справа.
    Простое извлечение текста их перемешивает, поэтому мы делим страницу на половины.
    """
    midpoint = page.width / 2
    header_bottom = min(page.height * 0.25, 140)  # Шапка — примерно верхние 20-25%

    seller_addr = ""
    buyer_addr = ""

    try:
        left = page.crop((0, 0, midpoint, header_bottom))
        left_text = left.extract_text() or ""
        # Адрес продавца: между "Адрес:" и "ИНН/КПП"
        m = re.search(r"Адрес:\s*\n?\s*(.+?)(?:ИНН/КПП|Грузоотправитель)", left_text, re.DOTALL)
        if m:
            addr = _clean(m.group(1))
            addr = re.sub(r"Статус:\s*\d\s*", "", addr).strip()
            seller_addr = addr

        right = page.crop((midpoint, 0, page.width, header_bottom))
        right_text = right.extract_text() or ""
        # Адрес покупателя: между "Адрес:" и "(6а)"
        m = re.search(r"Адрес:\s*(.+?)\s*\(6а\)", right_text, re.DOTALL)
        if m:
            addr = _clean(m.group(1))
            # Очистить стылые маркеры полей
            addr = re.sub(r"\(\d+[а-я]?\)", "", addr).strip()
            buyer_addr = addr
    except Exception:
        pass

    return seller_addr, buyer_addr


def _parse_header(text: str, doc: UPDDocument, page=None) -> None:
    """Извлечь структурированные поля из блока шапки страницы 1."""

    # Статус
    m = re.search(r"Статус:\s*(\d)", text)
    if m:
        doc.status = int(m.group(1))

    # Номер и дата счета-фактуры
    m = re.search(r"Счет-фактура\s*№\s*(\S+)\s+от\s+(.+?)\s*\(1\)", text)
    if m:
        doc.invoice_number = m.group(1).strip()
        doc.invoice_date, doc.invoice_date_iso = _parse_russian_date(m.group(2))

    # Исправление
    m = re.search(r"Исправление\s*№\s*(.+?)\s+от\s+(.+?)\s*\(1а\)", text)
    if m:
        val = m.group(1).strip()
        if val not in ("--", "-"):
            doc.correction_number = val

    # Продавец
    m = re.search(r"Продавец:\s*(.+?)\s*\(2\)", text)
    if m:
        doc.seller.name = _clean(m.group(1))

    # Адреса — использовать пространственное обрезание при наличии объекта page
    if page is not None:
        seller_addr, buyer_addr = _parse_header_addresses(page)
        if seller_addr:
            doc.seller.address = seller_addr
        if buyer_addr:
            doc.buyer.address = buyer_addr

    # ИНН/КПП продавца
    m = re.search(r"ИНН/КПП\s+продавца:\s*(\d+)[/\\](\d+)", text)
    if m:
        doc.seller.inn = m.group(1)
        doc.seller.kpp = m.group(2)

    # Покупатель
    m = re.search(r"Покупатель:\s*(.+?)\s*\(6\)", text)
    if m:
        doc.buyer.name = _clean(m.group(1))

    # Адрес покупателя — обработан пространственным извлечением выше; резервный regex при отсутствии page
    if page is None:
        m = re.search(r"\(6\)\s*\n?\s*Адрес:\s*(.+?)\s*\(6а\)", text, re.DOTALL)
        if not m:
            m = re.search(r"Покупатель:.*?Адрес:\s*(.+?)\s*\(6а\)", text, re.DOTALL)
        if m:
            doc.buyer.address = _clean(m.group(1))

    # ИНН/КПП покупателя
    m = re.search(r"ИНН/КПП\s+покупателя:\s*(\d+)[/\\](\d+)", text)
    if m:
        doc.buyer.inn = m.group(1)
        doc.buyer.kpp = m.group(2)

    # Грузоотправитель
    m = re.search(r"Грузоотправитель и его адрес:\s*(.+?)\s*\(3\)", text)
    if m:
        doc.consigner = _clean(m.group(1))

    # Грузополучатель — использовать обрезание левой стороны для более чистого извлечения
    if page is not None:
        try:
            midpoint = page.width / 2
            left = page.crop((0, 0, midpoint, min(page.height * 0.25, 140)))
            left_text = left.extract_text() or ""
            m = re.search(r"Грузополучатель и его адрес:\s*(.+?)(?:К платежно|$)", left_text, re.DOTALL)
            if m:
                doc.consignee = _clean(m.group(1))
        except Exception:
            pass
    if not doc.consignee:
        m = re.search(r"Грузополучатель и его адрес:\s*(.+?)\s*\(4\)", text, re.DOTALL)
        if m:
            doc.consignee = _clean(m.group(1))

    # Платежный документ
    m = re.search(r"К платежно-расчетному документу\s*№\s*(.+?)\s*\(5\)", text)
    if m:
        val = _clean(m.group(1))
        # Убрать пустые заполнители — "-- от --", "от", просто дефисы
        val = re.sub(r"^[\s\-]*от[\s\-]*$", "", val).strip()
        if val and val not in ("--", "-", "от --", "от"):
            doc.payment_document = val

    # Документ об отгрузке
    m = re.search(r"Документ об отгрузке\s+(.+?)\s*\(5а\)", text)
    if m:
        doc.shipping_document = _clean(m.group(1))

    # Валюта
    m = re.search(r"Валюта:\s*наименование,\s*код\s+(.+?)\s*\(7\)", text)
    if m:
        parts = _clean(m.group(1))
        # "Российский рубль, 643"
        if "," in parts:
            name_part, code_part = parts.rsplit(",", 1)
            doc.currency = name_part.strip()
            doc.currency_code = code_part.strip()
        else:
            doc.currency = parts

    # ID государственного контракта — поле (8)
    # Извлечь только значение между текстом метки и маркером (8)
    m = re.search(
        r"договора\s*\(соглашения\)\s*\(при наличии\):\s*(.+?)\s*\(8\)",
        text, re.DOTALL
    )
    if m:
        val = _clean(m.group(1))
        if val and val not in ("--", "-", ""):
            doc.government_contract_id = val


# ---------------------------------------------------------------------------
# Парсер табличной части
# ---------------------------------------------------------------------------

# Метки столбцов, используемые как якоря
_COL_LABELS = ["А", "1", "1а", "1б", "2", "2а", "3", "4", "5", "6", "7", "8", "9", "10", "10а", "11"]


def _is_label_row(row: list) -> bool:
    """Проверить, является ли строка строкой с метками столбцов (А, 1, 1а, 1б, ...)."""
    cleaned = [_clean(c) for c in row if c]
    if not cleaned:
        return False
    # Проверить наличие характерной последовательности
    return "А" in cleaned and "1а" in cleaned and "11" in cleaned


def _is_header_row(row: list) -> bool:
    """Проверить, является ли строка описательной строкой заголовка столбца (не данные)."""
    text = " ".join(_clean(c) for c in row if c)
    return any(kw in text for kw in [
        "Код товара", "Наименование товара", "Единица измерения",
        "условное", "нацио", "краткое", "циф-"
    ])


def _is_totals_row(row: list) -> bool:
    """Проверить, является ли строка строкой 'Всего к оплате'."""
    text = " ".join(_clean(c) for c in row if c)
    return "Всего к оплате" in text or "всего к оплате" in text.lower()


def _is_signature_row(row: list) -> bool:
    """Проверить, принадлежит ли строка блоку подписей/нижней части."""
    text = " ".join(_clean(c) for c in row if c)
    return any(kw in text for kw in [
        "Руководитель организации", "Главный бухгалтер",
        "уполномоченное лицо", "подпись", "ф.и.о."
    ])


def _normalize_row(row: list, expected_cols: int = 16) -> list:
    """
    Нормализовать строку таблицы к консистентному количеству столбцов данных.

    Таблицы в 1C часто имеют ведущую пустую ячейку (из объединенной области заголовка),
    что делает их на 17 столбцов шире, в то время как таблицы страниц 2+ имеют 16 столбцов.
    Мы нормализуем всё к 16 столбцам данных, отображаемых в:
      [product_code, row_num, name, type_code, unit_code, unit_name,
       qty, price, subtotal, excise, vat_rate, vat_amount, total,
       country_code, country_name, customs_decl]
    """
    if len(row) == expected_cols + 1:
        # Формат страницы 1 — убрать ведущую пустую ячейку
        return row[1:]
    elif len(row) == expected_cols:
        return row
    elif len(row) > expected_cols + 1:
        # Слишком широкая — попробовать убрать ведущие пустые
        stripped = row
        while len(stripped) > expected_cols and not _clean(stripped[0]):
            stripped = stripped[1:]
        return stripped[:expected_cols]
    else:
        # Слишком узкая — дополнить
        return row + [""] * (expected_cols - len(row))


def _parse_line_item(row: list, fallback_row_num: int) -> Optional[LineItem]:
    """Распарсить нормализованную 16-колонную строку в LineItem."""
    cols = _normalize_row(row)

    product_code = _clean(cols[0])
    row_num_str = _clean(cols[1])
    name = _clean(cols[2])

    # Пропустить пустые или не-данные строки
    if not name and not product_code:
        return None

    # Номер строки
    try:
        row_num = int(re.sub(r"\D", "", row_num_str)) if row_num_str else fallback_row_num
    except ValueError:
        row_num = fallback_row_num

    vat_rate_raw = _clean(cols[10])
    item = LineItem(
        row_number=row_num,
        product_code=product_code,
        name=name,
        product_type_code=_clean(cols[3]),
        unit_code=_clean(cols[4]),
        unit_name=_clean(cols[5]),
        quantity=_parse_decimal(cols[6]),
        unit_price=_parse_decimal(cols[7]),
        subtotal=_parse_decimal(cols[8]),
        excise=_clean(cols[9]),
        vat_rate=vat_rate_raw,
        vat_rate_percent=_parse_vat_rate_percent(vat_rate_raw),
        vat_amount=_parse_decimal(cols[11]),
        total=_parse_decimal(cols[12]),
        country_code=_clean(cols[13]),
        country_name=_clean(cols[14]),
        customs_declaration=_clean(cols[15]),
    )
    return item


def _parse_totals_row(row: list) -> Totals:
    """Извлечь итоги из строки 'Всего к оплате'."""
    cols = _normalize_row(row)
    return Totals(
        subtotal=_parse_decimal(cols[8]),
        vat=_parse_decimal(cols[11]),
        total=_parse_decimal(cols[12]),
    )


def _extract_items_from_page(page, item_counter: int) -> tuple[list[LineItem], Optional[Totals]]:
    """Извлечь строки товаров и опционально итоги из одной страницы."""
    table_settings = {
        "vertical_strategy": "lines",
        "horizontal_strategy": "lines",
        "snap_tolerance": 5,
    }
    tables = page.extract_tables(table_settings)
    items = []
    totals = None

    for table in tables:
        for row in table:
            if _is_label_row(row) or _is_header_row(row) or _is_signature_row(row):
                continue
            if _is_totals_row(row):
                totals = _parse_totals_row(row)
                continue

            item = _parse_line_item(row, fallback_row_num=item_counter + 1)
            if item:
                item_counter += 1
                items.append(item)

    return items, totals


# ---------------------------------------------------------------------------
# Парсер раздела передачи/приемки (последняя страница)
# ---------------------------------------------------------------------------

def _parse_transfer_section(text: str) -> TransferInfo:
    """Распарсить поля [8]–[19] из текста последней страницы."""
    info = TransferInfo()

    # [8] Основание передачи
    m = re.search(
        r"Основание передачи\s*\(сдачи\)\s*/\s*получения\s*\(приемки\)\s+(.+?)\s*\[8\]",
        text, re.DOTALL
    )
    if not m:
        m = re.search(
            r"Основание передачи\s*\(сдачи\)\s*/\s*получения\s*\(приемки\)\s+(.+?)(?:\n|$)",
            text
        )
    if m:
        val = _clean(m.group(1))
        if val and val not in ("(договор; доверенность и др.)",):
            info.transfer_basis = val

    # [14] Организация отправителя — искать текст в строке(ах) прямо перед [14]
    m = re.search(r"комиссионера\s*/\s*агента\)\s*\n(.+?)\s*\[14\]", text, re.DOTALL)
    if m:
        info.entity_shipper = _clean(m.group(1))

    # [19] Организация получателя — искать текст в строке(ах) прямо перед [19]
    m = re.search(r"составителя документа\s*\n(.+?)\s*\[19\]", text, re.DOTALL)
    if m:
        val = _clean(m.group(1))
        # Иногда текст блока [14] просачивается — взять только после [14] при наличии
        if "[14]" in val:
            val = val.split("[14]")[-1].strip()
        info.entity_receiver = val

    return info


# ---------------------------------------------------------------------------
# Определение режима НДС
# ---------------------------------------------------------------------------

def _detect_vat_mode(doc: UPDDocument) -> VATInfo:
    """
    Определить режим НДС из строк товаров путем анализа отношения между
    ценой, стоимостью без налога, суммой НДС и итоговой стоимостью.

    Возвращает VATInfo, согласованный с именованием 1C:
      - "none"     → нет НДС в документе (БезНДС / НДСНеВыделять)
      - "ontop"    → НДС добавляется на цену (СуммаВключаетНДС=false)
      - "included" → цена уже включает НДС (СуммаВключаетНДС=true)
    """
    info = VATInfo()

    if not doc.items:
        info.vat_mode = "none"
        info.detection_confidence = "low"
        info.detection_reason = "Нет строк товаров для анализа"
        return info

    # Собрать уникальные ставки НДС
    rates = set()
    for item in doc.items:
        rates.add(item.vat_rate_percent)
    info.vat_rates = sorted(r for r in rates if r > 0)

    # --- Режим 1: БЕЗ НДС ---
    # Все товары имеют 0% / "--" / "без НДС" и все суммы НДС равны нулю
    all_zero_rate = all(item.vat_rate_percent == 0 for item in doc.items)
    all_zero_vat = all(item.vat_amount == 0 for item in doc.items)
    no_vat_strings = all(
        item.vat_rate in ("--", "-", "", "без НДС", "Без НДС")
        for item in doc.items
    )

    if all_zero_rate and all_zero_vat and no_vat_strings:
        info.vat_mode = "none"
        info.detection_confidence = "high"
        info.detection_reason = (
            "Все товары без ставки НДС (--/без НДС) и нулевыми суммами НДС"
        )
        return info

    # Для товаров с фактическим НДС определить, добавляется ли он или включен
    # Проверяем математическое соотношение для каждого товара с НДС:
    #
    # "ontop":    цена нетто    → цена × кол-во = стоимость, стоимость + ндс = итого
    # "included": цена брутто   → цена × кол-во = итого,    итого - ндс = стоимость

    ontop_score = 0
    included_score = 0
    items_with_vat = [i for i in doc.items if i.vat_rate_percent > 0 and i.quantity > 0]

    for item in items_with_vat:
        price_x_qty = item.unit_price * item.quantity

        # Проверить ontop: цена × кол-во ≈ стоимость (цена без НДС)
        diff_ontop = abs(price_x_qty - item.subtotal)

        # Проверить included: цена × кол-во ≈ итого (цена включает НДС)
        diff_included = abs(price_x_qty - item.total)

        tolerance = Decimal("0.02") * item.quantity  # Допустить округление на единицу

        if diff_ontop <= tolerance:
            ontop_score += 1
        if diff_included <= tolerance:
            included_score += 1

    total_checked = len(items_with_vat)

    if total_checked == 0:
        # Товары имеют ставки, но без кол-ва — необычно, вернуть резервный режим
        info.vat_mode = "none"
        info.detection_confidence = "low"
        info.detection_reason = "Нет товаров с ставкой НДС и кол-вом для анализа"
        return info

    # --- Режим 2: НДС НА СУММУ ---
    if ontop_score == total_checked and included_score < total_checked:
        info.vat_mode = "ontop"
        info.detection_confidence = "high"
        info.detection_reason = (
            f"цена × кол-во = стоимость для всех {total_checked} товаров → "
            f"цена нетто, НДС добавляется"
        )
    # --- Режим 3: НДС ВКЛЮЧЕН ---
    elif included_score == total_checked and ontop_score < total_checked:
        info.vat_mode = "included"
        info.detection_confidence = "high"
        info.detection_reason = (
            f"цена × кол-во = итого для всех {total_checked} товаров → "
            f"цена брутто, НДС включен"
        )
    # --- Неоднозначно: оба совпадают (например, смешанные товары с 0% НДС) ---
    elif ontop_score >= included_score:
        info.vat_mode = "ontop"
        info.detection_confidence = "medium"
        info.detection_reason = (
            f"ontop совпал {ontop_score}/{total_checked}, "
            f"included совпал {included_score}/{total_checked} — "
            f"по умолчанию ontop (наиболее часто для УПД)"
        )
    else:
        info.vat_mode = "included"
        info.detection_confidence = "medium"
        info.detection_reason = (
            f"included совпал {included_score}/{total_checked}, "
            f"ontop совпал {ontop_score}/{total_checked}"
        )

    return info


# ---------------------------------------------------------------------------
# Валидация
# ---------------------------------------------------------------------------

def _validate(doc: UPDDocument) -> ValidationResult:
    """Перекрестная проверка извлеченных данных на консистентность, включая логику НДС."""
    result = ValidationResult()

    # --- Проверки математики на уровне товара ---
    for item in doc.items:
        if item.quantity and item.unit_price:
            expected = item.quantity * item.unit_price
            diff = abs(expected - item.subtotal)
            if diff > Decimal("0.02"):
                result.warnings.append(
                    f"Товар {item.row_number} ({item.product_code}): "
                    f"цена*кол-во={expected} != стоимость={item.subtotal} (разница={diff})"
                )

    # --- Консистентность итогов ---
    if doc.items and doc.totals.subtotal:
        sum_subtotals = sum(i.subtotal for i in doc.items)
        diff = abs(sum_subtotals - doc.totals.subtotal)
        if diff > Decimal("0.05"):
            result.errors.append(
                f"Сумма стоимостей товаров ({sum_subtotals}) != "
                f"объявленная стоимость ({doc.totals.subtotal}), разница={diff}"
            )

    if doc.items and doc.totals.vat:
        sum_vat = sum(i.vat_amount for i in doc.items)
        diff = abs(sum_vat - doc.totals.vat)
        if diff > Decimal("0.05"):
            result.errors.append(
                f"Сумма НДС по товарам ({sum_vat}) != "
                f"объявленный НДС ({doc.totals.vat}), разница={diff}"
            )

    if doc.items and doc.totals.total:
        sum_totals = sum(i.total for i in doc.items)
        diff = abs(sum_totals - doc.totals.total)
        if diff > Decimal("0.05"):
            result.errors.append(
                f"Сумма итогов по товарам ({sum_totals}) != "
                f"объявленный итог ({doc.totals.total}), разница={diff}"
            )

    # стоимость + ндс = итого
    if doc.totals.subtotal and doc.totals.total:
        expected_total = doc.totals.subtotal + doc.totals.vat
        diff = abs(expected_total - doc.totals.total)
        if diff > Decimal("0.05"):
            result.warnings.append(
                f"Стоимость ({doc.totals.subtotal}) + НДС ({doc.totals.vat}) = "
                f"{expected_total} != итого ({doc.totals.total})"
            )

    # --- Валидация, специфичная для НДС ---
    vat = doc.vat

    if vat.vat_mode == "none":
        # Проверить: ни один товар не должен иметь ненулевую сумму НДС
        items_with_vat = [i for i in doc.items if i.vat_amount > 0]
        if items_with_vat:
            result.errors.append(
                f"Режим НДС 'none', но {len(items_with_vat)} товар(ов) "
                f"имеют ненулевую сумму НДС"
            )
        # Проверить: стоимость должна равняться итого (нет налога)
        if doc.totals.subtotal and doc.totals.total:
            if abs(doc.totals.subtotal - doc.totals.total) > Decimal("0.05"):
                result.errors.append(
                    f"Режим НДС 'none', но стоимость ({doc.totals.subtotal}) "
                    f"!= итого ({doc.totals.total})"
                )

    elif vat.vat_mode == "ontop":
        # Проверить: для каждого товара стоимость + ндс ≈ итого
        for item in doc.items:
            if item.vat_rate_percent > 0 and item.subtotal > 0:
                expected = item.subtotal + item.vat_amount
                diff = abs(expected - item.total)
                if diff > Decimal("0.02"):
                    result.warnings.append(
                        f"Товар {item.row_number}: проверка ontop не прошла — "
                        f"стоимость({item.subtotal}) + ндс({item.vat_amount}) = "
                        f"{expected} != итого({item.total})"
                    )
        # Проверить: НДС товара = стоимость × ставка
        for item in doc.items:
            if item.vat_rate_percent > 0 and item.subtotal > 0:
                expected_vat = item.subtotal * Decimal(str(item.vat_rate_percent)) / Decimal("100")
                diff = abs(expected_vat - item.vat_amount)
                if diff > Decimal("0.02") * item.quantity:
                    result.warnings.append(
                        f"Товар {item.row_number}: математика НДС — "
                        f"стоимость({item.subtotal}) × {item.vat_rate_percent}% = "
                        f"{expected_vat} != объявленный ндс({item.vat_amount})"
                    )

    elif vat.vat_mode == "included":
        # Проверить: для каждого товара итого - ндс ≈ стоимость
        for item in doc.items:
            if item.vat_rate_percent > 0 and item.total > 0:
                expected_subtotal = item.total - item.vat_amount
                diff = abs(expected_subtotal - item.subtotal)
                if diff > Decimal("0.02"):
                    result.warnings.append(
                        f"Товар {item.row_number}: проверка included не прошла — "
                        f"итого({item.total}) - ндс({item.vat_amount}) = "
                        f"{expected_subtotal} != стоимость({item.subtotal})"
                    )

    # Предупреждение о уровне доверия
    if vat.detection_confidence in ("low", "medium"):
        result.warnings.append(
            f"Уровень уверенности определения режима НДС: {vat.detection_confidence} — "
            f"{vat.detection_reason}"
        )

    result.is_valid = len(result.errors) == 0
    return result


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def parse_upd(pdf_path: str | Path) -> UPDDocument:
    """
    Распарсить PDF-файл УПД в структурированный UPDDocument.

    Args:
        pdf_path: Путь к PDF-файлу.

    Returns:
        UPDDocument со всеми извлеченными полями, товарами, итогами и валидацией.
    """
    pdf_path = Path(pdf_path)
    doc = UPDDocument(source_file=pdf_path.name)

    # Получить метаданные PDF
    from pypdf import PdfReader
    reader = PdfReader(str(pdf_path))
    if reader.metadata:
        doc.generator = reader.metadata.get("/Creator", "")

    with pdfplumber.open(str(pdf_path)) as pdf:
        doc.page_count = len(pdf.pages)
        all_items: list[LineItem] = []
        final_totals: Optional[Totals] = None

        for pg_idx, page in enumerate(pdf.pages):
            text = page.extract_text() or ""

            # Страница 1: распарсить шапку
            if pg_idx == 0:
                _parse_header(text, doc, page=page)

            # Извлечь строки товаров со всех страниц
            items, totals = _extract_items_from_page(page, len(all_items))
            all_items.extend(items)
            if totals:
                final_totals = totals

            # Последняя страница: распарсить раздел передачи
            if pg_idx == len(pdf.pages) - 1:
                doc.transfer = _parse_transfer_section(text)

        doc.items = all_items
        if final_totals:
            doc.totals = final_totals

    # Определить режим НДС
    doc.vat = _detect_vat_mode(doc)

    # Валидировать (включая проверки НДС)
    doc.validation = _validate(doc)

    return doc
