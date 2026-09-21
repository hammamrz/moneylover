"""Antarmuka baris perintah pipeline Gmail -> pencatatan transaksi.

Contoh pemakaian:

    python3 -m automation.cli ingest --input emails.json
    python3 -m automation.cli review --month 2026-09
    python3 -m automation.cli export --format moneylover-csv --out /tmp/ml.csv
    python3 -m automation.cli export --format cashflowplus --base backup.json --out merged.json
    python3 -m automation.cli push --status ready            # dry-run
    python3 -m automation.cli push --status ready --live     # benar-benar kirim
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from . import categorize as categorize_module
from . import extract as extract_module
from . import ledger
from .adapters import moneylover
from .exporters import to_cashflowplus_backup, to_moneylover_csv, to_review_markdown
from .models import RawEmail, Transaction, STATUS_IMPORTED, STATUS_READY, STATUS_REVIEW

CONFIG_PATH = Path(__file__).parent / 'config.json'


def load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def _read_input(source: str) -> Any:
    if source == '-':
        return json.load(sys.stdin)
    with open(source, encoding='utf-8') as handle:
        return json.load(handle)


def _as_emails(payload: Any) -> List[RawEmail]:
    if isinstance(payload, dict):
        payload = payload.get('emails', payload.get('messages', []))
    if not isinstance(payload, list):
        raise SystemExit('input harus berupa array email atau objek dengan kunci "emails"')
    return [RawEmail.from_dict(item) for item in payload]


def _select(transactions: List[Transaction], month: str, status: str) -> List[Transaction]:
    chosen = transactions
    if month:
        chosen = [item for item in chosen if item.date.strftime('%Y-%m') == month]
    if status and status != 'all':
        chosen = [item for item in chosen if item.status == status]
    return sorted(chosen, key=lambda item: item.date)


def cmd_ingest(args: argparse.Namespace) -> int:
    config = load_config()
    source_rules = extract_module.load_rules()
    category_rules = categorize_module.load_rules()

    emails = _as_emails(_read_input(args.input))
    candidates = extract_module.extract_many(emails, source_rules, config)
    candidates = categorize_module.categorize_many(candidates, category_rules, config)
    skipped = len(emails) - len(candidates)

    if args.dry_run:
        added, duplicates = candidates, []
    else:
        added, duplicates = ledger.merge(candidates, config)

    report = {
        'emails_dibaca': len(emails),
        'bukan_transaksi': skipped,
        'transaksi_baru': len(added),
        'duplikat_dilewati': len(duplicates),
        'siap_impor': len([item for item in added if item.status == STATUS_READY]),
        'perlu_review': len([item for item in added if item.status == STATUS_REVIEW]),
        'dry_run': args.dry_run,
    }
    if args.json:
        print(json.dumps({'ringkasan': report,
                          'transaksi': [item.to_dict() for item in added]},
                         ensure_ascii=False, indent=2))
    else:
        for key, value in report.items():
            print(f'{key}: {value}')
        print()
        print(to_review_markdown(added))
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    chosen = _select(ledger.load_all(), args.month, args.status)
    if args.json:
        print(json.dumps([item.to_dict() for item in chosen], ensure_ascii=False, indent=2))
    else:
        print(to_review_markdown(chosen))
        print(f'\nTotal: {len(chosen)} transaksi')
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    config = load_config()
    category_rules = categorize_module.load_rules()
    chosen = _select(ledger.load_all(), args.month, args.status)

    if args.format == 'moneylover-csv':
        output = to_moneylover_csv(chosen, category_rules, config)
    elif args.format == 'markdown':
        output = to_review_markdown(chosen)
    elif args.format == 'cashflowplus':
        if not args.base:
            raise SystemExit(
                'format cashflowplus butuh --base: ekspor dulu backup JSON dari '
                'aplikasi (Settings > Export backup), karena fitur Import backup '
                'mengganti seluruh data, bukan menambah.'
            )
        with open(args.base, encoding='utf-8') as handle:
            base = json.load(handle)
        output = json.dumps(to_cashflowplus_backup(chosen, category_rules, config, base),
                            ensure_ascii=False, indent=2)
    else:
        raise SystemExit(f'format tidak dikenal: {args.format}')

    if args.out:
        Path(args.out).write_text(output, encoding='utf-8')
        print(f'{len(chosen)} transaksi ditulis ke {args.out}')
    else:
        print(output)
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    config = load_config()
    category_rules = categorize_module.load_rules()
    chosen = _select(ledger.load_all(), args.month, args.status)
    result = moneylover.push(chosen, category_rules, config, dry_run=not args.live)
    if result.get('sent') and not result['dry_run']:
        ledger.update_status(result['sent'], STATUS_IMPORTED)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get('errors') else 0


def cmd_check(args: argparse.Namespace) -> int:
    """Pastikan token Money Lover masih hidup sebelum run harian mengandalkannya.

    Token sesi web hanya berumur sekitar seminggu, jadi kegagalan paling sering
    bukan pada parsing email melainkan pada token yang kedaluwarsa.
    """
    config = load_config()
    settings = config.get('moneylover', {})
    token_info = moneylover.describe_token()
    if token_info.get('sudah_kedaluwarsa'):
        print(json.dumps({'ok': False, 'token': token_info,
                          'error': 'token sudah kedaluwarsa, ambil ulang dari browser'},
                         ensure_ascii=False, indent=2))
        return 2
    try:
        client = moneylover.MoneyLoverClient(base_url=settings.get('base_url', moneylover.DEFAULT_BASE_URL))
        wallets = client.wallets()
    except moneylover.MoneyLoverError as exc:
        print(json.dumps({'ok': False, 'token': token_info, 'error': str(exc)},
                         ensure_ascii=False, indent=2))
        return 2

    report: Dict[str, Any] = {
        'ok': True,
        'metode_auth': client.auth_method,
        # Token yang benar-benar dipakai, bukan sekadar isi environment
        # variable, karena token bisa berasal dari login atau dari singgahan.
        'token': moneylover.describe_token(client.token),
        'dompet_di_money_lover': [item.get('name') for item in wallets],
    }
    override = settings.get('category_map', {})
    all_categories = categorize_module.load_rules()['categories']
    wanted = {
        key: (override.get(key) or meta.get('moneylover') or '')
        for key, meta in all_categories.items()
    }
    parents = {key: (meta.get('moneylover_parent') or '') for key, meta in all_categories.items()}
    # Kategori tanpa padanan sama sekali perlu disebut terpisah: transaksinya
    # tertahan di staging dan tidak akan pernah terkirim sampai dipetakan.
    report['kategori_tanpa_padanan'] = sorted(key for key, name in wanted.items() if not name)

    # Setiap dompet punya daftar kategorinya sendiri, jadi semuanya diperiksa,
    # bukan hanya satu. Satu dompet yang tertinggal berarti transaksi dari
    # rekening itu gagal diam-diam saat run harian.
    targets = collections.OrderedDict()
    for account in config.get('own_accounts', []):
        name = account.get('moneylover_wallet')
        if name:
            targets.setdefault(name, []).append(account.get('bank') or account.get('id'))
    if settings.get('default_wallet'):
        targets.setdefault(settings['default_wallet'], ['(default)'])

    report['pemetaan_dompet'] = {name: banks for name, banks in targets.items()}
    audit = moneylover.audit_wallets(wallets, list(targets))
    report['audit_dompet'] = audit
    # Mengirim ke dompet yang diarsipkan atau dikeluarkan dari total akan
    # mengacaukan angka yang justru ingin dijaga, jadi itu kesalahan, bukan
    # sekadar catatan.
    if audit['dipetakan_tapi_diabaikan'] or audit['dipetakan_tapi_tidak_ada']:
        report['ok'] = False
    if not targets:
        report['ok'] = False
        report['error'] = 'own_accounts belum memetakan satu pun dompet di automation/config.json'

    per_wallet = collections.OrderedDict()
    for name in targets:
        try:
            resolved = moneylover.resolve_ids(client, name)
        except moneylover.MoneyLoverError as exc:
            per_wallet[name] = {'ok': False, 'error': str(exc)}
            report['ok'] = False
            continue
        # Sub-kategori yang tidak ada di dompet ini tetap terkirim lewat
        # induknya, jadi hanya yang induknya pun tidak ada yang benar-benar
        # menghambat.
        tertutup_induk, menghambat = set(), set()
        for key, value in wanted.items():
            if not value or value.lower() in resolved['categories']:
                continue
            parent = parents.get(key) or ''
            if parent and parent.lower() in resolved['categories']:
                tertutup_induk.add(f'{value} -> {parent}')
            else:
                menghambat.add(value)
        per_wallet[name] = {
            'ok': not menghambat,
            'jumlah_kategori': len(resolved['categories']),
            'dikirim_ke_induk': sorted(tertutup_induk),
            'menghambat': sorted(menghambat),
        }
        if menghambat:
            report['ok'] = False
    report['per_dompet'] = per_wallet
    if report['kategori_tanpa_padanan']:
        report['ok'] = False
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


def cmd_tag(args: argparse.Namespace) -> int:
    """Tambahkan catatan ke transaksi lalu hitung ulang kategorinya.

    Inilah cara menandai perjalanan dinas: penandanya masuk ke catatan, lalu
    context_overrides memindahkan kategorinya ke pohon Business trip.
    """
    config = load_config()
    category_rules = categorize_module.load_rules()

    def mutate(item):
        item.note = ' | '.join(part for part in [item.note, args.note] if part)
        item.reasons.append(f'catatan manual ditambahkan: {args.note!r}')
        categorize_module.categorize(item, category_rules, config)

    touched = ledger.transform(args.fingerprint, mutate)
    if not touched:
        print('tidak ada transaksi dengan fingerprint tersebut')
        return 1
    for item in touched:
        print(f'{item.fingerprint} -> {item.category_label} ({item.category}), status {item.status}')
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    """Tetapkan kategori sebuah transaksi berdasarkan keputusan manusia.

    ``tag`` hanya bekerja bila ada context_override yang cocok; perintah ini
    dipakai untuk kasus yang jauh lebih sering: merchant tak dikenal yang
    kategorinya hanya pemiliknya yang tahu. Status langsung menjadi 'ready'
    karena keputusannya sudah diambil, bukan lagi ditebak.
    """
    category_rules = categorize_module.load_rules()
    categories = category_rules['categories']
    if args.category not in categories:
        print(f"kategori '{args.category}' tidak dikenal.", file=sys.stderr)
        print('Pilihan yang tersedia:', file=sys.stderr)
        for key, meta in sorted(categories.items()):
            print(f"  {key:28} {meta.get('label', key)}", file=sys.stderr)
        return 1

    meta = categories[args.category]

    def mutate(item):
        previous = item.category
        item.category = args.category
        item.category_label = meta.get('label', args.category)
        if meta.get('type') == 'transfer':
            item.kind = 'transfer'
        item.status = STATUS_READY
        item.reasons.append(
            f"kategori ditetapkan manual: '{previous}' -> '{args.category}'"
        )

    touched = ledger.transform(args.fingerprint, mutate)
    if not touched:
        print('tidak ada transaksi dengan fingerprint tersebut', file=sys.stderr)
        return 1
    for item in touched:
        print(f'{item.fingerprint} -> {item.category_label} ({item.category}), status {item.status}')
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    changed = ledger.update_status(args.fingerprint, args.status)
    print(f'{changed} transaksi diubah menjadi {args.status}')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='automation', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest='command', required=True)

    ingest = subparsers.add_parser('ingest', help='baca email mentah dan simpan ke staging')
    ingest.add_argument('--input', default='-', help='berkas JSON email, atau - untuk stdin')
    ingest.add_argument('--dry-run', action='store_true', help='jangan tulis ke staging')
    ingest.add_argument('--json', action='store_true', help='keluarkan JSON, bukan tabel')
    ingest.set_defaults(func=cmd_ingest)

    review = subparsers.add_parser('review', help='tampilkan isi staging')
    review.add_argument('--month', default='', help='filter bulan YYYY-MM')
    review.add_argument('--status', default=STATUS_REVIEW,
                        choices=['all', 'ready', 'needs_review', 'imported', 'ignored', 'duplicate'])
    review.add_argument('--json', action='store_true')
    review.set_defaults(func=cmd_review)

    export = subparsers.add_parser('export', help='ekspor ke format tujuan')
    export.add_argument('--format', required=True,
                        choices=['moneylover-csv', 'cashflowplus', 'markdown'])
    export.add_argument('--month', default='')
    export.add_argument('--status', default=STATUS_READY,
                        choices=['all', 'ready', 'needs_review', 'imported', 'ignored', 'duplicate'])
    export.add_argument('--base', help='berkas backup My CashFlow+ untuk format cashflowplus')
    export.add_argument('--out', help='tulis ke berkas, default stdout')
    export.set_defaults(func=cmd_export)

    push = subparsers.add_parser('push', help='kirim ke Money Lover lewat API tidak resmi')
    push.add_argument('--month', default='')
    push.add_argument('--status', default=STATUS_READY,
                      choices=['all', 'ready', 'needs_review', 'imported', 'ignored', 'duplicate'])
    push.add_argument('--live', action='store_true', help='benar-benar kirim (default dry-run)')
    push.set_defaults(func=cmd_push)

    tag = subparsers.add_parser(
        'tag', help='tambahkan catatan ke transaksi dan hitung ulang kategorinya')
    tag.add_argument('fingerprint', nargs='+')
    tag.add_argument('--note', required=True,
                     help='contoh: "perjalanan dinas" untuk memindahkannya ke pohon Business trip')
    tag.set_defaults(func=cmd_tag)

    decide = subparsers.add_parser(
        'decide', help='tetapkan kategori sebuah transaksi yang menunggu keputusan')
    decide.add_argument('fingerprint', nargs='+')
    decide.add_argument('--category', required=True,
                        help='kunci kategori di rules/categories.json, misalnya makan_minum')
    decide.set_defaults(func=cmd_decide)

    check = subparsers.add_parser('check', help='cek token dan pemetaan kategori Money Lover')
    check.set_defaults(func=cmd_check)

    mark = subparsers.add_parser('mark', help='ubah status transaksi di staging')
    mark.add_argument('fingerprint', nargs='+')
    mark.add_argument('--status', required=True,
                      choices=['ready', 'needs_review', 'imported', 'ignored'])
    mark.set_defaults(func=cmd_mark)

    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
