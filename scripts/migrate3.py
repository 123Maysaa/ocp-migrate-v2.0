#!/usr/bin/env python3
"""V3 manifest transformation and retry-aware Vault HTTP provisioning (stdlib only).

Cluster operations deliberately stay in the Jenkins OpenShift Client Plugin context.
Never log values, CLI stdout/stderr, or complete source/generated manifests.
"""
import base64
import copy
import json
import os
from pathlib import Path
import re
import sys
import hashlib
import secrets
import ssl
import urllib.request
import urllib.error
import urllib.parse

WORK = Path('.migration3-work')
API = 'secrets.hashicorp.com/v1beta1'
# Edit these constants to change generated names. Explicit newname takes precedence.
CLONE_PREFIX = 'newvault-'
TEST_SERVICE_SUFFIX = '-newvault-svc'
NAMED_SERVICE_SUFFIX = '-svc'
LABEL_VALUE_PREFIX = 'new-'


class MigrationError(Exception):
    pass


def require(ok, message):
    if not ok:
        raise MigrationError(message)


def read(path):
    with open(path, encoding='utf-8') as stream:
        return json.load(stream)


def write(path, value):
    with open(path, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
    os.chmod(path, 0o600)


def name(value, limit=253):
    require(isinstance(value, str) and len(value) <= limit and
            re.fullmatch(r'[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?', value),
            'Nama resource tidak valid atau terlalu panjang')
    return value


def entries(document):
    require(isinstance(document, dict) and isinstance(document.get('data'), list)
            and document['data'], 'migrate.yaml: data harus array tidak kosong')
    result, seen = [], set()
    for group in document['data']:
        ns = name(group['namespace'], 63)
        require('.' not in ns, 'Namespace tidak boleh mengandung titik')
        for field, kind in [('deploymentconfigs', 'deploymentconfig'), ('deployments', 'deployment')]:
            for item in group.get(field, []):
                service = name(item['name'])
                require((ns, service) not in seen, 'Nama workload duplikat dalam namespace')
                seen.add((ns, service))
                require(item.get('containers'), 'Daftar containers wajib diisi')
                containers = set()
                for container in item['containers']:
                    cname = name(container['name'], 63)
                    require(cname not in containers, 'Container duplikat')
                    containers.add(cname)
                    for key in ('secrets', 'configmap', 'env'):
                        require(isinstance(container.get(key, []), list), key + ' harus array')
                    for key in ('secrets', 'configmap'):
                        for resource in container.get(key, []):
                            name(resource)
                    require(all(isinstance(v, str) and v for v in container.get('env', [])),
                            'Nama env harus string tidak kosong')
                clone_config = item.get('clone', {})
                clone = True if kind == 'deploymentconfig' else clone_config.get('enabled', True)
                require(isinstance(clone, bool), 'clone.enabled harus boolean')
                custom_name = item.get('newname') if kind == 'deploymentconfig' else clone_config.get('newname')
                target = name(custom_name or CLONE_PREFIX + service) if clone else service
                testsvc = item.get('testsvc', False)
                require(isinstance(testsvc, bool), 'testsvc harus boolean')
                require(clone or not testsvc, 'testsvc harus false untuk migrasi in-place')
                configs = copy.deepcopy(item['containers'])
                excluded = {'secrets': set(), 'configmap': set()}
                for c in configs:
                    keep = c.get('useexisting', {})
                    require(isinstance(keep, dict), 'useexisting harus object')
                    for field_name in ('secrets', 'configmap', 'env'):
                        values = keep.get(field_name, [])
                        require(isinstance(values, list) and all(isinstance(v, str) and v for v in values),
                                'useexisting harus berisi array nama')
                        if field_name != 'env':
                            for value in values:
                                name(value)
                            excluded[field_name].update(values)
                # A resource explicitly excluded anywhere in the workload stays local.
                for c in configs:
                    for field_name in ('secrets', 'configmap'):
                        c[field_name] = list(dict.fromkeys(v for v in c.get(field_name, []) if v not in excluded[field_name]))
                    c['env'] = list(dict.fromkeys(v for v in c.get('env', []) if v not in c.get('useexisting', {}).get('env', [])))
                labels = metadata_input(item.get('labels'), 'labels') if clone else {}
                annotations = metadata_input(item.get('annotations'), 'annotations') if clone else {}
                result.append(dict(namespace=ns, name=service, kind=kind, containers=configs,
                                   clone=clone, target=target, testsvc=testsvc, labels=labels,
                                   annotations=annotations, customName=bool(custom_name) if clone else False,
                                   sharedsecret=group.get('sharedsecret', []),
                                   sharedconfigmap=group.get('sharedconfigmap', [])))
    require(result, 'Tidak ada workload untuk dimigrasikan')
    return result


def metadata_input(value, field):
    if value is None or value == []:
        return {}
    require(isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()),
            field + ' harus mapping key: string-value; [] hanya diterima untuk kosong')
    return value






def resource_data(resource):
    """Canonical bytes for exact comparison, including ConfigMap binaryData."""
    if resource['kind'] == 'Secret':
        return {k: base64.b64decode(v, validate=True) for k, v in resource.get('data', {}).items()}
    data = {k: v.encode('utf-8') for k, v in resource.get('data', {}).items()}
    data.update({k: base64.b64decode(v, validate=True) for k, v in resource.get('binaryData', {}).items()})
    return data


