import base64
import copy
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('migrate3', Path(__file__).parents[1] / 'scripts/migrate3.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def fixture(kind='deploymentconfig', clone=True):
    conf = dict(name='app', secrets=['credentials'], configmap=['settings'], env=['APP_MODE'],
                useexisting=dict(secrets=[], configmap=[], env=[]))
    raw = dict(name='example', containers=[conf], testsvc=True, labels=[], annotations=[])
    group = dict(namespace='demo', **{'deploymentconfigs' if kind == 'deploymentconfig' else 'deployments': [raw]})
    if kind == 'deployment':
        raw['clone'] = dict(enabled=clone)
        raw['testsvc'] = clone
    source = dict(apiVersion='apps/v1' if kind == 'deployment' else 'apps.openshift.io/v1',
                  kind='Deployment' if kind == 'deployment' else 'DeploymentConfig',
                  metadata=dict(name='example', namespace='demo', uid='uid-one', resourceVersion='123',
                                labels=dict(app='old'), annotations={'user/annotation': 'old'}),
                  spec=dict(replicas=4, selector=dict(matchLabels=dict(app='old')) if kind == 'deployment' else dict(app='old'),
                            strategy=dict(type='RollingUpdate' if kind == 'deployment' else 'Rolling'),
                            template=dict(metadata=dict(labels=dict(app='old'), annotations={'pod/annotation': 'keep'}),
                                          spec=dict(serviceAccountName='keep', containers=[dict(name='app', image='image@sha256:abc',
                                              ports=[dict(containerPort=8080)], resources=dict(requests=dict(cpu='100m')),
                                              env=[dict(name='APP_MODE', value='testing'),
                                                   dict(name='PASSWORD', valueFrom=dict(secretKeyRef=dict(name='credentials', key='PASSWORD'))),
                                                   dict(name='LOG', valueFrom=dict(configMapKeyRef=dict(name='settings', key='LOG')))],
                                              envFrom=[dict(prefix='CFG_', configMapRef=dict(name='settings'))],
                                              volumeMounts=[dict(name='config', mountPath='/config', subPath='app.conf')])],
                                              volumes=[dict(name='config', configMap=dict(name='settings', defaultMode=288))]))))
    resources = {('demo', 'secret', 'credentials'): dict(kind='Secret', data={'PASSWORD': 'cGFzcwo=', 'UNUSED': 'a2VlcA=='}),
                 ('demo', 'configmap', 'settings'): dict(kind='ConfigMap', data={'LOG': 'info', 'app.conf': 'line1\nline2\n'})}
    return dict(data=[group]), source, resources


def apply_patch(document, operations):
    result = copy.deepcopy(document)
    for op in operations:
        bits = op['path'].strip('/').split('/')
        parent = result
        for key in bits[:-1]:
            parent = parent[int(key)] if isinstance(parent, list) else parent[key]
        key = int(bits[-1]) if isinstance(parent, list) else bits[-1]
        if op['op'] == 'test':
            if parent[key] != op['value']:
                raise ValueError('stale resource')
        else:
            parent[key] = copy.deepcopy(op['value'])
    return result


class FakeVault:
    def __init__(self):
        self.mounts = {}
        self.auth = {}
        self.data = {}
        self.policies = {}
        self.roles = {}
        self.ids = set()
        self.calls = []
        self.fail_after = None

    def __call__(self, method, path, payload=None, missing=False):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if path == 'sys/mounts':
            return {'data': copy.deepcopy(self.mounts)}
        if path == 'sys/auth':
            return {'data': copy.deepcopy(self.auth)}
        if path.startswith('sys/mounts/'):
            self.mounts[path[len('sys/mounts/'):] + '/'] = copy.deepcopy(payload)
            return {}
        if path.startswith('sys/auth/'):
            self.auth[path[len('sys/auth/'):] + '/'] = copy.deepcopy(payload)
            return {}
        if '/metadata/' in path or '/data/' in path:
            kind = 'metadata' if '/metadata/' in path else 'data'
            mount, key = path.split('/' + kind + '/', 1)
            identity = (mount, key)
            current = self.data.get(identity)
            if method == 'GET':
                if current is None:
                    if missing:
                        return None
                    raise m.VaultError(method, path, 404)
                if kind == 'metadata':
                    return {'data': copy.deepcopy(current['metadata'])}
                if current['values'] is None:
                    raise m.VaultError(method, path, 404)
                return {'data': {'data': copy.deepcopy(current['values']), 'metadata': {'version': current['metadata']['current_version']}}}
            if kind == 'metadata':
                current = self.data.setdefault(identity, {'metadata': {'current_version': 0, 'versions': {}}, 'values': None})
                current['metadata']['custom_metadata'] = copy.deepcopy(payload['custom_metadata'])
                return {}
            version = current['metadata']['current_version'] if current else 0
            if payload['options']['cas'] != version:
                raise m.VaultError(method, path, 400)
            current = self.data.setdefault(identity, {'metadata': {'current_version': 0, 'versions': {}}, 'values': None})
            version += 1
            current['metadata']['current_version'] = version
            current['metadata']['versions'][str(version)] = {'destroyed': False, 'deletion_time': ''}
            current['values'] = copy.deepcopy(payload['data'])
            if self.fail_after == path:
                self.fail_after = None
                raise m.MigrationError('simulated lost response after successful write')
            return {}
        if path.startswith('sys/policies/acl/'):
            if method == 'GET':
                return {'data': {'policy': self.policies[path]}} if path in self.policies else None
            self.policies[path] = payload['policy']
            return {}
        if path.endswith('/role-id'):
            return {'data': {'role_id': 'role-id-test'}}
        if path.endswith('/custom-secret-id'):
            self.ids.add(payload['secret_id'])
            return {}
        if path.endswith('/secret-id/lookup'):
            if payload['secret_id'] not in self.ids:
                raise m.VaultError(method, path, 400)
            return {'data': {'secret_id_accessor': 'accessor'}}
        if '/role/' in path:
            if method == 'GET':
                return {'data': self.roles[path]} if path in self.roles else None
            self.roles[path] = copy.deepcopy(payload)
            return {}
        raise AssertionError((method, path))


class V3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work_patch = patch.object(m, 'WORK', Path(self.tmp.name))
        self.work_patch.start()
        self.vault = FakeVault()
        self.api_patch = patch.object(m, 'api', self.vault)
        self.api_patch.start()
        self.output = io.StringIO()
        self.redirect = redirect_stdout(self.output)
        self.redirect.__enter__()
        m.write(m.WORK / 'config.json', dict(ocp='crc-sip', vaultaddr='http://vault:8200', vaultcred='vault'))
        m.write(m.WORK / 'run.json', dict(id='test-run'))

    def tearDown(self):
        self.redirect.__exit__(None, None, None)
        self.api_patch.stop()
        self.work_patch.stop()
        self.tmp.cleanup()

    def build(self, document, source, resources, index=0, init=True):
        if init:
            m.write(m.WORK / 'input.json', document)
            m.init()
        m.write(m.location(index, 'source'), source)
        m.source_requests(index)
        if m.read(m.location(index, 'eligibility'))['skip']:
            return None
        m.inspect_shared(index)
        for entry in m.read(m.location(index, 'inputs')):
            if not entry['existing']:
                m.write(entry['snapshot'], resources[('demo', entry['kind'], entry['name'])])
        m.prepare(index)
        return m.record_at(index)

    def shared_fixture(self, kind='deploymentconfig', clone=True):
        doc, source, resources = fixture(kind, clone)
        doc['data'][0]['sharedsecret'] = ['credentials']
        doc['data'][0]['sharedconfigmap'] = ['settings']
        return doc, source, resources

    def provision_all(self, record):
        for i in range(len(record['operations'])):
            m.vault_action(record['index'], i)

    def test_three_paths_and_source_specific_refs(self):
        doc, source, resources = self.shared_fixture()
        before = copy.deepcopy(source)
        r = self.build(doc, source, resources)
        self.assertEqual(source, before)
        self.assertEqual([d['path'] for d in r['datasets']], ['example', 'shared/configmap/settings', 'shared/secret/credentials'])
        self.assertEqual(m.read(r['datasets'][0]['payloadFile']), {'APP_MODE': 'testing'})
        clone = m.read(r['deploymentFile'])
        container = clone['spec']['template']['spec']['containers'][0]
        dest = {d['path']: d['destination'] for d in r['datasets']}
        self.assertEqual(container['env'][0]['valueFrom']['secretKeyRef']['name'], dest['example'])
        self.assertEqual(container['env'][1]['valueFrom']['secretKeyRef']['name'], dest['shared/secret/credentials'])
        self.assertEqual(container['envFrom'][0]['secretRef']['name'], dest['shared/configmap/settings'])
        self.assertEqual(container['envFrom'][0]['prefix'], 'CFG_')
        self.assertEqual(clone['spec']['template']['spec']['volumes'][0]['secret']['secretName'], dest['shared/configmap/settings'])
        self.assertEqual(clone['spec']['template']['spec']['volumes'][0]['secret']['defaultMode'], 288)
        self.assertEqual(container['volumeMounts'], source['spec']['template']['spec']['containers'][0]['volumeMounts'])
        self.assertEqual(clone['spec']['replicas'], 1)

    def test_cross_run_shared_reuse_does_not_read_ocp_or_overwrite_rotation(self):
        doc, source, resources = self.shared_fixture()
        r = self.build(doc, source, resources)
        self.provision_all(r)
        shared = self.vault.data[('demo-kv', 'shared/secret/credentials')]
        shared['values']['PASSWORD'] = 'rotated-new-password'
        shared['metadata']['current_version'] += 1
        old_count = len([c for c in self.vault.calls if c[0] == 'POST' and '/data/shared/' in c[1]])
        other = copy.deepcopy(doc['data'][0]['deploymentconfigs'][0])
        other['name'] = 'second'
        doc['data'][0]['deploymentconfigs'].append(other)
        m.write(m.WORK / 'input.json', doc)
        m.init()
        second = copy.deepcopy(source)
        second['metadata']['name'] = 'second'
        r2 = self.build(doc, second, {}, index=1, init=False)
        self.provision_all(r2)
        d = next(d for d in r2['datasets'] if d['path'] == 'shared/secret/credentials')
        self.assertEqual(m.read(d['expectedFile'])['PASSWORD'], 'rotated-new-password')
        self.assertEqual(len([c for c in self.vault.calls if c[0] == 'POST' and '/data/shared/' in c[1]]), old_count)
        self.assertNotEqual(d['destination'], r['datasets'][2]['destination'])

    def test_existing_shared_missing_metadata_is_not_adopted(self):
        doc, source, resources = self.shared_fixture()
        self.vault.mounts['demo-kv/'] = dict(type='kv', options=dict(version='2'))
        self.vault('POST', 'demo-kv/data/shared/secret/credentials', dict(options=dict(cas=0), data={'PASSWORD': 'x'}))
        with self.assertRaisesRegex(m.MigrationError, 'Metadata pemilik'):
            self.build(doc, source, resources)

    def test_omitted_shared_flag_detected_across_runs(self):
        doc, source, resources = self.shared_fixture()
        r = self.build(doc, source, resources)
        self.provision_all(r)
        doc['data'][0]['sharedsecret'] = []
        with self.assertRaisesRegex(m.MigrationError, 'field shared'):
            self.build(doc, source, resources)

    def test_deleted_shared_path_not_treated_as_absent(self):
        doc, source, resources = self.shared_fixture()
        r = self.build(doc, source, resources)
        self.provision_all(r)
        self.vault.data[('demo-kv', 'shared/secret/credentials')]['metadata']['versions']['1']['deletion_time'] = 'deleted'
        with self.assertRaisesRegex(m.MigrationError, 'terhapus'):
            self.build(doc, source, resources)

    def test_403_not_treated_as_absent(self):
        doc, source, resources = self.shared_fixture()
        self.vault.mounts['demo-kv/'] = dict(type='kv', options=dict(version='2'))
        real = self.vault
        def forbidden(method, path, *args, **kwargs):
            if '/metadata/' in path:
                raise m.VaultError(method, path, 403)
            return real(method, path, *args, **kwargs)
        with patch.object(m, 'api', forbidden):
            with self.assertRaisesRegex(m.VaultError, '403'):
                self.build(doc, source, resources)
        self.assertFalse(any(c[0] != 'GET' for c in self.vault.calls))

    def test_lost_shared_write_response_retries_without_another_version(self):
        doc, source, resources = self.shared_fixture()
        r = self.build(doc, source, resources)
        for i in (0, 1, 2):
            m.vault_action(0, i)
        self.vault.fail_after = 'demo-kv/data/shared/configmap/settings'
        with self.assertRaises(m.MigrationError):
            m.vault_action(0, 3)
        self.assertEqual(m.receipt(0, 'vault-3')['status'], 'pending')
        m.vault_action(0, 3)
        self.assertEqual(self.vault.data[('demo-kv', 'shared/configmap/settings')]['metadata']['current_version'], 1)
        count = len(self.vault.calls)
        m.vault_action(0, 3)
        self.assertEqual(len(self.vault.calls), count)

    def test_lost_private_write_response_reconciles(self):
        doc, source, resources = fixture()
        r = self.build(doc, source, resources)
        m.vault_action(0, 0)
        m.vault_action(0, 1)
        self.vault.fail_after = 'demo-kv/data/example'
        with self.assertRaises(m.MigrationError):
            m.vault_action(0, 2)
        m.vault_action(0, 2)
        self.assertEqual(self.vault.data[('demo-kv', 'example')]['metadata']['current_version'], 1)

    def test_policy_readback_and_role_access_all_selected_paths(self):
        doc, source, resources = self.shared_fixture()
        r = self.build(doc, source, resources)
        self.provision_all(r)
        text = self.vault.policies['sys/policies/acl/demo-example-access']
        for d in r['datasets']:
            self.assertIn('demo-kv/data/' + d['path'], text)
        self.assertEqual(text.count('capabilities = ["read"]'), 3)
        self.assertEqual(self.vault.roles['auth/demo-approle/role/example']['token_policies'], ['demo-example-access'])
        self.assertEqual(m.read(r['authFile'])['spec']['mount'], 'demo-approle')
        secret_id = m.read(r['holderFile'])['stringData']['id']
        self.assertIn(secret_id, self.vault.ids)
        self.assertNotIn(secret_id, self.output.getvalue())

    def test_custom_secret_id_recovery_reuses_generated_id(self):
        doc, source, resources = fixture()
        r = self.build(doc, source, resources)
        self.provision_all(r)
        op = len(r['operations']) - 1
        m.receipt(0, 'vault-' + str(op), 'pending', 'lost response')
        old_ids = set(self.vault.ids)
        m.vault_action(0, op)
        self.assertEqual(self.vault.ids, old_ids)
        self.assertEqual(sum(c[1].endswith('/custom-secret-id') for c in self.vault.calls), 1)

    def test_pvc_skips_before_vault(self):
        doc, source, resources = fixture()
        source['spec']['template']['spec']['volumes'].append(dict(name='data', persistentVolumeClaim=dict(claimName='data')))
        self.assertIsNone(self.build(doc, source, resources))
        self.assertEqual(self.vault.calls, [])

    def test_duplicate_shared_private_key_rejected(self):
        doc, source, resources = self.shared_fixture()
        resources[('demo', 'secret', 'credentials')]['data']['APP_MODE'] = 'dGVzdGluZw=='
        with self.assertRaisesRegex(m.MigrationError, 'KEY duplikat'):
            self.build(doc, source, resources)

    def test_useexisting_excludes_even_shared_sources(self):
        doc, source, resources = self.shared_fixture()
        c = doc['data'][0]['deploymentconfigs'][0]['containers'][0]
        c['useexisting']['secrets'] = ['credentials']
        r = self.build(doc, source, resources)
        self.assertNotIn('shared/secret/credentials', [d['path'] for d in r['datasets']])
        env = m.read(r['deploymentFile'])['spec']['template']['spec']['containers'][0]['env']
        self.assertEqual(env[1]['valueFrom']['secretKeyRef']['name'], 'credentials')

    def test_no_port_skips_only_service(self):
        doc, source, resources = fixture()
        del source['spec']['template']['spec']['containers'][0]['ports']
        r = self.build(doc, source, resources)
        self.assertTrue(r['migrate'])
        self.assertEqual(r['serviceName'], '')
        self.assertIn('SKIP SERVICE', self.output.getvalue())

    def test_clone_create_retry_requires_same_run_and_matching_spec(self):
        doc, source, resources = fixture()
        r = self.build(doc, source, resources)
        desired = m.read(r['deploymentFile'])
        m.write(r['currentFile'], {})
        m.create_check(0, 'deployment')
        self.assertFalse(m.read(m.location(0, 'decision'))['noop'])
        desired['spec']['template']['spec']['dnsPolicy'] = 'ClusterFirst'
        m.write(r['currentFile'], desired)
        m.create_check(0, 'deployment')
        self.assertTrue(m.read(m.location(0, 'decision'))['noop'])
        desired['metadata']['annotations'][m.RUN_MARKER] = 'someone-else'
        m.write(r['currentFile'], desired)
        with self.assertRaisesRegex(m.MigrationError, 'bukan hasil'):
            m.create_check(0, 'deployment')

    def test_inplace_retry_preserves_unrelated_updates_and_is_noop_after_success(self):
        doc, source, resources = self.shared_fixture('deployment', False)
        r = self.build(doc, source, resources)
        current = copy.deepcopy(source)
        current['metadata']['resourceVersion'] = '125'
        current['spec']['replicas'] = 7
        current['spec']['template']['spec']['containers'][0]['image'] = 'new-image:1'
        m.write(r['currentFile'], current)
        m.patch_refresh(0)
        changed = apply_patch(current, m.read(r['patchFile']))
        self.assertEqual(changed['spec']['replicas'], 7)
        self.assertEqual(changed['spec']['template']['spec']['containers'][0]['image'], 'new-image:1')
        self.assertEqual(changed['metadata'], current['metadata'])
        m.write(r['currentFile'], changed)
        m.patch_refresh(0)
        self.assertTrue(m.read(m.location(0, 'decision'))['noop'])

    def test_inplace_retry_rejects_changed_source_config(self):
        doc, source, resources = fixture('deployment', False)
        r = self.build(doc, source, resources)
        current = copy.deepcopy(source)
        current['spec']['template']['spec']['containers'][0]['env'][0]['value'] = 'edited-after-review'
        m.write(r['currentFile'], current)
        with self.assertRaisesRegex(m.MigrationError, 'berubah sejak review'):
            m.patch_refresh(0)

    def test_shared_rotation_after_review_not_overwritten(self):
        doc, source, resources = self.shared_fixture()
        r = self.build(doc, source, resources)
        self.provision_all(r)
        self.vault.data[('demo-kv', 'shared/configmap/settings')]['values']['LOG'] = 'new'
        m.receipt(0, 'vault-3', 'pending', 'retry')
        with self.assertRaisesRegex(m.MigrationError, 'berubah sejak review'):
            m.vault_action(0, 3)
        self.assertEqual(self.vault.data[('demo-kv', 'shared/configmap/settings')]['values']['LOG'], 'new')

    def test_verify_and_review_never_log_values(self):
        doc, source, resources = self.shared_fixture()
        r = self.build(doc, source, resources)
        for d in r['datasets']:
            expected = m.read(d['expectedFile'])
            m.write(d['actualFile'], dict(data={k:base64.b64encode(v.encode()).decode() for k,v in expected.items()}))
            self.assertEqual(m.verify(0, d['index']), 0)
        output = io.StringIO()
        with redirect_stdout(output):
            m.review(0)
            m.report(0)
        self.assertIn('PASSWORD', output.getvalue())
        self.assertNotIn('testing', output.getvalue())
        self.assertNotIn('line1', output.getvalue())

    def test_secret_and_configmap_same_name_have_different_paths_and_destinations(self):
        doc, source, resources = fixture()
        group = doc['data'][0]
        group['sharedsecret'] = ['credentials']
        group['sharedconfigmap'] = ['credentials']
        c = group['deploymentconfigs'][0]['containers'][0]
        c['configmap'] = ['credentials']
        resources[('demo','configmap','credentials')] = resources.pop(('demo','configmap','settings'))
        r = self.build(doc, source, resources)
        shared = [d for d in r['datasets'] if d['shared']]
        self.assertEqual(len({d['destination'] for d in shared}), 2)
        self.assertEqual({d['path'] for d in shared}, {'shared/secret/credentials','shared/configmap/credentials'})


if __name__ == '__main__':
    unittest.main()
