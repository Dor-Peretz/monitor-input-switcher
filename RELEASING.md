# Publish checklist

## One-time setup

1. Ensure the GitHub repo is public (or grant Actions access for releases).
2. Enable **Actions** in the repository settings.

## Each release

1. Update version in:
   - `version.py` → `__version__`
   - `version_info.txt` → `filevers`, `prodvers`, `FileVersion`, `ProductVersion`
2. Commit: `git commit -m "Release vX.Y.Z"`
3. Tag and push. The tag must be `v` + `__version__` from `version.py`
   (for example `v1.1.0`); the Release workflow fails on a mismatch.

```powershell
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z
```

4. Wait for the [Release workflow](.github/workflows/release.yml) to finish.
5. Verify the release on GitHub has `MonitorInputSwitcher.exe`,
   `MonitorInputSwitcher-vX.Y.Z-win64.zip`, and
   `MonitorInputSwitcher-macos-vX.Y.Z.zip`.

## Optional: winget

A starter manifest is in `packaging/winget/`. To publish to winget:

1. Set `PackageVersion` and the version in `InstallerUrl` to match `version.py`.
2. Download `MonitorInputSwitcher.exe` from the GitHub Release (not a local
   build — the hash must match the published asset) and compute its SHA256:

```powershell
certutil -hashfile MonitorInputSwitcher.exe SHA256
# or: (Get-FileHash MonitorInputSwitcher.exe -Algorithm SHA256).Hash
```

```bash
shasum -a 256 MonitorInputSwitcher.exe
```

3. Replace `REPLACE_WITH_SHA256` in the manifest with that hash.
4. Fork [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs).
5. Add the manifest under `manifests/d/DorPeretz/MonitorInputSwitcher/<version>/`.
6. Open a PR to winget-pkgs.

## Optional: code signing

Unsigned builds trigger Windows SmartScreen. To avoid that, purchase an Authenticode certificate and sign the exe after PyInstaller:

```powershell
signtool sign /fd SHA256 /a dist\MonitorInputSwitcher.exe
```
