"""Konversi transaksi staging ke format tujuan."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import Transaction, STATUS_READY

MONEYLOVER_COLUMNS = ['Date', 'Amount', 'Category', 'Note', 'Wallet']

_PAYMENT_METHOD = {
    'transfer': 'transfer',
    'va': 'transfer',
    'card': 'card',
    'kartu': 'card',
    'ewallet': 'ewallet',
    'qris': 'ewallet',
    'cash': 'cash',
    'tunai': 'cash',
}


def _wallet_for(transaction: Transaction, config: Dict[str, Any]) -> str:
    from .categorize import wallet_for
    return wallet_for(transaction, config) or transaction.bank or ''


def to_moneylover_csv(transactions: List[Transaction], rules: Dict[str, Any], config: Dict[str, Any]) -> str:
    """CSV untuk direview di spreadsheet atau dipakai importer pihak ketiga.

    Money Lover tidak punya impor CSV resmi (impor resminya memakai berkas
    .mlx). Kolom di sini mengikuti template yang dipakai komunitas; nominal
    pengeluaran ditulis negatif.
    """
    override = config.get('moneylover', {}).get('category_map', {})
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(MONEYLOVER_COLUMNS)
    for item in transactions:
        meta = rules['categories'].get(item.category, {})
        category = override.get(item.category) or meta.get('moneylover') or item.category_label
        amount = item.amount if item.kind == 'income' else -item.amount
        note_parts = [item.merchant, item.reference and f'ref {item.reference}', item.email.subject]
        note = ' - '.join(part for part in note_parts if part)
        writer.writerow([
            item.date.strftime('%d/%m/%Y'),
            f'{amount:.2f}',
            category,
            note[:200],
            _wallet_for(item, config),
        ])
    return buffer.getvalue()


def to_review_markdown(transactions: List[Transaction], currency: str = 'Rp') -> str:
    """Tabel ringkas untuk dibaca manusia di ringkasan Routine."""
    if not transactions:
        return '_Tidak ada transaksi._'
    # Fingerprint ikut ditampilkan karena tabel inilah dasar keputusan manual,
    # dan 'decide', 'tag', serta 'mark' semuanya dialamatkan lewat fingerprint.
    lines = ['| Tanggal | Nominal | Merchant | Kategori | Sumber | Status | Fingerprint |',
             '| --- | ---: | --- | --- | --- | --- | --- |']
    for item in transactions:
        sign = '-' if item.kind == 'expense' else ('+' if item.kind == 'income' else '~')
        lines.append(
            f"| {item.date.strftime('%d/%m/%Y %H:%M')} "
            f"| {sign}{currency}{item.amount:,.0f} "
            f"| {(item.merchant or item.email.subject or '-')[:40]} "
            f"| {item.category_label or item.category} "
            f"| {item.bank or item.source_id} "
            f"| {item.status} "
            f"| `{item.fingerprint}` |"
        )
    return '\n'.join(lines)


def _match_category_id(name: str, categories: List[Dict[str, Any]], wanted_type: str) -> Optional[int]:
    lowered = (name or '').lower()
    for category in categories:
        if category.get('name', '').lower() == lowered:
            if category.get('type') in (None, wanted_type):
                return category.get('id')
    for category in categories:
        if category.get('name', '').lower() == lowered:
            return category.get('id')
    return None


def to_cashflowplus_backup(
    transactions: List[Transaction],
    rules: Dict[str, Any],
    config: Dict[str, Any],
    base_backup: Dict[str, Any],
) -> Dict[str, Any]:
    """Sisipkan transaksi ke berkas backup JSON My CashFlow+ yang sudah ada.

    Fitur "Import backup" di aplikasi mengganti seluruh state, bukan menambah.
    Karena itu jalur yang aman adalah: ekspor backup dari aplikasi, gabungkan
    di sini, lalu impor kembali berkas hasil gabungan.
    """
    merged = json.loads(json.dumps(base_backup))
    categories = merged.get('categories', [])
    entries = merged.get('entries', [])
    next_id = max((entry.get('id', 0) for entry in entries), default=0) + 1
    default_book = merged.get('currentBookId') or (merged.get('books') or [{'id': 1}])[0]['id']
    fallback_id = _match_category_id('Other', categories, 'expense') or (categories[0]['id'] if categories else 1)

    book_by_account = {
        account.get('label', '').lower(): account.get('cashflowplus_book_id')
        for account in config.get('own_accounts', [])
        if account.get('cashflowplus_book_id')
    }

    for item in transactions:
        meta = rules['categories'].get(item.category, {})
        wanted_type = 'income' if item.kind == 'income' else 'expense'
        category_id = _match_category_id(meta.get('cashflowplus', ''), categories, wanted_type) or fallback_id
        title = item.merchant or item.email.subject or item.category_label
        note_parts = [
            item.reference and f'Ref {item.reference}',
            item.bank and f'Sumber {item.bank}',
            item.account and f'Rek {item.account}',
            item.email.link(),
        ]
        entries.append({
            'id': next_id,
            'bookId': book_by_account.get((item.bank or '').lower(), default_book),
            'amount': item.amount,
            'type': 'income' if item.kind == 'income' else 'expense',
            'title': title[:60],
            'categoryId': category_id,
            'paymentMethod': _PAYMENT_METHOD.get((item.channel or '').lower(), 'transfer'),
            'date': item.date.isoformat(),
            'note': ' | '.join(part for part in note_parts if part) or None,
            'receiptImagePath': None,
            'isRecurring': False,
            'recurringFrequency': None,
        })
        next_id += 1

    merged['entries'] = entries
    return merged


def ready_only(transactions: List[Transaction]) -> List[Transaction]:
    return [item for item in transactions if item.status == STATUS_READY]
