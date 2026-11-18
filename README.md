# apk_sync

`apk_sync.py` copies user-installed apps (base APK plus all split APKs) from one Android device to another, using `adb` on a host computer. It can also copy the private cache or data of **debuggable** apps.

- Runs on macOS, Linux and Windows. Needs Python 3.9+ and only the standard library.
- Finds adb on `PATH`, through `ANDROID_HOME`/`ANDROID_SDK_ROOT`, or in the default SDK location. You can also pass `--adb`.
- Pulls from the source and installs on the destination run in parallel, as a pipeline.
- Neither device needs a helper app.

An endpoint can be:
- an adb serial: a USB device such as `R52X90F9TKF`, or a Wi-Fi device such as `192.168.1.42:5555` (the script runs `adb connect` for you)
- `dev`: the only attached device. The script exits with an error if no device or more than one device is attached. Write `./dev` to mean a folder named `dev`.
- a folder: any path that contains `/` or `\`, such as `./snapshot` or `C:\apks`

A folder lets you sync with only one device plugged in at a time: device A → folder, then folder → device B. With one device plugged in, `dev` saves you looking up its serial: `python3 apk_sync.py dev ./snapshot`.

## Examples (macOS tcsh)

```
python3 apk_sync.py devices
python3 apk_sync.py -n R52X90F9TKF 192.168.1.42:5555
python3 apk_sync.py R52X90F9TKF 192.168.1.42:5555
python3 apk_sync.py -b R52X90F9TKF OTHER_SERIAL
python3 apk_sync.py --cache R52X90F9TKF ./snapshot
python3 apk_sync.py --cache ./snapshot OTHER_SERIAL
python3 apk_sync.py -I --cache R52X90F9TKF ./snapshot
python3 apk_sync.py --packages R52X90F9TKF
python3 apk_sync.py --packages -v --sort cache R52X90F9TKF ./snapshot
python3 apk_sync.py -o '^com\.example\.' -x 'test$' A B
```

These are, in order:
1. List the attached devices.
2. Dry run: show what would be installed or updated.
3. One-way sync.
4. Two-way sync (`-b`).
5. Device → folder, including the cache of debuggable apps.
6. Folder → device, including the cache.
7. Rerun with `-I`: only what changed is sent (see "Incremental reruns" below).
8. List the apps on a device.
9. List with the debug, cache and data columns, largest cache first, for a device and a folder.
10. Filter by package regex (`re.search`).

On Windows, use `py apk_sync.py ...` instead of `python3 apk_sync.py ...`.

| Option | Meaning |
|---|---|
| `-n` | dry run |
| `-b` | both directions; data is copied in the first direction only |
| `-o REGEX` / `-x REGEX` | include / exclude packages |
| `-m` | only install apps that are missing on the destination |
| `-d` | allow downgrades |
| `-g` | grant runtime permissions |
| `-i com.android.vending` | record the Play Store as installer |
| `-k DIR` | keep pulled files in DIR |
| `-j N` | parallel pulls from the source (default 4) |
| `-J N` | parallel installs on the destination (default 2) |
| `--cache` | also copy `cache/` of debuggable apps |
| `--data` | also copy the whole private data dir of debuggable apps |
| `--no-stop-src` | with `--data`, don't force-stop the app on the source first |
| `-I` | incremental, rsync-like (see below) |
| `-c` | with `-I`, sha256 every same-version APK, not only debuggable ones |
| `--no-delete` | with `-I`, keep destination data files that are gone from the source |
| `-v` | sync: also list unchanged apps. `--packages`: add the DEBUG / CACHE / FILES / DATA columns |
| `-p`, `--packages` | list the user-installed apps on each endpoint given (see below) |
| `--sort name\|size\|date\|cache` | `--packages` sort order: size and cache largest first, date newest first |
| `--debug-only` | with `--packages -v`, only show debuggable apps |
| `--no-preflight` | skip the destination settings check, and don't abort on blockers |

Debug APKs built by Android Studio's Run button are marked `testOnly`. They install fine because the script always passes `-t`.

## Preflight: settings that block installs

Before syncing, the script checks each destination device (both devices with `-b`) for settings that make `adb install` fail or stall. For each one it finds, it prints the problem and how to fix it on the device. Run the check alone with `python3 apk_sync.py check SERIAL [SERIAL ...]`. It exits 1 if anything blocks installs.

| Check | Level | How to fix |
|---|---|---|
| Xiaomi / Redmi / POCO "Install via USB" off (`persist.security.adbinstall`) | BLOCKS | Developer options > Install via USB (needs a SIM and a Mi account) |
| User restriction `no_install_apps` or `no_debugging_features` (MDM / work policy) | BLOCKS | device-management app or the admin |
| User restriction `no_install_unknown_sources`, or a Device Owner present | may block | device-management app or the admin |
| Oppo / Realme / OnePlus, vivo / iQOO: a prompt on the phone for each USB install | may block | accept the prompts, or turn the prompt off in Developer options |
| Play Protect scans adb installs (`verifier_verify_adb_installs`) | may block | Developer options > Verify apps over USB off, or `adb -s SERIAL shell settings put global verifier_verify_adb_installs 0` |
| Less than 1 GB free on `/data` | may block | free up space |

If a BLOCKS item is found, a real sync stops before installing anything. A dry run (`-n`) only reports it. The Android "Install unknown apps" setting isn't checked, because it doesn't apply to adb installs.

## Listing packages (`--packages`)

`--packages` lists every endpoint given, whether a device or a snapshot folder. `-o`/`-x` filter the list.

The columns are package, versionName, versionCode, total APK size, number of APK files, last update date, targetSdk, and (for devices) installer. On a device, the data comes from three commands: a filtered `dumpsys package packages`, `pm list -f`, and one `stat` over every APK. That's about 5.5 s for 325 apps on a Pixel 5, most of it `dumpsys`. `-I` uses the same table for its APK comparison, with a single `sha256sum` call for the hashes.

With `-v`, four more columns are added:
- **DEBUG:** whether the app is debuggable.
- **CACHE:** the size of its `cache/`.
- **FILES:** the number of files in `cache/`.
- **DATA:** the size of its whole private data dir.

These come from `du` + `find`, run with `run-as` only for apps that `dumpsys` flags as debuggable, in parallel. With `-v`, the Pixel 5 listing takes about 5.4 s. On a device, sizes are disk usage rounded up to blocks, so an empty cache shows 8 KB. In a folder, sizes are exact file sizes.

`refused` in the CACHE column means `run-as` rejected the app, and the reason is printed below the table. For example, an app that made its own data folder world-writable (`0777`) is refused for security.

If the phone has been rebooted but not unlocked since, app data is encrypted and unreadable, even with `run-as`. The listing warns and shows `locked`, and a `--cache`/`--data` sync stops with an error. Unlock the screen once and rerun.

Snapshot folders record versionName, targetSdk and the debuggable flag in `meta.json` when copied from a device. Older snapshots show `-` for those.

## Incremental reruns (`-I`)

By default, apps are compared by versionCode only, and `--cache`/`--data` copy everything. That's fastest the first time, because nothing is checked. Add `-I` when you rerun against a destination that already has the apps:

- **APKs with the same versionCode** are checked again:
  - The base APK and feature splits are compared by size. Config splits (`split_config.*` for ABI, density, language) are ignored, because they legitimately differ between devices.
  - Debuggable apps are also compared by sha256. `-c` hashes every app.
  - Any difference means a reinstall, which catches a rebuilt debug APK that kept its versionCode.
  - All the device-side checks run in one adb call per device. Folder hashes are cached in `meta.json`.
- **Cache/data** works like rsync:
  - Both sides list path, type, size and mtime (`find` + `stat`).
  - Only new or changed files are tarred and sent, and destination files that are gone from the source are deleted. `--no-delete` keeps them.
  - `tar` preserves mtimes, so a rerun finds nothing to copy.
  - An app that is new on the destination skips the destination scan.

On the emulator test: the first `--cache` copy of 6 apps (43 MB of cache) took 9 s. A rerun with `-I` took 0.8 s. After changing 2 files and deleting 1, only those 3 were transferred.

## Copying cache/data of debuggable apps (`--cache`, `--data`)

`run-as <pkg>` only works for apps whose manifest has `android:debuggable="true"`. It gives a shell running as that app's user, inside `/data/data/<pkg>`. The script tests each app with `run-as`, so release apps are skipped automatically.

How it works:
- **Pull:** `adb exec-out run-as <pkg> sh -c 'tar -cf - …'` streams a tar to the host. With `-I`, it streams only the listed changed files. In a snapshot folder, the data is stored as a normal directory tree: `<folder>/<pkg>/data/cache/...`.
- **Push:** the archive is staged in `/data/local/tmp` and extracted as the app's user with `run-as <pkg> tar -xof -`. In full mode, the existing entries are emptied first. With `-I`, only stale files are deleted. The app is force-stopped on the destination first, and only if something changed.
- **Ownership:** restored files get the app's user, the app's SELinux label, and the setgid `u0_aNNN_cache` group on `cache/`. These are verified on an emulator. Directories are created with `mkdir`, not by `tar`, so they keep setgid. That group matters: Android uses it to count cache usage and to clear cache when storage runs low.
- **Version check:** data is only copied when the destination ends up on exactly the same versionCode as the source.
- **`--data` skips:** `code_cache/` (JIT/ART artifacts), `lib` (a symlink to native libraries) and `no_backup/`. The last one holds per-install IDs such as Firebase installation IDs, and copying it would give two devices the same identity.

**Will the destination app use the copied data?** Usually, yes, if both devices run the same versionCode:

| Content | Works after copy? |
|---|---|
| OkHttp, Glide, Coil, Retrofit HTTP caches, map-tile caches, plain files, SQLite/Room databases, SharedPreferences | Yes. Paths are the same on every device for user 0 (`/data/user/0/<pkg>`). |
| Anything encrypted with an **Android Keystore** key (EncryptedSharedPreferences, Tink, auth tokens) | No. Keystore keys are tied to the device's hardware and never leave it. Decryption fails, and the app either resets or crashes. |
| Login and session tokens, FCM tokens, device IDs | Often rejected by the server, or both devices end up with the same identity. Prefer `--cache` over `--data` for this reason. |
| WebView data (`app_webview/`) | Usually works. The destination's WebView rebuilds anything it can't read. |
| Data from a **different app version** | Refused by the script. Room migrates older databases forward but crashes or wipes on a downgrade. |

Other notes:
- Cache is disposable by definition, so `--cache` is the low-risk option.
- With `--data`, the source app is force-stopped first, so SQLite WAL files and prefs are flushed. Skip that with `--no-stop-src`.
- Release (non-debuggable) apps can't be accessed without root. `adb backup` is deprecated, and most apps opt out of it.
- External cache (`/sdcard/Android/data/<pkg>/cache`) is not copied yet.

## Performance

The pipeline overlaps work: while app N installs on the destination, later apps are still being pulled from the source. With two devices on separate USB ports or Wi-Fi links, both transfers run at the same time.

Parallel pulls from **one** device help less, because they share one adb link. On one emulator, copying 6 apps (621 MB) took 9.2 s with `-j 1` and 8.8 s with `-j 4`. That run was bound by bandwidth and dominated by one large APK. Expect more gain when there are many small apps.

## Connecting the devices

- **USB:** turn on Developer options, then USB debugging. Plug both devices in. To use one cable, sync through a folder.
- **Wi-Fi (Android 11+):** on the device, open Developer options → Wireless debugging → Pair device with pairing code. Then run:
  ```
  adb pair 192.168.1.42:37123
  adb connect 192.168.1.42:41234
  ```
  The pairing port and the connect port are different. Both are shown on the device.
- **Wi-Fi (older devices):** plug the device in by USB once and run `adb tcpip 5555`. After that, use `IP:5555` as the endpoint.

## Why not Bluetooth, NFC, or an on-device app

- **Bluetooth:** real-world throughput is about 1–2 Mbit/s. A 70 MB app would take several minutes.
- **NFC:** it can only send a few KB, enough to start a pairing but not to move an APK. Android Beam was removed in Android 10.
- **On-device app:** Android makes the user tap "Install" for each app unless the phone is rooted or managed by an organization. Also, `run-as` is only available to the shell, so an app couldn't copy other apps' cache.

## Limits

- Split APKs are copied for the source device's CPU, screen density and language. If the destination has a different CPU, apps with native code can fail (`NO_MATCHING_ABIS`). The script warns about this.
- If an app on the destination was signed with a different key, the update fails (`UPDATE_INCOMPATIBLE`). The script never uninstalls anything, because that would delete the app's data.
- OBB files, Play Asset Delivery packs and the data of release apps are not copied.
- Some apps are tied to Play Store licensing or Play Integrity. These may refuse to run when they were sideloaded.

## Test status (2026-11-16/17)

- **Galaxy Tab S9 FE** (SM-X510, SDK 36, USB): the device → folder pull was tested with the earlier bash version. That covered 117 apps in a dry run, and real pulls of a split-APK app and a base-only app.
- **Emulator** (Pixel_Tablet AVD, SDK 35, read-only boot), using `apk_sync.py`:
  - folder → device install of a 4-part split APK and of a `testOnly` debug APK
  - device → folder with `--cache` for 6 apps
  - `--cache` and `--data` round trips, checking owner, cache group, setgid, symlinks, removal of stale files, and cleanup of staged files
  - the restored app cold-started with no crash
  - `-I`:
    - a no-change rerun did nothing
    - changed, added and deleted files went in both directions (folder ↔ device)
    - mtimes were preserved on the device
    - a same-versionCode APK change was detected
    - the default full restore still works
- **`--packages`:**
  - Pixel 5 (SDK 34): 325 apps listed.
    - While it was locked after a reboot: the warning, the `locked` column and the refused `--cache` sync.
    - After unlocking: the `-v` sizes for 25 debug apps, and `refused` with its reason for one app.
- **Pixel 5 → folder, read-only on the phone:**
  - `-I --cache` copied 3 apps: 2 debug apps with cache, and 1 app with 3 split APKs.
  - A rerun reported everything unchanged in 4.9 s.
  - A reverse dry run showed nothing to do.
  - A changed debug APK with the same versionCode was detected.
  - Emulator: `-v` cache, file and data sizes for 5 debug apps, and `--sort cache`.
  - Folders: listing, with versionName and targetSdk stored by a device → folder copy.
- **Not yet tested:**
  - a sync between two physical devices, or over Wi-Fi
  - running the script on Windows
  - a device without `--show-versioncode` (Android < 9)
