"""在临时目录中验证部署顺序及保留数据，不操作真实 Docker 或生产文件。"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest import TestCase
import zipfile


FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ['PROBE_PROJECT'])
with open(os.environ['PROBE_CALLS'], 'a') as log:
    log.write(json.dumps(args) + '\n')
if args[0] == 'inspect':
    template = args[-1]
    print(str(root) if '.Source' in template else ('bind' if '.Type' in template else 'sha256:local-test-image'))
elif args[0] == 'run':
    assert args[args.index('--network')+1] == 'none'
    assert (root/'app/code.py').read_text() == 'old'
    stage_app = next(arg.rsplit(':',2)[0] for arg in args if arg.endswith(':/app/app:ro'))
    assert (pathlib.Path(stage_app)/'code.py').read_text() == 'new'
    if os.environ.get('PROBE_PREFLIGHT_FAIL') == '1':
        sys.exit(1)
    print('PASS simulated offline preflight')
elif args[0] == 'start':
    assert (root/'app/code.py').read_text() == 'new'
elif args[0] == 'exec':
    sys.stdin.read()
    if os.environ.get('PROBE_NOT_READY') == '1':
        print('NOT_READY simulated dependency unavailable')
        sys.exit(3)
    print('PASS simulated readiness')
'''

FAKE_SHA = r'''#!/usr/bin/env python3
import hashlib, pathlib, sys
for line in pathlib.Path(sys.argv[-1]).read_text().splitlines():
    expected, name = line.split('  ',1)
    if hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest() != expected:
        sys.exit(1)
'''


class ProductionDeployerTest(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="energy-deploy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "production"
        self.release = self.project / "update-v5"
        self.release.mkdir(parents=True)
        for relative, content in {
            "app/code.py": "old", "scripts/old.py": "old", "main.py": "old", "chat.html": "old",
            "patch-manifest.json": "old", ".env": "keep-secret-config",
            "static/exports/report.xlsx": "keep-report", "logs/service.log": "keep-log",
            "config/business_data_catalog.yaml": "keep-catalog", "docker-compose.yml": "keep-compose",
        }.items():
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.script = self.release / "部署更新.sh"
        shutil.copyfile(Path(__file__).resolve().parents[1]/"scripts/deploy_production_update.sh", self.script)
        groups = {
            "app完整.zip": {"app/code.py": "new"},
            "scripts完整.zip": {"scripts/verify_production_patch.py": "new"},
            "根目录文件.zip": {"main.py": "new", "chat.html": "new", "patch-manifest.json": "new"},
        }
        for name, members in groups.items():
            with zipfile.ZipFile(self.release/name, "w") as archive:
                for relative, content in members.items():
                    archive.writestr(relative, content)
        self.checksums = self.release/"SHA256SUMS.txt"
        self.checksums.write_text("".join(
            hashlib.sha256(path.read_bytes()).hexdigest()+"  "+path.name+"\n"
            for path in [self.script, *(self.release/name for name in groups)]
        ))
        binary = self.root/"bin"
        binary.mkdir()
        for name, source in (("docker", FAKE_DOCKER), ("sha256sum", FAKE_SHA)):
            path = binary/name
            path.write_text(source)
            path.chmod(0o755)
        self.calls = self.root/"calls.jsonl"
        self.environment = {**os.environ, "PATH": str(binary)+os.pathsep+os.environ["PATH"],
                            "PROBE_PROJECT": str(self.project), "PROBE_CALLS": str(self.calls)}

    def run_deploy(self, **extra):
        return subprocess.run(["bash", str(self.script)], env={**self.environment, **extra}, capture_output=True, text=True, timeout=10)

    def test_success_replaces_complete_code_and_preserves_data_and_container(self):
        result = self.run_deploy()
        self.assertEqual(0, result.returncode, result.stdout+result.stderr)
        self.assertEqual("new", (self.project/"app/code.py").read_text())
        for relative, expected in ((".env", "keep-secret-config"), ("static/exports/report.xlsx", "keep-report"),
                                   ("logs/service.log", "keep-log"), ("config/business_data_catalog.yaml", "keep-catalog"),
                                   ("docker-compose.yml", "keep-compose")):
            self.assertEqual(expected, (self.project/relative).read_text())
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        actions = [call[0] for call in calls]
        self.assertEqual(["run", "stop", "start", "exec"], [a for a in actions if a != "inspect"])
        backup = next(self.root.glob("energy-before-update.*"))
        self.assertEqual("old", (backup/"app/code.py").read_text())
        self.assertTrue((self.release/"部署结果.txt").is_file())

    def test_failed_preflight_does_not_stop_or_replace_production(self):
        result = self.run_deploy(PROBE_PREFLIGHT_FAIL="1")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("old", (self.project/"app/code.py").read_text())
        self.assertFalse(any(json.loads(line)[0] == "stop" for line in self.calls.read_text().splitlines()))

    def test_bad_checksum_stops_before_any_container_action(self):
        with (self.release/"app完整.zip").open("ab") as file:
            file.write(b"corrupt")
        self.assertNotEqual(0, self.run_deploy().returncode)
        self.assertFalse(self.calls.exists())
        self.assertEqual("old", (self.project/"app/code.py").read_text())

    def test_pending_database_does_not_claim_deployment_is_ready(self):
        result = self.run_deploy(PROBE_NOT_READY="1")
        self.assertEqual(3, result.returncode, result.stdout+result.stderr)
        self.assertEqual("new", (self.project/"app/code.py").read_text())
        self.assertIn("未声明业务恢复", result.stdout)
        self.assertNotIn("部署流程完成", result.stdout)
