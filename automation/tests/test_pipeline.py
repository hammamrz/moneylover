"""Uji end-to-end pipeline tanpa menyentuh Gmail maupun Money Lover."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from automation import categorize as categorize_module
from automation import cli
from automation import extract as extract_module
from automation import ledger
from automation.exporters import to_cashflowplus_backup, to_moneylover_csv
from automation.models import RawEmail, Transaction, STATUS_READY, STATUS_REVIEW
from automation.normalize import ParseError, parse_amount, parse_date

FIXTURES = Path(__file__).parent / 'fixtures_emails.json'
WIB = timezone(timedelta(hours=7))


def build_config() -> dict:
    with open(Path(__file__).parents[1] / 'config.json', encoding='utf-8') as handle:
        config = json.load(handle)
    config['own_accounts'][0]['last4'] = '7890'
    config['thresholds']['large_amount_review'] = 5000000
    return config


def run_pipeline(config: dict):
    with open(FIXTURES, encoding='utf-8') as handle:
        emails = [RawEmail.from_dict(item) for item in json.load(handle)]
    found = extract_module.extract_many(emails, extract_module.load_rules(), config)
    return categorize_module.categorize_many(found, categorize_module.load_rules(), config)


class TestNormalize(unittest.TestCase):
    def test_amount_formats(self):
        self.assertEqual(parse_amount('Rp125.000,00'), 125000.0)
        self.assertEqual(parse_amount('IDR 1,250,000.50'), 1250000.50)
        self.assertEqual(parse_amount('349.000'), 349000.0)
        self.assertEqual(parse_amount('12,5'), 12.5)
        self.assertEqual(parse_amount(-45000), 45000.0)

    def test_amount_rejects_garbage(self):
        with self.assertRaises(ParseError):
            parse_amount('tidak ada angka')

    def test_date_formats(self):
        self.assertEqual(parse_date('18/09/2026 14:32:05').hour, 14)
        self.assertEqual(parse_date('12 Sep 2026').month, 9)
        self.assertEqual(parse_date('5 Desember 2026').month, 12)
        self.assertEqual(parse_date('2026-09-18T10:00:00Z').tzinfo, timezone.utc)

    def test_date_defaults_to_jakarta(self):
        self.assertEqual(parse_date('18/09/2026').utcoffset(), timedelta(hours=7))


class TestExtraction(unittest.TestCase):
    def setUp(self):
        self.config = build_config()
        self.transactions = run_pipeline(self.config)

    def test_newsletter_is_not_a_transaction(self):
        subjects = [item.email.subject for item in self.transactions]
        self.assertNotIn('5 lowongan baru untuk kamu', subjects)

    def test_amount_and_date_come_from_body_not_email_header(self):
        qris = next(item for item in self.transactions if item.email.message_id == 'msg-bca-qris')
        self.assertEqual(qris.amount, 125000.0)
        self.assertEqual(qris.date.strftime('%Y-%m-%d %H:%M'), '2026-09-18 14:32')
        self.assertEqual(qris.reference, 'TRX889123')
        self.assertEqual(qris.account, '****7890')

    def test_account_number_is_masked(self):
        for item in self.transactions:
            self.assertNotIn('1234567890', item.account)


class TestCategorization(unittest.TestCase):
    def setUp(self):
        self.config = build_config()
        self.transactions = run_pipeline(self.config)
        self.by_id = {item.email.message_id: item for item in self.transactions}

    def test_user_rules(self):
        # Aturan menyasar sub-kategori, bukan kategori induk: Grab masuk ke
        # Taxi,Ojol dan PLN ke Electricity Bill, sesuai pohon kategori akun.
        self.assertEqual(self.by_id['msg-bca-qris'].category, 'taksi_ojol')
        self.assertEqual(self.by_id['msg-pln'].category, 'listrik')
        self.assertEqual(self.by_id['msg-tokopedia'].category, 'belanja')

    def test_categories_map_to_names_that_exist_in_money_lover(self):
        rules = categorize_module.load_rules()
        for item in self.transactions:
            meta = rules['categories'][item.category]
            self.assertIn('moneylover', meta)
            self.assertIn('cashflowplus', meta)

    def test_salary_is_income(self):
        salary = self.by_id['msg-gaji']
        self.assertEqual(salary.kind, 'income')
        self.assertEqual(salary.category, 'gaji')

    def test_large_amount_forced_to_review(self):
        self.assertNotEqual(self.by_id['msg-gaji'].status, STATUS_READY)

    def test_internal_transfer_pair_becomes_one_transaction(self):
        self.assertIn('msg-topup-out', self.by_id)
        self.assertNotIn('msg-topup-in', self.by_id)
        self.assertEqual(self.by_id['msg-topup-out'].kind, 'transfer')

    def test_transfer_is_not_counted_as_expense(self):
        expenses = [item for item in self.transactions if item.kind == 'expense']
        self.assertNotIn('transfer', {item.category for item in expenses})


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.config = build_config()
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_second_run_adds_nothing(self):
        first, _ = ledger.merge(run_pipeline(self.config), self.config, self.data_dir)
        second, duplicates = ledger.merge(run_pipeline(self.config), self.config, self.data_dir)
        self.assertTrue(first)
        self.assertEqual(second, [])
        self.assertEqual(len(duplicates), len(first))
        self.assertEqual(len(ledger.load_all(self.data_dir)), len(first))

    def test_same_transaction_from_two_senders_is_one_row(self):
        base = dict(amount=125000.0, direction='debit', kind='expense', merchant='Grab Trip')
        first = Transaction(date=datetime(2026, 9, 18, 14, 32, tzinfo=WIB), bank='BCA', **base)
        echo = Transaction(date=datetime(2026, 9, 18, 14, 34, tzinfo=WIB), bank='Grab', **base)
        added, duplicates = ledger.merge([first, echo], self.config, self.data_dir)
        self.assertEqual(len(added), 1)
        self.assertEqual(len(duplicates), 1)

    def test_status_update_persists(self):
        added, _ = ledger.merge(run_pipeline(self.config), self.config, self.data_dir)
        target = added[0].fingerprint
        self.assertEqual(ledger.update_status([target], 'imported', self.data_dir), 1)
        stored = {item.fingerprint: item.status for item in ledger.load_all(self.data_dir)}
        self.assertEqual(stored[target], 'imported')


class TestExporters(unittest.TestCase):
    def setUp(self):
        self.config = build_config()
        self.rules = categorize_module.load_rules()
        self.transactions = run_pipeline(self.config)

    def test_moneylover_csv_signs(self):
        rows = to_moneylover_csv(self.transactions, self.rules, self.config).strip().splitlines()
        self.assertEqual(rows[0], 'Date,Amount,Category,Note,Wallet')
        amounts = [float(row.split(',')[1]) for row in rows[1:]]
        self.assertTrue(any(value < 0 for value in amounts))
        self.assertTrue(any(value > 0 for value in amounts))

    def test_cashflowplus_merge_keeps_existing_entries(self):
        base = {
            'accountName': 'Primary Wallet',
            'initialBalance': 0,
            'books': [{'id': 1, 'name': 'Personal Finance', 'createdAt': '2026-01-01T00:00:00.000'}],
            'currentBookId': 1,
            'month': '2026-09-01T00:00:00.000',
            'categories': [
                {'id': 1, 'name': 'Transport', 'type': 'expense'},
                {'id': 2, 'name': 'Other', 'type': 'expense'},
                {'id': 3, 'name': 'Salary', 'type': 'income'},
            ],
            'entries': [{'id': 7, 'bookId': 1, 'amount': 1000.0, 'type': 'expense',
                         'title': 'Lama', 'categoryId': 2, 'paymentMethod': 'cash',
                         'date': '2026-09-01T00:00:00.000'}],
            'overallLimit': 0,
            'prefs': {},
        }
        merged = to_cashflowplus_backup(self.transactions, self.rules, self.config, base)
        self.assertEqual(len(merged['entries']), 1 + len(self.transactions))
        ids = [entry['id'] for entry in merged['entries']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(merged['entries'][0]['title'], 'Lama')
        grab = next(entry for entry in merged['entries'] if 'GRAB' in entry['title'].upper())
        self.assertEqual(grab['categoryId'], 1)
        self.assertEqual(base['entries'][0]['id'], 7)


class TestRealEmailFormats(unittest.TestCase):
    """Format asli tiap penerbit, dianonimkan dari email yang benar-benar masuk."""

    REAL = Path(__file__).parent / 'fixtures_real_formats.json'

    def setUp(self):
        self.config = build_config()
        self.source_rules = extract_module.load_rules()
        self.category_rules = categorize_module.load_rules()
        with open(self.REAL, encoding='utf-8') as handle:
            self.emails = {item['id']: RawEmail.from_dict(item) for item in json.load(handle)}

    def parse(self, key):
        found = extract_module.extract(self.emails[key], self.source_rules, self.config)
        if found is not None:
            categorize_module.categorize(found, self.category_rules, self.config)
        return found

    def test_jago_payment_table_layout(self):
        item = self.parse('jago-payment')
        self.assertEqual(item.amount, 281435.0)
        self.assertEqual(item.date.strftime('%Y-%m-%d %H:%M'), '2026-09-20 20:29')
        self.assertEqual(item.direction, 'debit')
        self.assertEqual(item.merchant, 'TPC&TB BINTARO X CH')
        self.assertEqual(categorize_module.wallet_for(item, self.config), 'Bank Jago')

    def test_jago_incoming_uses_sender_not_own_pocket(self):
        item = self.parse('jago-received')
        self.assertEqual(item.direction, 'credit')
        self.assertEqual(item.kind, 'income')
        # Sisi 'To' adalah kantong sendiri, jadi yang dicatat harus pengirimnya.
        self.assertNotEqual(item.merchant, 'MA')
        self.assertIn('PENGIRIM', item.merchant.upper())

    def test_bni_wondr_joins_date_and_time_from_separate_rows(self):
        item = self.parse('bni-wondr-qris')
        self.assertEqual(item.amount, 213400.0)
        self.assertEqual(item.date.strftime('%Y-%m-%d %H:%M:%S'), '2026-09-19 14:20:10')
        self.assertEqual(item.merchant, 'SARI INDAH JAKARTA MBL')
        self.assertEqual(item.reference, '20260919141959000416')

    def test_mandiri_livin_layout(self):
        item = self.parse('mandiri-qr')
        self.assertEqual(item.amount, 63423.0)
        self.assertEqual(item.date.strftime('%Y-%m-%d %H:%M:%S'), '2026-09-20 08:16:05')
        self.assertEqual(categorize_module.wallet_for(item, self.config), 'Rekening Mandiri')

    def test_transfer_to_someone_else_at_the_same_bank_is_an_expense(self):
        # Penerima kebetulan memakai bank yang sama dengan rekening sendiri;
        # itu tetap pengeluaran, bukan pindah buku.
        item = self.parse('mandiri-transfer')
        self.assertNotEqual(item.kind, 'transfer')
        self.assertEqual(item.amount, 100000.0)

    def test_credit_card_email_is_not_recorded_at_all(self):
        # Dompet kartu kredit dikeluarkan dari total, jadi penerbitnya terdaftar
        # di ignored_sources. Sebelumnya transaksinya terurai lalu tertahan tanpa
        # dompet, dan menumpuk di antrean review tanpa pernah bisa diputuskan.
        self.assertIn('BNI Kartu Kredit', self.config['ignored_sources'])
        self.assertIsNone(self.parse('bni-cc'))

    def test_credit_card_email_is_still_parseable_when_monitored_again(self):
        # Aturan penerbitnya tetap utuh; yang menghentikannya hanya keputusan di
        # config, sehingga memantau lagi cukup mengosongkan ignored_sources.
        config = dict(self.config, ignored_sources=[])
        item = extract_module.extract(self.emails['bni-cc'], self.source_rules, config)
        self.assertIsNotNone(item)
        self.assertEqual(item.amount, 64500.0)
        self.assertEqual(item.bank, 'BNI Kartu Kredit')

    def test_an_unknown_issuer_is_held_not_dropped(self):
        # Pembeda pentingnya: diabaikan hanya yang sudah diputuskan diabaikan.
        # Penerbit yang belum dikenal harus tetap muncul supaya ketahuan.
        config = dict(self.config, ignored_sources=['Bank Antah Berantah'])
        item = Transaction(date=datetime(2026, 9, 18, 10, 0, tzinfo=WIB), amount=50000,
                           direction='debit', kind='expense', merchant='Warung',
                           bank='Bank Baru Yang Belum Dikenal')
        self.assertEqual(categorize_module.wallet_for(item, config), '')

    def test_jago_topup_uses_colon_labels_and_counts_as_transfer(self):
        item = self.parse('jago-topup')
        self.assertEqual(item.amount, 200000.0)
        self.assertEqual(item.date.strftime('%Y-%m-%d %H:%M'), '2026-09-21 11:48')
        # Mengisi dompet sendiri adalah pindah buku, bukan pengeluaran baru.
        self.assertEqual(item.kind, 'transfer')

    def test_virtual_account_layout_and_hyphenated_month(self):
        item = self.parse('bni-virtual-account')
        self.assertEqual(item.amount, 35000.0)
        self.assertEqual(item.date.strftime('%Y-%m-%d %H:%M'), '2026-09-20 18:32')
        self.assertIn('Finpay', item.merchant)
        self.assertEqual(item.category, 'internet')

    def test_non_transaction_emails_are_skipped(self):
        self.assertIsNone(self.parse('jago-survey'))
        self.assertIsNone(self.parse('bni-ebilling'))

    def test_success_wording_does_not_leak_into_a_category(self):
        # 'erha' pernah cocok di dalam 'berhasil' dan menjadikan setiap
        # notifikasi sukses sebagai perawatan diri.
        for key in ('bni-wondr-qris', 'mandiri-qr'):
            with self.subTest(key=key):
                self.assertNotEqual(self.parse(key).category, 'perawatan_diri')

    def test_unknown_merchant_lands_in_uncategorized_not_other(self):
        item = self.parse('bni-wondr-qris')
        self.assertEqual(item.category, 'tidak_terkategori')
        self.assertEqual(self.category_rules['categories'][item.category]['moneylover'],
                         'Uncategorized Expense')


class TestWalletRouting(unittest.TestCase):
    """Tiap transaksi harus mendarat di dompet rekening asalnya."""

    def setUp(self):
        self.config = build_config()
        self.rules = categorize_module.load_rules()

    def build(self, bank):
        return Transaction(date=datetime(2026, 9, 18, 10, 0, tzinfo=WIB), amount=50000,
                           direction='debit', kind='expense', merchant='Warung', bank=bank)

    def test_each_bank_routes_to_its_own_wallet(self):
        expected = {
            'BNI': 'Rekening BNI IP',
            'Bank Mandiri': 'Rekening Mandiri',
            'Bank Jago': 'Bank Jago',
            'Superbank': 'Superbank',
            'GoPay': 'Gopai',
        }
        for bank, wallet in expected.items():
            with self.subTest(bank=bank):
                self.assertEqual(categorize_module.wallet_for(self.build(bank), self.config), wallet)

    def test_unknown_issuer_gets_no_wallet(self):
        # Lebih baik tertahan daripada mendarat di dompet yang salah: satu
        # transaksi di dompet keliru merusak saldo dua rekening sekaligus.
        self.assertEqual(categorize_module.wallet_for(self.build('Bank Antah Berantah'), self.config), '')
        self.assertEqual(categorize_module.wallet_for(self.build(''), self.config), '')

    def test_wallet_names_match_money_lover_exactly(self):
        for account in self.config['own_accounts']:
            with self.subTest(account=account['id']):
                self.assertTrue(account['moneylover_wallet'])
                self.assertEqual(account['moneylover_wallet'], account['moneylover_wallet'].strip())

    def test_every_own_account_bank_exists_in_sources(self):
        labels = {source['label'] for source in extract_module.load_rules()['sources']}
        for account in self.config['own_accounts']:
            with self.subTest(account=account['id']):
                self.assertIn(account['bank'], labels)

    def test_merchant_email_first_still_ends_up_with_a_wallet(self):
        # Struk Tokopedia tidak menyebut rekening; notifikasi BNI menyebutnya.
        # Urutan kedatangan email tidak boleh menentukan hasil akhir.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        data_dir = Path(temp.name)

        common = dict(amount=349000.0, direction='debit', kind='expense')
        struk = Transaction(date=datetime(2026, 9, 12, 19, 58, tzinfo=WIB),
                            merchant='Tokopedia', bank='Tokopedia', source_id='tokopedia', **common)
        notifikasi = Transaction(date=datetime(2026, 9, 12, 20, 0, tzinfo=WIB),
                                 merchant='Tokopedia', bank='BNI', source_id='bni',
                                 account='****1234', **common)

        added, duplicates = ledger.merge([struk], self.config, data_dir)
        self.assertEqual(categorize_module.wallet_for(added[0], self.config), '')

        ledger.merge([notifikasi], self.config, data_dir)
        stored = ledger.load_all(data_dir)
        self.assertEqual(len(stored), 1)
        self.assertEqual(categorize_module.wallet_for(stored[0], self.config), 'Rekening BNI IP')
        self.assertEqual(stored[0].account, '****1234')

    def test_bank_email_first_is_not_downgraded_by_merchant_email(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        data_dir = Path(temp.name)

        common = dict(amount=349000.0, direction='debit', kind='expense', merchant='Tokopedia')
        notifikasi = Transaction(date=datetime(2026, 9, 12, 20, 0, tzinfo=WIB),
                                 bank='BNI', source_id='bni', **common)
        struk = Transaction(date=datetime(2026, 9, 12, 19, 58, tzinfo=WIB),
                            bank='Tokopedia', source_id='tokopedia', **common)

        ledger.merge([notifikasi], self.config, data_dir)
        ledger.merge([struk], self.config, data_dir)
        stored = ledger.load_all(data_dir)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].bank, 'BNI')

    def test_audit_flags_archived_and_excluded_wallets(self):
        from automation.adapters import moneylover
        wallets = [
            {'name': 'Rekening BNI IP', 'exclude_total': False, 'archived': False},
            {'name': 'Gopai', 'exclude_total': False, 'archived': False},
            {'name': 'Superbank', 'exclude_total': False, 'archived': False},
            {'name': 'CCBNI', 'exclude_total': True, 'archived': False},
            {'name': 'Rekening Blu', 'exclude_total': True, 'archived': True},
        ]
        audit = moneylover.audit_wallets(wallets, ['Rekening BNI IP', 'CCBNI', 'Dompet Hantu'])
        self.assertEqual(audit['ikut_total'], ['Gopai', 'Rekening BNI IP', 'Superbank'])
        self.assertEqual(audit['belum_dipetakan'], ['Gopai', 'Superbank'])
        self.assertEqual(audit['dipetakan_tapi_diabaikan'], ['CCBNI'])
        self.assertEqual(audit['dipetakan_tapi_tidak_ada'], ['Dompet Hantu'])

    def test_config_maps_only_wallets_that_count_toward_total(self):
        from automation.adapters import moneylover
        # Snapshot dari akun sebenarnya: hanya lima dompet ini yang ikut total
        # dan belum diarsipkan.
        wallets = [
            {'name': 'Bank Jago', 'exclude_total': False, 'archived': False},
            {'name': 'Superbank', 'exclude_total': False, 'archived': False},
            {'name': 'Rekening BNI IP', 'exclude_total': False, 'archived': False},
            {'name': 'Rekening Mandiri', 'exclude_total': False, 'archived': False},
            {'name': 'Gopai', 'exclude_total': False, 'archived': False},
            {'name': 'CCBNI', 'exclude_total': True, 'archived': False},
            {'name': 'Bibit', 'exclude_total': True, 'archived': False},
            {'name': 'Rekening Blu', 'exclude_total': True, 'archived': True},
        ]
        mapped = [account['moneylover_wallet'] for account in self.config['own_accounts']]
        audit = moneylover.audit_wallets(wallets, mapped)
        self.assertEqual(audit['belum_dipetakan'], [])
        self.assertEqual(audit['dipetakan_tapi_diabaikan'], [])
        self.assertEqual(audit['dipetakan_tapi_tidak_ada'], [])

    def test_push_dry_run_plans_one_wallet_per_transaction(self):
        from automation.adapters import moneylover
        items = []
        for bank in ('BNI', 'GoPay', 'Bank Antah Berantah'):
            item = self.build(bank)
            categorize_module.categorize(item, self.rules, self.config)
            items.append(item)
        result = moneylover.push(items, self.rules, self.config, dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertEqual([row['wallet'] for row in result['planned']],
                         ['Rekening BNI IP', 'Gopai', ''])


class TestBusinessTripMarker(unittest.TestCase):
    """Penanda 'perjalanan dinas' memindahkan kategori ke pohon Business trip."""

    def setUp(self):
        self.config = build_config()
        self.rules = categorize_module.load_rules()

    def build(self, merchant, note=''):
        item = Transaction(date=datetime(2026, 9, 18, 10, 0, tzinfo=WIB), amount=1500000,
                           direction='debit', kind='expense', merchant=merchant, note=note)
        return categorize_module.categorize(item, self.rules, self.config)

    def test_without_marker_defaults_to_vacation_tree(self):
        self.assertEqual(self.build('Agoda Hotel Bali').category, 'hotel')
        self.assertEqual(self.build('Garuda Indonesia').category, 'pesawat')

    def test_marker_moves_hotel_and_flight_to_business_tree(self):
        self.assertEqual(self.build('Agoda Hotel Bali', 'perjalanan dinas').category, 'hotel_dinas')
        self.assertEqual(self.build('Garuda Indonesia', 'perjalanan dinas').category, 'pesawat_dinas')

    def test_marker_moves_meals_to_meals_biz_trip(self):
        for merchant in ('Warung Padang', 'Starbucks', 'Holland Bakery'):
            with self.subTest(merchant=merchant):
                self.assertEqual(self.build(merchant, 'perjalanan dinas').category, 'makan_dinas')

    def test_marker_leaves_unrelated_categories_alone(self):
        item = self.build('Token listrik PLN', 'perjalanan dinas')
        self.assertEqual(item.category, 'listrik')

    def test_reason_records_the_marker(self):
        item = self.build('Agoda Hotel Bali', 'perjalanan dinas')
        self.assertTrue(any('perjalanan dinas' in reason for reason in item.reasons))

    def test_business_tree_maps_to_money_lover_names(self):
        names = {key: self.rules['categories'][key]['moneylover']
                 for key in ('hotel_dinas', 'pesawat_dinas', 'kereta_dinas',
                             'bus_dinas', 'makan_dinas', 'perjalanan_dinas')}
        self.assertEqual(names, {
            'hotel_dinas': 'Hotel',
            'pesawat_dinas': 'Flight',
            'kereta_dinas': 'Trains',
            'bus_dinas': 'Bus/Travel',
            'makan_dinas': 'Meals Biz Trip',
            'perjalanan_dinas': 'Business trip',
        })


class TestTagRecategorises(unittest.TestCase):
    def setUp(self):
        self.config = build_config()
        self.rules = categorize_module.load_rules()
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_note_added_later_changes_the_category(self):
        item = Transaction(date=datetime(2026, 9, 18, 10, 0, tzinfo=WIB), amount=2000000,
                           direction='debit', kind='expense', merchant='Hotel Santika')
        categorize_module.categorize(item, self.rules, self.config)
        ledger.merge([item], self.config, self.data_dir)
        target = item.fingerprint

        def mutate(stored):
            stored.note = 'perjalanan dinas'
            categorize_module.categorize(stored, self.rules, self.config)

        touched = ledger.transform([target], mutate, self.data_dir)
        self.assertEqual(len(touched), 1)
        stored = {row.fingerprint: row for row in ledger.load_all(self.data_dir)}
        self.assertEqual(stored[target].category, 'hotel_dinas')
        # Fingerprint tidak boleh bergeser, supaya dedup lintas run tetap jalan.
        self.assertIn(target, stored)


class TestMoneyLoverLogin(unittest.TestCase):
    """Alur login diuji dengan transport tiruan, tanpa menyentuh server."""

    def setUp(self):
        from automation.adapters import moneylover
        self.moneylover = moneylover
        self.calls = []

        def fake_request(url, headers, payload=None, timeout=30):
            self.calls.append({'url': url, 'headers': headers, 'payload': payload})
            if url.endswith('/user/login-url'):
                return {'data': {'request_token': 'REQ123',
                                 'login_url': 'https://oauth.moneylover.me/?client=CLIENT9&x=1'}}
            return {'access_token': 'JWT-HASIL-LOGIN'}

        original = moneylover._request
        moneylover._request = fake_request
        self.addCleanup(setattr, moneylover, '_request', original)

    def test_two_step_flow_sends_request_token_and_client(self):
        token = self.moneylover.login('orang@example.com', 'kata-sandi')
        self.assertEqual(token, 'JWT-HASIL-LOGIN')
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[0]['url'].endswith('/user/login-url'))
        self.assertEqual(self.calls[1]['url'], self.moneylover.OAUTH_URL)
        self.assertEqual(self.calls[1]['headers']['Authorization'], 'Bearer REQ123')
        self.assertEqual(self.calls[1]['headers']['Client'], 'CLIENT9')

    def test_handshake_step_carries_no_credentials(self):
        self.moneylover.login('orang@example.com', 'kata-sandi')
        self.assertNotIn('kata-sandi', json.dumps(self.calls[0]))
        self.assertNotIn('orang@example.com', json.dumps(self.calls[0]))

    def test_login_rejected_without_access_token(self):
        def refuse(url, headers, payload=None, timeout=30):
            if url.endswith('/user/login-url'):
                return {'data': {'request_token': 'REQ123',
                                 'login_url': 'https://oauth.moneylover.me/?client=CLIENT9'}}
            return {'error': 1, 'msg': 'wrong password'}

        self.moneylover._request = refuse
        with self.assertRaises(self.moneylover.MoneyLoverError) as ctx:
            self.moneylover.login('orang@example.com', 'salah')
        self.assertIn('wrong password', str(ctx.exception))

    def test_missing_client_id_is_reported(self):
        def no_client(url, headers, payload=None, timeout=30):
            return {'data': {'request_token': 'REQ123', 'login_url': 'https://oauth.moneylover.me/'}}

        self.moneylover._request = no_client
        with self.assertRaises(self.moneylover.MoneyLoverError) as ctx:
            self.moneylover.login('orang@example.com', 'kata-sandi')
        self.assertIn('client id', str(ctx.exception))

    def test_token_prefix_from_browser_is_stripped(self):
        client = self.moneylover.MoneyLoverClient(token='AuthJWT abc.def.ghi')
        self.assertEqual(client.token, 'abc.def.ghi')
        self.assertEqual(client.auth_method, 'token')


class TestUndecidedCategoriesAreHeld(unittest.TestCase):
    """Transaksi tanpa kategori tidak boleh terkirim otomatis.

    Confidence mengukur keyakinan pada nominal dan tanggal, bukan pada kategori.
    Email yang terurai sempurna tapi merchantnya tak dikenal justru bernilai
    tinggi, sehingga dulu lolos sebagai 'ready' dan mendarat di Money Lover
    sebagai 'Uncategorized Expense' tanpa pernah dilihat manusia.
    """

    def setUp(self):
        self.config = build_config()
        self.rules = categorize_module.load_rules()

    def build(self, merchant, direction='debit', kind='expense', confidence=1.0):
        return Transaction(date=datetime(2026, 9, 18, 10, 0, tzinfo=WIB), amount=50000,
                           direction=direction, kind=kind, merchant=merchant,
                           bank='BNI', confidence=confidence)

    def test_unknown_merchant_is_held_even_at_full_confidence(self):
        item = categorize_module.categorize(self.build('MERCHANT TAK DIKENAL XYZ'),
                                            self.rules, self.config)
        self.assertEqual(item.category, 'tidak_terkategori')
        self.assertEqual(item.status, STATUS_REVIEW)

    def test_unknown_income_is_held_too(self):
        item = categorize_module.categorize(
            self.build('PENGIRIM TAK DIKENAL', direction='credit', kind='income'),
            self.rules, self.config)
        self.assertEqual(item.category, 'tidak_terkategori_masuk')
        self.assertEqual(item.status, STATUS_REVIEW)

    def test_a_recognised_merchant_still_goes_through(self):
        # Pengamannya harus menahan yang ragu saja, bukan melumpuhkan pipeline.
        item = categorize_module.categorize(self.build('Grab A-9RLCNK9GWW4T'),
                                            self.rules, self.config)
        self.assertNotIn(item.category, categorize_module.undecided_categories(self.rules))
        self.assertEqual(item.status, STATUS_READY)

    def test_transfer_is_a_decision_not_an_absence_of_one(self):
        # 'transfer' juga nilai fallback, tapi dipilih karena lawan transaksinya
        # terbukti rekening sendiri. Ia tidak boleh ikut tertahan.
        self.assertNotIn('transfer', categorize_module.undecided_categories(self.rules))

    def test_uncategorized_never_reaches_the_push_plan(self):
        found = run_pipeline(self.config)
        undecided = categorize_module.undecided_categories(self.rules)
        for item in found:
            if item.category in undecided:
                with self.subTest(fingerprint=item.fingerprint):
                    self.assertEqual(item.status, STATUS_REVIEW)


class TestDecideCommand(unittest.TestCase):
    """Keputusan kategori dari manusia harus bisa direkam."""

    def setUp(self):
        self.rules = categorize_module.load_rules()

    def run_decide(self, item, category):
        captured = {}

        def fake_transform(fingerprints, mutate, *args, **kwargs):
            captured['fingerprints'] = list(fingerprints)
            mutate(item)
            return [item]

        original = cli.ledger.transform
        cli.ledger.transform = fake_transform
        self.addCleanup(setattr, cli.ledger, 'transform', original)
        args = argparse.Namespace(fingerprint=[item.fingerprint], category=category)
        # Perintahnya memang mencetak ke stdout/stderr; di sini keluarannya
        # dibuang supaya laporan tes tetap terbaca.
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            return cli.cmd_decide(args)

    def build(self):
        return Transaction(date=datetime(2026, 9, 18, 10, 0, tzinfo=WIB), amount=50000,
                           direction='debit', kind='expense', merchant='WARUNG TAK DIKENAL',
                           bank='BNI', category='tidak_terkategori', status=STATUS_REVIEW)

    def test_a_decision_sets_the_category_and_releases_the_transaction(self):
        item = self.build()
        code = self.run_decide(item, 'makan')
        self.assertEqual(code, 0)
        self.assertEqual(item.category, 'makan')
        self.assertEqual(item.category_label,
                         self.rules['categories']['makan']['label'])
        self.assertEqual(item.status, STATUS_READY)

    def test_an_unknown_category_changes_nothing(self):
        item = self.build()
        code = self.run_decide(item, 'kategori_karangan')
        self.assertEqual(code, 1)
        self.assertEqual(item.category, 'tidak_terkategori')
        self.assertEqual(item.status, STATUS_REVIEW)

    def test_the_reason_records_that_a_human_decided(self):
        item = self.build()
        self.run_decide(item, 'makan')
        self.assertTrue(any('ditetapkan manual' in reason for reason in item.reasons))


if __name__ == '__main__':
    unittest.main(verbosity=2)
