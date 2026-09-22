# Pipeline migrasi versi 3

Script Path Jenkins: **`pipeline3.groovy`**. Helper: **`scripts/migrate3.py`**.
Keduanya file baru; V1/V2 tidak diubah atau di-import runtime V3.
Input default tetap `newmigrate.yaml` dan `env.yaml`.

## Alur satu workload sampai selesai

Validasi umum membaca struktur input dan memeriksa nama target. Setelah itu pipeline
memproses setiap workload secara berurutan, mengikuti urutan namespace, DC, lalu Deployment:

1. Baca workload sumber dan periksa PVC. PVC atau generic ephemeral volumeClaimTemplate
   menyebabkan skip sebelum akses data Vault dan sebelum pembacaan Secret/ConfigMap.
2. Periksa path shared di Vault menggunakan credential Jenkins, tanpa mutation.
3. Baca sumber OCP yang diperlukan. Shared yang sudah ada dibaca dari Vault, bukan OCP.
4. Validasi key, siapkan manifest, server dry-run workload/Service/VSO.
5. Console menampilkan sumber dan nama key **tanpa value**. Tombol **Continue Vault**.
6. Provision auth mount, KV mount, data per path, policy, role, lalu credential. Setiap
   operasi mempunyai langkah Retry sendiri serta pencatatan hasil.
7. Apply VaultConnection, holder, VaultAuth, dan masing-masing VSS satu per satu.
8. Verifikasi tiap Secret tujuan, termasuk byte nilai dan newline.
9. Tombol **Continue workload**, lalu create clone atau patch konfigurasi existing.
10. Buat Service NodePort jika diminta dan port container tersedia; log NodePort.
11. Tunggu pengujian manual. **Continue** melanjutkan workload berikutnya (atau selesai
    jika terakhir); **Skip all remaining** melewati sisanya.
12. Laporan resource di console dan cleanup setelah seluruh proses selesai.

Workload B tidak dibaca/provision sebelum pengujian A selesai, kecuali A di-skip.
Tidak ada timeout global atau batas waktu approval/testing manual. Agent tetap dialokasikan
selama menunggu. Default **SYNC_TIMEOUT_SECONDS=120** berlaku per Secret hasil VSO/per
percobaan verifikasi. Bisa diubah pada Build with Parameters; default berada pada deklarasi
parameter di `pipeline3.groovy`. Request API/oc individual dibatasi 30 detik agar error
koneksi dapat masuk ke pilihan Retry/Skip.

## Shared dan data pribadi

```yaml
data:
  - namespace: task-api-a
    sharedsecret:
      - thirdpartyconn
    sharedconfigmap:
      - httptemplate
    # deploymentconfigs/deployments dan containers seperti input V2
```

Daftar sources pada container menentukan apa yang dimigrasikan. Daftar shared per
namespace hanya mengklasifikasikan sources yang dipilih; tidak membuat resource yang
tidak dipakai. `useexisting` diprioritaskan. Resource yang dikecualikan pada salah satu
container tetap lokal untuk seluruh workload, sama seperti V2; pengecualian env per container.

| Jenis                                   | Path dalam mount `<namespace>-kv` |
| --------------------------------------- | --------------------------------- |
| Shared Secret                           | `shared/secret/<nama-sumber>`     |
| Shared ConfigMap                        | `shared/configmap/<nama-sumber>`  |
| Secret/CM pribadi + env literal pilihan | `<nama-workload-sumber>`          |

AppRole tetap per workload, misalnya `gengar-api` dan `pikachu-dc`, dalam auth mount
`task-api-a-approle`. Tidak membuat role `task-api-a`. Satu policy per workload memiliki
aturan **read** untuk path pribadi dan semua shared yang dipilih. Policy dibaca ulang
setelah ditulis, lalu pemasangannya ke role juga diverifikasi.
Field `vaultcapabilities` belum diaktifkan; naming Vault tetap nama sumber, bukan newname.

Untuk sumber pribadi yang dipakai berulang dalam satu workload, data dibaca sekali dan
digabung sekali. Shared pada workload/run berikutnya memakai path existing yang sama.
Data private tetap mempunyai satu snapshot baru pada giliran workload masing-masing.

## Reuse shared dan perubahan lintas run

