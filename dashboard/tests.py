import json
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils.dateparse import parse_datetime

from .analytics import commercial_metrics, performance_metrics, spend_coverage
from .models import (AirbyteRecord, AnalyticsSource, EditorialChange,
                     FieldDefinition, SubjectEvent)
from .views import _resolve_tags, _spend_by_tag_dimension

User = get_user_model()


class TagAttributionTests(SimpleTestCase):
    def tag(self, id_, name, dimension_slug):
        return {
            'id': id_,
            'name': name,
            'dimension': dimension_slug.title(),
            'dimension_slug': dimension_slug,
        }

    def test_nearest_level_wins_but_keeps_multiple_values_in_same_dimension(self):
        genitori = self.tag(1, 'Genitori', 'audience')
        studenti = self.tag(2, 'Studenti', 'audience')
        dsa = self.tag(3, 'DSA', 'bisogno')
        taggings = {
            ('campaign', 'c1'): {'stage_id': None, 'tags': [genitori, dsa]},
            ('ad_set', 'as1'): {'stage_id': None, 'tags': [studenti, genitori]},
        }

        by_dim, inherited = _resolve_tags(
            'ad', 'ad1', taggings, {'ad1': 'as1'}, {'as1': 'c1'})

        self.assertEqual([t['id'] for t in by_dim['audience']], [2, 1])
        self.assertEqual([t['id'] for t in by_dim['bisogno']], [3])
        self.assertEqual(inherited['audience'], 'ad_set')
        self.assertEqual(inherited['bisogno'], 'campaign')

    def test_spend_is_split_within_dimension_and_not_across_dimensions(self):
        genitori = self.tag(1, 'Genitori', 'audience')
        studenti = self.tag(2, 'Studenti', 'audience')
        dsa = self.tag(3, 'DSA', 'bisogno')
        taggings = {
            ('campaign', 'c1'): {'stage_id': None, 'tags': [dsa]},
            ('ad_set', 'as1'): {'stage_id': None, 'tags': [genitori, studenti]},
        }
        rows = [
            {'date_start': '2026-07-10', 'campaign_id': 'c1', 'adset_id': 'as1',
             'ad_id': 'ad1', 'spend': '100'},
            {'date_start': '2026-07-10', 'campaign_id': 'c2', 'adset_id': 'as2',
             'ad_id': 'ad2', 'spend': '50'},
            {'date_start': '2026-08-01', 'campaign_id': 'c1', 'adset_id': 'as1',
             'ad_id': 'ad1', 'spend': '25'},
        ]

        spend_by_tag, dim_totals, untagged, tagged_total = _spend_by_tag_dimension(
            rows, taggings, {'ad1': 'as1', 'ad2': 'as2'}, {'as1': 'c1', 'as2': 'c2'},
            '2026-07-01', '2026-07-31')

        self.assertEqual(spend_by_tag[1], 50)
        self.assertEqual(spend_by_tag[2], 50)
        self.assertEqual(spend_by_tag[3], 100)
        self.assertEqual(dim_totals['audience'], 100)
        self.assertEqual(dim_totals['bisogno'], 100)
        self.assertEqual(untagged, 50)
        self.assertEqual(tagged_total, 100)


class GenericIngestionTests(TestCase):
    def setUp(self):
        self.source = AnalyticsSource.objects.create(
            name='WUNDT', slug='wundt', identity_mode='pseudonymous_events')
        self.key = self.source.issue_api_key()
        self.url = '/api/v1/ingest/events/'
        self.headers = {
            'HTTP_X_SOURCE_SLUG': 'wundt',
            'HTTP_AUTHORIZATION': f'Bearer {self.key}',
        }
        self.fields = [
            {'namespace': 'marketing', 'name': 'campaign_id',
             'data_type': 'string', 'role': 'dimension',
             'sensitivity': 'internal', 'aggregation': 'none'},
            {'namespace': 'commerce', 'name': 'revenue_eur',
             'data_type': 'number', 'role': 'measure',
             'sensitivity': 'internal', 'aggregation': 'sum'},
        ]

    def event(self, version=1, revenue='245.00'):
        return {
            'schema_version': 1,
            'event_id': 'wundt:payment:1',
            'event_version': version,
            'external_subject_id': 'psn_opaque',
            'event_type': 'purchase',
            'occurred_at': '2026-08-24T10:00:00Z',
            'source_system': 'wundt',
            'dimensions': {'marketing.campaign_id': 'cmp-1'},
            'measures': {'commerce.revenue_eur': revenue},
        }

    def post(self, payload):
        return self.client.post(
            self.url, data=json.dumps(payload), content_type='application/json',
            **self.headers)

    def test_registers_fields_and_ingests_idempotent_versioned_event(self):
        response = self.post({'fields': self.fields, 'events': [self.event()]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['created'], 1)
        event = SubjectEvent.objects.get()
        self.assertEqual(event.external_subject_id, 'psn_opaque')
        self.assertEqual(FieldDefinition.objects.count(), 2)

        replay = self.post({'events': [self.event()]})
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()['stale'], 1)
        updated = self.post({'events': [self.event(version=2, revenue='300.00')]})
        self.assertEqual(updated.status_code, 200)
        event.refresh_from_db()
        self.assertEqual(event.event_version, 2)
        self.assertEqual(event.measures['commerce.revenue_eur'], '300.00')

    def test_rejects_direct_contact_information_and_unregistered_fields(self):
        bad = self.event()
        bad['dimensions']['crm.email'] = 'person@example.com'
        response = self.post({'fields': self.fields, 'events': [bad]})
        self.assertEqual(response.status_code, 400)
        self.assertIn('direct contact information', response.json()['error'])
        self.assertEqual(SubjectEvent.objects.count(), 0)

    def test_aggregate_source_rejects_subject_identifiers(self):
        self.source.identity_mode = 'aggregate_only'
        self.source.save(update_fields=['identity_mode'])
        response = self.post({'fields': self.fields, 'events': [self.event()]})
        self.assertEqual(response.status_code, 400)
        self.assertIn('aggregate_only', response.json()['error'])

    def test_conflicting_same_version_is_rejected(self):
        self.post({'fields': self.fields, 'events': [self.event()]})
        response = self.post({'events': [self.event(revenue='999.00')]})
        self.assertEqual(response.status_code, 400)
        self.assertIn('Conflicting replay', response.json()['error'])


