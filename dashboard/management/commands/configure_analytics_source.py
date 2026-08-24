from django.core.management.base import BaseCommand, CommandError

from dashboard.models import AnalyticsSource


class Command(BaseCommand):
    help = 'Create/update an analytics source and optionally rotate its API key.'

    def add_arguments(self, parser):
        parser.add_argument('slug')
        parser.add_argument('--name', required=True)
        parser.add_argument('--identity-mode', choices=dict(AnalyticsSource.IDENTITY_CHOICES),
                            default='aggregate_only')
        parser.add_argument('--rotate-key', action='store_true')

    def handle(self, *args, **options):
        source, created = AnalyticsSource.objects.update_or_create(
            slug=options['slug'], defaults={'name': options['name'],
                                             'identity_mode': options['identity_mode'],
                                             'is_active': True})
        if options['rotate_key'] or not source.api_key_hash:
            raw = source.issue_api_key()
            self.stdout.write(self.style.WARNING(
                f'API key (shown once) for {source.slug}: {raw}'))
        self.stdout.write(self.style.SUCCESS(
            f'Analytics source {source.slug}: {"created" if created else "updated"}, '
            f'identity_mode={source.identity_mode}'))