- Path shared baru dibuat dengan KV CAS=0, sehingga tidak menimpa data yang sudah ditulis
  oleh operasi lain. Metadata menyimpan `manager=ocpmigrate-v3`, `source_namespace`,
  `source_kind`, dan `source_name`.
- Path shared existing digunakan tanpa update data. Metadata asal harus cocok. Path
  manual/legacy tanpa metadata tersebut tidak diadopsi otomatis: error Retry/Skip.
- Jika path shared ada tetapi input tidak mencantumkan sumber sebagai shared, proses
  menolak penggabungan ke private. Penentuan shared tidak bergantung jumlah pemakai run ini.
- Metadata ada tetapi data terhapus/destroyed menyebabkan error; 403/koneksi gagal tidak
  diperlakukan sebagai path belum ada. Metadata kosong milik V3 dari percobaan pembuatan
  sebelumnya dapat dilanjutkan.
- Hasil VSO untuk shared existing dibandingkan dengan **snapshot Vault**. Rotasi yang
  sudah terjadi di Vault tidak ditimpa data OCP lama.
- Jika shared berubah setelah review, proses menolak menulis ulang. Skip dan jalankan
  workload kembali untuk snapshot/review baru. Verifikasi memakai snapshot yang disetujui;
  rotasi selama sinkronisasi dapat menyebabkan timeout sehingga perlu review ulang.
- Path pribadi masih diselaraskan dengan data OCP yang dipilih, seperti V2. Penulisan
  memakai CAS dan read-back untuk menghindari lost update dan pengulangan versi saat retry.
- Menjadikan sumber yang sudah tersimpan pada path pribadi V2 sebagai shared **tidak
  otomatis membersihkan salinan lama di Vault**. Migrasi/pembersihan historis terpisah.

## Naming OCP

Untuk namespace `task-api-a`, sumber workload `gengar-api`:

| Komponen                | Nama                                                             |
| ----------------------- | ---------------------------------------------------------------- |
| VaultConnection         | `vault-connection-task-api-a`                                    |
| VaultAuth               | `vaultauth-gengar-api`                                           |
| Holder                  | `holder-secret-gengar-api`                                       |
| VSS pribadi             | `vaultstaticsecret-task-api-a-gengar-api`                        |
| Secret pribadi          | `vaultsecret-task-api-a-gengar-api`                              |
| VSS shared Secret       | `vaultstaticsecret-task-api-a-gengar-api-secret-thirdpartyconn`  |
| Secret shared Secret    | `vaultsecret-task-api-a-gengar-api-secret-thirdpartyconn`        |
| VSS shared ConfigMap    | `vaultstaticsecret-task-api-a-gengar-api-configmap-httptemplate` |
| Secret shared ConfigMap | `vaultsecret-task-api-a-gengar-api-configmap-httptemplate`       |

Shared memakai path Vault bersama tetapi VSS/Secret tujuan berbeda per workload dan
menggunakan VaultAuth workload masing-masing. Path/VSS pribadi tidak dibuat jika tidak
ada data pribadi. Nama gabungan lebih dari 253 karakter dipendekkan dengan suffix hash.

Konstanta penamaan berada di `scripts/migrate3.py`:

| Baris | Konstanta            | Default         |
| ----- | -------------------- | --------------- |
| 24    | CLONE_PREFIX         | `newvault-`     |
| 25    | TEST_SERVICE_SUFFIX  | `-newvault-svc` |
| 26    | NAMED_SERVICE_SUFFIX | `-svc`          |
| 27    | LABEL_VALUE_PREFIX   | `new-`          |

Label/annotation custom, image trigger DC, pemetaan env/volume, dan replica mengikuti
V2. Shared envFrom mengacu Secret shared; private envFrom menerima seluruh key private
gabungan. Nama env/prefix, item file, mode, mount, dan subPath dipertahankan.

## Retry, Skip, dan laporan resource

Setiap kegagalan langkah menampilkan nama workload/langkah. Error helper mencetak
endpoint/status atau alasan tanpa value. Pilihan **Retry** hanya menjalankan langkah itu;
**Skip / Continue next** melanjutkan workload berikutnya. Skip membuat build UNSTABLE.
Input Jenkins masih mempunyai kontrol Abort bawaan, dan tombol Stop Jenkins tetap
dihormati; tidak ada opsi Abort tambahan di menu migrasi.

