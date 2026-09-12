"""Growth worker: real external numbers recorded as metrics, leads from hosted-access requests."""
from __future__ import annotations

from revenueos.workers import growth, run_worker


def _fake_gh(responses):
    def gh(path):
        for key, value in responses.items():
            if path.startswith(key):
                return value
        return None
    return gh


def test_growth_records_metrics_deltas_and_leads(workspace, store, onboarded, monkeypatch):
    onboarded.config["growth"] = {"github_repo": "acme/revenueos"}
    monkeypatch.setattr(growth, "site_metrics", lambda url: {"growth_site_reachable": 1.0, "growth_site_sitemap": 1.0})
    monkeypatch.setattr(growth, "_gh", _fake_gh({
        "repos/acme/revenueos/traffic/views": {"count": 40, "uniques": 25},
        "repos/acme/revenueos/traffic/clones": {"count": 6, "uniques": 4},
        "repos/acme/revenueos/releases": [{"assets": [{"download_count": 3}, {"download_count": 2}]}],
        "repos/acme/revenueos/issues": [{"number": 1, "title": "Hosted access: pro"}],
        "repos/acme/revenueos": {"stargazers_count": 12, "forks_count": 2, "subscribers_count": 3, "open_issues_count": 1},
    }))
    r = run_worker("growth", workspace, store, onboarded, None)
    assert r.ok, r
    m = store.latest_metrics()
    assert m["growth_github_stars"] == 12 and m["growth_github_unique_visitors_14d"] == 25 and m["growth_release_downloads"] == 5
    assert m["growth_hosted_access_requests"] == 1 and m["growth_site_sitemap"] == 1.0
    leads = store.list_actions("pending", "prospect")
    assert len(leads) == 1 and "hosted-access" in leads[0]["title"]
    # second run: stars grew → delta reported; no duplicate lead action
    monkeypatch.setattr(growth, "_gh", _fake_gh({
        "repos/acme/revenueos/traffic/views": {"count": 90, "uniques": 60},
        "repos/acme/revenueos/traffic/clones": {"count": 6, "uniques": 4},
        "repos/acme/revenueos/releases": [],
        "repos/acme/revenueos/issues": [{"number": 1, "title": "Hosted access: pro"}],
        "repos/acme/revenueos": {"stargazers_count": 30, "forks_count": 2, "subscribers_count": 3, "open_issues_count": 1},
    }))
    r2 = run_worker("growth", workspace, store, onboarded, None)
    assert r2.details["deltas"]["growth_github_stars"] == 18 and "stars=30 (+18)" in r2.summary
    assert len(store.list_actions("pending", "prospect")) == 1


def test_growth_needs_configuration(workspace, store, onboarded):
    onboarded.config.pop("growth", None)
    onboarded.config.pop("website", None)
    r = run_worker("growth", workspace, store, onboarded, None)
    assert not r.ok and "growth.github_repo" in r.error
