"""The qualification gate: "qualified" can only ever mean business email + business website + real company."""
from __future__ import annotations

import pytest

from revenueos.qualify import clean_company, normalise_row, qualify
from revenueos.today import build_brief
from revenueos.workers import run_worker

# Rows shaped like the September GitHub scrape: qualified_at stamped on rows with no email at all.
SCRAPE_CSV = """email,first_name,last_name,company,title,website,linkedin_url,reason,lead_id,qualified_at
,Jeremy,Carmona,Clear Concise Consulting,Founder,,https://github.com/ccc,forked a repo,gh:ccc,2026-09-12
acmeai@acme-studio.com,La,Acme,{acmeAI},agency,https://www.acme-studio.com/,https://github.com/lc,forked a repo,gh:lc,2026-09-12
dasha.example@gmail.com,Dasha,Z,northwind-labs,,https://northwind-labs.example/,https://github.com/dk,forked a repo,gh:dk,2026-09-12
,Wu,Shuwen,Tiktok,intern,,https://github.com/ws,forked a repo,gh:ws,2026-09-12
,Zack,Zang,sugarcrm @IBM @Citigroup @GE,,,https://github.com/zz,forked a repo,gh:zz,2026-09-12
,Kareem,B,oxyz-official,,,https://github.com/kb,forked a repo,gh:kb,2026-09-12
,Dmitri,A,Example Clinic AI,,dmitri.example@gmail.com,https://github.com/da,forked a repo,gh:da,2026-09-12
,Khushi,,Horizen Foundation,devrel,youtube.com/@smilewithkhushi,https://github.com/sk,forked a repo,gh:sk,2026-09-12
hello@plumangola.com,Ana,Plum,PlumAngola,Owner,www.plumangola.com,,runs Google Ads; no marketing hire,gh:pa,2026-09-12
ops@fireplugins.com,Tassos,S,Tassos.gr,Founder,https://www.fireplugins.com/,,WordPress plugin business; Meta pixel on site,gh:ts,2026-09-12
"""


def test_gate_requires_business_email_website_and_real_company():
    ok = qualify({"email": "ops@fireplugins.com", "company": "Tassos.gr", "website": "https://www.fireplugins.com/"})
    assert ok.qualified and ok.reasons == []
    no_email = qualify({"email": "", "company": "Clear Concise Consulting", "website": "https://ccc.example"})
    assert not no_email.qualified and not no_email.contactable and "no contact email" in no_email.reasons
    freemail = qualify({"email": "dasha.example@gmail.com", "company": "northwind-labs", "website": "https://northwind-labs.example/"})
    assert freemail.contactable and not freemail.business_email and not freemail.qualified
    no_site = qualify({"email": "a@b-co.example", "company": "B Co", "website": ""})
    assert not no_site.qualified and "no business website" in no_site.reasons[0]
    profile_site = qualify({"email": "a@b-co.example", "company": "B Co", "website": "https://github.com/bco"})
    assert not profile_site.qualified and "profile URL only" in profile_site.reasons[0]
    for employer in ("google", "Tiktok", "DataArt", "sugarcrm @IBM @Citigroup @GE", "CUJO AI", "Cloud"):
        v = qualify({"email": "a@b-co.example", "company": employer, "website": "https://b-co.example"})
        assert not v.qualified and not v.company_ok, employer
    for handle in ("oxyz-official", "dXXX1988", "m3ta hu3man 0s"):
        assert clean_company(handle)[1] is not None, handle


def test_clean_company_fixes_scrape_artifacts():
    assert clean_company("{acmeAI}") == ("acmeAI", None)
    assert clean_company("@acme") == ("acme", None)
    assert clean_company("https://robotics.zhinno.com") == ("robotics.zhinno.com", None)
    assert clean_company("  B&C   Creativo ") == ("B&C Creativo", None)
    assert clean_company("")[1] == "no company name"
    assert clean_company("Baxter-Brunello") == ("Baxter-Brunello", None)  # a hyphenated name is not a handle
    assert clean_company("Tassos.gr") == ("Tassos.gr", None)