class CommercialMetricsTests(TestCase):
    def setUp(self):
        self.source = AnalyticsSource.objects.create(name='CRM', slug='crm')

    def event(self, event_id, subject, kind, when, revenue=None, campaign=''):
        return SubjectEvent.objects.create(
            source=self.source, event_id=event_id, event_type=kind,
            external_subject_id=subject, occurred_at=parse_datetime(when),
            dimensions={'marketing.campaign_id': campaign} if campaign else {},
            measures={'commerce.revenue_eur': revenue} if revenue else {})

    def test_ltv_cohorts_repeat_recovery_and_campaigns_are_aggregated(self):
        self.event('lead-1', 'opaque-1', 'lead_created', '2026-01-01T10:00:00Z')
        SubjectEvent.objects.create(
            source=self.source, event_id='message-1', event_type='message_sent',
            external_subject_id='opaque-1', occurred_at=parse_datetime('2026-01-01T12:00:00Z'),
            dimensions={'messaging.template': 'winback'}, measures={})
        self.event('buy-1', 'opaque-1', 'purchase', '2026-01-02T10:00:00Z', '100', 'cmp-a')
        self.event('buy-2', 'opaque-1', 'purchase', '2026-04-02T10:00:00Z', '50', 'cmp-b')
        self.event('buy-3', 'opaque-2', 'purchase', '2026-01-10T10:00:00Z', '250', 'cmp-a')

        result = commercial_metrics(source='crm')

        self.assertEqual(result['totals']['revenue_eur'], 400)
        self.assertEqual(result['totals']['average_ltv_eur'], 200)
        self.assertEqual(result['totals']['repeat_customers'], 1)
        self.assertEqual(result['totals']['recovered_customers'], 1)
        self.assertEqual(result['totals']['mature_ltv_horizons_eur']['30'], 175)
        self.assertEqual(result['campaigns'][0]['campaign'], 'cmp-a')
        self.assertEqual(result['campaign_effects_30d'][0]['campaign'], 'winback')
        self.assertEqual(result['campaign_effects_30d'][0]['revenue_eur'], 100)
        self.assertEqual(result['attribution']['attributed_purchases'], 3)
        self.assertEqual(result['attribution']['unattributed_purchases'], 0)
        self.assertEqual(result['daily'][0], {'date': '2026-01-01', 'leads': 1,
                                              'purchases': 0, 'revenue_eur': 0.0})
        self.assertFalse(result['privacy']['row_level_subjects_returned'])
        self.assertNotIn('opaque-1', json.dumps(result))

    def test_conversion_page_is_explicit_and_requires_login(self):
        response = self.client.get('/conversioni/')
        self.assertEqual(response.status_code, 302)
        self.client.force_login(User.objects.create_user('analyst', password='test'))
        response = self.client.get('/conversioni/')
        self.assertContains(response, 'Conversioni')

    def test_purchase_inherits_latest_prior_lead_campaign(self):
        self.event('lead-a', 'opaque-a', 'lead_created',
                   '2026-01-01T10:00:00Z', campaign='cmp-first')
        self.event('lead-b', 'opaque-a', 'lead_created',
                   '2026-02-01T10:00:00Z', campaign='cmp-latest')
        self.event('buy-a', 'opaque-a', 'purchase',
                   '2026-03-01T10:00:00Z', revenue='90')

        result = commercial_metrics(source='crm', start=date(2026, 3, 1))

        latest = next(row for row in result['campaigns']
                      if row['campaign'] == 'cmp-latest')
        self.assertEqual(latest['purchases'], 1)
        self.assertEqual(latest['revenue_eur'], 90)
        self.assertEqual(result['attribution']['attributed_purchases'], 1)
        self.assertEqual(result['attribution']['unattributed_purchases'], 0)

    def test_lead_attribution_is_reported_even_without_purchases(self):
        self.event('lead-a', 'opaque-a', 'lead_created',
                   '2026-01-01T10:00:00Z', campaign='cmp-a')
        self.event('lead-b', 'opaque-b', 'lead_created',
                   '2026-01-02T10:00:00Z')

        result = commercial_metrics(source='crm')

        self.assertEqual(result['attribution']['attributed_leads'], 1)
        self.assertEqual(result['attribution']['unattributed_leads'], 1)
        campaign = next(row for row in result['campaigns']
                        if row['campaign'] == 'cmp-a')
        self.assertEqual(campaign['leads'], 1)

    def test_duplicate_lead_event_for_same_subject_is_counted_once(self):
        self.event('action-created', 'opaque-a', 'lead_created',
                   '2026-01-01T09:59:00Z')
        self.event('canonical-lead', 'opaque-a', 'lead_created',
                   '2026-01-01T10:00:00Z', campaign='cmp-a')

        result = commercial_metrics(source='crm')

        self.assertEqual(result['event_types']['lead_created'], 1)
        self.assertEqual(result['attribution']['attributed_leads'], 1)
        self.assertEqual(result['attribution']['unattributed_leads'], 0)

    def test_performance_metrics_include_new_customers_spend_and_funnel(self):
        self.event('lead-a', 'opaque-a', 'lead_created',
                   '2026-01-01T10:00:00Z', campaign='cmp-a')
        self.event('status-a', 'opaque-a', 'lead_status_changed',
                   '2026-01-02T10:00:00Z')
        status = SubjectEvent.objects.get(event_id='status-a')
        status.dimensions = {'wundt.to_status': 'client_acquired'}
        status.save(update_fields=['dimensions'])
        self.event('buy-a', 'opaque-a', 'purchase',
                   '2026-01-03T10:00:00Z', revenue='100')
        self.event('buy-b', 'opaque-a', 'purchase',
                   '2026-01-04T10:00:00Z', revenue='50')
        AirbyteRecord.objects.create(
            stream='fb_ads_insights', ab_id='spend-a',
            data={'date_start': '2026-01-02', 'spend': 40,
                  'campaign_name': 'cmp-a'})
        AirbyteRecord.objects.create(
            stream='gads_campaign', ab_id='google-spend-a',
            data={'segments_date': '2026-01-02',
                  'metrics_cost_micros': 10_000_000,
                  'campaign_name': 'cmp-google'})

        # Il periodo si chiude il 4: la spesa arriva al 2, quindi due giorni
        # scoperti, dentro la tolleranza — il CAC si stampa ancora (con la "~"
        # che ci mette la UI). Con una finestra fino al 31 gennaio il cancello
        # della copertura lo toglierebbe, ed è giusto così.
        result = performance_metrics(source='crm', start='2026-01-01',
                                     end='2026-01-04')

        self.assertEqual(result['kpis']['leads'], 1)
        self.assertEqual(result['kpis']['new_customers'], 1)
        self.assertEqual(result['kpis']['purchases'], 2)
        self.assertEqual(result['kpis']['spend_eur'], 50)
        self.assertEqual(result['kpis']['cac_eur'], 50)
        self.assertTrue(result['spend_coverage']['approximate'])
        self.assertEqual(result['kpis']['average_realized_ltv_eur'], 150)
        self.assertEqual(result['funnel'][-1]['count'], 1)
        self.assertEqual(sum(row['new_customers'] for row in result['daily']), 1)


