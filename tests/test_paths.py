

def test_an_empty_data_directory_gets_its_shipped_defaults_back(tmp_path):
    """A container bind-mounts an empty host directory over data/ and masks the copy baked into
    the image, so the orchestrator came up with no schedule and said nothing about why."""
    from revenueos.paths import DATA_DEFAULTS, _default_dirs, seed_data_defaults

    assert _default_dirs(), "no shipped data/ defaults are reachable from this install"
    restored = seed_data_defaults(tmp_path)
    assert "automations.json" in restored
    for name in restored:
        assert (tmp_path / "data" / name).is_file()
    assert seed_data_defaults(tmp_path) == [], "seeding must never overwrite what is already there"
    assert set(restored) <= set(DATA_DEFAULTS)
