import hashlib
import secrets

from django.core.management.base import BaseCommand

from auth_keys.models import ApiKey


class Command(BaseCommand):
    help = 'Generate a new API key, store its SHA-256 hash, and print the plaintext key.'

    def add_arguments(self, parser):
        parser.add_argument('--label', type=str, default='', help='Optional human-readable label for this key.')

    def handle(self, *args, **options):
        plaintext_key = secrets.token_hex(32)
        key_hash = hashlib.sha256(plaintext_key.encode('utf-8')).hexdigest()

        ApiKey.objects.create(
            key_hash=key_hash,
            label=options['label'],
            is_active=True,
        )

        self.stdout.write(plaintext_key)
        self.stderr.write(
            self.style.SUCCESS(f"API key created successfully. Label: '{options['label']}'")
        )
