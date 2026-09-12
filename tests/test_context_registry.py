from revenueos.context import BusinessContext, extract_section, replace_section
from revenueos.registry import index_tools, load_registry, search_skills


def test_onboarding_fills_canon_and_validates(workspace, onboarded: BusinessContext):
    assert onboarded.is_onboarded()
    assert onboarded.company_name == "Acme Scheduling"
    assert onboarded.website == "https://acme-scheduling.example"
    assert onboarded.competitors == ["weave.com", "nexhealth.com"]
    assert "no-shows" in onboarded.section("audience.md", "Pain Points")
    assert onboarded.validate() == []  # markster-os gate still passes after filling sections
    brain = onboarded.brain()
    assert brain["entities"]["competitors"] == ["weave", "nexhealth"]  # brands, not domains, for search
    assert brain["search_seeds"]["pain"][0] == "no-shows eat 15% of chair time"
    assert "best appointment scheduling software for dental clinics" in brain["search_seeds"]["shopping"]
    summary = onboarded.prompt_summary()
    assert "Acme Recall" in summary and "two-way SMS" in summary


def test_corrections_are_prepended_and_injected(workspace, onboarded: BusinessContext):
    onboarded.add_correction("no em dashes", "draft used em dashes", "never use em dashes", "all external copy")
    onboarded.add_correction("cite proof", "claimed 40% lift", "only cite approved metrics", "any number in copy")
    text = workspace.corrections.read_text()
    assert text.index("cite proof") < text.index("no em dashes")  # newest first
    assert "<corrections>" in onboarded.prompt_summary() and "cite proof" in onboarded.recent_corrections()


def test_section_helpers():
    md = "# T\n\n## A\n\nold a\n\n## B\n\nold b\n"
    assert extract_section(md, "A") == "old a"
    new = replace_section(md, "A", "new a")
    assert extract_section(new, "A") == "new a" and extract_section(new, "B") == "old b"
    assert new.index("## A") < new.index("## B")


def test_registry_indexes_every_library(workspace):
    reg = load_registry(workspace)
    counts = reg["counts"]
    assert counts["skills"] >= 780 and counts["agents"] >= 100 and counts["tools"] >= 60
    assert counts["by_source"]["core"] == 236
    assert counts["by_source"]["playbooks"] == 172
    assert {"ads", "seo", "pipelines", "gtm", "creative"} <= set(counts["by_source"])
    assert all(s["license"] in ("MIT", "Apache") for s in reg["skills"])
    # kevinbadi (unlicensed) and adkit (proprietary) must not be in the catalogue
    assert not any(s["source"] in ("ai-os-skills", "adkit") for s in reg["skills"])


def test_registry_search_and_tools(workspace):
    hits = search_skills(workspace, "cold email")
    assert hits and any("cold" in h["slug"] or "outbound" in h["slug"] for h in hits)
    tools = index_tools(workspace)
    inst = next(t for t in tools if t.slug == "instantly")
    assert any("INSTANTLY" in v for v in inst.env_vars)
    assert inst.guide and inst.guide.endswith("instantly.md")