def test_field_mapping_is_repaired_before_the_gate():
    r = normalise_row({"email": "", "website": "dmitri.example@gmail.com", "company": "Example Clinic AI", "qualified_at": "2026-09-12"})
    assert r["email"] == "dmitri.example@gmail.com" and r["website"] == "" and "qualified_at" not in r
    r = normalise_row({"email": "", "website": "youtube.com/@smilewithkhushi", "company": "Horizen"})
    assert r["website"] == "" and r["profile_url"] == "youtube.com/@smilewithkhushi"
    r = normalise_row({"email": "HELLO@PlumAngola.com", "website": "www.plumangola.com", "company": "PlumAngola"})
    assert r["email"] == "hello@plumangola.com" and r["website"] == "https://plumangola.com"


def test_no_lead_without_a_contact_email_is_ever_counted_as_qualified(workspace, store, onboarded):
    """The claim on TODAY must mean what it says: qualified_at in the file is ignored, rows without
    an email are found (and maybe contactable) but never qualified, and the three numbers are separate."""
    (workspace.exports / "leads-scrape.csv").write_text(SCRAPE_CSV)
    r = run_worker("discover", workspace, store, onboarded, None)
    assert r.ok and r.actions_created == 3, r.summary  # acmeAI, PlumAngola, Tassos.gr
    funnel = store.lead_funnel()
    assert funnel == {"found": 10, "contactable": 5, "qualified": 3}
    assert {l["business_name"] for l in store.list_leads()} >= {"acmeAI", "Tassos.gr"}  # cleaned names, not '{acmeAI}'
    # a second import of the same rows re-cleans and never inflates the numbers
    run_worker("discover", workspace, store, onboarded, None)
    assert store.lead_funnel() == funnel
    for lead in store.list_leads():
        if not (lead.get("contact_email") or "").strip():
            assert lead["status"] == "new", lead["business_name"]
    assert {a["title"] for a in store.list_actions("pending", "prospect")} == {
        "Qualified prospect: La Acme at acmeAI", "Qualified prospect: Ana Plum at PlumAngola", "Qualified prospect: Tassos S at Tassos.gr"}
    brief = build_brief(store)
    text = brief.render_text("Acme")
    assert "leads: 10 found · 5 contactable · 3 qualified" in text and "3 qualified prospects found" in text
    assert "10 qualified" not in text
    # the store refuses the lie outright
    ghost = next(l for l in store.list_leads() if l["business_name"] == "Clear Concise Consulting")
    with pytest.raises(ValueError):
        store.transition_lead(ghost["id"], "scored")
    # and outreach drafts only for the qualified
    r = run_worker("outreach", workspace, store, onboarded, None)
    assert r.actions_created == 3 and store.lead_funnel()["qualified"] == 3


def test_gate_withdraws_a_previously_drafted_lead_that_fails(workspace, store, onboarded):
    lead = store.upsert_lead("csv", "gh:x", "northwind-labs", contact_email="someone@gmail.com", website_url="https://northwind-labs.example")
    store.transition_lead(lead, "scored")
    draft = store.create_draft(lead, "r/h", "v0", "hi", "body", "template-v1")
    aid = store.create_action("follow_up", "send", "body", context={"executor": "send_email", "draft_id": draft, "lead_id": lead, "to": "someone@gmail.com"})
    store.set_qualification(lead, False, ["personal address (gmail.com), not a business email"])
    assert store.get_lead(lead)["status"] == "new"
    assert store.get_draft(draft)["approval_state"] == "rejected"
    assert store.get_action(aid)["status"] == "ignored"
    assert store.lead_funnel() == {"found": 1, "contactable": 1, "qualified": 0}


def test_staging_never_writes_qualified_at(tmp_path):
    import csv
    import subprocess
    import sys

    src = tmp_path / "forks.csv"
    src.write_text("login,name,company,email,blog,location,bio,twitter_username,public_repos,followers,created_at,html_url\n"
                   "acme-studio,Acme Studio,{acmeAI},acmeai@acme-studio.com,https://www.acme-studio.com/,,agency,,130,5,2020,https://github.com/acme-studio\n"
                   "ccc,Jeremy Carmona,Clear Concise Consulting,,,,founder,,12,2,2021,https://github.com/ccc\n")
    out = subprocess.run([sys.executable, "scripts/stage_leads.py", str(src), "--signal", "forked marketingskills"],
                         capture_output=True, text=True, check=True, cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    rows = list(csv.DictReader(out.stdout.splitlines()))
    assert len(rows) == 2 and "qualified_at" not in rows[0]
    assert rows[0]["lead_id"] == "gh:acme-studio" and rows[0]["reason"] == "forked marketingskills; profile lists company {acmeAI}"
    assert rows[1]["email"] == "" and "qualified" not in out.stdout.lower()
