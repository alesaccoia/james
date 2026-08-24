import json
from datetime import date

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils.dateparse import parse_datetime

from .analytics import commercial_metrics, performance_metrics
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
            data={'date_start': '2026-01-01', 'spend': 40,
                  'campaign_name': 'cmp-a'})
        AirbyteRecord.objects.create(
            stream='gads_campaign', ab_id='google-spend-a',
            data={'segments_date': '2026-01-02',
                  'metrics_cost_micros': 10_000_000,
                  'campaign_name': 'cmp-google'})

        result = performance_metrics(source='crm', start='2026-01-01',
                                     end='2026-01-31')

        self.assertEqual(result['kpis']['leads'], 1)
        self.assertEqual(result['kpis']['new_customers'], 1)
        self.assertEqual(result['kpis']['purchases'], 2)
        self.assertEqual(result['kpis']['spend_eur'], 50)
        self.assertEqual(result['kpis']['cac_eur'], 50)
        self.assertEqual(result['kpis']['average_realized_ltv_eur'], 150)
        self.assertEqual(result['funnel'][-1]['count'], 1)
        self.assertEqual(sum(row['new_customers'] for row in result['daily']), 1)


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
