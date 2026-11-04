import pytest

def test_bundle_requires_all_ci_digests_and_exact_release_commit(tmp_path):
    from scripts.package_ledger_risk_release import read_digests
    from scripts.ledger_risk_manifest import ReleaseBlocked,IMAGES
    with pytest.raises(ReleaseBlocked):read_digests(tmp_path)
    for name in IMAGES:(tmp_path/(name+'.txt')).write_text(f'ghcr.io/bearflower/trading-{name}@sha256:'+('a'*64))
    assert set(read_digests(tmp_path))==set(IMAGES)
    (tmp_path/'hrs.txt').write_text('ghcr.io/bearflower/trading-hrs:latest')
    with pytest.raises(ReleaseBlocked):read_digests(tmp_path)


def test_ci_download_outside_checkout():
    from pathlib import Path
    workflow=Path('.github/workflows/deploy.yml').read_text()
    assert 'path: ${{ runner.temp }}/ledger-risk-digests' in workflow
    assert '--digests "${RUNNER_TEMP}/ledger-risk-digests"' in workflow

def test_private_admin_secret_encoding(tmp_path):
    import pytest
    from scripts.scoped_ledger_risk_release import private_admin_environment
    from scripts.ledger_risk_manifest import ReleaseBlocked
    secret=tmp_path/'.secrets/trading_migration_admin_password'
    secret.parent.mkdir();secret.write_text('a@b:/ ?#\n');secret.chmod(0o600)
    assert private_admin_environment(tmp_path)['RISK_POLICY_ADMIN_DSN']=='postgresql://trading_migration_admin:a%40b%3A%2F%20%3F%23@postgres:5432/trading_platform'
    secret.chmod(0o644)
    with pytest.raises(ReleaseBlocked):private_admin_environment(tmp_path)
