from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import Competitor, MetaAdState, MetricPoint, SovRun


class SovOwnershipTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('analyst', password='test-password')
        self.client.force_login(self.user)
        self.competitor = Competitor.objects.create(name='Mentor', is_self=True)
        run = SovRun.objects.create(run_date=timezone.now().date(), status='completed')
        MetricPoint.objects.create(run=run, run_date=run.run_date, competitor=self.competitor,
                                   metric='trustpilot_reviews', value=100)
        MetaAdState.objects.create(ad_archive_id='ad-1', competitor=self.competitor,
                                   first_seen=run.run_date, last_seen=run.run_date,
                                   active=True, reach=1000)

    def test_sov_dashboard_and_aggregate_api_live_in_james(self):
        self.assertEqual(self.client.get('/competitor/sov/').status_code, 200)
        response = self.client.get('/competitor/sov/data.json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['rows'][0]['metric'], 'trustpilot_reviews')
        self.assertEqual(response.json()['ads'][0]['reach'], 1000)