def prefixed_labels(labels):
    result = {}
    for key, value in labels.items():
        new_value = LABEL_VALUE_PREFIX + value
        require(len(new_value) <= 63, 'Label terlalu panjang setelah prefix: ' + key)
        result[key] = new_value
    return result


def selected_sources(config):
    return ({('secret', n) for n in config.get('secrets', [])} |
            {('configmap', n) for n in config.get('configmap', [])})


def ref_source(ref):
    if 'secretKeyRef' in ref:
        return 'secret', ref['secretKeyRef']
    if 'configMapKeyRef' in ref:
        return 'configmap', ref['configMapKeyRef']
    return None, None


def secret_env(var, key, destination, optional=False):
    ref = dict(name=destination, key=key)
    if optional:
        ref['optional'] = True
    return dict(name=var, valueFrom=dict(secretKeyRef=ref))


def transform_container(container, config, sources, destination, merged):
    selected = selected_sources(config)
    chosen_env = set(config.get('env', []))
    original_env = container.get('env', [])
    require(chosen_env <= {v['name'] for v in original_env},
            'Env pilihan tidak ditemukan pada container ' + container['name'])
    for entry in original_env:
        if entry['name'] in config.get('useexisting', {}).get('env', []):
            continue
        if entry['name'] in chosen_env:
            # Dynamic fieldRef/resourceFieldRef cannot be frozen into a shared static value.
            require('value' in entry or 'valueFrom' not in entry,
                    'Env pilihan harus literal; valueFrom dipindah melalui daftar Secret/ConfigMap: ' + entry['name'])
            value = entry.get('value', '')
            require('$(' not in value, 'Env dengan ekspansi runtime belum didukung: ' + entry['name'])
            merged[entry['name']] = value.encode('utf-8')

    # Preserve envFrom usage and its prefix. All migrated references share one Secret.
    # Consequently envFrom exposes the merged key set, as documented in README.
    all_from = container.get('envFrom', [])
    rewritten_from = []
    for entry in all_from:
        kind = 'secret' if 'secretRef' in entry else 'configmap'
        ref = entry.get('secretRef', entry.get('configMapRef', {}))
        identity = (kind, ref.get('name'))
        if identity not in selected:
            rewritten_from.append(entry)
            continue
        converted = copy.deepcopy(entry)
        converted.pop('configMapRef', None)
        converted['secretRef'] = dict(ref, name=destination_for(destination, identity))
        rewritten_from.append(converted)
    rewritten = []
    for entry in original_env:
        if entry['name'] in config.get('useexisting', {}).get('env', []):
            rewritten.append(entry)
        elif entry['name'] in chosen_env:
            rewritten.append(secret_env(entry['name'], entry['name'], destination_for(destination, None)))
        else:
            kind, ref = ref_source(entry.get('valueFrom', {}))
            if ref and (kind, ref['name']) in selected:
                rewritten.append(secret_env(entry['name'], ref['key'], destination_for(destination, (kind, ref['name'])), ref.get('optional', False)))
            else:
                rewritten.append(entry)
    if original_env:
        container['env'] = rewritten
    if all_from:
        container['envFrom'] = rewritten_from


def rewrite_volume_source(source, kind, sources, selected, destination, projected=False):
    name_key = 'name' if kind == 'configmap' or projected else 'secretName'
    identity = (kind, source[name_key])
    if identity not in selected:
        return None
    result = copy.deepcopy(source)
    result.pop(name_key)
    result['name' if projected else 'secretName'] = destination_for(destination, identity)
    if 'items' not in result:
        keys = sorted(sources[identity])
        require(keys, 'Volume sumber kosong tidak dapat dipetakan ke Secret gabungan')
        result['items'] = [dict(key=k, path=k) for k in keys]
    return result


