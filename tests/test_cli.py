import json

from revenueos.cli import main

LEADS = "email,first_name,last_name,company,title,website,linkedin_url,reason\nx@y.example,X,Y,Y Inc,CEO,https://y.example,,fits\n"


def test_init_run_today_approve_flow(workspace, answers_file, capsys):
    assert main(["--root", str(workspace.root), "init", "--answers", str(answers_file)]) == 0
    assert "validates" in capsys.readouterr().out
    (workspace.exports / "leads.csv").write_text(LEADS)

    assert main(["--root", str(workspace.root), "run", "discover", "--json", "--no-llm"]) == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(line)  # the orchestrator parses exactly this line
    assert payload["ok"] is True and payload["actions_created"] == 1

    assert main(["--root", str(workspace.root), "today"]) == 0
    out = capsys.readouterr().out
    assert "TODAY — Acme Scheduling" in out and "1 qualified prospect found" in out and "$0 pipeline generated" in out
    assert "leads: 1 found · 1 contactable · 1 qualified" in out

    assert main(["--root", str(workspace.root), "today", "--json"]) == 0
    brief = json.loads(capsys.readouterr().out)
    aid = brief["actions"][0]["id"]
    assert main(["--root", str(workspace.root), "approve", str(aid)]) == 0
    assert main(["--root", str(workspace.root), "ignore", str(aid)]) == 0
    capsys.readouterr()
    assert main(["--root", str(workspace.root), "today", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["actions"] == []


def test_run_all_reports_each_worker(workspace, answers_file, capsys, monkeypatch):
    main(["--root", str(workspace.root), "init", "--answers", str(answers_file)])
    capsys.readouterr()
    from revenueos.vendor.pulse import discovery
    from revenueos.workers import seo

    async def no_crawl(url, max_pages=10):
        return json.dumps({"ok": False, "error": "offline"})

    async def no_hn(client, query, since):
        return []

    monkeypatch.setattr(seo, "crawl_website", no_crawl)
    monkeypatch.setattr(discovery, "_hn_search", no_hn)
    monkeypatch.setattr(seo, "authority_gap", lambda u, c: None)
    main(["--root", str(workspace.root), "run", "all", "--no-llm"])
    out = capsys.readouterr().out
    for name in ("discover", "outreach", "inbox", "seo", "ads-audit", "content", "monitor"):
        assert name in out


def test_skills_and_doctor(workspace, capsys):
    assert main(["--root", str(workspace.root), "skills", "search", "cold email", "--limit", "3"]) == 0
    assert capsys.readouterr().out.count("\n") >= 2
    assert main(["--root", str(workspace.root), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "skills indexed" in out and "✗ onboarded" in out


def test_correct_appends_to_learning_loop(workspace, capsys):
    assert main(["--root", str(workspace.root), "correct", "no exclamation marks", "--context", "draft shouted",
                 "--correction", "never use exclamation marks", "--apply-when", "external copy"]) == 0
    assert "no exclamation marks" in workspace.corrections.read_text()
