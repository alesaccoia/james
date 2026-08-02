from django.test import SimpleTestCase

from .views import _resolve_tags, _spend_by_tag_dimension


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