def deployment(source, item, sources, destination, merged):
    template = copy.deepcopy(source['spec']['template'])
    meta = template.setdefault('metadata', {})
    if item['clone']:
        meta.pop('creationTimestamp', None)
        meta['labels'] = copy.deepcopy(item['labels']) or prefixed_labels(meta.get('labels', {}))
        require(meta['labels'], 'Pod sumber harus mempunyai label untuk selector Deployment baru')
        for key in ('name', 'namespace', 'uid', 'resourceVersion', 'ownerReferences', 'managedFields', 'generateName'):
            meta.pop(key, None)
    pod = template['spec']
    configs = {c['name']: c for c in item['containers']}
    all_containers = pod.get('containers', []) + pod.get('initContainers', [])
    require(set(configs) <= {c['name'] for c in all_containers}, 'Container pilihan tidak ditemukan')
    selected = set()
    for container in all_containers:
        if container['name'] in configs:
            conf = configs[container['name']]
            selected |= selected_sources(conf)
            transform_container(container, conf, sources, destination, merged)
    # Volumes are pod-scoped. Only rewrite if every mounting container selected the source.
    for volume in pod.get('volumes', []):
        identities = []
        for field, kind in [('secret', 'secret'), ('configMap', 'configmap')]:
            if field in volume:
                ref = volume[field]
                identities.append((kind, ref.get('secretName', ref.get('name'))))
        for projection in volume.get('projected', {}).get('sources', []):
            for field, kind in [('secret', 'secret'), ('configMap', 'configmap')]:
                if field in projection:
                    identities.append((kind, projection[field]['name']))
        for identity in set(identities) & selected:
            consumers = [c for c in all_containers if any(m['name'] == volume['name'] for m in c.get('volumeMounts', []))]
            require(all(identity in selected_sources(configs.get(c['name'], {})) for c in consumers),
                    'Volume bersama harus dipilih pada seluruh container pemakai: ' + volume['name'])
        if 'configMap' in volume:
            changed = rewrite_volume_source(volume['configMap'], 'configmap', sources, selected, destination)
            if changed is not None:
                del volume['configMap']
                volume['secret'] = changed
        elif 'secret' in volume:
            changed = rewrite_volume_source(volume['secret'], 'secret', sources, selected, destination)
            if changed is not None:
                volume['secret'] = changed
        for projection in volume.get('projected', {}).get('sources', []):
            for field, kind in [('configMap', 'configmap'), ('secret', 'secret')]:
                if field in projection:
                    changed = rewrite_volume_source(projection[field], kind, sources, selected, destination, True)
                    if changed is not None:
                        del projection[field]
                        projection['secret'] = changed
                    break
    if not item['clone']:
        # Caller derives a narrow JSON Patch; never apply/replace the full source object.
        result = copy.deepcopy(source)
        result['spec']['template'] = template
        return result
    spec = dict(replicas=1, selector=dict(matchLabels=copy.deepcopy(meta['labels'])), template=template)
    for field in ('minReadySeconds', 'revisionHistoryLimit', 'progressDeadlineSeconds'):
        if field in source['spec']:
            spec[field] = source['spec'][field]
    strategy = source['spec'].get('strategy', {})
    if item['kind'] == 'deployment':
        spec = copy.deepcopy(source['spec'])
        spec.update(replicas=1, selector=dict(matchLabels=copy.deepcopy(meta['labels'])), template=template)
        if strategy:
            spec['strategy'] = copy.deepcopy(strategy)
    else:
        strategy_type = strategy.get('type', 'Rolling')
        require(strategy_type in ('Rolling', 'Recreate'), 'Strategi DC Custom belum didukung')
        for param in ('rollingParams', 'recreateParams'):
            require(not any(strategy.get(param, {}).get(h) for h in ('pre', 'mid', 'post')),
                    'DC lifecycle hook perlu konversi manual')
        spec['strategy'] = dict(type='Recreate' if strategy_type == 'Recreate' else 'RollingUpdate')
        if strategy_type == 'Rolling':
            rolling = {k: v for k, v in strategy.get('rollingParams', {}).items()
                       if k in ('maxSurge', 'maxUnavailable')}
            if rolling:
                spec['strategy']['rollingUpdate'] = rolling
        # DC triggers are translated to the Deployment annotation below.
    require(all(c.get('image') for c in all_containers), 'Image container sumber belum terisi')
    annotations = copy.deepcopy(source['metadata'].get('annotations', {}))
    for key in list(annotations):
        if key in ('kubectl.kubernetes.io/last-applied-configuration', 'image.openshift.io/triggers') or key.startswith(('deployment.kubernetes.io/', 'openshift.io/deployment')):
            del annotations[key]
    if item['annotations']:
        annotations = copy.deepcopy(item['annotations'])
    if item['kind'] == 'deploymentconfig':
        triggers = image_triggers(source)
        if triggers:
            annotations['image.openshift.io/triggers'] = json.dumps(triggers, separators=(',', ':'))
    return dict(apiVersion='apps/v1', kind='Deployment', metadata=dict(
        name=item['target'], namespace=item['namespace'],
        labels=copy.deepcopy(item['labels']) or prefixed_labels(source['metadata'].get('labels', {})), annotations=annotations), spec=spec)


def image_triggers(source):
    result = []
    pod = source['spec']['template']['spec']
    containers = {c['name']: group for group in ('containers', 'initContainers') for c in pod.get(group, [])}
    for trigger in source['spec'].get('triggers', []):
        if trigger.get('type') != 'ImageChange':
            continue
        params = trigger['imageChangeParams']
        ref = copy.deepcopy(params['from'])
        require(ref.get('kind') == 'ImageStreamTag', 'DC image trigger bukan ImageStreamTag')
        ref.setdefault('namespace', source['metadata']['namespace'])
        for container in params.get('containerNames', []):
            require(container in containers, 'Container image trigger tidak ditemukan: ' + container)
            field_path = 'spec.template.spec.' + containers[container] + '[?(@.name=="' + container + '")].image'
            result.append(dict(from_=ref, fieldPath=field_path, paused=not params.get('automatic', False)))
            result[-1]['from'] = result[-1].pop('from_')
    return result


def configuration_patch(source, result):
    """Only configuration fields may change; tests reject stale or replaced sources."""
    meta = source['metadata']
    require(meta.get('uid') and meta.get('resourceVersion'), 'Source uid/resourceVersion wajib untuk patch')
    patch = [dict(op='test', path='/metadata/uid', value=meta['uid']),
             dict(op='test', path='/metadata/resourceVersion', value=meta['resourceVersion'])]
    old = source['spec']['template']['spec']
    new = result['spec']['template']['spec']
    for group in ('containers', 'initContainers'):
        for index, before in enumerate(old.get(group, [])):
            after = new[group][index]
            for field in ('env', 'envFrom'):
                if before.get(field) != after.get(field):
                    path = '/spec/template/spec/{}/{}/{}'.format(group, index, field)
                    patch.append(dict(op='replace' if field in before else 'add', path=path, value=after[field]))
    if old.get('volumes') != new.get('volumes'):
        patch.append(dict(op='replace', path='/spec/template/spec/volumes', value=new['volumes']))
    return patch


