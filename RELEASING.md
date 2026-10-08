# Releasing story-gate (maintainers)

People install story-gate on their computers with `gate.py install --user`. That command only accepts a release signed with the story-gate release key, so every signed release needs a signature. Setup (`story-gate init`) installs a signed release with its signature checked, and uses `--unsigned` only for a development copy that has no signature.

**Release key**
- Public key: embedded in `.story-gate/sg_trust.py` (`RELEASE_SIGNERS`).
- Fingerprint: `SHA256:YN6hCUUHe1XHbhYDj1VdYwoeVoIWDDlFJ6yEXDAcR+4`. Publish it on every release page.
- The private key stays with the maintainer, never in this repository or in CI.

**Steps**
1. Merge the release to `main` and bump `VERSION` in `.story-gate/gate.py`. Bump the same version in `pyproject.toml`, `plugin.json`, `.claude-plugin/plugin.json`, `plugin/.claude-plugin/plugin.json` and `gemini-extension.json`, and in the install pin (`vX.Y.Z`) in `README.md`, `docs/guide.md`, `install.sh`, `install.ps1`, `.story-gate/SKILL.md` and `skills/story-gate/SKILL.md`, then copy that SKILL.md to `plugin/skills/story-gate/SKILL.md` (the Claude directory plugin folder). `tests/test_gate.py` (`TestMarketplacePackaging`) and `tests/test_packaging.py` fail until they all match.
2. On the maintainer's computer, from a clean checkout of `main`:
   ```bash
   python3 .story-gate/gate.py release-sign --key /path/to/release_ed25519
   ```
   This writes `.story-gate/release.json` (sha256 of every runtime file) and `.story-gate/release.json.sig`.
3. Commit both files in a PR of their own; a code owner's approval confirms it. After merge, tag the commit `vX.Y.Z` and paste the fingerprint into the release notes.
4. Check it: `python3 .story-gate/gate.py install --user --dry-run` must say `Signature: valid`.

**Between releases:** `release.json` lists the exact hash of every runtime file, so it only fits the commit it was signed for. A pull request that changes any runtime file (`.story-gate/*.py`, `PROTOCOL.md`, `SKILL.md`, `vendor/`) deletes `release.json` and `release.json.sig`, which makes `main` a development copy again (installs from it need `--unsigned` and say so). Tagged releases stay signed, and the next release pull request signs again. Two tests fail if a branch keeps a signature that doesn't match its files: `test_the_signed_release_installs_after_its_signature_checks_out` (in `TestTrustedRuntime`, run in CI; it installs this checkout's own signed copy) and `test_signed_release_verifies_from_the_wheel` (in `tests/test_packaging.py`).

**People already on an older version** run the upgrade with their installed copy, so the new release is checked with the key they already trust:
```bash
gate.py upgrade             # from this repository's default branch
gate.py rollback            # go back one version
```

**If the key is ever lost or exposed:** create a new key, ship a release signed with the old key that embeds the new public key, and announce the new fingerprint. If the old key was exposed, tell users to reinstall from a release they verify by hand.
