"""Static source/release checks; never inspect installation state or credentials."""
import ast
from pathlib import Path
import re
import subprocess
import unittest

from test_mcp_connector import RUNTIME_MODULES

PACKAGE = Path(__file__).resolve().parents[1]


def release_paths():
    """Inspect tracked/new source in a checkout, or files in an extracted archive."""
    if (PACKAGE / '.git').exists():
        result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others',
                                 '--exclude-standard'], cwd=PACKAGE,
                                check=True, capture_output=True)
        return [Path(name.decode()) for name in result.stdout.split(b'\0') if name]
    generated = {'.git', '.test-venv', '__pycache__'}
    return [path.relative_to(PACKAGE) for path in PACKAGE.rglob('*')
            if path.is_file() and not generated.intersection(path.relative_to(PACKAGE).parts)]


class PackagePrivacyTests(unittest.TestCase):
    def test_release_excludes_installation_state_and_private_artifacts(self):
        private_directories = {'observer-data', '.venv', '.native', '.aws', 'output'}
        private_files = {'robot.json', 'activation-result.json', 'connector.json',
                         'credentials.json', 'credentials.toml', 'password.json',
                         'observer-mcp-config.json', 'mcp-installation-result.json'}
        private_suffixes = {'.sqlite', '.sqlite3', '.db', '.log', '.pyc', '.pem', '.key'}
        for path in release_paths():
            with self.subTest(path=path.as_posix()):
                self.assertFalse(private_directories.intersection(path.parts))
                self.assertNotIn(path.name, private_files)
                self.assertNotIn(path.suffix, private_suffixes)
                self.assertFalse(path.name.startswith(('.write-approval', '.reset-approval')))

    def test_runtime_has_no_owner_machine_paths(self):
        owner_path = re.compile(r'/(?:Users|Volumes)/[^\s\"\'<>]+')
        for relative in release_paths():
            if relative.parts[0] != 'runtime' or relative.suffix not in ('.py', '.js', '.html', '.css'):
                continue
            with self.subTest(path=relative.as_posix()):
                self.assertIsNone(owner_path.search((PACKAGE / relative).read_text()),
                                  'Runtime source contains a machine-specific path')

    def test_selected_identity_is_not_a_hardcoded_owner_device(self):
        for name in ('control_layer', 'roomba_observer', 'room_labels', 'write_approval'):
            tree = ast.parse((PACKAGE / 'runtime' / (name + '.py')).read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                identity = any(isinstance(target, ast.Name) and target.id in ('TARGET', 'FINGERPRINT')
                               for target in node.targets)
                if identity and isinstance(node.value, ast.Constant):
                    self.fail(name + ' hardcodes the selected robot identity')

    def test_recovery_modules_and_local_import_dependencies_are_present(self):
        runtime = PACKAGE / 'runtime'
        required = {'recovery_context', 'recovery_store', 'map_recovery', 'recovery_diagnostics'}
        for name in required:
            self.assertTrue((runtime / (name + '.py')).is_file(), 'Missing recovery module: ' + name)
        for name in RUNTIME_MODULES:
            path = runtime / (name + '.py')
            if not path.is_file():
                continue
            tree = ast.parse(path.read_text())
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name.split('.')[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module.split('.')[0])
            for dependency in set(imports).intersection(RUNTIME_MODULES):
                self.assertTrue((runtime / (dependency + '.py')).is_file(),
                                name + ' imports missing local module ' + dependency)


if __name__ == '__main__':
    unittest.main()
