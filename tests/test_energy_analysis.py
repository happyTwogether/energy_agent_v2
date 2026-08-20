"""V1.4 节电分析纯业务规则测试。"""

from app.services.energy_analysis import (
    build_expansion_hour_evidence,
    build_expansion_record,
    collect_site_type_cgis,
    derive_expansion_result_status,
    derive_process_evidence_status,
    derive_whitelist_status,
    enrich_constriction_records,
    evaluate_neighbor_relation,
    filter_judgeable_expansion_evidence,
    neighbor_policy,
    normalize_boolean_flag,
    normalize_network_type,
)


def test_expansion_result_status_distinguishes_absence_from_no_candidate():
    assert derive_expansion_result_status(None) == "unavailable"
    assert derive_expansion_result_status(
        {"hour_filter": None, "hour_filter_early": None},
    ) == "no_candidate"
    assert derive_expansion_result_status(
        {"hour_filter": "22"},
    ) == "candidate_available"
    assert derive_expansion_result_status(
        {"hour_filter": 0},
    ) == "candidate_available"


def test_process_table_filters_hours_without_both_judgement_inputs():
    rows = [
        {"hour": 22, "low_flow_pct": 95.0, "avg_sleep_seconds": 0.0},
        {"hour": 23, "low_flow_pct": None, "avg_sleep_seconds": 0.0},
        {"hour": 0, "low_flow_pct": 95.0, "avg_sleep_seconds": None},
    ]

    assert filter_judgeable_expansion_evidence(rows) == [rows[0]]
    assert derive_process_evidence_status(rows) == "partial"


def test_process_evidence_status_is_missing_without_judgeable_rows():
    rows = [{"hour": 22, "low_flow_pct": None, "avg_sleep_seconds": 0.0}]

    assert derive_process_evidence_status(rows) == "missing"


def test_process_evidence_status_is_complete_for_all_expansion_hours():
    rows = [
        {"hour": hour, "low_flow_pct": 95.0, "avg_sleep_seconds": 0.0}
        for hour in [22, 23, 0, 1, 2, 3, 4, 5, 6, 7]
    ]

    assert derive_process_evidence_status(rows) == "complete"


def test_whitelist_status_does_not_turn_missing_source_into_no():
    assert derive_whitelist_status({}) == "unknown"
    assert derive_whitelist_status({"is_whitelist": "否"}) == "not_whitelisted"
    assert derive_whitelist_status(
        {"is_whitelist": "否"}, {"is_whitelist": "是"},
    ) == "whitelisted"


def test_expansion_record_preserves_database_boundary_and_continuous_fields():
    """防止应用重新过滤 90% 边界或丢失连续部署字段。"""
    row = {
        "cgi": "460-00-1-1",
        "hour_detail": [{"hour": 22, "low_flow_pct": 90.0}],
        "hour_filter": "22",
        "hour_int": 1,
        "hour_filter_early": None,
        "hour_int_early": 0,
        "deploy_hours": "22,23,0",
        "deploy_hours_continuous": "22:00-00:59",
        "deploy_hours_early": None,
        "deploy_hours_continuous_early": None,
    }

    result = build_expansion_record(row)

    assert result["hour_detail"][0]["low_flow_pct"] == 90.0
    assert result["hour_filter"] == "22"
    assert result["hour_int"] == 1
    assert result["deploy_hours_continuous"] == "22:00-00:59"


def test_boolean_flags_do_not_treat_chinese_no_as_true():
    """防止数据库文本“否”被 Python 的 bool(str) 误判为真。"""
    assert normalize_boolean_flag("是") is True
    assert normalize_boolean_flag("否") is False
    assert normalize_boolean_flag("true") is True
    assert normalize_boolean_flag(None) is False


def test_expansion_hour_evidence_keeps_source_result_as_decision():
    """防止应用按过程指标重新覆盖数据库给出的扩展结论。"""
    record = {
        "hour_detail": [
            {"hour": 22, "low_flow_pct": 90.0},
            {"hour": 23, "low_flow_pct": 95.0},
        ],
        "hour_filter": "22",
    }
    sleep_rows = [
        {"hours": 22, "avg_sleep_sum": 0},
        {"hours": 23, "avg_sleep_sum": 0},
    ]

    evidence = build_expansion_hour_evidence(record, sleep_rows)

    assert [item["hour"] for item in evidence] == [22, 23, 0, 1, 2, 3, 4, 5, 6, 7]
    assert evidence[0] == {
        "hour": 22,
        "low_flow_pct": 90.0,
        "is_low_business": True,
        "avg_sleep_seconds": 0.0,
        "is_zero_sleep": True,
        "suggestion": "可扩展",
    }
    assert evidence[1]["suggestion"] == "以综合分析结果为准"
    assert evidence[2]["suggestion"] == "数据缺失"