class WundtDefinitionsTests(TestCase):
    """Acquisizioni e lead entrati con le definizioni indurite in WUNDT.

    Un pagante conta come cliente acquisito solo se il lead esisteva già il
    giorno prima e non viene da una fonte fuori funnel; fra i lead entrati non
    ci sono le migrazioni né i potenziali tutor.
    """

    def setUp(self):
        self.source = AnalyticsSource.objects.create(name='CRM', slug='crm')
        # Spesa su tutti i giorni del periodo: qui si guardano le definizioni,
        # il cancello della copertura ha i suoi test.
        for day in ('2026-03-01', '2026-03-02', '2026-03-03'):
            AirbyteRecord.objects.create(
                stream='fb_ads_insights', ab_id=f'fb-{day}',
                data={'date_start': day, 'spend': 10, 'campaign_name': 'cmp'})

    def lead(self, subject, when, source=None, lead_type=None):
        dimensions = {}
        if source:
            dimensions['wundt.lead_source'] = source
        if lead_type:
            dimensions['wundt.lead_type'] = lead_type
        return SubjectEvent.objects.create(
            source=self.source, event_id=f'lead:{subject}',
            event_type='lead_created', external_subject_id=subject,
            occurred_at=parse_datetime(when), dimensions=dimensions)

    def purchase(self, subject, when, revenue='100'):
        return SubjectEvent.objects.create(
            source=self.source, event_id=f'buy:{subject}', event_type='purchase',
            external_subject_id=subject, occurred_at=parse_datetime(when),
            measures={'commerce.revenue_eur': revenue})

    def metrics(self):
        return performance_metrics(source='crm', start='2026-03-01',
                                   end='2026-03-03')

    def test_lead_that_pays_the_day_after_is_an_acquisition(self):
        self.lead('opaque-1', '2026-03-01T10:00:00Z')
        self.purchase('opaque-1', '2026-03-02T09:00:00Z')

        kpis = self.metrics()['kpis']

        self.assertEqual(kpis['leads'], 1)
        self.assertEqual(kpis['new_customers'], 1)
        self.assertEqual(kpis['excluded_payers'], 0)

    def test_payer_of_the_same_day_is_not_an_acquisition(self):
        self.lead('opaque-1', '2026-03-01T10:00:00Z')
        self.purchase('opaque-1', '2026-03-01T18:00:00Z')

        result = self.metrics()

        self.assertEqual(result['kpis']['new_customers'], 0)
        self.assertEqual(result['kpis']['excluded_payers'], 1)
        self.assertIsNone(result['kpis']['cac_eur'])
        self.assertEqual(sum(row['excluded_payers'] for row in result['daily']), 1)

    def test_platform_source_payer_is_not_an_acquisition(self):
        self.lead('opaque-1', '2026-03-01T10:00:00Z', source='piattaforma')
        self.purchase('opaque-1', '2026-03-03T09:00:00Z')

        kpis = self.metrics()['kpis']

        self.assertEqual(kpis['new_customers'], 0)
        self.assertEqual(kpis['excluded_payers'], 1)
        # 'piattaforma' è anche una fonte di migrazione: niente lead entrato.
        self.assertEqual(kpis['leads'], 0)

    def test_days_are_compared_in_rome_not_in_utc(self):
        # 23:30 UTC del 1° marzo a Roma sono le 00:30 del 2: acquisizione.
        self.lead('opaque-1', '2026-03-01T10:00:00Z')
        self.purchase('opaque-1', '2026-03-01T23:30:00Z')

        self.assertEqual(self.metrics()['kpis']['new_customers'], 1)

    def test_migration_sources_stay_out_of_the_leads(self):
        self.lead('opaque-1', '2026-03-01T10:00:00Z', source='import_airtable')
        self.lead('opaque-2', '2026-03-01T11:00:00Z', source='facebook-leads')

        self.assertEqual(self.metrics()['kpis']['leads'], 1)

    def test_potential_tutors_stay_out_when_the_dimension_is_there(self):
        self.lead('opaque-1', '2026-03-01T10:00:00Z', source='facebook-leads',
                  lead_type='potential_tutor')
        self.lead('opaque-2', '2026-03-01T11:00:00Z', source='facebook-leads',
                  lead_type='client')

        result = self.metrics()

        self.assertEqual(result['kpis']['leads'], 1)
        self.assertTrue(result['definitions']['lead_type_available'])
        self.assertEqual(result['definitions']['lead_type_note'], '')

    def test_without_the_dimension_potential_tutors_are_counted_and_declared(self):
        self.lead('opaque-1', '2026-03-01T10:00:00Z', source='facebook-leads')
        self.lead('opaque-2', '2026-03-01T11:00:00Z', source='facebook-leads')

        result = self.metrics()

        self.assertEqual(result['kpis']['leads'], 2)
        self.assertFalse(result['definitions']['lead_type_available'])
        self.assertIn('wundt.lead_type', result['definitions']['lead_type_note'])