Laporan memisahkan **DONE**, **PENDING**, **FAILED**, dan **RECOVERED**. PENDING berarti
request mungkin berhasil tetapi belum terkonfirmasi; bukan klaim object pasti belum ada.
Mount/path existing yang diverifikasi dicatat sebagai digunakan/diselaraskan, bukan
selalu diklaim baru dibuat. Laporan final dicetak juga saat Stop/Abort sebelum cleanup.

- Vault memakai receipts dan pemeriksaan server untuk menyelesaikan respons yang hilang.
  Data yang sudah sesuai tidak ditulis ulang. Policy/AppRole dibaca ulang.
- SecretID custom acak dibuat sekali per workload/build, disimpan dalam file private,
  lalu dipasang ke Vault. Retry mencari SecretID yang sama sehingga tidak membuat ID
  tambahan ketika respons sebelumnya hilang. Tidak ada perubahan ke role_id.
- Create clone/Service memakai annotation `migration.local/run` (build + index). Retry
  menerima resource dengan marker run yang sama dan spec sesuai rencana. Nama existing
  dari run sebelumnya atau resource lain tidak ditimpa; masuk Retry/Skip.
- Apply resource VSO idempotent dan dilakukan per object. Retry apply tidak mengulang
  provisioning Vault yang sudah berhasil.
- Patch in-place membaca ulang resource. UID harus sama; field konfigurasi harus tetap
  sesuai snapshot awal atau sudah sesuai hasil patch. Perubahan replica/image/metadata
  lain dipertahankan. Test resourceVersion tetap dipasang pada patch terbaru.
- Tidak ada rollback otomatis. Cleanup hanya menghapus file lokal sementara, bukan
  resource Vault/OCP. Service tanpa port hanya di-skip; migrasi dan testing tetap berjalan.

## Persyaratan dan batas versi ini

Agent Python 3.8+ dan oc, tanpa library pip tambahan. V3 menggunakan **Vault HTTP API**
agar 404/403 dibedakan secara pasti, dengan VAULT_TOKEN/VAULT_ADDR/VAULT_NAMESPACE dari
HashiCorp Vault credential binding. VAULT_CACERT/VAULT_SKIP_VERIFY didukung untuk request
Python. VaultConnection masih skipTLSVerify=true seperti referensi V2.

Plugin Jenkins: OpenShift Client, HashiCorp Vault, Credentials Binding, Pipeline Utility
Steps, Pipeline: Input Step, dan Pipeline standar. Credential Vault membutuhkan read
mounts/auth, pengelolaan mount, read/write KV data dan metadata, read/write ACL policy,
read/write AppRole, role-id, **custom-secret-id** dan **secret-id/lookup**. Ini tambahan
endpoint dibanding pembuatan SecretID acak langsung di V2. Akses HTTP/API memakai token
credential Jenkins, bukan token aplikasi.

Key duplikat masih ditolak per workload, termasuk shared vs private dan env sama antar
container. Kasus ini masuk Retry/Skip; nilai tidak diganti otomatis. Data non-UTF8, DC
Custom strategy/lifecycle hooks, env dinamis pilihan, serta volume bersama dengan pilihan
migrasi berbeda antar-container belum didukung. PVC selalu di-skip.

`.migration3-work` berisi snapshot/credential sensitif, direktori mode 700, file helper
mode 600. Tidak di-stash/archive. Jangan aktifkan verbose plugin atau commit direktori
runtime itu. Agent yang hilang bisa memerlukan cleanup manual. Job berbeda/V1/V2 tetap
perlu dijalankan bergantian untuk workload yang sama; disableConcurrentBuilds hanya
membatasi satu job.

Pengujian lokal:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_migrate3.py -v
```

Pengujian memakai fixture, mock API Vault, dan pemeriksaan transformasi/rekonsiliasi;
bukan pengujian Jenkins/OCP langsung. Referensi:
[Vault KV v2 API](https://developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2),
[AppRole API](https://developer.hashicorp.com/vault/api-docs/auth/approle),
[Jenkins Input](https://www.jenkins.io/doc/pipeline/steps/pipeline-input-step/).
