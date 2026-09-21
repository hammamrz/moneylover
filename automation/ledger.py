"""Penyimpanan staging transaksi hasil ekstraksi.

Semua transaksi disimpan sebagai file JSON per bulan di ``automation/data``
sehingga riwayatnya ikut ter-commit di git: setiap perubahan bisa ditelusuri,
dan tidak ada state tersembunyi di container yang umurnya pendek.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .models import Transaction, STATUS_DUPLICATE

DATA_DIR = Path(__file__).parent / 'data'


def month_key(transaction: Transaction) -> str:
    return transaction.date.strftime('%Y-%m')


def path_for(month: str, data_dir: Path = DATA_DIR) -> Path:
    return data_dir / f'{month}.json'


def load_month(month: str, data_dir: Path = DATA_DIR) -> List[Transaction]:
    path = path_for(month, data_dir)
    if not path.exists():
        return []
    with open(path, encoding='utf-8') as handle:
        payload = json.load(handle)
    return [Transaction.from_dict(item) for item in payload.get('transactions', [])]


def load_all(data_dir: Path = DATA_DIR) -> List[Transaction]:
    found: List[Transaction] = []
    for path in sorted(data_dir.glob('[0-9][0-9][0-9][0-9]-[0-9][0-9].json')):
        found.extend(load_month(path.stem, data_dir))
    return found


def save_month(month: str, transactions: List[Transaction], data_dir: Path = DATA_DIR) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = path_for(month, data_dir)
    ordered = sorted(transactions, key=lambda item: (item.date, item.fingerprint))
    payload = {
        'month': month,
        'count': len(ordered),
        'transactions': [item.to_dict() for item in ordered],
    }
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return path


def find_duplicate(candidate: Transaction, existing: Iterable[Transaction],
                   window_minutes: int) -> Optional[Transaction]:
    """Cari transaksi tersimpan yang sama dengan kandidat.

    Deduplikasi dua lapis: fingerprint sama, atau nominal sama berdekatan waktu.
    Lapis kedua menangkap kasus satu transaksi yang dilaporkan oleh dua email
    berbeda (mis. notifikasi bank dan struk merchant) dengan referensi berbeda.
    """
    window = timedelta(minutes=window_minutes)
    fingerprint = candidate.fingerprint
    for other in existing:
        if other.fingerprint == fingerprint:
            return other
        if other.direction != candidate.direction:
            continue
        if abs(other.amount - candidate.amount) > 0.01:
            continue
        if abs((other.date - candidate.date).total_seconds()) > window.total_seconds():
            continue
        if other.reference and candidate.reference and other.reference == candidate.reference:
            return other
        if other.email.message_id and other.email.message_id == candidate.email.message_id:
            return other
        merchant_a = (other.merchant or '').lower()
        merchant_b = (candidate.merchant or '').lower()
        if merchant_a and merchant_b and (merchant_a in merchant_b or merchant_b in merchant_a):
            return other
    return None


def is_duplicate(candidate: Transaction, existing: Iterable[Transaction], window_minutes: int) -> bool:
    return find_duplicate(candidate, existing, window_minutes) is not None


def adopt_wallet(stored: Transaction, candidate: Transaction, config: Dict) -> bool:
    """Ambil informasi rekening dari email yang lebih tahu, saat terjadi duplikat.

    Struk Tokopedia dan notifikasi bank melaporkan transaksi yang sama, tetapi
    hanya notifikasi bank yang menyebut rekening mana yang terdebit. Tanpa ini,
    urutan kedatangan email menentukan apakah transaksi punya dompet tujuan
    atau tertahan selamanya.
    """
    from .categorize import wallet_for

    if wallet_for(stored, config) or not wallet_for(candidate, config):
        return False
    previous = stored.bank or stored.source_id
    stored.bank = candidate.bank
    stored.source_id = candidate.source_id
    stored.account = candidate.account or stored.account
    stored.channel = candidate.channel or stored.channel
    stored.reference = stored.reference or candidate.reference
    stored.reasons.append(
        f"rekening diambil dari email {candidate.source_id} (sebelumnya '{previous}')"
    )
    return True


def merge(candidates: List[Transaction], config: Dict, data_dir: Path = DATA_DIR) -> Tuple[List[Transaction], List[Transaction]]:
    """Gabungkan kandidat baru ke staging. Mengembalikan (baru, duplikat)."""
    window = config.get('thresholds', {}).get('duplicate_window_minutes', 5)
    by_month: Dict[str, List[Transaction]] = {}
    added: List[Transaction] = []
    duplicates: List[Transaction] = []

    for candidate in sorted(candidates, key=lambda item: item.date):
        key = month_key(candidate)
        if key not in by_month:
            by_month[key] = load_month(key, data_dir)
        stored = find_duplicate(candidate, by_month[key], window)
        if stored is not None:
            adopt_wallet(stored, candidate, config)
            candidate.status = STATUS_DUPLICATE
            duplicates.append(candidate)
            continue
        by_month[key].append(candidate)
        added.append(candidate)

    for key, items in by_month.items():
        save_month(key, items, data_dir)
    return added, duplicates


def transform(fingerprints: Iterable[str], mutate, data_dir: Path = DATA_DIR) -> List[Transaction]:
    """Ubah transaksi tertentu di staging lewat ``mutate`` lalu simpan kembali.

    Dipakai untuk koreksi manual, misalnya menandai satu transaksi sebagai
    perjalanan dinas sehingga kategorinya dihitung ulang.
    """
    wanted = set(fingerprints)
    touched: List[Transaction] = []
    for path in sorted(data_dir.glob('[0-9][0-9][0-9][0-9]-[0-9][0-9].json')):
        items = load_month(path.stem, data_dir)
        changed = False
        for item in items:
            if item.fingerprint in wanted:
                mutate(item)
                touched.append(item)
                changed = True
        if changed:
            save_month(path.stem, items, data_dir)
    return touched


def update_status(fingerprints: Iterable[str], status: str, data_dir: Path = DATA_DIR) -> int:
    """Tandai transaksi tertentu, mis. setelah berhasil diimpor."""
    wanted = set(fingerprints)
    changed = 0
    for path in sorted(data_dir.glob('[0-9][0-9][0-9][0-9]-[0-9][0-9].json')):
        items = load_month(path.stem, data_dir)
        touched = False
        for item in items:
            if item.fingerprint in wanted and item.status != status:
                item.status = status
                changed += 1
                touched = True
        if touched:
            save_month(path.stem, items, data_dir)
    return changed