class SpendCoverageTests(TestCase):
    """Il cancello della copertura: due giorni di tolleranza, il giorno in corso
    mai scoperto, e nessun numero quando la spesa di un canale è ferma."""

    def spend(self, channel, day, amount=10, synced=None, status=None):
        # `emitted_at` = quando la pipeline ha girato (lo scrive import_airbyte
        # da _airbyte_extracted_at). Di default: il giorno stesso del dato.
        # `status` = campaign_status di Google all'ultimo sync (attributo
        # attuale, non storico): senza, la riga non dice niente sulla pausa.
        emitted = parse_datetime(f'{synced or day}T06:00:00Z')
        if channel == 'meta':
            AirbyteRecord.objects.create(
                stream='fb_ads_insights', ab_id=f'fb-{day}', emitted_at=emitted,
                data={'date_start': day, 'spend': amount, 'campaign_name': 'cmp'})
        else:
            data = {'segments_date': day, 'campaign_name': 'cmp-google',
                    'metrics_cost_micros': amount * 1_000_000}
            if status:
                data['campaign_status'] = status
            AirbyteRecord.objects.create(
                stream='gads_campaign', ab_id=f'gads-{day}', emitted_at=emitted,
                data=data)

    def september(self, meta_through, google_through,
                  meta_synced=None, google_synced=None, google_status=None):
        for day in range(1, meta_through + 1):
            self.spend('meta', f'2026-09-{day:02d}', synced=meta_synced)
        for day in range(1, google_through + 1):
            self.spend('google', f'2026-09-{day:02d}', synced=google_synced,
                       status=google_status)

    def test_beyond_tolerance_no_number_and_the_reason_names_the_channel(self):
        self.september(meta_through=12, google_through=9)

        verdict = spend_coverage(date(2026, 9, 1), date(2026, 9, 12),
                                 today=date(2026, 9, 13))

        self.assertEqual(verdict['uncovered_days'], 3)
        self.assertTrue(verdict['blocked'])
        self.assertFalse(verdict['approximate'])
        self.assertIn('Google', verdict['reason'])
        self.assertIn('09/09/2026', verdict['reason'])
        self.assertIn('3 giorni del periodo non coperti', verdict['reason'])

    def test_within_tolerance_the_number_is_only_approximate(self):
        self.september(meta_through=12, google_through=9)

        # Oggi è il 12: il giorno in corso non conta, restano scoperti 10 e 11.
        verdict = spend_coverage(date(2026, 9, 1), date(2026, 9, 12),
                                 today=date(2026, 9, 12))

        self.assertEqual(verdict['uncovered_days'], 2)
        self.assertTrue(verdict['approximate'])
        self.assertFalse(verdict['blocked'])
        self.assertIn('spesa Google ferma al 09/09/2026', verdict['reason'])

    def test_covered_period_shows_both_dates_and_no_reason(self):
        self.september(meta_through=12, google_through=12)

        verdict = spend_coverage(date(2026, 9, 1), date(2026, 9, 12),
                                 today=date(2026, 9, 13))

        self.assertTrue(verdict['reliable'])
        self.assertEqual(verdict['uncovered_days'], 0)
        self.assertEqual(verdict['reason'], '')
        self.assertIn('spesa Meta aggiornata al 12/09/2026', verdict['note'])
        self.assertIn('spesa Google aggiornata al 12/09/2026', verdict['note'])

    def test_a_channel_without_recent_data_is_off_not_stale(self):
        self.september(meta_through=12, google_through=0)

        verdict = spend_coverage(date(2026, 9, 1), date(2026, 9, 12),
                                 today=date(2026, 9, 13))

        self.assertTrue(verdict['reliable'])
        self.assertIn('spesa Google: nessun dato recente', verdict['note'])

    def landing(self, meta, google):
        """La landing Airbyte, finta: {'meta': (ultimo giorno, giorno di sync)}.

        In produzione la legge `_landing_probe` via l'alias `airbyte`; qui
        l'alias non esiste, quindi la si sostituisce.
        """
        def probe(channel):
            last_day, sync_day = {'meta': meta, 'google': google}[channel['key']]
            return {'available': True, 'last_day': last_day,
                    'sync_day': date.fromisoformat(sync_day) if sync_day else None}
        return patch('dashboard.analytics._landing_probe', side_effect=probe)

    def test_a_still_channel_is_not_stale_if_the_landing_heartbeat_is_fresh(self):
        # Campagne in pausa: nel mirror `emitted_at` resta al 9/9 perché nessuna
        # riga cambia, ma la landing dice che la pipeline ha girato il 15.
        self.september(meta_through=12, google_through=9,
                       meta_synced='2026-09-09', google_synced='2026-09-09')

        with self.landing(meta=('2026-09-12', '2026-09-15'),
                          google=('2026-09-09', '2026-09-15')):
            verdict = spend_coverage(date(2026, 9, 1), date(2026, 9, 12),
                                     today=date(2026, 9, 16))

        google = next(row for row in verdict['channels'] if row['key'] == 'google')
        self.assertEqual(google['last_sync'], '2026-09-15')
        self.assertFalse(google['mirror_behind'])
        self.assertEqual(google['state'], 'no_delivery')
        self.assertTrue(verdict['reliable'])

        # Senza il battito della landing lo stesso dataset sembrerebbe fermo:
        # è il limite del ripiego, ed è la ragione per cui si legge la landing.
        fallback = spend_coverage(date(2026, 9, 1), date(2026, 9, 12),
                                  today=date(2026, 9, 16))
        self.assertTrue(fallback['blocked'])

    def test_a_mirror_left_behind_says_so_instead_of_showing_a_number(self):
        # Il mirror si è fermato al 9/9 e la landing è arrivata al 12: è il
        # guasto che a WUNDT è passato inosservato per cinque mesi e mezzo.
        self.september(meta_through=9, google_through=9,
                       meta_synced='2026-09-09', google_synced='2026-09-09')

        with self.landing(meta=('2026-09-12', '2026-09-12'),
                          google=('2026-09-12', '2026-09-12')):
            verdict = spend_coverage(date(2026, 9, 1), date(2026, 9, 12),
                                     today=date(2026, 9, 13))

        meta = next(row for row in verdict['channels'] if row['key'] == 'meta')
        self.assertTrue(meta['mirror_behind'])
        self.assertEqual(meta['state'], 'stale')
        self.assertTrue(verdict['blocked'])
        self.assertIn('i dati Meta nel mirror si fermano al 09/09/2026 mentre '
                      'la pipeline è arrivata al 12/09/2026', verdict['reason'])

    def test_a_mirror_in_line_with_the_landing_changes_nothing(self):
        self.september(meta_through=12, google_through=9,
                       meta_synced='2026-09-12', google_synced='2026-09-12')

        with self.landing(meta=('2026-09-12', '2026-09-12'),
                          google=('2026-09-09', '2026-09-12')):
            verdict = spend_coverage(date(2026, 9, 1), date(2026, 9, 12),
                                     today=date(2026, 9, 15))

        self.assertTrue(verdict['reliable'])
        self.assertFalse(any(row['mirror_behind'] for row in verdict['channels']))
        self.assertIn('Google: nessuna erogazione dal 10/09/2026', verdict['note'])

    def test_paused_campaigns_are_no_delivery_not_a_stopped_pipeline(self):
        # Il caso vero del 12/9/2026: Meta arriva al 12, Google al 9 perché le
        # sue campagne sono in pausa, ed ENTRAMBI i connettori hanno girato il
        # 12. Guardato il 15, il periodo 1-12 è completo: quello zero è una
        # misura, il CAC si stampa pieno e senza "~".
        self.september(meta_through=12, google_through=9,
                       meta_synced='2026-09-12', google_synced='2026-09-12')

        verdict = spend_coverage(date(2026, 9, 1), date(2026, 9, 12),
                                 today=date(2026, 9, 15))

        google = next(row for row in verdict['channels'] if row['key'] == 'google')
        self.assertEqual(google['state'], 'no_delivery')
        self.assertEqual(google['missing_days'], 3)
        self.assertEqual(google['uncovered_days'], 0)
        self.assertEqual(verdict['uncovered_days'], 0)
        self.assertTrue(verdict['reliable'])
        self.assertFalse(verdict['approximate'])
        self.assertFalse(verdict['blocked'])
        self.assertEqual(verdict['reason'], '')
        self.assertIn('Google: nessuna erogazione dal 10/09/2026', verdict['note'])

    def test_the_same_data_with_an_old_sync_is_a_stopped_pipeline(self):
        # Stesso dataset, ma il connettore Google non gira da cinque giorni:
        # qui la coda non è zero, è ignota.
        self.september(meta_through=12, google_through=9,
                       meta_synced='2026-09-12', google_synced='2026-09-10')

        verdict = spend_coverage(date(2026, 9, 1), date(2026, 9, 12),
                                 today=date(2026, 9, 15))

        google = next(row for row in verdict['channels'] if row['key'] == 'google')
        self.assertEqual(google['state'], 'stale')
        self.assertEqual(verdict['uncovered_days'], 3)
        self.assertTrue(verdict['blocked'])
        self.assertIn('spesa Google ferma al 09/09/2026', verdict['reason'])

    def test_paused_campaigns_keep_the_number_when_the_heartbeat_is_frozen(self):
        # Il caso del 22/9/2026 nella forma peggiore: Google in pausa dal
        # 10/9, le sue righe riestratte l'ultima volta il 10/9 (poi fuori
        # dalla finestra di lookback: il battito si congela PRIMA di aver
        # dovuto portare il primo giorno mancante), Meta al 21 sincronizzata
        # il 22. Col solo battito sarebbe "pipeline ferma"; ma all'ultimo dato
        # le campagne risultano PAUSED: la coda vale zero, numero pieno.
        self.september(meta_through=21, google_through=9,
                       meta_synced='2026-09-22', google_synced='2026-09-10',
                       google_status='PAUSED')

        verdict = spend_coverage(date(2026, 9, 1), date(2026, 9, 22),
                                 today=date(2026, 9, 22))

        google = next(row for row in verdict['channels'] if row['key'] == 'google')
        self.assertEqual(google['state'], 'no_delivery')
        self.assertTrue(google['paused'])
        self.assertEqual(google['missing_days'], 12)
        self.assertEqual(google['uncovered_days'], 0)
        self.assertTrue(verdict['reliable'])
        self.assertFalse(verdict['blocked'])
        self.assertEqual(verdict['reason'], '')
        self.assertIn('Google: campagne in pausa, nessuna erogazione dal 10/09/2026',
                      verdict['note'])

    def test_enabled_campaigns_with_a_frozen_heartbeat_are_a_stopped_pipeline(self):
        # Stessi dati, ma le campagne risultano ENABLED: allora i giorni vuoti
        # non sono una pausa, e con un battito fermo al 10/9 il dato è ignoto.
        self.september(meta_through=21, google_through=9,
                       meta_synced='2026-09-22', google_synced='2026-09-10',
                       google_status='ENABLED')

        verdict = spend_coverage(date(2026, 9, 1), date(2026, 9, 22),
                                 today=date(2026, 9, 22))

        google = next(row for row in verdict['channels'] if row['key'] == 'google')
        self.assertEqual(google['state'], 'stale')
        self.assertFalse(google['paused'])
        self.assertTrue(verdict['blocked'])
        self.assertIn('spesa Google ferma al 09/09/2026', verdict['reason'])

    def test_a_window_inside_the_pause_still_prints_the_number(self):
        self.september(meta_through=12, google_through=9,
                       meta_synced='2026-09-12', google_synced='2026-09-12')

        verdict = spend_coverage(date(2026, 9, 10), date(2026, 9, 12),
                                 today=date(2026, 9, 15))

        google = next(row for row in verdict['channels'] if row['key'] == 'google')
        self.assertEqual(google['days_with_data'], 0)
        self.assertEqual(google['state'], 'no_delivery')
        self.assertTrue(verdict['reliable'])
        self.assertIn('Google: nessuna erogazione dal 10/09/2026', verdict['note'])

    def test_no_delivery_keeps_the_cac_in_performance_metrics(self):
        source = AnalyticsSource.objects.create(name='CRM', slug='crm')
        SubjectEvent.objects.create(
            source=source, event_id='lead-1', event_type='lead_created',
            external_subject_id='opaque-1',
            occurred_at=parse_datetime('2026-09-01T10:00:00Z'),
            dimensions={'wundt.lead_source': 'facebook-leads'})
        SubjectEvent.objects.create(
            source=source, event_id='buy-1', event_type='purchase',
            external_subject_id='opaque-1',
            occurred_at=parse_datetime('2026-09-03T10:00:00Z'),
            measures={'commerce.revenue_eur': '500'})
        self.september(meta_through=12, google_through=9,
                       meta_synced='2026-09-12', google_synced='2026-09-12')

        result = performance_metrics(source='crm', start='2026-09-01',
                                     end='2026-09-12')

        self.assertEqual(result['kpis']['spend_eur'], 210)
        self.assertEqual(result['kpis']['cac_eur'], 210)
        self.assertTrue(result['spend_coverage']['reliable'])

    def test_blocked_coverage_takes_the_cac_out_of_performance_metrics(self):
        source = AnalyticsSource.objects.create(name='CRM', slug='crm')
        SubjectEvent.objects.create(
            source=source, event_id='lead-1', event_type='lead_created',
            external_subject_id='opaque-1',
            occurred_at=parse_datetime('2026-03-01T10:00:00Z'),
            dimensions={'wundt.lead_source': 'facebook-leads',
                        'marketing.campaign_id': 'cmp'})
        SubjectEvent.objects.create(
            source=source, event_id='buy-1', event_type='purchase',
            external_subject_id='opaque-1',
            occurred_at=parse_datetime('2026-03-05T10:00:00Z'),
            measures={'commerce.revenue_eur': '200'})
        self.spend('meta', '2026-03-01', amount=40)

        result = performance_metrics(source='crm', start='2026-03-01',
                                     end='2026-03-10')

        self.assertEqual(result['kpis']['new_customers'], 1)
        self.assertEqual(result['kpis']['spend_eur'], 40)
        self.assertIsNone(result['kpis']['cac_eur'])
        self.assertIsNone(result['kpis']['median_cac_payback_days'])
        self.assertIsNone(result['campaigns'][0]['cac_eur'])
        self.assertTrue(result['spend_coverage']['blocked'])


