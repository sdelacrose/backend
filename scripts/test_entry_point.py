"""Exercise the actual shell entrypoint with fake executables; no database or server."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(Path('/bin/sh').exists(), 'Requires a POSIX shell, as in the Docker image')
class EntryPointTests(unittest.TestCase):
    def run_entrypoint(self, migration_flag=None, migration_exit='0'):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            script = directory / 'entry.sh'
            script.write_text((ROOT / 'scripts/entry_point.sh').read_text(), encoding='utf-8')
            log = directory / 'calls'
            python = directory / 'python'
            python.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_CALL_LOG"\n'
                'if [ "$*" = "manage.py migrate" ]; then exit "$TEST_MIGRATION_EXIT"; fi\n',
                encoding='utf-8',
            )
            daphne = directory / 'daphne'
            daphne.write_text('#!/bin/sh\necho daphne >> "$TEST_CALL_LOG"\n', encoding='utf-8')
            python.chmod(0o755)
            daphne.chmod(0o755)
            environment = dict(os.environ, PATH=f'{directory}:/usr/bin:/bin',
                               TEST_CALL_LOG=str(log), TEST_MIGRATION_EXIT=migration_exit)
            environment.pop('LINKS_RUN_MIGRATIONS', None)
            if migration_flag is not None:
                environment['LINKS_RUN_MIGRATIONS'] = migration_flag
            result = subprocess.run(['/bin/sh', str(script)], env=environment,
                                    text=True, capture_output=True, check=False)
            return result, log.read_text().splitlines() if log.exists() else []

    def test_default_preserves_existing_startup(self):
        result, calls = self.run_entrypoint()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, ['manage.py migrate', 'manage.py collectstatic --noinput', 'daphne'])

    def test_explicit_one_runs_migrations(self):
        result, calls = self.run_entrypoint('1')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls[0], 'manage.py migrate')

    def test_zero_starts_without_migrations(self):
        result, calls = self.run_entrypoint('0')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, ['manage.py collectstatic --noinput', 'daphne'])

    def test_invalid_value_stops_before_any_command(self):
        result, calls = self.run_entrypoint('false')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, [])

    def test_failed_migration_prevents_startup(self):
        result, calls = self.run_entrypoint('1', migration_exit='5')
        self.assertEqual(result.returncode, 5)
        self.assertEqual(calls, ['manage.py migrate'])


class NoMigrateOverrideTests(unittest.TestCase):
    def test_override_only_disables_api_migrations(self):
        path = ROOT / 'deploy/recette/docker-compose.no-migrate.yml'
        data = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(data, {'services': {'links-recette-api': {
            'environment': {'LINKS_RUN_MIGRATIONS': '0'},
        }}})


if __name__ == '__main__':
    unittest.main()