def test_service(item, clone):
    if not item['testsvc']:
        return None
    ports = {}
    for container in clone['spec']['template']['spec'].get('containers', []):
        for p in container.get('ports', []):
            port, protocol = p.get('containerPort'), p.get('protocol', 'TCP')
            require(isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535,
                    'containerPort tidak valid')
            ports[(port, protocol)] = dict(name='port-{}-{}'.format(port, protocol.lower()),
                                           port=port, targetPort=port, protocol=protocol)
    if not ports:
        print('SKIP SERVICE {}/{}: containerPort tidak tersedia; migrasi/clone tetap lanjut'.format(item['namespace'], item['name']))
        return None
    service_name = item['target'] + NAMED_SERVICE_SUFFIX if item['customName'] else item['name'] + TEST_SERVICE_SUFFIX
    require(re.fullmatch(r'[a-z]([-a-z0-9]*[a-z0-9])?', service_name) and len(service_name) <= 63,
            'Nama Service harus DNS label maksimum 63 karakter: ' + service_name)
    return dict(apiVersion='v1', kind='Service', metadata=dict(name=service_name, namespace=item['namespace']),
                spec=dict(type='NodePort', selector=copy.deepcopy(clone['spec']['selector']['matchLabels']),
                          ports=list(ports.values())))


def manifest(kind, ns, resource_name, spec):
    return dict(apiVersion=API, kind=kind, metadata=dict(name=name(resource_name), namespace=ns), spec=spec)




















def destination_for(mapping, identity):
    return mapping[identity] if isinstance(mapping, dict) else mapping

# V3 orchestration: a work directory and persistent receipts per workload in this build.
RUN_MARKER = 'migration.local/run'
MANAGER = 'ocpmigrate-v3'


def location(index, suffix):
    return WORK / '{}-{}.json'.format(index, suffix)


def item_at(index):
    return read(WORK / 'items.json')[index]


def record_at(index):
    return read(location(index, 'record'))


def init():
    config = read(WORK / 'config.json')
    for key in ('ocp', 'vaultaddr', 'vaultcred'):
        require(isinstance(config.get(key), str) and config[key] and not re.search(r'\s', config[key]),
                'env.yaml: ' + key + ' wajib string tanpa whitespace')
    require(re.fullmatch(r'https?://[^\s]+', config['vaultaddr']), 'Alamat Vault harus HTTP/HTTPS')
    items = entries(read(WORK / 'input.json'))
    targets, service_names = set(), set()
    source_names = {(i['namespace'], i['name']) for i in items if i['kind'] == 'deployment'}
    for index, item in enumerate(items):
        item['index'] = index
        for field in ('sharedsecret', 'sharedconfigmap'):
            require(isinstance(item[field], list), field + ' harus array nama resource')
            item[field] = sorted(set(name(v) for v in item[field]))
        identity = (item['namespace'], item['target'])
        require(identity not in targets, 'Nama Deployment tujuan duplikat')
        require(not item['clone'] or identity not in source_names, 'Nama clone sama dengan Deployment sumber')
        targets.add(identity)
        if item['testsvc']:
            service = item['target'] + NAMED_SERVICE_SUFFIX if item['customName'] else item['name'] + TEST_SERVICE_SUFFIX
            require((item['namespace'], service) not in service_names, 'Nama Service tujuan duplikat')
            service_names.add((item['namespace'], service))
    write(WORK / 'items.json', items)
    print('VALID: {} workload; proses dan testing berurutan'.format(len(items)))


def all_selected(item):
    return sorted(set().union(*(selected_sources(c) for c in item['containers'])))


def shared_path(kind, resource):
    return 'shared/{}/{}'.format(kind, resource)


def is_shared(item, identity):
    kind, resource = identity
    return resource in item['sharedsecret' if kind == 'secret' else 'sharedconfigmap']


def source_requests(index):
    item = item_at(index)
    source = read(location(index, 'source'))
    pvc = [v['name'] for v in source['spec']['template']['spec'].get('volumes', [])
           if 'persistentVolumeClaim' in v or 'volumeClaimTemplate' in v.get('ephemeral', {})]
    reason = 'PVC pada volume: ' + ', '.join(pvc) if pvc else ''
    write(location(index, 'eligibility'), dict(skip=bool(pvc), reason=reason))
    if pvc:
        print('SKIP {}/{}: {}'.format(item['namespace'], item['name'], reason))