class DashboardSurfacesTests(TestCase):
    """Le pagine che stampano un CAC ricevono il verdetto dal server."""

    def setUp(self):
        self.client.force_login(User.objects.create_user('analista', password='test'))
        source = AnalyticsSource.objects.create(name='WUNDT', slug='wundt')
        SubjectEvent.objects.create(
            source=source, event_id='lead-1', event_type='lead_created',
            external_subject_id='opaque-1',
            occurred_at=parse_datetime('2026-09-01T10:00:00Z'),
            dimensions={'wundt.lead_source': 'facebook-leads',
                        'marketing.attribution_source': 'meta',
                        'marketing.campaign_id': 'cmp-1'})
        SubjectEvent.objects.create(
            source=source, event_id='lead-2', event_type='lead_created',
            external_subject_id='opaque-2',
            occurred_at=parse_datetime('2026-09-02T10:00:00Z'),
            dimensions={'wundt.lead_source': 'import_airtable'})
        SubjectEvent.objects.create(
            source=source, event_id='buy-1', event_type='purchase',
            external_subject_id='opaque-1',
            occurred_at=parse_datetime('2026-09-03T10:00:00Z'),
            measures={'commerce.revenue_eur': '300'})
        AirbyteRecord.objects.create(
            stream='fb_ads_insights', ab_id='fb-1',
            data={'date_start': '2026-09-01', 'spend': 100,
                  'campaign_id': 'cmp-1', 'campaign_name': 'cmp-1'})

    def test_compare_page_and_json_carry_the_coverage_verdict(self):
        page = self.client.get('/dashboard/')
        self.assertContains(page, 'coverage-note')
        self.assertContains(page, 'spend-coverage.json')

        payload = self.client.get('/data/compare.json').json()
        self.assertIn('spend_coverage', payload)
        self.assertIn('crm_definitions', payload)
        self.assertIn('crm_excluded_payers', payload['metrics'])

        verdict = self.client.get(
            '/data/spend-coverage.json?start=2026-09-01&end=2026-09-12').json()
        self.assertEqual(verdict['start'], '2026-09-01')
        self.assertEqual(verdict['tolerance_days'], 2)

    def test_home_json_counts_leads_and_customers_like_the_kpis(self):
        home = self.client.get('/data/home.json').json()

        # Il lead migrato resta fuori, l'acquisizione c'è.
        cohorts = {row['date']: row for row in home['conversion_cohorts']}
        self.assertEqual(cohorts['2026-09-01']['leads'], 1)
        self.assertEqual(cohorts['2026-09-01']['customers'], 1)
        self.assertNotIn('2026-09-02', cohorts)


