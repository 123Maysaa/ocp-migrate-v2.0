// V3: one workload through manual testing before the next workload starts.
// No global timeout. SYNC_TIMEOUT_SECONDS bounds only VSO synchronization.
def config

def markResult(item, key, status, detail) {
    def path = ".migration3-work/${item.index}-receipts.json"
    def entries = fileExists(path) ? readJSON(file: path, returnPojo: true) : [:]
    entries[key] = [status: status, detail: detail]
    writeJSON(file: path, json: entries)
}

def runStep(item, phase, Closure body) {
    while (true) {
        try {
            stage("${item.namespace}/${item.name}: ${phase}") {
                body()
            }
            def receiptFile = ".migration3-work/${item.index}-receipts.json"
            if (fileExists(receiptFile)) {
                def receipts = readJSON(file: receiptFile, returnPojo: true)
                if (receipts.containsKey("failed-${phase}".toString())) {
                    markResult(item, "failed-${phase}", 'recovered', "Langkah ${phase}: berhasil setelah Retry")
                }
            }
            return true
        } catch (org.jenkinsci.plugins.workflow.steps.FlowInterruptedException interruption) {
            // Honor Jenkins' own Stop/Abort; do not trap cancellation in a retry loop.
            throw interruption
        } catch (Exception failure) {
            echo "FAILED ${item.namespace}/${item.name}: ${phase}. Lihat error langkah di console; tidak ada rollback otomatis."
            markResult(item, "failed-${phase}", 'failed', "Langkah ${phase} gagal; periksa pesan error di console")
            sh "python3 scripts/migrate3.py report ${item.index}"
            def action = input(message: "${item.namespace}/${item.name}: ${phase} gagal. Retry langkah ini atau lanjut workload berikutnya?",
                ok: 'Continue', parameters: [choice(name: 'ACTION', choices: ['Retry', 'Skip / Continue next'], description: 'Resource yang sudah ada tidak dihapus')])
            if (action != 'Retry') {
                currentBuild.result = 'UNSTABLE'
                echo "SKIP ${item.namespace}/${item.name} pada ${phase}"
                sh "python3 scripts/migrate3.py report ${item.index}"
                return false
            }
            echo "RETRY ${item.namespace}/${item.name}: ${phase} saja"
        }
    }
}

def fetchCurrent(record, kind, name) {
    def result = openshift.raw('get', kind, name, '--ignore-not-found', '-o=json', '--request-timeout=30s')
    writeFile(file: record.currentFile, text: result.out.trim() ?: '{}')
}

def applyResource(item, resource) {
    markResult(item, "ocp-${resource.kind}-${resource.name}", 'pending', "OCP ${resource.kind}/${resource.name}: apply belum terkonfirmasi")
    openshift.raw('apply', '-f', resource.file, '-o=name', '--request-timeout=30s')
    markResult(item, "ocp-${resource.kind}-${resource.name}", 'done', "OCP ${resource.kind}/${resource.name}: apply berhasil")
}