def test_network_type_normalization_covers_database_values():
    """防止 lte/nr 数据库枚举无法路由到对应工参表。"""
    assert normalize_network_type("lte") == "4G"
    assert normalize_network_type("NR") == "5G"
    assert normalize_network_type("4G") == "4G"
    assert normalize_network_type("5g") == "5G"
    assert normalize_network_type(None) is None


def test_site_type_cgis_are_grouped_for_batch_queries():
    rows = [
        {
            "cgi": "main-a",
            "around_cgi": "nr-neighbor",
            "site_type": None,
            "around_cgi_network_type": "nr",
        },
        {
            "cgi": "main-b",
            "around_cgi": "lte-neighbor",
            "site_type": "宏站",
            "around_cgi_network_type": None,
        },
    ]
    relation_by_pair = {
        ("main-b", "lte-neighbor"): {"around_cgi_network_type": "lte"},
    }

    nr_cgis, lte_cgis = collect_site_type_cgis(rows, relation_by_pair)

    assert nr_cgis == {"main-a", "nr-neighbor"}
    assert lte_cgis == {"lte-neighbor"}


def test_neighbor_policies_match_confirmed_site_rules():
    """防止三类主小区采用错误距离或错误邻区站型范围。"""
    assert neighbor_policy("宏站") == (200.0, frozenset({"宏站"}))
    assert neighbor_policy("室分") == (100.0, frozenset({"宏站", "室分"}))
    assert neighbor_policy("微站") == (200.0, None)


def test_relation_evaluation_distinguishes_known_invalid_and_missing_evidence():
    """防止把明确违规关系或缺失关系当成同一种情况。"""
    assert evaluate_neighbor_relation("宏站", "宏站", 200.0) == "allowed"
    assert evaluate_neighbor_relation("宏站", "室分", 80.0) == "site_type_mismatch"
    assert evaluate_neighbor_relation("室分", "宏站", 100.1) == "out_of_range"
    assert evaluate_neighbor_relation("微站", "室分", 200.0) == "allowed"
    assert evaluate_neighbor_relation(None, "宏站", 50.0) == "missing"
    assert evaluate_neighbor_relation("室分", None, 50.0) == "missing"
    assert evaluate_neighbor_relation("室分", "宏站", None) == "missing"


def test_prb_increase_equal_ten_is_preserved_and_enriched():
    """防止应用层按严格大于 10 再次过滤数据库结果。"""
    rows = [
        {
            "cgi": "main",
            "around_cgi": "neighbor",
            "site_type": None,
            "around_cgi_network_type": "lte",
            "prb_increase": 10.0,
            "self_prb_rate_ul_before": 8.0,
            "self_prb_rate_dl_before": 12.0,
            "prb_rate_ul_before": 45.0,
            "prb_rate_dl_before": 50.0,
            "prb_rate_ul": 60.0,
            "prb_rate_dl": 55.0,
        },
    ]
    result = enrich_constriction_records(
        rows,
        relation_by_pair={
            ("main", "neighbor"): {
                "distance": 80.0,
                "around_cgi_network_type": "lte",
            },
        },
        site_type_by_cell={
            ("5G", "main"): "室分",
            ("4G", "neighbor"): "宏站",
        },
    )

    assert result == [
        {
            **rows[0],
            "around_network_type": "4G",
            "distance": 80.0,
            "main_site_type": "室分",
            "around_site_type": "宏站",
            "relation_status": "allowed",
            "main_prb_before": 12.0,
            "around_prb_before": 50.0,
            "around_prb_during": 60.0,
        },
    ]


def test_known_invalid_relations_are_removed_but_missing_evidence_is_retained():
    """防止越界邻区进入结果，同时避免因关系数据缺失静默丢结果。"""
    rows = [
        {
            "cgi": "indoor",
            "around_cgi": "too-far",
            "site_type": "室分",
            "around_cgi_network_type": "nr",
            "prb_increase": 12.0,
        },
        {
            "cgi": "unknown",
            "around_cgi": "missing",
            "site_type": None,
            "around_cgi_network_type": "lte",
            "prb_increase": 15.0,
        },
    ]
    result = enrich_constriction_records(
        rows,
        relation_by_pair={
            ("indoor", "too-far"): {
                "distance": 150.0,
                "around_cgi_network_type": "nr",
            },
        },
        site_type_by_cell={
            ("5G", "indoor"): "室分",
            ("5G", "too-far"): "宏站",
        },
    )

    assert len(result) == 1
    assert result[0]["cgi"] == "unknown"
    assert result[0]["relation_status"] == "missing"
