# Publish checklist

## One-time setup

1. Ensure the GitHub repo is public (or grant Actions access for releases).
2. Enable **Actions** in the repository settings.

## Each release

1. Update version in:
   - `version.py` → `__version__`
   - `version_info.txt` → `filevers`, `prodvers`, `FileVersion`, `ProductVersion`
2. Commit: `git commit -m "Release v1.x.x"`
3. Tag and push:

```powershell
git tag v1.0.0
git push origin main
git push origin v1.0.0
```

4. Wait for the [Release workflow](.github/workflows/release.yml) to finish.
5. Verify the release on GitHub has `MonitorInputSwitcher.exe` and the zip.

## Optional: winget

A starter manifest is in `packaging/winget/`. To publish to winget:

1. Fork [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs).
2. Add a folder under `manifests/d/DorPeretz/MonitorInputSwitcher/`.
3. Point the installer URL to the GitHub Release `.exe` and its SHA256.
4. Open a PR to winget-pkgs.

## Optional: code signing

Unsigned builds trigger Windows SmartScreen. To avoid that, purchase an Authenticode certificate and sign the exe after PyInstaller:

```powershell
signtool sign /fd SHA256 /a dist\MonitorInputSwitcher.exe
```