def runWorkload(item, config) {
    def index = item.index
    if (!runStep(item, 'Read source') {
        def result = openshift.raw('get', item.kind, item.name, '-o=json', '--request-timeout=30s')
        writeFile(file: ".migration3-work/${index}-source.json", text: result.out)
    }) { return true }
    if (!runStep(item, 'Check PVC') {
        sh "python3 scripts/migrate3.py source-requests ${index}"
    }) { return true }
    def eligibility = readJSON(file: ".migration3-work/${index}-eligibility.json", returnPojo: true)
    if (eligibility.skip) {
        currentBuild.result = 'UNSTABLE'
        echo "SKIP ${item.namespace}/${item.name}: ${eligibility.reason}"
        sh "python3 scripts/migrate3.py report ${index}"
        return true
    }
    if (!runStep(item, 'Inspect shared Vault paths') {
        withCredentials([[$class: 'VaultTokenCredentialBinding', credentialsId: config.vaultcred, vaultAddr: config.vaultaddr]]) {
            sh "python3 scripts/migrate3.py inspect ${index}"
        }
    }) { return true }
    def sources = readJSON(file: ".migration3-work/${index}-inputs.json", returnPojo: true)
    for (source in sources) {
        if (!source.existing) {
            if (!runStep(item, "Read ${source.kind}/${source.name}") {
                def result = openshift.raw('get', source.kind, source.name, '-o=json', '--request-timeout=30s')
                writeFile(file: source.snapshot, text: result.out)
            }) { return true }
        }
    }
    if (!runStep(item, 'Validate keys and prepare manifests') {
        sh "python3 scripts/migrate3.py prepare ${index}"
    }) { return true }
    def record = readJSON(file: ".migration3-work/${index}-record.json", returnPojo: true)
    if (!runStep(item, 'Preflight workload') {
        if (item.clone) {
            fetchCurrent(record, 'deployment', record.target)
            sh "python3 scripts/migrate3.py create-check ${index} deployment"
            openshift.raw('create', '--dry-run=server', '-f', record.deploymentFile, '-o=name', '--request-timeout=30s')
        } else {
            fetchCurrent(record, 'deployment', record.target)
            sh "python3 scripts/migrate3.py patch-refresh ${index}"
            openshift.raw('patch', 'deployment', record.target, '--type=json', '--patch-file', record.patchFile,
                '--dry-run=server', '-o=name', '--request-timeout=30s')
        }
    }) { return true }
    if (record.serviceName) {
        if (!runStep(item, 'Preflight service') {
            fetchCurrent(record, 'service', record.serviceName)
            sh "python3 scripts/migrate3.py create-check ${index} service"
            openshift.raw('create', '--dry-run=server', '-f', record.serviceFile, '-o=name', '--request-timeout=30s')
        }) { return true }
    }
    if (record.migrate) {
        if (!runStep(item, 'Preflight VSO') {
            openshift.raw('apply', '--dry-run=server', '-f', record.connectionFile, '-o=name', '--request-timeout=30s')
            for (dataset in record.datasets) {
                openshift.raw('apply', '--dry-run=server', '-f', dataset.vssFile, '-o=name', '--request-timeout=30s')
            }
            openshift.raw('get', 'vaultauths.secrets.hashicorp.com', '-o=name', '--request-timeout=30s')
        }) { return true }
        sh "python3 scripts/migrate3.py review ${index}"
        input(id: "vault-${index}", ok: 'Continue',
            message: "Provision Vault untuk ${item.kind} ${item.namespace}/${item.name}? Review nama sumber/key di console. Shared existing tidak ditimpa.")
        for (int op = 0; op < record.operations.size(); op++) {
            if (!runStep(item, record.operations[op].label) {
                withCredentials([[$class: 'VaultTokenCredentialBinding', credentialsId: config.vaultcred, vaultAddr: config.vaultaddr]]) {
                    sh "python3 scripts/migrate3.py vault-action ${index} ${op}"
                }
            }) { return true }
        }
        for (resource in record.ocpResources) {
            if (!runStep(item, "Apply ${resource.kind}/${resource.name}") {
                applyResource(item, resource)
            }) { return true }
        }
        for (dataset in record.datasets) {
            if (!runStep(item, "Verify VSO ${dataset.destination}") {
                try {
                    timeout(time: params.SYNC_TIMEOUT_SECONDS.toInteger(), unit: 'SECONDS') {
                        waitUntil(initialRecurrencePeriod: 5000, quiet: true) {
                            def result = openshift.raw('get', 'secret', dataset.destination,
                                '--ignore-not-found', '-o=json', '--request-timeout=30s')
                            writeFile(file: dataset.actualFile, text: result.out.trim() ?: '{}')
                            return sh(script: "python3 scripts/migrate3.py verify ${index} ${dataset.index}", returnStatus: true) == 0
                        }
                    }
                } catch (org.jenkinsci.plugins.workflow.steps.FlowInterruptedException interruption) {
                    if (interruption.isActualInterruption()) { throw interruption }
                    echo "VSO TIMEOUT ${dataset.destination}: belum cocok setelah ${params.SYNC_TIMEOUT_SECONDS} detik"
                    error('VSO synchronization timeout')
                }
                markResult(item, "synced-${dataset.destination}", 'done', "Secret OCP ${dataset.destination}: data terverifikasi")
            }) { return true }
        }
    }
    sh "python3 scripts/migrate3.py review ${index}"
    input(id: "workload-${index}", ok: 'Continue',
        message: item.clone ? "Clone ${item.namespace}/${item.name} menjadi ${record.target}, replica 1? Service testing: ${record.serviceName ?: 'tidak dibuat'}." :
                             "Update konfigurasi Deployment ${item.namespace}/${item.name}? Replica dan metadata dipertahankan; dapat memicu rollout.")
    if (!runStep(item, item.clone ? 'Create clone' : 'Patch existing configuration') {
        fetchCurrent(record, 'deployment', record.target)
        sh "python3 scripts/migrate3.py ${item.clone ? 'create-check' : 'patch-refresh'} ${index}${item.clone ? ' deployment' : ''}"
        def decision = readJSON(file: ".migration3-work/${index}-decision.json", returnPojo: true)
        if (!decision.noop) {
            markResult(item, 'deployment', 'pending', "Deployment ${record.target}: hasil create/patch belum terkonfirmasi")
            if (item.clone) {
                openshift.raw('create', '-f', record.deploymentFile, '-o=name', '--request-timeout=30s')
            } else {
                openshift.raw('patch', 'deployment', record.target, '--type=json', '--patch-file', record.patchFile, '-o=name', '--request-timeout=30s')
            }
        }
        markResult(item, 'deployment', 'done', "Deployment ${record.target}: ${decision.noop ? 'hasil sebelumnya/no-op terverifikasi' : (item.clone ? 'dibuat replica=1' : 'konfigurasi diubah')}")
    }) { return true }
    if (record.serviceName) {
        if (!runStep(item, 'Create NodePort service') {
            fetchCurrent(record, 'service', record.serviceName)
            sh "python3 scripts/migrate3.py create-check ${index} service"
            def decision = readJSON(file: ".migration3-work/${index}-decision.json", returnPojo: true)
            if (!decision.noop) {
                markResult(item, 'service', 'pending', "Service ${record.serviceName}: hasil create belum terkonfirmasi")
                openshift.raw('create', '-f', record.serviceFile, '-o=name', '--request-timeout=30s')
            }
            markResult(item, 'service', 'done', "Service ${record.serviceName}: dibuat/hasil sebelumnya terverifikasi")
        }) { return true }
        if (!runStep(item, 'Report NodePort') {
            fetchCurrent(record, 'service', record.serviceName)
            sh "python3 scripts/migrate3.py service-report ${index}"
        }) { return true }
    }
    sh "python3 scripts/migrate3.py report ${index}"
    def next = input(id: "testing-${index}", ok: 'Continue',
        message: "Silakan testing manual ${item.namespace}/${record.target}. Setelah selesai pilih Continue untuk workload berikutnya/selesai, atau Skip all remaining.",
        parameters: [choice(name: 'ACTION', choices: ['Continue', 'Skip all remaining'], description: 'Testing dilakukan manual; resource tetap tersedia')])
    if (next == 'Skip all remaining') {
        echo "SKIP ALL REMAINING setelah testing ${item.namespace}/${record.target}"
        currentBuild.result = 'UNSTABLE'
        return false
    }
    return true
}

