"""业务数据自服务确定性指标口径测试。"""

from app.self_service.metrics import calculate_metric, get_metric
from app.tools.report_query_tool import _process_nr_data


def test_nr_readable_ratio_uses_online_and_total_station_counts() -> None:
    metric = get_metric("nr_readable_ratio")

    value = calculate_metric(
        metric,
        {"logic_read_station_total": 90, "logic_station_total": 120},
    )

    assert value == 75.0
    assert metric.source_table == "nr_report_day_collect"
    assert metric.source_fields == (
        "logic_read_station_total",
        "logic_station_total",
    )


def test_ratio_metric_returns_zero_when_denominator_is_zero() -> None:
    metric = get_metric("nr_readable_ratio")

    value = calculate_metric(
        metric,
        {"logic_read_station_total": 5, "logic_station_total": 0},
    )

    assert value == 0.0


def test_nr_high_power_ratio_combines_32_and_64_channel_cells() -> None:
    metric = get_metric("nr_high_power_ratio")

    value = calculate_metric(
        metric,
        {
            "thirtytwo_channel_total": 20,
            "sixtyfour_channel_total": 10,
            "all_cell_total": 120,
        },
    )

    assert value == 25.0


def test_nr_station_energy_converts_kwh_to_ten_thousand_kwh() -> None:
    metric = get_metric("nr_station_energy_wan")

    value = calculate_metric(metric, {"nr_sa_station_power": 25_000})

    assert value == 2.5
    assert metric.unit == "万度"


def test_average_sleep_hour_matches_existing_daily_report_precision() -> None:
    row = {"deepsleep_effect_hour": 1, "all_cell_total": 3}

    self_service_value = calculate_metric(
        get_metric("nr_deep_sleep_average_hour"),
        row,
    )
    report_value, _ = _process_nr_data(row, [])

    assert report_value is not None
    assert self_service_value == report_value["deep_sleep_avg_hour"] == 0.3
