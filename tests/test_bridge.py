"""Tests for railway-bridge. Standard library only; no network."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import railway_bridge as rb

STEP = 60


def groups(*rows):
    """Build an httpMetricsGroupedByStatus answer from (status, [(ts, count), ...])."""
    return [{"statusCode": code, "samples": [{"ts": ts, "value": n} for ts, n in samples]}
            for code, samples in rows]


class StatusClass(unittest.TestCase):
    def test_codes_fold_into_classes(self):
        self.assertEqual(rb.status_class(200), "2xx")
        self.assertEqual(rb.status_class(304), "3xx")
        self.assertEqual(rb.status_class(404), "4xx")
        self.assertEqual(rb.status_class(503), "5xx")

    def test_anything_else_is_other(self):
        self.assertEqual(rb.status_class(0), "other")
        self.assertEqual(rb.status_class(None), "other")
        self.assertEqual(rb.status_class(99), "other")


class RequestCounter(unittest.TestCase):
    def test_counts_only_completed_buckets(self):
        counter = rb.RequestCounter(step=STEP, lag=120)
        # now=1000: a bucket counts once ts + step <= now - lag, i.e. ts <= 820
        counter.add(groups((200, [(700, 5), (760, 7), (820, 1), (880, 9)])), now=1000)
        self.assertEqual(counter.totals["2xx"], 13)

    def test_a_bucket_is_never_counted_twice(self):
        counter = rb.RequestCounter(step=STEP, lag=0)
        answer = groups((200, [(100, 4), (160, 6)]))
        counter.add(answer, now=1000)
        counter.add(answer, now=1000)
        self.assertEqual(counter.totals["2xx"], 10)

    def test_overlapping_polls_add_only_new_buckets(self):
        counter = rb.RequestCounter(step=STEP, lag=0)
        counter.add(groups((200, [(100, 4), (160, 6)])), now=1000)
        counter.add(groups((200, [(160, 6), (220, 3)])), now=1000)
        self.assertEqual(counter.totals["2xx"], 13)

    def test_codes_are_summed_into_their_class(self):
        counter = rb.RequestCounter(step=STEP, lag=0)
        counter.add(groups((200, [(100, 4)]), (204, [(100, 1)]), (500, [(100, 2)]), (502, [(100, 1)])), now=1000)
        self.assertEqual(counter.totals["2xx"], 5)
        self.assertEqual(counter.totals["5xx"], 3)

    def test_a_late_code_in_an_already_seen_bucket_is_still_counted(self):
        # The high-water mark is per status code, so a first 500 inside a minute
        # whose 200s were already counted is not dropped.
        counter = rb.RequestCounter(step=STEP, lag=0)
        counter.add(groups((200, [(100, 4)])), now=1000)
        counter.add(groups((200, [(100, 4)]), (500, [(100, 1)])), now=1000)
        self.assertEqual(counter.totals["2xx"], 4)
        self.assertEqual(counter.totals["5xx"], 1)

    def test_every_class_is_present_from_the_start(self):
        counter = rb.RequestCounter(step=STEP, lag=0)
        self.assertEqual(sorted(counter.totals), ["2xx", "3xx", "4xx", "5xx", "other"])
        self.assertTrue(all(v == 0 for v in counter.totals.values()))

    def test_first_poll_ignores_history_before_start(self):
        # Counting the whole look-back window on start would make every restart
        # look like a traffic spike in increase().
        counter = rb.RequestCounter(step=STEP, lag=0, start=500)
        counter.add(groups((200, [(400, 50), (500, 2), (560, 3)])), now=1000)
        self.assertEqual(counter.totals["2xx"], 5)


class DeployParsing(unittest.TestCase):
    def test_merge_commit_gives_version_and_change(self):
        msg = "Merge pull request #48 from example/dev\n\nv1.4.2: faster search and a new report"
        self.assertEqual(rb.parse_deploy_message(msg), ("v1.4.2", "Faster search and a new report"))

    def test_plain_message_has_no_version(self):
        self.assertEqual(rb.parse_deploy_message("Fix typo in footer"), ("", "Fix typo in footer"))

    def test_version_line_without_a_change(self):
        self.assertEqual(rb.parse_deploy_message("Merge pull request #9\n\nv1.2.3"), ("v1.2.3", ""))

    def test_hotfix_has_a_space_not_a_colon_after_the_version(self):
        msg = "Merge pull request #45 from example/hotfix/empty-cart\n\nv1.4.1 hotfix: an empty cart no longer checks out"
        self.assertEqual(rb.parse_deploy_message(msg),
                         ("v1.4.1", "Hotfix: an empty cart no longer checks out"))

    def test_version_at_the_end_of_the_line(self):
        msg = "Merge pull request #32 from example/dev\n\nTrigger deploy: v1.3.0"
        self.assertEqual(rb.parse_deploy_message(msg), ("v1.3.0", "Trigger deploy"))

    def test_merge_without_version_uses_its_body(self):
        msg = "Merge pull request #50 from example/dev\n\nButton colour tweaks"
        self.assertEqual(rb.parse_deploy_message(msg), ("", "Button colour tweaks"))

    def test_merge_header_alone_names_the_branch(self):
        msg = "Merge pull request #51 from example/hotfix/login-loop"
        self.assertEqual(rb.parse_deploy_message(msg), ("", "hotfix/login-loop"))

    def test_empty_message(self):
        self.assertEqual(rb.parse_deploy_message(None), ("", ""))


class LatestValue(unittest.TestCase):
    def test_latest_is_the_newest_sample(self):
        values = [{"ts": 300, "value": 3.0}, {"ts": 100, "value": 1.0}, {"ts": 200, "value": 2.0}]
        self.assertEqual(rb.latest(values), 3.0)

    def test_no_samples_is_none(self):
        self.assertIsNone(rb.latest([]))

    def test_peak_is_the_largest(self):
        self.assertEqual(rb.peak([{"ts": 1, "value": 0.4}, {"ts": 2, "value": 1.7}]), 1.7)
        self.assertIsNone(rb.peak([]))


class Exposition(unittest.TestCase):
    STATE = {
        "service": "web",
        "requests": {"2xx": 10, "3xx": 2, "4xx": 1, "5xx": 0, "other": 0},
        "latency": {"p50": 120.0, "p90": 300.0, "p95": 450.5, "p99": 900.0},
        "resources": {"CPU_USAGE": 0.02, "CPU_LIMIT": 8.0, "MEMORY_USAGE_GB": 0.7,
                      "MEMORY_LIMIT_GB": 2.0, "DISK_USAGE_GB": 0.8},
        "summary": {"requests_7d": 12480, "errors_5xx_7d": 1, "memory_peak_7d_gb": 1.7},
        "deployments": [
            {"id": "d1", "status": "SUCCESS", "created": 1791600000, "version": "v1.4.2",
             "change": 'faster "search" results'},
            {"id": "d2", "status": "REMOVED", "created": 1791500000, "version": "v1.4.0",
             "change": "Reports"},
        ],
        "last_success": 1791630000,
        "up": 1,
    }

    def setUp(self):
        self.text = rb.to_prometheus(self.STATE)

    def test_request_counters_per_class(self):
        self.assertIn('railway_http_requests_total{class="2xx",service="web"} 10', self.text)
        self.assertIn("# TYPE railway_http_requests_total counter", self.text)

    def test_latency_percentiles(self):
        self.assertIn('railway_http_latency_ms{quantile="p95",service="web"} 450.5', self.text)

    def test_resources_are_named_for_prometheus(self):
        self.assertIn('railway_memory_usage_gb{service="web"} 0.7', self.text)
        self.assertIn('railway_cpu_limit_vcpu{service="web"} 8.0', self.text)

    def test_seven_day_summary(self):
        self.assertIn('railway_requests_7d{service="web"} 12480', self.text)
        self.assertIn('railway_memory_peak_7d_gb{service="web"} 1.7', self.text)

    def test_deployments_carry_their_labels_and_escape_quotes(self):
        self.assertIn('version="v1.4.2"', self.text)
        self.assertIn('change="faster \\"search\\" results"', self.text)
        self.assertIn('status="SUCCESS"', self.text)

    def test_missing_values_are_left_out_not_zeroed(self):
        state = dict(self.STATE, latency={"p50": None, "p90": None, "p95": None, "p99": None})
        self.assertNotIn("railway_http_latency_ms{", rb.to_prometheus(state))

    def test_up_and_last_success(self):
        self.assertIn('railway_bridge_up{service="web"} 1', self.text)
        self.assertIn('railway_bridge_last_success_timestamp_seconds{service="web"} 1791630000', self.text)


if __name__ == "__main__":
    unittest.main()
