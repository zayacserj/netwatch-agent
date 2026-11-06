"""Test actual /add_router command generation and its shell bootstrap offline."""
import ast
import asyncio
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SHELL = os.environ.get('NETWATCH_TEST_SHELL') or shutil.which('sh')


class BotInstallation(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT / 'netwatch_telegram_bot.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name in ('_agent_install_command', 'cmd_add_router')]
        self.assertEqual(len(nodes), 2)
        for node in nodes:
            node.decorator_list = []
            for arg in node.args.args:
                arg.annotation = None
        self.codes = []
        namespace = dict(os=os, secrets=SimpleNamespace(token_hex=lambda count: '11223344'),
                         upsert_user=lambda *args: None,
                         create_pairing_code=lambda *args, **kwargs: self.codes.append((args, kwargs)))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'bot-installation', 'exec'), namespace)
        self.command = namespace['_agent_install_command']
        self.handler = namespace['cmd_add_router']
        self.replies = []

        async def reply(text, **kwargs):
            self.replies.append((text, kwargs))

        self.message = SimpleNamespace(from_user=SimpleNamespace(id=1, username='test', first_name='Test'),
                                       reply=reply)

    def render(self, local='http://192.168.10.222:8080', public=''):
        with patch.dict(os.environ, PI_URL=local, PI_URL_PUBLIC=public):
            asyncio.run(self.handler(self.message))
        return self.replies[-1]

    def test_command_pins_an_immutable_release_and_its_actual_checksum(self):
        command = self.command('11223344', 'http://pi.invalid:8080')
        self.assertRegex(command, r'/[0-9a-f]{40}/deploy_files/agent3_fixed\.sh')
        self.assertNotIn('/master/', command)
        self.assertNotIn('/main/', command)
        digest = hashlib.sha256((ROOT / 'deploy_files/agent3_fixed.sh').read_bytes()).hexdigest()
        self.assertIn(digest, command)
        self.assertLess(command.index('sha256sum -c -'), command.index(' pair --code '))
        self.assertIn('--fail', command)
        self.assertIn('--disable', command)
        self.assertNotIn(' -k ', command)
        self.assertNotIn('opkg ', command)
        self.assertNotIn('apk ', command)

    def test_local_command_is_rendered_as_copyable_code(self):
        text, kwargs = self.render()
        self.assertEqual(kwargs['parse_mode'], 'Markdown')
        self.assertIn('```\n(\n', text)
        self.assertEqual(len(self.codes), 1)
        self.assertEqual(self.codes[0], (('11223344', 1), {'expires_sec': 900}))

    def test_external_command_requires_configured_https(self):
        text, _ = self.render(public='https://api.example.invalid')
        self.assertIn('--url https://api.example.invalid', text)
        self.assertLessEqual(len(text), 4096)
        self.assertEqual(len(self.codes), 1)
        self.assertEqual(text.count('pair --code 11223344'), 2)

    def test_old_public_http_default_is_not_offered(self):
        text, _ = self.render(public='http://95.31.29.233:8080')
        self.assertNotIn('95.31.29.233', text)
        self.assertIn('публичный HTTPS-адрес', text)

    def test_invalid_local_address_does_not_issue_code_or_expose_credentials(self):
        text, _ = self.render(local='http://admin:private-password@pi.invalid')
        self.assertEqual(self.codes, [])
        self.assertNotIn('private-password', text)

    def test_invalid_codes_and_urls_are_rejected(self):
        for code in ('', 'not-code', '1122;344'):
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.command(code, 'http://pi.invalid')
        for url in ('file:///tmp/agent.sh', 'http://pi.invalid?x=1', 'http://pi.invalid#x',
                    'http://pi.invalid:invalid', 'http://pi.invalid\ncommand'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.command('11223344', url)

    def shell(self):
        if not SHELL:
            self.skipTest('shell bootstrap checks require sh')
        return [SHELL, 'sh'] if 'busybox' in Path(SHELL).name.lower() else [SHELL]

    def bootstrap(self, artifact, *, correct_hash=True, curl_exit='0', url='http://pi.invalid:8080'):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as name:
            folder = Path(name)
            download = folder / 'artifact.sh'
            download.write_text(artifact, encoding='utf-8', newline='\n')
            marker = folder / 'paired'
            command = self.command('11223344', url)
            if correct_hash:
                command = re.sub(r'(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])',
                                 hashlib.sha256(download.read_bytes()).hexdigest(), command)
            command = command.replace('/tmp/netwatch-install.XXXXXX', (folder / 'download.XXXXXX').as_posix())
            mock = '''curl() {
    [ "$CURL_EXIT" = 0 ] || return "$CURL_EXIT"
    while [ "$#" -gt 0 ]; do
        if [ "$1" = --output ]; then cp "$ARTIFACT" "$2"; return; fi
        shift
    done
    return 99
}
'''
            result = subprocess.run(self.shell() + ['-c', mock + command], capture_output=True, text=True,
                                    env=dict(os.environ, CURL_EXIT=curl_exit, ARTIFACT=download.as_posix(),
                                             PAIR_MARKER=marker.as_posix()), timeout=15)
            args = marker.read_text().splitlines() if marker.exists() else None
            # Linux can unlink a script just executed by a child shell;
            # busybox-w32 may retain its handle until the parent unwinds.
            if os.name != 'nt':
                self.assertEqual(list(folder.glob('download.*')), [])
            return result, args

    def test_bootstrap_passes_code_and_url_without_shell_interpretation(self):
        url = 'http://pi.invalid/path;literal'
        result, args = self.bootstrap('printf "%s\\n" "$@" > "$PAIR_MARKER"\n', url=url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(args, ['pair', '--code', '11223344', '--url', url])

    def test_checksum_mismatch_prevents_execution(self):
        result, args = self.bootstrap('echo executed > "$PAIR_MARKER"\n', correct_hash=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(args)

    def test_download_failure_prevents_execution(self):
        result, args = self.bootstrap('echo executed > "$PAIR_MARKER"\n', curl_exit='22')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(args)

    def test_invalid_shell_artifact_is_not_executed_even_with_matching_hash(self):
        result, args = self.bootstrap(') invalid shell\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(args)

    def test_generated_command_through_actual_installer_preserves_cron(self):
        from test_agent_installation import Installation
        fixture = Installation()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        command = self.command('11223344', 'http://pi.invalid:8080')
        # The harness adds fake radio/HTTP functions to the real release script.
        command = re.sub(r'(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])',
                         hashlib.sha256(fixture.agent.read_bytes()).hexdigest(), command)
        command = command.replace('/tmp/netwatch-install.XXXXXX', (fixture.root / 'download.XXXXXX').as_posix())
        mock = '''curl() {
    while [ "$#" -gt 0 ]; do
        if [ "$1" = --output ]; then cp "$ARTIFACT" "$2"; return; fi
        shift
    done
    return 99
}
'''
        result = subprocess.run(self.shell() + ['-c', mock + command], capture_output=True, text=True,
                                env=dict(fixture.env, ARTIFACT=fixture.agent.as_posix(), CANONICAL_ID='rt_from_server'),
                                timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        fixture.assert_cron()
        self.assertEqual(fixture.marker.read_text().strip(), 'rt_from_server')
        self.assertEqual(len(fixture.requests()), 1)
        self.assertNotIn('a'*32, result.stdout + result.stderr)