@override_settings(PED_SERVICE_TOKEN='ped-test-token')
class EditorialCalendarApiTests(TestCase):
    def setUp(self):
        self.headers = {'HTTP_AUTHORIZATION': 'Bearer ped-test-token'}

    def test_external_workflow_upserts_canonical_dates(self):
        payload = {'external_origin': 'smp', 'external_ref': 'entry-1',
                   'title': 'Reel test', 'channel': 'reels',
                   'content_format': 'reel', 'planned_date': '2026-09-01',
                   'planned_time': '18:30', 'status': 'produzione',
                   'workflow_metadata': {'volto_id': 'opaque-workflow-id'}}
        response = self.client.post('/api/v1/editorial-calendar/', data=json.dumps(payload),
                                    content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['created'])
        payload['planned_date'] = '2026-09-02'
        response = self.client.post('/api/v1/editorial-calendar/', data=json.dumps(payload),
                                    content_type='application/json', **self.headers)
        self.assertFalse(response.json()['created'])
        self.assertEqual(response.json()['entry']['planned_date'], '2026-09-02')
        self.assertEqual(response.json()['entry']['canonical_version'], 2)
        self.assertEqual(EditorialChange.objects.count(), 2)

        conflict = {**payload, 'planned_date': '2026-09-03', 'expected_version': 1}
        response = self.client.post('/api/v1/editorial-calendar/', data=json.dumps(conflict),
                                    content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['current']['planned_date'], '2026-09-02')

    def test_token_is_required(self):
        response = self.client.get('/api/v1/editorial-calendar/')
        self.assertEqual(response.status_code, 401)


