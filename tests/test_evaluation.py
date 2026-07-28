from orchestrator.evaluation import evaluate_text


def test_numeric_samples_and_statistics_pass():
    text = "period_ms=998\nperiod_ms=1002\nperiod_ms=1000\nREADY\n"
    expected = {"marker": "READY", "measurements": [{"name": "period", "regex": r"period_ms=(\d+)", "group": 1, "unit": "ms", "sample_count_min": 3, "statistic": "mean", "target": 1000, "tolerance": 5, "p95_max": 1003, "stdev_max": 2}]}
    passed, actual, reasons = evaluate_text(text, expected)
    assert passed and reasons == [] and actual["measurements"][0]["summary"]["mean"] == 1000


def test_out_of_range_sample_fails():
    passed, _, reasons = evaluate_text("v=1\nv=20\n", {"measurements": [{"name": "voltage", "regex": r"v=(\d+)", "min": 0, "max": 5, "sample_count_min": 2}]})
    assert not passed and any("above" in item for item in reasons)


def test_order_forbidden_and_exact_count():
    expected = {"ordered_markers": ["INIT", "READY"], "forbidden_markers": ["PANIC"], "count_marker": "SAMPLE", "count_exact": 2}
    assert evaluate_text("INIT\nSAMPLE\nSAMPLE\nREADY\n", expected)[0]
    assert not evaluate_text("READY\nINIT\nSAMPLE\nPANIC\n", expected)[0]


def test_measurement_requires_enough_samples():
    passed, _, reasons = evaluate_text("latency=10\n", {"measurements": [{"name": "latency", "regex": r"latency=(\d+)", "sample_count_min": 3, "p95_max": 20}]})
    assert not passed and any("sample count" in item for item in reasons)