pipeline {
    agent any
    options { disableConcurrentBuilds() }
    parameters {
        string(name: 'ENV_FILE', defaultValue: 'env.yaml', description: 'Konfigurasi OCP/Vault')
        string(name: 'MIGRATE_FILE', defaultValue: 'newmigrate.yaml', description: 'Daftar workload dan shared sources')
        string(name: 'SYNC_TIMEOUT_SECONDS', defaultValue: '120', description: 'Batas verifikasi VSO; approval/testing tanpa timeout')
    }
    stages {
        stage('Validate common configuration') {
            steps {
                script {
                    dir('.migration3-work') { deleteDir() }
                    sh '''#!/bin/sh
                        set -eu
                        set +x
                        command -v python3 >/dev/null
                        command -v oc >/dev/null
                        python3 -c 'import sys; assert sys.version_info >= (3,8), "Python 3.8+ diperlukan"'
                        mkdir -p .migration3-work
                        chmod 700 .migration3-work
                    '''
                    config = readYaml(file: params.ENV_FILE)
                    writeJSON(file: '.migration3-work/config.json', json: config)
                    writeJSON(file: '.migration3-work/input.json', json: readYaml(file: params.MIGRATE_FILE))
                    writeJSON(file: '.migration3-work/run.json', json: [id: env.BUILD_TAG])
                    if (!(params.SYNC_TIMEOUT_SECONDS ==~ /[1-9][0-9]{0,5}/)) { error('SYNC_TIMEOUT_SECONDS harus integer positif') }
                    sh 'python3 scripts/migrate3.py init'
                }
            }
        }
        stage('Migrate workloads sequentially') {
            steps {
                script {
                    def items = readJSON(file: '.migration3-work/items.json', returnPojo: true)
                    boolean proceed = true
                    openshift.withCluster(config.ocp) {
                        openshift.verbose(false)
                        for (item in items) {
                            if (!proceed) {
                                echo "SKIPPED REMAINING ${item.namespace}/${item.name}"
                            } else {
                                openshift.withProject(item.namespace) { proceed = runWorkload(item, config) }
                            }
                        }
                    }
                }
            }
        }
    }
    post {
        always {
            script {
                // Reports contain names/status only, never values or credentials.
                if (fileExists('.migration3-work/items.json')) {
                    def items = readJSON(file: '.migration3-work/items.json', returnPojo: true)
                    for (item in items) {
                        sh(script: "python3 scripts/migrate3.py report ${item.index}", returnStatus: true)
                    }
                }
            }
            dir('.migration3-work') { deleteDir() }
        }
    }
}