class VaultError(MigrationError):
    def __init__(self, method, path, status):
        self.status = status
        super().__init__('Vault {} {}: HTTP {}; periksa endpoint/izin/koneksi'.format(method, path, status))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(method, path, payload=None, missing=False):
    """No values/credentials in errors; distinguish 404 from 403 and transport failures."""
    addr = os.environ.get('VAULT_ADDR') or read(WORK / 'config.json')['vaultaddr']
    token = os.environ.get('VAULT_TOKEN')
    require(token, 'VAULT_TOKEN tidak tersedia dalam credential binding')
    headers = {'X-Vault-Token': token, 'Content-Type': 'application/json'}
    if os.environ.get('VAULT_NAMESPACE'):
        headers['X-Vault-Namespace'] = os.environ['VAULT_NAMESPACE']
    context = ssl.create_default_context(cafile=os.environ.get('VAULT_CACERT') or None)
    if os.environ.get('VAULT_SKIP_VERIFY', '').lower() in ('true', '1'):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    encoded_path = urllib.parse.quote(path, safe='/')
    request = urllib.request.Request(addr.rstrip('/') + '/v1/' + encoded_path,
        data=json.dumps(payload).encode() if payload is not None else None, headers=headers, method=method)
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if missing and exc.code == 404:
            return None
        raise VaultError(method, path, exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise MigrationError('Koneksi Vault gagal pada {} {}; hasil operasi mungkin belum pasti'.format(method, path)) from None


def owner_for(item, identity):
    return dict(manager=MANAGER, source_namespace=item['namespace'], source_kind=identity[0], source_name=identity[1])


def kv_state(mount, path):
    meta = api('GET', mount + '/metadata/' + path, missing=True)
    if meta is None:
        return None
    metadata = meta['data']
    version = int(metadata.get('current_version', 0))
    if version == 0:
        return dict(metadata=metadata, version=0, values=None)
    info = metadata.get('versions', {}).get(str(version), {})
    require(not info.get('destroyed') and not info.get('deletion_time'),
            'Versi aktif KV terhapus/destroyed: ' + mount + '/' + path)
    data = api('GET', mount + '/data/' + path)
    values = data['data']['data']
    require(isinstance(values, dict) and all(isinstance(v, str) for v in values.values()),
            'Nilai KV harus string untuk sinkronisasi byte-exact: ' + path)
    return dict(metadata=metadata, version=int(data['data']['metadata']['version']), values=values)


def inspect_shared(index):
    item = item_at(index)
    mount = item['namespace'] + '-kv'
    mounts = api('GET', 'sys/mounts')['data']
    existing = mounts.get(mount + '/')
    require(not existing or (existing['type'] == 'kv' and str(existing.get('options', {}).get('version')) == '2'),
            'Mount existing bukan KV v2: ' + mount)
    sources = []
    for kind, resource in all_selected(item):
        identity = (kind, resource)
        shared = is_shared(item, identity)
        path = shared_path(kind, resource)
        state = kv_state(mount, path) if existing else None
        if state is not None:
            require(shared, 'Sumber sudah mempunyai path shared tetapi field shared input tidak mencantumkan: ' + path)
            wanted = owner_for(item, identity)
            actual = state['metadata'].get('custom_metadata') or {}
            require(all(actual.get(k) == v for k, v in wanted.items()),
                    'Metadata pemilik path shared tidak cocok/belum ada: ' + path)
        ready = bool(state and state['values'] is not None)
        values = state['values'] if ready else None
        entry = dict(kind=kind, name=resource, shared=shared, path=path, existing=ready, values=values,
                     version=state['version'] if state else 0, owner=owner_for(item, identity),
                     snapshot=str(location(index, 'input-' + str(len(sources)))))
        sources.append(entry)
        if ready:
            print('REUSE VAULT {}/{}; sumber OCP tidak dibaca/ditimpa'.format(mount, path))
    write(location(index, 'inputs'), sources)


def generated_name(prefix, ns, source, kind='', resource=''):
    raw = '-'.join(v for v in (prefix, ns, source, kind, resource) if v)
    if len(raw) > 253:
        raw = raw[:235].rstrip('-.') + '-' + hashlib.sha256(raw.encode()).hexdigest()[:16]
    return name(raw)


def mark_manifest(doc, index):
    run = read(WORK / 'run.json')['id']
    doc.setdefault('metadata', {}).setdefault('annotations', {})[RUN_MARKER] = run + '/' + str(index)
    return doc


def prepare(index):
    item, source = item_at(index), read(location(index, 'source'))
    inputs = read(location(index, 'inputs'))
    ns, service, mount = item['namespace'], item['name'], item['namespace'] + '-kv'
    name('holder-secret-' + service)
    name('vaultauth-' + service)
    name('vault-connection-' + ns)
    sources, private, all_keys, review_rows = {}, {}, {}, []
    destinations = {None: generated_name('vaultsecret', ns, service)}
    datasets = []
    for entry in inputs:
        identity = (entry['kind'], entry['name'])
        values = ({k: v.encode('utf-8') for k, v in entry['values'].items()} if entry['existing']
                  else resource_data(read(entry['snapshot'])))
        sources[identity] = values
        for key in values:
            require(key not in all_keys, 'KEY duplikat dalam konfigurasi workload (shared/pribadi): ' + key)
            all_keys[key] = '/'.join(identity)
        review_rows.append(dict(source='/'.join(identity), keys=sorted(values), shared=entry['shared'], existing=entry['existing']))
        if entry['shared']:
            destination = generated_name('vaultsecret', ns, service, entry['kind'], entry['name'])
            destinations[identity] = destination
            datasets.append(dict(path=entry['path'], values=values, shared=True, destination=destination,
                                 existing=entry['existing'], owner=entry['owner'], source=entry['name'], kind=entry['kind']))
        else:
            private.update(values)
            destinations[identity] = destinations[None]
    containers = {c['name']: c for group in ('containers', 'initContainers')
                  for c in source['spec']['template']['spec'].get(group, [])}
    for c in item['containers']:
        require(c['name'] in containers, 'Container tidak ditemukan: ' + c['name'])
        envs = containers[c['name']].get('env', [])
        chosen = set(c.get('env', []))
        require(chosen <= {e['name'] for e in envs}, 'Env pilihan tidak ditemukan: ' + c['name'])
        for e in envs:
            if e['name'] in chosen:
                require('valueFrom' not in e and '$(' not in e.get('value', ''), 'Env harus literal tanpa ekspansi: ' + e['name'])
                require(e['name'] not in all_keys, 'KEY duplikat env antar-container/sumber: ' + e['name'])
                all_keys[e['name']] = 'env/' + c['name']
                private[e['name']] = e.get('value', '').encode('utf-8')
        if chosen:
            review_rows.append(dict(source='env/' + c['name'], keys=sorted(chosen), shared=False, existing=False))
    if private:
        datasets.insert(0, dict(path=service, values=private, shared=False, destination=destinations[None], existing=False))
    # Transform refs per source; shared data is never merged into the private path.
    clone = deployment(source, item, sources, destinations, dict(private))
    svc = test_service(item, clone)
    record = dict(index=index, namespace=ns, source=service, sourceKind=item['kind'], clone=item['clone'],
                  target=item['target'], mount=mount, authMount=ns + '-approle', policy=ns + '-' + service + '-access',
                  keyCount=len(all_keys), review=review_rows, datasets=[], operations=[],
                  deploymentFile=str(location(index, 'deployment')), patchFile=str(location(index, 'patch')),
                  serviceFile=str(location(index, 'service')), serviceName=svc['metadata']['name'] if svc else '',
                  sourceFile=str(location(index, 'source')), currentFile=str(location(index, 'current')),
                  authFile=str(location(index, 'auth')), holderFile=str(location(index, 'holder')),
                  connectionFile=str(location(index, 'connection')), ocpResources=[])
    protected = set()
    for group in read(WORK / 'input.json')['data']:
        for field in ('deploymentconfigs', 'deployments'):
            for w in group.get(field, []):
                for c in w['containers']:
                    protected.update((group['namespace'], n) for n in c.get('secrets', []) + c.get('useexisting', {}).get('secrets', []))
    for pos, dataset in enumerate(datasets):
        require((ns, dataset['destination']) not in protected, 'Secret tujuan bertabrakan dengan sumber')
        try:
            payload = {k: v.decode('utf-8') for k, v in dataset.pop('values').items()}
        except UnicodeDecodeError:
            raise MigrationError('Nilai biner non-UTF8 belum didukung')
        dataset.update(index=pos, payloadFile=str(location(index, 'payload-' + str(pos))),
                       expectedFile=str(location(index, 'expected-' + str(pos))),
                       actualFile=str(location(index, 'actual-' + str(pos))),
                       vssFile=str(location(index, 'vss-' + str(pos))))
        write(dataset['payloadFile'], payload)
        write(dataset['expectedFile'], payload)
        vssname = generated_name('vaultstaticsecret', ns, service,
                                dataset.get('kind', ''), dataset.get('source', ''))
        vss = manifest('VaultStaticSecret', ns, vssname,
            dict(vaultAuthRef='vaultauth-' + service, mount=mount, type='kv-v2', path=dataset['path'], refreshAfter='5s',
                 destination=dict(create=True, name=dataset['destination'], transformation=dict(excludeRaw=True))))
        write(dataset['vssFile'], mark_manifest(vss, index))
        record['datasets'].append(dataset)
    record['migrate'] = bool(datasets)
    if datasets:
        require((ns, 'holder-secret-' + service) not in protected, 'Secret holder bertabrakan dengan sumber')
        record['operations'] = [dict(type='auth-mount', label='Auth mount ' + record['authMount']),
                                dict(type='kv-mount', label='KV mount ' + mount)]
        record['operations'] += [dict(type='data', dataset=i, label='KV ' + mount + '/' + d['path']) for i, d in enumerate(datasets)]
        record['operations'] += [dict(type='policy', label='Policy ' + record['policy']),
                                 dict(type='role', label='AppRole ' + service),
                                 dict(type='credential', label='Credential AppRole ' + service)]
        connection = manifest('VaultConnection', ns, 'vault-connection-' + ns,
                              dict(address=read(WORK / 'config.json')['vaultaddr'], skipTLSVerify=True))
        write(record['connectionFile'], mark_manifest(connection, index))
        record['ocpResources'] = [dict(kind='vaultconnection', name='vault-connection-' + ns, file=record['connectionFile']),
            dict(kind='secret', name='holder-secret-' + service, file=record['holderFile']),
            dict(kind='vaultauth', name='vaultauth-' + service, file=record['authFile'])]
        record['ocpResources'] += [dict(kind='vaultstaticsecret', name=read(d['vssFile'])['metadata']['name'], file=d['vssFile']) for d in datasets]
    if item['clone']:
        write(record['deploymentFile'], mark_manifest(clone, index))
    else:
        write(record['deploymentFile'], clone)
        write(record['patchFile'], configuration_patch(source, clone))
    if svc:
        write(record['serviceFile'], mark_manifest(svc, index))
    write(location(index, 'record'), record)
    print('READY {}/{}: {} private/shared paths; {} key, tanpa duplikasi'.format(ns, service, len(datasets), len(all_keys)))


def review(index):
    r = record_at(index)
    print('REVIEW {}/{} -> {} ({}); {} key, tidak ada duplikasi'.format(
        r['namespace'], r['source'], r['target'], 'clone' if r['clone'] else 'in-place', r['keyCount']))
    for row in r['review']:
        print('  {} [{}]: {}'.format(row['source'], 'shared Vault existing' if row['existing'] else
              ('shared baru' if row['shared'] else 'pribadi'), ', '.join(row['keys'])))
    for d in r['datasets']:
        print('  PATH {}/{} -> Secret {}'.format(r['mount'], d['path'], d['destination']))


def receipt(index, key, status=None, detail=None):
    file = location(index, 'receipts')
    state = read(file) if file.exists() else {}
    if status:
        state[key] = dict(status=status, detail=detail or key)
        write(file, state)
        print('{}: {}'.format(status.upper(), detail or key))
    return state.get(key, {})


def report(index):
    r = item_at(index)
    print('RESOURCE REPORT {}/{} (tidak ada rollback otomatis)'.format(r['namespace'], r['name']))
    file = location(index, 'receipts')
    if not file.exists():
        print('  Belum ada operasi resource yang dicatat')
        return
    for key, value in read(file).items():
        print('  {}: {}'.format(value['status'].upper(), value['detail']))
    print('  PENDING berarti hasil belum terkonfirmasi; periksa resource sebelum tindakan manual.')


def policy_text(record):
    return '\n'.join('path "' + record['mount'] + '/data/' + d['path'] + '" { capabilities = ["read"] }'
                     for d in record['datasets']) + '\n'


def vault_action(index, operation):
    r = record_at(index)
    op = r['operations'][operation]
    key = 'vault-' + str(operation)
    if receipt(index, key).get('status') == 'done':
        print('ALREADY DONE: ' + op['label'])
        return
    receipt(index, key, 'pending', op['label'])
    kind = op['type']
    role = 'auth/' + r['authMount'] + '/role/' + r['source']
    if kind in ('auth-mount', 'kv-mount'):
        auth = kind == 'auth-mount'
        endpoint = 'sys/auth' if auth else 'sys/mounts'
        mount = r['authMount'] if auth else r['mount']
        current = api('GET', endpoint)['data'].get(mount + '/')
        if current is None:
            api('POST', endpoint + '/' + mount, dict(type='approle') if auth else dict(type='kv', options=dict(version='2')))
            current = api('GET', endpoint)['data'].get(mount + '/')
        require(current and current['type'] == ('approle' if auth else 'kv'), 'Tipe mount tidak sesuai')
        require(auth or str(current.get('options', {}).get('version')) == '2', 'Mount harus KV v2')
    elif kind == 'data':
        d = r['datasets'][op['dataset']]
        expected = read(d['payloadFile'])
        current = kv_state(r['mount'], d['path'])
        if d['shared']:
            if current is not None:
                actual_owner = current['metadata'].get('custom_metadata') or {}
                require(all(actual_owner.get(k) == v for k, v in d['owner'].items()), 'Pemilik shared path tidak cocok: ' + d['path'])
            else:
                api('POST', r['mount'] + '/metadata/' + d['path'], dict(custom_metadata=d['owner']))
                current = kv_state(r['mount'], d['path'])
            if current['values'] is None:
                require(not d['existing'], 'Data shared yang direview hilang; ulangi workload dengan snapshot baru')
                api('POST', r['mount'] + '/data/' + d['path'], dict(options=dict(cas=0), data=expected))
                current = kv_state(r['mount'], d['path'])
            # Never overwrite existing shared data, including a concurrent creation/rotation.
            require(current['values'] == expected, 'Data shared berubah sejak review; skip dan ulangi workload untuk review data terbaru')
        else:
            intent_file = location(index, 'intent-' + str(operation))
            if not intent_file.exists():
                write(intent_file, dict(cas=current['version'] if current else 0))
            intent = read(intent_file)
            if current is None or current['values'] != expected:
                require((current['version'] if current else 0) == intent['cas'], 'KV pribadi berubah selama retry; tidak ditimpa')
                api('POST', r['mount'] + '/data/' + d['path'], dict(options=dict(cas=intent['cas']), data=expected))
                current = kv_state(r['mount'], d['path'])
            require(current['values'] == expected, 'Read-back KV pribadi tidak cocok')
        write(d['expectedFile'], current['values'])
    elif kind == 'policy':
        path = 'sys/policies/acl/' + r['policy']
        expected = policy_text(r)
        current = api('GET', path, missing=True)
        if not current or current['data']['policy'].strip() != expected.strip():
            api('PUT', path, dict(policy=expected))
        require(api('GET', path)['data']['policy'].strip() == expected.strip(), 'Read-back policy tidak cocok')
    elif kind == 'role':
        current = api('GET', role, missing=True)
        if not current or set(current['data'].get('token_policies', [])) != {r['policy']}:
            api('POST', role, dict(token_policies=[r['policy']]))
        require(r['policy'] in api('GET', role)['data']['token_policies'], 'Policy belum terpasang pada AppRole')
    elif kind == 'credential':
        credential_file = location(index, 'credential')
        if not credential_file.exists():
            write(credential_file, dict(secret_id=secrets.token_urlsafe(32)))
        credential = read(credential_file)
        secret_id = credential['secret_id']
        # Deterministic per-build custom SecretID permits recovery after a lost response.
        try:
            found = api('POST', role + '/secret-id/lookup', dict(secret_id=secret_id))
        except VaultError as exc:
            if exc.status not in (400, 404):
                raise
            found = None
        if not found:
            api('POST', role + '/custom-secret-id', dict(secret_id=secret_id))
        api('POST', role + '/secret-id/lookup', dict(secret_id=secret_id))
        role_id = api('GET', role + '/role-id')['data']['role_id']
        holder = dict(apiVersion='v1', kind='Secret', metadata=dict(name='holder-secret-' + r['source'], namespace=r['namespace']),
                      type='Opaque', stringData=dict(id=secret_id))
        auth = manifest('VaultAuth', r['namespace'], 'vaultauth-' + r['source'],
                        dict(vaultConnectionRef='vault-connection-' + r['namespace'], method='appRole', mount=r['authMount'],
                             appRole=dict(roleId=role_id, secretRef='holder-secret-' + r['source'])))
        write(r['holderFile'], mark_manifest(holder, index))
        write(r['authFile'], mark_manifest(auth, index))
    receipt(index, key, 'done', op['label'] + ' [dibuat/diselaraskan atau existing terverifikasi]')


def verify(index, dataset):
    d = record_at(index)['datasets'][dataset]
    expected = {k: v.encode('utf-8') for k, v in read(d['expectedFile']).items()}
    actual = resource_data(dict(read(d['actualFile']), kind='Secret'))
    missing, extra = sorted(expected.keys() - actual.keys()), sorted(actual.keys() - expected.keys())
    changed = sorted(k for k in expected.keys() & actual.keys() if expected[k] != actual[k])
    if missing or extra or changed:
        print('WAIT {}: missing={}, extra={}, different={}'.format(d['destination'], missing, extra, changed))
        return 1
    print('VERIFIED {}: {} key/value cocok'.format(d['destination'], len(expected)))
    return 0


def contains(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and contains(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(contains(a, b) for a, b in zip(actual, expected))
    return actual == expected


def create_check(index, what):
    r = record_at(index)
    wanted = read(r['deploymentFile' if what == 'deployment' else 'serviceFile'])
    current = read(r['currentFile'])
    noop = False
    if current:
        require(current['metadata'].get('annotations', {}).get(RUN_MARKER) == wanted['metadata']['annotations'][RUN_MARKER],
                'Nama tujuan sudah ada dan bukan hasil create run ini: ' + wanted['metadata']['name'])
        require(contains(current['spec'], wanted['spec']), 'Resource hasil create berbeda dari rencana; perlu diperiksa')
        noop = True
    write(location(index, 'decision'), dict(noop=noop))


def patch_refresh(index):
    r = record_at(index)
    original, desired, current = read(r['sourceFile']), read(r['deploymentFile']), read(r['currentFile'])
    require(current and current['metadata']['uid'] == original['metadata']['uid'], 'Deployment sumber diganti/hilang')
    operations = configuration_patch(original, desired)[2:]
    result = copy.deepcopy(current)
    for op in operations:
        keys = op['path'].strip('/').split('/')
        def get(root):
            value = root
            for k in keys:
                if isinstance(value, list):
                    value = value[int(k)]
                elif k in value:
                    value = value[k]
                else:
                    return None
            return value
        # Container order/name must be stable for index-based JSON Patch.
        if 'containers' in keys or 'initContainers' in keys:
            group, pos = keys[3], int(keys[4])
            require(current['spec']['template']['spec'][group][pos]['name'] == original['spec']['template']['spec'][group][pos]['name'],
                    'Urutan/nama container berubah')
        existing, before = get(current), get(original)
        require(existing == before or existing == op['value'], 'Konfigurasi sumber berubah sejak review: ' + op['path'])
        parent = result
        for k in keys[:-1]:
            parent = parent[int(k)] if isinstance(parent, list) else parent[k]
        parent[keys[-1]] = op['value']
    patch = configuration_patch(current, result)
    write(r['patchFile'], patch)
    write(location(index, 'decision'), dict(noop=len(patch) == 2))


def service_report(index):
    svc = read(record_at(index)['currentFile'])
    for p in svc['spec']['ports']:
        print('NODEPORT {}/{}: {} -> {} / {} nodePort={}'.format(svc['metadata']['namespace'], svc['metadata']['name'],
            p['port'], p['targetPort'], p.get('protocol', 'TCP'), p['nodePort']))


def main():
    os.umask(0o077)
    command = sys.argv[1]
    if command == 'init':
        init()
        return 0
    index = int(sys.argv[2])
    if command == 'vault-action':
        vault_action(index, int(sys.argv[3]))
    elif command == 'verify':
        return verify(index, int(sys.argv[3]))
    elif command == 'receipt':
        receipt(index, sys.argv[3], sys.argv[4], sys.argv[5])
    elif command == 'create-check':
        create_check(index, sys.argv[3])
    else:
        {'source-requests': source_requests, 'inspect': inspect_shared, 'prepare': prepare,
         'review': review, 'report': report, 'patch-refresh': patch_refresh, 'service-report': service_report}[command](index)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except MigrationError as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print('ERROR: {} saat memproses langkah; nilai sensitif tidak ditampilkan'.format(type(exc).__name__), file=sys.stderr)
        sys.exit(1)
