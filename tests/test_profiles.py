from trafficgen.profiles import APPLICATIONS, PERSONAS, WORKLOAD_PROFILES, normalized_mix


def test_profile_catalog_is_consistent():
    assert "office" in WORKLOAD_PROFILES
    for profile in WORKLOAD_PROFILES.values():
        assert set(profile["personas"]) <= set(PERSONAS)
        assert set(profile["applications"]) <= set(APPLICATIONS)
        assert sum(profile["personas"].values()) > 0
        assert sum(profile["applications"].values()) > 0


def test_normalized_mix_sums_to_100():
    result = normalized_mix({"web_saas": 3, "dns": 1}, APPLICATIONS)
    assert round(sum(result.values()), 6) == 100.0
    assert result["web_saas"] == 75.0
    assert result["dns"] == 25.0