@override_settings(PLATFORM_METRICS_API_KEY='chiave-di-prova')
class LeadMensiliApiTests(TestCase):
    """L'API che la piattaforma legge al posto della tabella Airtable di Make:
    lead entrati per mese di ingresso e fonte, con call e convertiti della
    stessa coorte, e le definizioni dentro la risposta."""

    url = '/api/v1/metrics/lead-mensili/'

    def setUp(self):
        self.source = AnalyticsSource.objects.create(name='WUNDT', slug='wundt')
        self.serial = 0

    def event(self, subject, kind, when, dimensions=None, measures=None):
        self.serial += 1
        return SubjectEvent.objects.create(
            source=self.source, event_id=f'wundt:test:{self.serial}',
            event_type=kind, external_subject_id=subject,
            occurred_at=parse_datetime(when), dimensions=dimensions or {},
            measures=measures or {})

    def lead(self, subject, when, lead_source='facebook-leads', lead_type='client',
             **marketing):
        dimensions = {'wundt.lead_source': lead_source}
        if lead_type:
            dimensions['wundt.lead_type'] = lead_type
        dimensions.update({f'marketing.{key}': value
                           for key, value in marketing.items()})
        return self.event(subject, 'lead_created', when, dimensions)

    def status(self, subject, when, to_status, from_status='new'):
        return self.event(subject, 'lead_status_changed', when,
                          {'wundt.from_status': from_status,
                           'wundt.to_status': to_status})

    def purchase(self, subject, when):
        return self.event(subject, 'purchase', when,
                          measures={'commerce.revenue_eur': '280'})

    def get(self, da='2026-01', a='2026-12', key='chiave-di-prova'):
        headers = {'HTTP_AUTHORIZATION': f'Bearer {key}'} if key is not None else {}
        return self.client.get(self.url, {'da': da, 'a': a}, **headers)

    def test_without_the_right_key_it_is_401(self):
        self.assertEqual(self.get(key=None).status_code, 401)
        self.assertEqual(self.get(key='sbagliata').status_code, 401)
        # La chiave va dopo "Bearer ": da sola non basta.
        naked = self.client.get(self.url, {'da': '2026-01', 'a': '2026-12'},
                                HTTP_AUTHORIZATION='chiave-di-prova')
        self.assertEqual(naked.status_code, 401)
        # Un header con caratteri non ASCII è una chiave errata, non un 500.
        self.assertEqual(self.get(key='chiave-di-pròva').status_code, 401)
        self.assertEqual(self.get().status_code, 200)
        post = self.client.post(self.url, HTTP_AUTHORIZATION='Bearer chiave-di-prova')
        self.assertEqual(post.status_code, 405)

    @override_settings(PLATFORM_METRICS_API_KEY='')
    def test_with_no_key_configured_everyone_gets_401(self):
        self.assertEqual(self.get(key='').status_code, 401)
        self.assertEqual(self.get(key='qualsiasi').status_code, 401)

    def test_invalid_months_are_400(self):
        for da, a in [('', ''), ('2026-01', ''), ('2026-7', '2026-08'),
                      ('2026-13', '2026-12'), ('2026-00', '2026-01'),
                      ('26-07', '2026-08'), ('2026-07-01', '2026-08'),
                      ('2026-07\n', '2026-08'), ('٢٠٢٦-07', '2026-08'),
                      ('2026-08', '2026-07'),     # da dopo a
                      ('2024-01', '2027-01')]:    # 37 mesi
            with self.subTest(da=da, a=a):
                response = self.get(da=da, a=a)
                self.assertEqual(response.status_code, 400)
                self.assertIn('error', response.json())
        missing = self.client.get(self.url, HTTP_AUTHORIZATION='Bearer chiave-di-prova')
        self.assertEqual(missing.status_code, 400)
        # 36 mesi sono ancora ammessi, e un mese solo pure.
        self.assertEqual(self.get(da='2024-01', a='2026-12').status_code, 200)
        self.assertEqual(self.get(da='2026-07', a='2026-07').status_code, 200)
        # Senza chiave i parametri non si guardano nemmeno.
        self.assertEqual(self.get(da='x', a='y', key=None).status_code, 401)

    def test_rows_and_totals_by_month_and_source(self):
        video = 'MNT_RIP_LEADS_JUL2026 - Video'
        # Luglio, Video: uno prenota l'incontro e paga, uno no.
        self.lead('psn-a', '2026-07-03T10:00:00Z', utm_campaign=video)
        self.status('psn-a', '2026-07-05T10:00:00Z', 'booked_meeting')
        self.purchase('psn-a', '2026-07-10T10:00:00Z')
        self.lead('psn-b', '2026-07-04T10:00:00Z', utm_campaign=video)
        # Luglio, WhatsApp senza campagna.
        self.lead('psn-c', '2026-07-20T10:00:00Z', lead_source='whatsapp')
        # Agosto, DSA: uno ha presenziato all'incontro e non paga; l'altro
        # (lead migrato) l'ingresso nell'incontro non l'ha registrato, ma ne
        # esce diventando cliente, e paga.
        self.lead('psn-d', '2026-08-02T10:00:00Z', utm_campaign='MNT_DSA - Video')
        self.status('psn-d', '2026-08-06T10:00:00Z', 'meeting_attended',
                    from_status='booked_meeting')
        self.lead('psn-e', '2026-08-03T10:00:00Z', utm_campaign='MNT_DSA - Video')
        self.status('psn-e', '2026-08-08T10:00:00Z', 'client_acquired',
                    from_status='booked_meeting')
        self.purchase('psn-e', '2026-08-09T10:00:00Z')
        # Giugno e settembre non hanno lead: niente righe.

        response = self.get(da='2026-06', a='2026-09')

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {'generato_il', 'da', 'a', 'righe', 'totali',
                                     'definizioni'})
        self.assertEqual((body['da'], body['a']), ('2026-06', '2026-09'))
        self.assertEqual(body['righe'], [
            {'mese': '2026-07', 'fonte': video, 'lead': 2, 'call': 1,
             'convertiti': 1, 'call_convertiti': 1},
            {'mese': '2026-07', 'fonte': 'WhatsApp diretto', 'lead': 1, 'call': 0,
             'convertiti': 0, 'call_convertiti': 0},
            {'mese': '2026-08', 'fonte': 'MNT_DSA - Video', 'lead': 2, 'call': 2,
             'convertiti': 1, 'call_convertiti': 1},
        ])
        self.assertEqual(body['totali'], {'lead': 5, 'call': 3, 'convertiti': 2,
                                          'call_convertiti': 2})
        self.assertEqual(set(body['definizioni']),
                         {'lead', 'fonte', 'call', 'convertiti'})
        self.assertIn('potential_tutor', body['definizioni']['lead'])
        self.assertRegex(body['generato_il'],
                         r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$')
        # Niente identificativi nella risposta.
        self.assertNotIn('psn-', response.content.decode())

    def test_exclusions_follow_leads_entered_and_acquisitions(self):
        # Fuori dai lead: il tutor potenziale e le migrazioni, anche se pagano.
        self.lead('psn-tutor', '2026-07-01T10:00:00Z', lead_type='potential_tutor')
        self.lead('psn-airtable', '2026-07-01T10:00:00Z', lead_source='import_airtable')
        self.lead('psn-piattaforma', '2026-07-01T10:00:00Z', lead_source='piattaforma')
        self.purchase('psn-piattaforma', '2026-07-05T10:00:00Z')
        # Dentro ma non convertito: paga lo stesso giorno in cui entra.
        self.lead('psn-stesso-giorno', '2026-07-02T08:00:00Z')
        self.purchase('psn-stesso-giorno', '2026-07-02T18:00:00Z')
        # Dentro e convertito: paga il giorno dopo.
        self.lead('psn-giorno-dopo', '2026-07-02T08:00:00Z')
        self.purchase('psn-giorno-dopo', '2026-07-03T09:00:00Z')
        # Pagante senza lead_created: non è un lead e non è una conversione.
        self.purchase('psn-senza-lead', '2026-07-03T09:00:00Z')

        body = self.get(da='2026-07', a='2026-07').json()

        self.assertEqual(body['righe'], [
            {'mese': '2026-07', 'fonte': 'Meta', 'lead': 2, 'call': None,
             'convertiti': 1, 'call_convertiti': None}])

    def test_calls_and_conversions_stay_in_the_month_the_lead_entered(self):
        # Entra il 1° luglio alle 00:30 di Roma (30 giugno in UTC), prenota ad
        # agosto, paga a settembre: tutto sta nella riga di luglio.
        self.lead('psn-a', '2026-06-30T22:30:00Z', utm_campaign='Video')
        self.status('psn-a', '2026-08-10T10:00:00Z', 'booked_meeting')
        self.purchase('psn-a', '2026-09-15T10:00:00Z')
        july = {'mese': '2026-07', 'fonte': 'Video', 'lead': 1, 'call': 1,
                'convertiti': 1, 'call_convertiti': 1}

        self.assertEqual(self.get(da='2026-06', a='2026-09').json()['righe'], [july])
        # La riga di un mese non dipende dall'intervallo chiesto...
        self.assertEqual(self.get(da='2026-07', a='2026-07').json()['righe'], [july])
        # ...e i mesi della prenotazione e del pagamento non hanno righe.
        september = self.get(da='2026-08', a='2026-09').json()
        self.assertEqual(september['righe'], [])
        self.assertEqual(september['totali'], {'lead': 0, 'call': 0, 'convertiti': 0,
                                               'call_convertiti': 0})
        self.assertEqual(self.get(da='2026-06', a='2026-06').json()['righe'], [])

    def test_without_status_events_call_is_null_and_says_why(self):
        self.lead('psn-a', '2026-07-03T10:00:00Z', utm_campaign='Video')
        self.purchase('psn-a', '2026-07-10T10:00:00Z')

        body = self.get(da='2026-07', a='2026-07').json()

        self.assertEqual(body['righe'], [
            {'mese': '2026-07', 'fonte': 'Video', 'lead': 1, 'call': None,
             'convertiti': 1, 'call_convertiti': None}])
        self.assertEqual(body['totali'], {'lead': 1, 'call': None, 'convertiti': 1,
                                          'call_convertiti': None})
        self.assertIn('Non disponibile', body['definizioni']['call'])
        self.assertIn('lead_status_changed', body['definizioni']['call'])

        # Appena WUNDT manda un cambio di stato qualsiasi, zero è un numero vero.
        self.status('psn-a', '2026-07-04T10:00:00Z', 'contacted_whatsapp')
        body = self.get(da='2026-07', a='2026-07').json()
        self.assertEqual(body['totali']['call'], 0)
        self.assertEqual(body['totali']['call_convertiti'], 0)
        self.assertNotIn('Non disponibile', body['definizioni']['call'])

    def test_source_is_the_campaign_name_or_the_source_label(self):
        # L'id Meta si traduce col nome dell'ultimo giorno nel mirror.
        AirbyteRecord.objects.create(
            stream='fb_ads_insights', ab_id='fb-1',
            data={'campaign_id': '1201', 'campaign_name': 'Nome vecchio',
                  'date_start': '2026-07-01'})
        AirbyteRecord.objects.create(
            stream='fb_ads_insights', ab_id='fb-2',
            data={'campaign_id': '1201', 'campaign_name': 'MNT_RIP_LEADS_JUL2026 - Video',
                  'date_start': '2026-07-10'})
        # Google porta l'id come numero.
        AirbyteRecord.objects.create(
            stream='gads_campaign', ab_id='gads-1',
            data={'campaign_id': 987, 'campaign_name': 'Search brand',
                  'segments_date': '2026-07-01'})
        self.lead('psn-id-meta', '2026-07-01T10:00:00Z', campaign_id='1201',
                  utm_campaign='rip_luglio')
        self.lead('psn-id-google', '2026-07-01T10:00:00Z', lead_source='website',
                  campaign_id='987')
        self.lead('psn-utm', '2026-07-01T10:00:00Z', utm_campaign='MNT_DSA - Video')
        # Il doppione che WUNDT manda per ogni lead (l'azione "creato"): un
        # lead solo, con la campagna dell'evento che la porta.
        self.event('psn-utm', 'lead_created', '2026-07-01T09:59:00Z',
                   {'wundt.lead_source': 'facebook-leads',
                    'wundt.action_type': 'created'})
        self.lead('psn-id-ignoto', '2026-07-01T10:00:00Z', campaign_id='555')
        self.lead('psn-ctwa', '2026-07-01T10:00:00Z', lead_source='whatsapp',
                  attribution_source='meta')
        self.lead('psn-whatsapp', '2026-07-01T10:00:00Z', lead_source='whatsapp')
        self.lead('psn-calendly', '2026-07-01T10:00:00Z', lead_source='calendly')
        self.lead('psn-nessuna', '2026-07-01T10:00:00Z', lead_source='')

        rows = self.get(da='2026-07', a='2026-07').json()['righe']

        self.assertEqual({row['fonte']: row['lead'] for row in rows}, {
            'MNT_RIP_LEADS_JUL2026 - Video': 1, 'Search brand': 1,
            'MNT_DSA - Video': 1, '555': 1, 'Meta': 1, 'WhatsApp diretto': 1,
            'Calendly': 1, 'Non attribuito': 1})

    def test_without_lead_type_the_definition_says_tutors_are_included(self):
        self.lead('psn-a', '2026-07-01T10:00:00Z', lead_type=None)

        body = self.get(da='2026-07', a='2026-07').json()

        self.assertEqual(body['totali']['lead'], 1)
        self.assertIn('tutor potenziali sono inclusi', body['definizioni']['lead'])
